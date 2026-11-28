// Copyright: Anki Software, LLC and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Frozen copy of the low-rank codec as merged into the release candidate
//! (rc-2026-11 at 514f3a41d), before the allocation-free rewrite of SM156.
//! Tests only: the bit-identity tests run this and the current codec on the
//! same writes and compare the stored bytes; the micro-benchmark times both.

#![allow(dead_code)]

use half::f16;

use super::super::precision::f16_scale;
use super::super::precision::quantize;
use super::super::HEADS;
use super::super::HEAD_SIZE;
use super::RankSchedule;
use super::KEEP_TOL;
use super::MAX_RANK;

// The int8-only definitions the frozen codec used from its module, copied
// here unchanged: the module's own carry the factor width since the 6-bit
// factors (SM162). The codec below is untouched; int6 has no frozen Rust
// reference, the scorer's vectors are its reference.
const LIMIT: f32 = 127.0;

fn block_len(rank: usize) -> usize {
    2 + HEADS * 2 * rank * (2 + HEAD_SIZE)
}

#[derive(Clone, Copy)]
pub(super) enum Previous<'a> {
    Fresh,
    Block(&'a [u8]),
    Matrix,
}

fn header(block: &[u8]) -> Result<(u8, usize), ()> {
    if block.len() < 2 {
        return Err(());
    }
    let rank = block[1] as usize;
    if rank > MAX_RANK.min(HEAD_SIZE) || block.len() != block_len(rank) {
        return Err(());
    }
    Ok((block[0], rank))
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
pub(super) fn decode_block(block: &[u8], matrix: &mut [f32]) -> Result<(), ()> {
    let (_, rank) = header(block)?;
    for chunk in block[2..].chunks_exact(VECTOR_BYTES) {
        let scale = f16::from_bits(u16::from_le_bytes([chunk[0], chunk[1]])).to_f32();
        if !scale.is_finite() || scale <= 0.0 {
            return Err(());
        }
        if chunk[2..].contains(&128) {
            return Err(());
        }
    }
    reconstruct(block, rank, matrix);
    if matrix.iter().any(|value| !value.is_finite()) {
        return Err(());
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
