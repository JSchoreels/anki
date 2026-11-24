// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Low-rank card / note WKV state (precision byte 0x28): every head's matrix
//! is stored as a rank-c product L R with int8 factors.
//!
//! This is the training repository's `LowRankCodec` (`scripts/state_lowrank.py`,
//! the low-rank int8 state experiment). After each write the stored state is
//! the best rank-r approximation of the float update S' = S G + v k^T. Because
//! the previous stored state is L R, the columns of S' lie in span(L, v), so
//! the truncation needs only small eigen problems:
//!
//! 1. P = [L_1 .. L_c, v] (m = c + 1 vectors of `HEAD_SIZE` values), each
//!    vector normalised; the Gram matrix of the normalised vectors is
//!    eigen-decomposed and directions with an eigenvalue at or below 1e-5 x
//!    the largest are dropped; Q is the orthonormal basis that remains.
//! 2. B = Q^T S' (m x K); the eigenvectors U of B B^T (by decreasing
//!    eigenvalue) give component i: left_i = Q u_i, right_i = u_i^T B, with
//!    sigma_i = |right_i|, split evenly: L_i = left_i sqrt(sigma_i),
//!    R_i = right_i / sqrt(sigma_i).
//! 3. Each factor vector is rounded to int8 with its own float16 scale (the
//!    compact rows' rule: abs-max / 127, clamped to 65,504, 1.0 on zero), and
//!    the stored matrix is the product of the decoded factors.
//!
//! The rank follows a schedule (`RankSchedule`): rank `low` below `switch`
//! answers, rank `high` from `switch`; an entity with n answers stores rank
//! min(n, r) (a state with n answers has rank at most n). The solve runs in
//! f64 (the scorer: float32); the tests bound the difference.
//!
//! Stored block per layer: answer count n (u8, saturating at 255), rank c
//! (u8), then per head c left vectors and c right vectors, each a float16
//! scale and `HEAD_SIZE` int8 codes.

use std::io;

use half::f16;

use super::precision::f16_scale;
use super::precision::quantize;
use super::HEADS;
use super::HEAD_SIZE;

/// The largest rank a schedule may use.
pub(super) const MAX_RANK: usize = 8;
/// Gram eigenvalues kept: above this share of the largest (the scorer's
/// float32 `KEEP_TOL`).
const KEEP_TOL: f64 = 1e-5;
const LIMIT: f32 = 127.0;

/// Rank `low` below `switch` answers, rank `high` from `switch` answers.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct RankSchedule {
    pub low: u8,
    pub switch: u8,
    pub high: u8,
}

impl RankSchedule {
    /// The default: rank 2 below 16 answers, rank 4 from 16 (the training
    /// repository's `w8+lr2.16.4q8x8`).
    pub const DEFAULT: Self = Self {
        low: 2,
        switch: 16,
        high: 4,
    };

    /// One rank for every answer count.
    pub const fn uniform(rank: u8) -> Self {
        Self {
            low: rank,
            switch: 0,
            high: rank,
        }
    }

    /// `r` or `r.N.r2` (the scorer's tier syntax with one switch).
    pub fn parse(spec: &str) -> Option<Self> {
        let parts: Vec<&str> = spec.split('.').collect();
        let number = |text: &str| text.parse::<u8>().ok();
        let schedule = match parts.as_slice() {
            [rank] => Self::uniform(number(rank)?),
            [low, switch, high] => Self {
                low: number(low)?,
                switch: number(switch)?,
                high: number(high)?,
            },
            _ => return None,
        };
        schedule.valid().then_some(schedule.canonical())
    }

    fn valid(self) -> bool {
        let rank_ok = |rank: u8| (1..=MAX_RANK.min(HEAD_SIZE)).contains(&(rank as usize));
        rank_ok(self.low) && rank_ok(self.high) && (self.low == self.high || self.switch >= 2)
    }

    fn canonical(self) -> Self {
        if self.low == self.high {
            Self::uniform(self.low)
        } else {
            self
        }
    }

    /// The stored rank of an entity with `answers` answers (this write
    /// included).
    pub fn rank(self, answers: u8) -> usize {
        let tier = if answers >= self.switch {
            self.high
        } else {
            self.low
        };
        tier.min(answers) as usize
    }
}

/// Bytes of one layer's block at rank `rank`.
pub(super) fn block_len(rank: usize) -> usize {
    2 + HEADS * 2 * rank * (2 + HEAD_SIZE)
}

/// Splits a stored block into (answer count, rank, factor bytes) after
/// checking its length.
fn header(block: &[u8]) -> io::Result<(u8, usize)> {
    if block.len() < 2 {
        return Err(invalid("truncated low-rank block"));
    }
    let rank = block[1] as usize;
    if rank > MAX_RANK.min(HEAD_SIZE) || block.len() != block_len(rank) {
        return Err(invalid("invalid low-rank block size"));
    }
    Ok((block[0], rank))
}

fn invalid(what: &str) -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidData,
        format!("invalid RWKV compact state: {what}"),
    )
}

const VECTOR_BYTES: usize = 2 + HEAD_SIZE;

/// Decoded factor vector `index` (0..2c: left vectors, then right vectors) of
/// `head`.
fn vector(block: &[u8], rank: usize, head: usize, index: usize, out: &mut [f32]) {
    let start = 2 + (head * 2 * rank + index) * VECTOR_BYTES;
    let bytes = &block[start..start + VECTOR_BYTES];
    let scale = f16::from_bits(u16::from_le_bytes([bytes[0], bytes[1]])).to_f32();
    for (value, &code) in out.iter_mut().zip(&bytes[2..]) {
        *value = code as i8 as f32 * scale;
    }
}

/// Checks a stored block (sizes, scales, codes) and writes the matrix it
/// stores (`HEADS * HEAD_SIZE * HEAD_SIZE` values).
pub(super) fn decode_block(block: &[u8], matrix: &mut [f32]) -> io::Result<()> {
    let (_, rank) = header(block)?;
    for chunk in block[2..].chunks_exact(VECTOR_BYTES) {
        let scale = f16::from_bits(u16::from_le_bytes([chunk[0], chunk[1]])).to_f32();
        if !scale.is_finite() || scale <= 0.0 {
            return Err(invalid("non-finite or non-positive factor scale"));
        }
        if chunk[2..].contains(&128) {
            return Err(invalid("out-of-range factor code"));
        }
    }
    reconstruct(block, rank, matrix);
    if matrix.iter().any(|value| !value.is_finite()) {
        return Err(invalid("non-finite matrix value"));
    }
    Ok(())
}

fn reconstruct(block: &[u8], rank: usize, matrix: &mut [f32]) {
    matrix.fill(0.0);
    let mut left = vec![[0.0_f32; HEAD_SIZE]; rank];
    let mut right = vec![[0.0_f32; HEAD_SIZE]; rank];
    for head in 0..HEADS {
        for component in 0..rank {
            vector(block, rank, head, component, &mut left[component]);
            vector(block, rank, head, rank + component, &mut right[component]);
        }
        let base = head * HEAD_SIZE * HEAD_SIZE;
        product(
            &left,
            &right,
            &mut matrix[base..base + HEAD_SIZE * HEAD_SIZE],
        );
    }
}

/// out[row][column] = sum_i left_i[row] right_i[column], in component order.
fn product(left: &[[f32; HEAD_SIZE]], right: &[[f32; HEAD_SIZE]], out: &mut [f32]) {
    for row in 0..HEAD_SIZE {
        let out_row = &mut out[row * HEAD_SIZE..(row + 1) * HEAD_SIZE];
        out_row.fill(0.0);
        for (l, r) in left.iter().zip(right) {
            let scale = l[row];
            for (value, &rv) in out_row.iter_mut().zip(r) {
                *value += scale * rv;
            }
        }
    }
}

/// The previous write's factors, the basis of the next solve.
#[derive(Clone, Copy)]
pub(super) enum Previous<'a> {
    /// A fresh state (all zeros): the basis is the new value vector alone.
    Fresh,
    /// A stored low-rank block.
    Block(&'a [u8]),
    /// A non-zero matrix without factors (a state written in another format
    /// or held at float32): the full basis, answer count unknown (saturated).
    Matrix,
}

/// Encodes one layer's float update `matrix` (replaced by the stored
/// reconstruction) after a write with value vector `v` (`HEADS * HEAD_SIZE`)
/// and appends the block to `out`.
pub(super) fn encode_layer(
    matrix: &mut [f32],
    previous: Previous<'_>,
    v: &[f32],
    schedule: RankSchedule,
    out: &mut Vec<u8>,
) {
    let (answers, previous_rank) = match previous {
        Previous::Fresh => (1, 0),
        Previous::Block(block) => match header(block) {
            Ok((answers, rank)) => (answers.saturating_add(1), rank),
            Err(_) => (u8::MAX, 0),
        },
        Previous::Matrix => (u8::MAX, 0),
    };
    let full_basis = match previous {
        Previous::Matrix => true,
        Previous::Block(block) => header(block).is_err(),
        Previous::Fresh => false,
    };
    let rank = schedule.rank(answers);
    let start = out.len();
    out.push(answers);
    out.push(rank as u8);
    out.resize(start + block_len(rank), 0);
    let mut basis = Vec::with_capacity((previous_rank + 1) * HEAD_SIZE);
    let mut left = vec![0.0_f32; rank * HEAD_SIZE];
    let mut right = vec![0.0_f32; rank * HEAD_SIZE];
    for head in 0..HEADS {
        let base = head * HEAD_SIZE * HEAD_SIZE;
        let head_matrix = &mut matrix[base..base + HEAD_SIZE * HEAD_SIZE];
        sanitize(head_matrix);
        basis.clear();
        if !full_basis {
            if let Previous::Block(block) = previous {
                let mut values = [0.0_f32; HEAD_SIZE];
                for component in 0..previous_rank {
                    vector(block, previous_rank, head, component, &mut values);
                    basis.extend_from_slice(&values);
                }
            }
            let mut value = v[head * HEAD_SIZE..(head + 1) * HEAD_SIZE].to_vec();
            debug_assert_eq!(v.len(), HEADS * HEAD_SIZE);
            sanitize(&mut value);
            basis.extend_from_slice(&value);
        }
        factor_head(
            head_matrix,
            HEAD_SIZE,
            (!full_basis).then_some(basis.as_slice()),
            rank,
            &mut left,
            &mut right,
        );
        let mut write = out.len() - block_len(rank) + 2 + head * 2 * rank * VECTOR_BYTES;
        for vector in left
            .chunks_exact_mut(HEAD_SIZE)
            .chain(right.chunks_exact_mut(HEAD_SIZE))
        {
            quantize_vector(vector, &mut out[write..write + VECTOR_BYTES]);
            write += VECTOR_BYTES;
        }
    }
    reconstruct(&out[start..], rank, matrix);
}

/// Encodes a matrix that no write at this precision produced (a state held
/// at float32 or stored in another layout): an all-zero matrix as answer
/// count 0 and rank 0, any other on the full basis with the answer count
/// unknown (saturated).
pub(super) fn encode_existing(matrix: &mut [f32], schedule: RankSchedule, out: &mut Vec<u8>) {
    if matrix.iter().all(|value| *value == 0.0) {
        out.extend_from_slice(&[0, 0]);
        matrix.fill(0.0);
        return;
    }
    encode_layer(matrix, Previous::Matrix, &[], schedule, out);
}

/// Non-finite values (never written by a finite model) become finite so
/// the stored state stays decodable: NaN as 0, infinities as +-f32::MAX.
fn sanitize(values: &mut [f32]) {
    for value in values {
        if !value.is_finite() {
            *value = if value.is_nan() {
                0.0
            } else {
                value.signum() * f32::MAX
            };
        }
    }
}

/// Rounds `values` to int8 codes with one float16 scale (written to `out`:
/// scale, then the codes) and replaces them with the decoded values.
pub(super) fn quantize_vector(values: &mut [f32], out: &mut [u8]) {
    let maximum = values.iter().map(|v| v.abs()).fold(0.0_f32, f32::max);
    let scale = f16_scale(maximum, LIMIT);
    out[..2].copy_from_slice(&scale.to_bits().to_le_bytes());
    let scale = scale.to_f32();
    for (value, code) in values.iter_mut().zip(&mut out[2..]) {
        let q = quantize(*value, scale, LIMIT);
        *code = q as u8;
        *value = q as f32 * scale;
    }
}

/// The rank-`rank` factors of one head's matrix (`k x k`, row = value
/// index) whose column space lies in the span of `basis` (vectors of `k`
/// values; `None` = every direction). Writes `rank` left vectors and `rank`
/// right vectors of `k` float32 values (zero for components beyond the
/// basis), by decreasing singular value.
pub(super) fn factor_head(
    matrix: &[f32],
    k: usize,
    basis: Option<&[f32]>,
    rank: usize,
    left_out: &mut [f32],
    right_out: &mut [f32],
) {
    left_out[..rank * k].fill(0.0);
    right_out[..rank * k].fill(0.0);
    if rank == 0 {
        return;
    }
    // P: the basis vectors, each normalised (zero vectors stay zero).
    let p: Vec<f64> = match basis {
        Some(basis) => basis.iter().map(|&x| x as f64).collect(),
        None => {
            let mut identity = vec![0.0; k * k];
            for i in 0..k {
                identity[i * k + i] = 1.0;
            }
            identity
        }
    };
    let m0 = p.len() / k;
    let mut pn = p;
    for vector in pn.chunks_exact_mut(k) {
        let norm = vector.iter().map(|x| x * x).sum::<f64>().sqrt();
        if norm > 0.0 {
            for x in vector.iter_mut() {
                *x /= norm;
            }
        }
    }
    let mut gram = vec![0.0; m0 * m0];
    for i in 0..m0 {
        for j in 0..=i {
            let dot = dot(&pn[i * k..(i + 1) * k], &pn[j * k..(j + 1) * k]);
            gram[i * m0 + j] = dot;
            gram[j * m0 + i] = dot;
        }
    }
    let (lam, w) = eigh(&gram, m0);
    let top = lam.iter().copied().fold(0.0_f64, f64::max);
    // Q: orthonormal basis vectors of the kept directions.
    let mut q: Vec<f64> = Vec::new();
    for (j, &value) in lam.iter().enumerate() {
        if value > top * KEEP_TOL && value > 0.0 {
            let inv = value.sqrt().recip();
            let mut column = vec![0.0; k];
            for i in 0..m0 {
                let weight = w[i * m0 + j] * inv;
                if weight != 0.0 {
                    for (c, &x) in column.iter_mut().zip(&pn[i * k..(i + 1) * k]) {
                        *c += weight * x;
                    }
                }
            }
            q.extend(column);
        }
    }
    let m = q.len() / k;
    if m == 0 {
        return;
    }
    // B = Q^T S (m x k).
    let mut b = vec![0.0; m * k];
    for j in 0..m {
        let qj = &q[j * k..(j + 1) * k];
        let bj = &mut b[j * k..(j + 1) * k];
        for (row, &weight) in qj.iter().enumerate() {
            if weight != 0.0 {
                for (value, &s) in bj.iter_mut().zip(&matrix[row * k..(row + 1) * k]) {
                    *value += weight * s as f64;
                }
            }
        }
    }
    let mut bbt = vec![0.0; m * m];
    for i in 0..m {
        for j in 0..=i {
            let dot = dot(&b[i * k..(i + 1) * k], &b[j * k..(j + 1) * k]);
            bbt[i * m + j] = dot;
            bbt[j * m + i] = dot;
        }
    }
    let (s2, u) = eigh(&bbt, m);
    let mut order: Vec<usize> = (0..m).collect();
    order.sort_by(|&x, &y| s2[y].total_cmp(&s2[x]));
    for (component, &column) in order.iter().take(rank).enumerate() {
        let mut left = vec![0.0; k];
        let mut right = vec![0.0; k];
        for j in 0..m {
            let weight = u[j * m + column];
            for (value, &x) in left.iter_mut().zip(&q[j * k..(j + 1) * k]) {
                *value += weight * x;
            }
            for (value, &x) in right.iter_mut().zip(&b[j * k..(j + 1) * k]) {
                *value += weight * x;
            }
        }
        let sigma = right.iter().map(|x| x * x).sum::<f64>().sqrt();
        let g = sigma.sqrt();
        let divisor = if g > 0.0 { g } else { 1.0 };
        for i in 0..k {
            left_out[component * k + i] = (left[i] * g) as f32;
            right_out[component * k + i] = (right[i] / divisor) as f32;
        }
    }
}

fn dot(a: &[f64], b: &[f64]) -> f64 {
    a.iter().zip(b).map(|(x, y)| x * y).sum()
}

/// Eigenvalues and eigenvectors (columns of the row-major `n x n` result)
/// of a symmetric matrix: cyclic Jacobi rotations in f64.
fn eigh(matrix: &[f64], n: usize) -> (Vec<f64>, Vec<f64>) {
    let mut a = matrix.to_vec();
    let mut v = vec![0.0; n * n];
    for i in 0..n {
        v[i * n + i] = 1.0;
    }
    let mut last_off = f64::INFINITY;
    for _sweep in 0..60 {
        let off: f64 = (0..n)
            .flat_map(|i| (0..n).filter(move |&j| j != i).map(move |j| (i, j)))
            .map(|(i, j)| a[i * n + j] * a[i * n + j])
            .sum();
        let diagonal: f64 = (0..n).map(|i| a[i * n + i] * a[i * n + i]).sum();
        // Converged, or stalled at rounding level: a sweep that no longer
        // reduces the off-diagonal mass only rotates by angles below one ulp
        // (without this stop, a third of the 5 x 5 problems ran all 60 sweeps).
        if off <= diagonal * 1e-32 || off == 0.0 || off >= last_off {
            break;
        }
        last_off = off;
        for p in 0..n {
            for q in p + 1..n {
                let apq = a[p * n + q];
                if apq == 0.0 {
                    continue;
                }
                let app = a[p * n + p];
                let aqq = a[q * n + q];
                let theta = (aqq - app) / (2.0 * apq);
                let t = theta.signum() / (theta.abs() + (theta * theta + 1.0).sqrt());
                let t = if theta == 0.0 { 1.0 } else { t };
                let c = (t * t + 1.0).sqrt().recip();
                let s = t * c;
                for r in 0..n {
                    let arp = a[r * n + p];
                    let arq = a[r * n + q];
                    a[r * n + p] = c * arp - s * arq;
                    a[r * n + q] = s * arp + c * arq;
                }
                for r in 0..n {
                    let apr = a[p * n + r];
                    let aqr = a[q * n + r];
                    a[p * n + r] = c * apr - s * aqr;
                    a[q * n + r] = s * apr + c * aqr;
                }
                for r in 0..n {
                    let vrp = v[r * n + p];
                    let vrq = v[r * n + q];
                    v[r * n + p] = c * vrp - s * vrq;
                    v[r * n + q] = s * vrp + c * vrq;
                }
            }
        }
    }
    ((0..n).map(|i| a[i * n + i]).collect(), v)
}

/// The answer count and rank of a stored block.
pub(super) fn block_info(block: &[u8]) -> io::Result<(u8, usize)> {
    header(block)
}

#[cfg(test)]
mod tests {
    use std::io::Read;

    use super::*;

    struct Entry {
        entity: usize,
        n: u8,
        rank: usize,
        inputs: Vec<f32>,
        left: Vec<f32>,
        right: Vec<f32>,
    }

    struct Case {
        k: usize,
        heads: usize,
        entities: usize,
        spec: String,
        quantized: bool,
        steps: Vec<Vec<Entry>>,
    }

    fn floats(value: &serde_json::Value) -> Vec<f32> {
        data_encoding::BASE64
            .decode(value.as_str().unwrap().as_bytes())
            .unwrap()
            .chunks_exact(4)
            .map(|bytes| f32::from_le_bytes(bytes.try_into().unwrap()))
            .collect()
    }

    fn cases() -> Vec<Case> {
        let mut data = Vec::new();
        flate2::read::GzDecoder::new(&include_bytes!("testdata/lowrank-state-codec.json.gz")[..])
            .read_to_end(&mut data)
            .unwrap();
        let value: serde_json::Value = serde_json::from_slice(&data).unwrap();
        value["cases"]
            .as_array()
            .unwrap()
            .iter()
            .map(|case| Case {
                k: case["head_size"].as_u64().unwrap() as usize,
                heads: case["heads"].as_u64().unwrap() as usize,
                entities: case["entities"].as_u64().unwrap() as usize,
                spec: case["spec"].as_str().unwrap().to_string(),
                quantized: case["quant"] == "q8",
                steps: case["steps"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|step| {
                        step.as_array()
                            .unwrap()
                            .iter()
                            .map(|entry| Entry {
                                entity: entry["entity"].as_u64().unwrap() as usize,
                                n: entry["n"].as_u64().unwrap() as u8,
                                rank: entry["rank"].as_u64().unwrap() as usize,
                                inputs: floats(&entry["inputs"]),
                                left: floats(&entry["left"]),
                                right: floats(&entry["right"]),
                            })
                            .collect()
                    })
                    .collect(),
            })
            .collect()
    }

    /// sum_i left_i right_i^T for `rank` vectors of `k` values.
    fn outer(left: &[f32], right: &[f32], k: usize, rank: usize) -> Vec<f32> {
        let mut out = vec![0.0_f32; k * k];
        for row in 0..k {
            for i in 0..rank {
                let scale = left[i * k + row];
                for column in 0..k {
                    out[row * k + column] += scale * right[i * k + column];
                }
            }
        }
        out
    }

    /// The synthetic recurrence of the generator:
    /// S' = S diag(w) - (S kd) (a kd)^T + v k^T.
    fn update(state: &[f32], inputs: &[f32], k: usize) -> Vec<f32> {
        let (w, rest) = inputs.split_at(k);
        let (kd, rest) = rest.split_at(k);
        let (a, rest) = rest.split_at(k);
        let (key, v) = rest.split_at(k);
        let mut out = vec![0.0_f32; k * k];
        for row in 0..k {
            let s = &state[row * k..(row + 1) * k];
            let skd: f32 = s.iter().zip(kd).map(|(x, y)| x * y).sum();
            for column in 0..k {
                out[row * k + column] =
                    s[column] * w[column] - skd * (a[column] * kd[column]) + v[row] * key[column];
            }
        }
        out
    }

    fn head_inputs(entry: &Entry, heads: usize, k: usize, head: usize) -> Vec<f32> {
        (0..5)
            .flat_map(|part| {
                let start = (part * heads + head) * k;
                entry.inputs[start..start + k].to_vec()
            })
            .collect()
    }

    /// One write of one head: the Rust factors (rounded when `quantized`).
    fn write(
        state: &[f32],
        previous_left: &[f32],
        inputs: &[f32],
        k: usize,
        rank: usize,
        quantized: bool,
    ) -> (Vec<f32>, Vec<f32>) {
        let next = update(state, inputs, k);
        let mut basis = previous_left.to_vec();
        basis.extend_from_slice(&inputs[4 * k..5 * k]);
        let mut left = vec![0.0; rank * k];
        let mut right = vec![0.0; rank * k];
        factor_head(&next, k, Some(&basis), rank, &mut left, &mut right);
        if quantized {
            let mut scratch = vec![0_u8; 2 + k];
            for vector in left.chunks_exact_mut(k).chain(right.chunks_exact_mut(k)) {
                quantize_vector(vector, &mut scratch);
            }
        }
        let stored = outer(&left, &right, k, rank);
        (stored, left)
    }

    fn relative(a: &[f32], b: &[f32]) -> f64 {
        let diff: f64 = a
            .iter()
            .zip(b)
            .map(|(x, y)| (*x as f64 - *y as f64).powi(2))
            .sum();
        let norm: f64 = b.iter().map(|y| (*y as f64).powi(2)).sum();
        if norm == 0.0 {
            diff.sqrt()
        } else {
            (diff / norm).sqrt()
        }
    }

    /// Per write, from the scorer's own previous state and factors: the Rust
    /// solve reproduces the scorer's stored state. Float factors: within
    /// float32 rounding of the scorer's float32 solve. Int8 factors: a code
    /// flips where the two solves differ by float noise near a rounding
    /// boundary, so most writes are identical and the rest stay within a
    /// few code steps.
    #[test]
    fn rwkv_state_lowrank_codec_matches_the_scorer() {
        for case in cases() {
            let schedule = RankSchedule::parse(&case.spec).unwrap();
            let (k, heads) = (case.k, case.heads);
            let mut last: Vec<Option<&Entry>> = vec![None; case.entities];
            let (mut worst, mut sum, mut count, mut exact) = (0.0_f64, 0.0, 0usize, 0usize);
            for step in &case.steps {
                for entry in step {
                    assert_eq!(
                        schedule.rank(entry.n),
                        entry.rank,
                        "{} n={}",
                        case.spec,
                        entry.n
                    );
                    for head in 0..heads {
                        let (state, previous_left) = match last[entry.entity] {
                            Some(previous) => {
                                let span = previous.rank * k;
                                assert_eq!(previous.left.len(), heads * span);
                                let left = &previous.left[head * span..(head + 1) * span];
                                let right = &previous.right[head * span..(head + 1) * span];
                                (outer(left, right, k, previous.rank), left.to_vec())
                            }
                            None => (vec![0.0; k * k], Vec::new()),
                        };
                        let inputs = head_inputs(entry, heads, k, head);
                        let (stored, _) = write(
                            &state,
                            &previous_left,
                            &inputs,
                            k,
                            entry.rank,
                            case.quantized,
                        );
                        let span = entry.rank * k;
                        let expected = outer(
                            &entry.left[head * span..(head + 1) * span],
                            &entry.right[head * span..(head + 1) * span],
                            k,
                            entry.rank,
                        );
                        let error = relative(&stored, &expected);
                        worst = worst.max(error);
                        sum += error;
                        count += 1;
                        exact += usize::from(stored == expected);
                    }
                    last[entry.entity] = Some(entry);
                }
            }
            let mean = sum / count as f64;
            println!(
                "K={k} {} {}: {count} head writes, relative error mean {mean:.2e} max {worst:.2e}, identical {exact}",
                case.spec,
                if case.quantized { "q8" } else { "f" }
            );
            if case.quantized {
                assert!(mean <= 2e-4 && worst <= 1e-2, "{mean} {worst}");
                assert!(exact * 20 >= count * 19, "{exact} of {count} identical");
            } else {
                assert!(worst <= 1e-5, "{worst}");
            }
        }
    }

    /// The Rust chain on its own (each write from its own previous factors)
    /// follows the scorer's chain: the recurrence contracts, so a flipped
    /// code does not grow.
    #[test]
    fn rwkv_state_lowrank_chain_follows_the_scorer() {
        for case in cases() {
            let (k, heads) = (case.k, case.heads);
            let schedule = RankSchedule::parse(&case.spec).unwrap();
            let mut states = vec![vec![vec![0.0_f32; k * k]; heads]; case.entities];
            let mut lefts = vec![vec![Vec::new(); heads]; case.entities];
            let (mut worst, mut sum, mut count) = (0.0_f64, 0.0, 0usize);
            for step in &case.steps {
                for entry in step {
                    let rank = schedule.rank(entry.n);
                    for head in 0..heads {
                        let inputs = head_inputs(entry, heads, k, head);
                        let (stored, left) = write(
                            &states[entry.entity][head],
                            &lefts[entry.entity][head],
                            &inputs,
                            k,
                            rank,
                            case.quantized,
                        );
                        let span = rank * k;
                        let expected = outer(
                            &entry.left[head * span..(head + 1) * span],
                            &entry.right[head * span..(head + 1) * span],
                            k,
                            rank,
                        );
                        assert!(stored.iter().all(|x| x.is_finite()));
                        let error = relative(&stored, &expected);
                        worst = worst.max(error);
                        sum += error;
                        count += 1;
                        states[entry.entity][head] = stored;
                        lefts[entry.entity][head] = left;
                    }
                }
            }
            let mean = sum / count as f64;
            println!(
                "chain K={k} {} {}: relative difference mean {mean:.2e} max {worst:.2e}",
                case.spec,
                if case.quantized { "q8" } else { "f" }
            );
            if case.quantized {
                assert!(mean <= 3e-3 && worst <= 3e-2, "{mean} {worst}");
            } else {
                assert!(worst <= 1e-4, "{worst}");
            }
        }
    }

    #[test]
    fn rwkv_state_lowrank_schedule_rules() {
        let t24 = RankSchedule::DEFAULT;
        assert_eq!(RankSchedule::parse("2.16.4"), Some(t24));
        assert_eq!(
            [1, 2, 3, 15, 16, 17, 255].map(|n| t24.rank(n)),
            [1, 2, 2, 2, 4, 4, 4]
        );
        let two = RankSchedule::parse("2").unwrap();
        assert_eq!(two, RankSchedule::uniform(2));
        assert_eq!(RankSchedule::parse("2.16.2"), Some(two));
        assert_eq!([1, 2, 16, 255].map(|n| two.rank(n)), [1, 2, 2, 2]);
        for bad in ["0", "9", "2.1.4", "2.16", "a", "2.16.4.32.8", ""] {
            assert_eq!(RankSchedule::parse(bad), None, "{bad}");
        }
    }

    fn random(seed: &mut u64, count: usize, scale: f32) -> Vec<f32> {
        (0..count)
            .map(|_| {
                *seed = seed
                    .wrapping_mul(6364136223846793005)
                    .wrapping_add(1442695040888963407);
                ((*seed >> 40) as f32 / (1u64 << 24) as f32 - 0.5) * scale
            })
            .collect()
    }

    /// A long synthetic stream across the 2 -> 4 switch: the block grows,
    /// decoding gives the encoder's matrix bit for bit, answer counts
    /// saturate at 255 and every value stays finite, also for huge and
    /// non-finite inputs.
    #[test]
    fn rwkv_state_lowrank_block_round_trip_and_growth() {
        let mut seed = 7;
        let size = HEADS * HEAD_SIZE * HEAD_SIZE;
        let mut block: Vec<u8> = Vec::new();
        let mut matrix = vec![0.0_f32; size];
        for answer in 1..=300_u32 {
            let mut next: Vec<f32> = matrix.iter().map(|x| x * 0.9).collect();
            let v = random(
                &mut seed,
                HEADS * HEAD_SIZE,
                if answer == 40 { 1e30 } else { 2.0 },
            );
            let key = random(&mut seed, HEADS * HEAD_SIZE, 2.0);
            for head in 0..HEADS {
                for row in 0..HEAD_SIZE {
                    for column in 0..HEAD_SIZE {
                        next[head * HEAD_SIZE * HEAD_SIZE + row * HEAD_SIZE + column] +=
                            v[head * HEAD_SIZE + row] * key[head * HEAD_SIZE + column];
                    }
                }
            }
            if answer == 50 {
                next[3] = f32::NAN;
                next[5] = f32::INFINITY;
            }
            let previous = if block.is_empty() {
                Previous::Fresh
            } else {
                Previous::Block(&block)
            };
            let mut out = Vec::new();
            encode_layer(&mut next, previous, &v, RankSchedule::DEFAULT, &mut out);
            let (answers, rank) = block_info(&out).unwrap();
            assert_eq!(answers as u32, answer.min(255));
            assert_eq!(rank, RankSchedule::DEFAULT.rank(answers));
            assert_eq!(out.len(), block_len(rank));
            let mut decoded = vec![1.0_f32; size];
            decode_block(&out, &mut decoded).unwrap();
            assert_eq!(decoded, next);
            assert!(next.iter().all(|x| x.is_finite()));
            block = out;
            matrix = next;
        }
        assert_eq!(block_len(2), 2 + HEADS * 4 * (2 + HEAD_SIZE));
    }

    /// Without factors (a state held at float32), the full basis gives the
    /// same truncation as the factored basis of a low-rank matrix.
    #[test]
    fn rwkv_state_lowrank_full_basis_matches_the_factored_basis() {
        let mut seed = 3;
        let k = HEAD_SIZE;
        let left = random(&mut seed, 3 * k, 2.0);
        let right = random(&mut seed, 3 * k, 2.0);
        let matrix = outer(&left, &right, k, 3);
        let (mut l1, mut r1) = (vec![0.0; 2 * k], vec![0.0; 2 * k]);
        let (mut l2, mut r2) = (vec![0.0; 2 * k], vec![0.0; 2 * k]);
        factor_head(&matrix, k, Some(&left), 2, &mut l1, &mut r1);
        factor_head(&matrix, k, None, 2, &mut l2, &mut r2);
        let a = outer(&l1, &r1, k, 2);
        let b = outer(&l2, &r2, k, 2);
        assert!(relative(&a, &b) < 1e-5, "{}", relative(&a, &b));
        let (mut l3, mut r3) = (vec![0.0; 3 * k], vec![0.0; 3 * k]);
        factor_head(&matrix, k, Some(&left), 3, &mut l3, &mut r3);
        assert!(relative(&outer(&l3, &r3, k, 3), &matrix) < 1e-5);
    }

    #[test]
    fn rwkv_state_lowrank_decoder_rejects_bad_blocks() {
        let mut matrix = vec![0.0_f32; HEADS * HEAD_SIZE * HEAD_SIZE];
        assert!(decode_block(&[1], &mut matrix).is_err());
        assert!(decode_block(&[1, 9], &mut matrix).is_err());
        let mut block = vec![0_u8; block_len(1)];
        block[1] = 1;
        assert!(decode_block(&block, &mut matrix).is_err(), "zero scale");
        for chunk in block[2..].chunks_exact_mut(2 + HEAD_SIZE) {
            chunk[..2].copy_from_slice(&f16::ONE.to_bits().to_le_bytes());
        }
        decode_block(&block, &mut matrix).unwrap();
        block[4] = 128;
        assert!(decode_block(&block, &mut matrix).is_err(), "code -128");
        assert!(decode_block(&block[..block.len() - 1], &mut matrix).is_err());
    }

    /// Manual micro-benchmark of the codec alone (release build, one
    /// thread): one layer's write (`encode_layer` on a previous block, the
    /// factored solve, the int8 rounding and the reconstruction) and one
    /// layer's read (`decode_block`), at rank 2 (a card below 16 answers)
    /// and rank 4, for this engine's head layout.
    ///
    /// ```sh
    /// cargo test --release -p anki --lib rwkv_state_lowrank_codec_micro_benchmark -- --ignored --nocapture
    /// ```
    #[test]
    #[ignore]
    fn rwkv_state_lowrank_codec_micro_benchmark() {
        let size = HEADS * HEAD_SIZE * HEAD_SIZE;
        for (answers, label) in [(8_u32, "rank 2"), (40, "rank 4")] {
            let mut seed = 11;
            let mut block: Vec<u8> = Vec::new();
            let mut matrix = vec![0.0_f32; size];
            let mut writes = Vec::new();
            for answer in 1..=answers + 64 {
                let mut next: Vec<f32> = matrix.iter().map(|x| x * 0.9).collect();
                let v = random(&mut seed, HEADS * HEAD_SIZE, 2.0);
                let key = random(&mut seed, HEADS * HEAD_SIZE, 2.0);
                for head in 0..HEADS {
                    for row in 0..HEAD_SIZE {
                        for column in 0..HEAD_SIZE {
                            next[head * HEAD_SIZE * HEAD_SIZE + row * HEAD_SIZE + column] +=
                                v[head * HEAD_SIZE + row] * key[head * HEAD_SIZE + column];
                        }
                    }
                }
                if answer > answers {
                    writes.push((block.clone(), next.clone(), v.clone()));
                }
                let previous = if block.is_empty() {
                    Previous::Fresh
                } else {
                    Previous::Block(&block)
                };
                let mut out = Vec::new();
                encode_layer(&mut next, previous, &v, RankSchedule::DEFAULT, &mut out);
                block = out;
                matrix = next;
            }
            let rounds = 2_000;
            let mut out = Vec::with_capacity(block.len());
            let started = std::time::Instant::now();
            let mut checksum = 0.0_f32;
            for _ in 0..rounds {
                for (previous, next, v) in &writes {
                    let mut next = next.clone();
                    out.clear();
                    encode_layer(
                        &mut next,
                        Previous::Block(previous),
                        v,
                        RankSchedule::DEFAULT,
                        &mut out,
                    );
                    checksum += next[0];
                }
            }
            let encode_ns =
                started.elapsed().as_secs_f64() * 1e9 / (rounds * writes.len()) as f64;
            let mut decoded = vec![0.0_f32; size];
            let started = std::time::Instant::now();
            for _ in 0..rounds {
                for (previous, _, _) in &writes {
                    decode_block(previous, &mut decoded).unwrap();
                    checksum += decoded[0];
                }
            }
            let decode_ns =
                started.elapsed().as_secs_f64() * 1e9 / (rounds * writes.len()) as f64;
            println!(
                "heads={HEADS} head_size={HEAD_SIZE} {label}: encode_layer {encode_ns:.0} ns, decode_block {decode_ns:.0} ns per layer (checksum {checksum:.3})"
            );
        }
    }
}
