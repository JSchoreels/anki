// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Low-rank card / note WKV state (precision bytes 0x28 and 0x26): every
//! head's matrix is stored as a rank-c product L R with int8 (0x28) or int6
//! (0x26) factors.
//!
//! This is the training repository's `LowRankCodec`
//! (`scripts/state_lowrank.py`, the low-rank int8 state experiment). After each
//! write the stored state is the best rank-r approximation of the float update
//! S' = S G + v k^T. Because the previous stored state is L R, the columns of
//! S' lie in span(L, v), so the truncation needs only small eigen problems:
//!
//! 1. P = [L_1 .. L_c, v] (m = c + 1 vectors of `HEAD_SIZE` values), each
//!    vector normalised; the Gram matrix of the normalised vectors is
//!    eigen-decomposed and directions with an eigenvalue at or below 1e-5 x the
//!    largest are dropped; Q is the orthonormal basis that remains.
//! 2. B = Q^T S' (m x K); the eigenvectors U of B B^T (by decreasing
//!    eigenvalue) give component i: left_i = Q u_i, right_i = u_i^T B, with
//!    sigma_i = |right_i|, split evenly: L_i = left_i sqrt(sigma_i), R_i =
//!    right_i / sqrt(sigma_i).
//! 3. Each factor vector is rounded to int8 (or int6) codes with its own
//!    float16 scale (the compact rows' rule: abs-max / 127, or abs-max / 31 for
//!    int6, clamped to 65,504, 1.0 on zero), and the stored matrix is the
//!    product of the decoded factors. Int6 is the scorer's `q6` factor format
//!    (`w8+lr2.16.4q6x8`, the smaller shift vectors and factors experiment
//!    (SM152)): the same arithmetic with codes in [-31, 31].
//!
//! Writes can be batched (`encode_layers`): the heads of several layer
//! writes (several cards of a replay) are the lanes of one solve, stored
//! lane by lane so the small eigen problems of all lanes run side by side
//! in SIMD registers (on x86-64 the solve is compiled for AVX2 and AVX-512
//! too, picked at run time). Every lane does exactly the operations of a
//! write alone, in the same order, so the bytes and the stored matrix are
//! the same bit for bit at every batch size and vector width.
//!
//! The rank follows a schedule (`RankSchedule`): rank `low` below `switch`
//! answers, rank `high` from `switch`; an entity with n answers stores rank
//! min(n, r) (a state with n answers has rank at most n). The solve runs in
//! f64 (the scorer: float32); the tests bound the difference.
//!
//! Stored block per layer: answer count n (u8, saturating at 255), rank c
//! (u8), then per head c left vectors and c right vectors, each a float16
//! scale and `HEAD_SIZE` codes: one byte per int8 code, or the int6 codes
//! bit-packed little-endian in two's complement (four codes in three bytes;
//! the code -32 is never written).

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

/// The code width of the factor vectors (the stored layout: precision byte
/// 0x28 for int8, 0x26 for int6).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Factors {
    Int8,
    Int6,
}

const _: () = assert!(
    HEAD_SIZE.is_multiple_of(4),
    "int6 codes pack in groups of four"
);

impl Factors {
    /// The largest code magnitude (the scorer's `qmax`).
    const fn limit(self) -> f32 {
        match self {
            Self::Int8 => 127.0,
            Self::Int6 => 31.0,
        }
    }

    /// Bytes of one factor vector: a float16 scale and the codes.
    const fn vector_bytes(self) -> usize {
        2 + match self {
            Self::Int8 => HEAD_SIZE,
            Self::Int6 => HEAD_SIZE / 4 * 3,
        }
    }
}

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
pub(super) fn block_len(rank: usize, factors: Factors) -> usize {
    2 + HEADS * 2 * rank * factors.vector_bytes()
}

/// Splits a stored block into (answer count, rank, factor bytes) after
/// checking its length.
fn header(block: &[u8], factors: Factors) -> io::Result<(u8, usize)> {
    if block.len() < 2 {
        return Err(invalid("truncated low-rank block"));
    }
    let rank = block[1] as usize;
    if rank > MAX_RANK.min(HEAD_SIZE) || block.len() != block_len(rank, factors) {
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

/// The largest basis of a write's solve: the previous write's left vectors
/// (at most `MAX_RANK`) and the new value vector.
const MAX_BASIS: usize = MAX_RANK + 1;
const _: () = assert!(MAX_BASIS == 9, "eigh_block dispatches n = 1..=9");

/// One factor vector of this engine's head size.
type Vector = [f32; HEAD_SIZE];

/// Decoded factor vector `index` (0..2c: left vectors, then right vectors) of
/// `head`.
fn vector(block: &[u8], rank: usize, factors: Factors, head: usize, index: usize, out: &mut [f32]) {
    let vector_bytes = factors.vector_bytes();
    let start = 2 + (head * 2 * rank + index) * vector_bytes;
    let bytes = &block[start..start + vector_bytes];
    let scale = f16::from_bits(u16::from_le_bytes([bytes[0], bytes[1]])).to_f32();
    match factors {
        Factors::Int8 => {
            for (value, &code) in out.iter_mut().zip(&bytes[2..]) {
                *value = code as i8 as f32 * scale;
            }
        }
        Factors::Int6 => {
            for (values, packed) in out.chunks_exact_mut(4).zip(bytes[2..].chunks_exact(3)) {
                for (value, code) in values.iter_mut().zip(unpack6(packed)) {
                    *value = code as f32 * scale;
                }
            }
        }
    }
}

/// Four int6 codes (two's complement, the first in the lowest bits) in
/// three little-endian bytes: the training repository's bit packing.
fn pack6(codes: [i8; 4]) -> [u8; 3] {
    let word = codes.iter().enumerate().fold(0_u32, |word, (i, &code)| {
        word | ((code as u8 & 63) as u32) << (6 * i)
    });
    [word as u8, (word >> 8) as u8, (word >> 16) as u8]
}

/// The four codes of `pack6` (sign-extended).
fn unpack6(bytes: &[u8]) -> [i8; 4] {
    let word = bytes[0] as u32 | (bytes[1] as u32) << 8 | (bytes[2] as u32) << 16;
    std::array::from_fn(|i| (((word >> (6 * i)) as u8) << 2) as i8 >> 2)
}

/// Checks a stored block (sizes, scales, codes) and writes the matrix it
/// stores (`HEADS * HEAD_SIZE * HEAD_SIZE` values).
pub(super) fn decode_block(block: &[u8], factors: Factors, matrix: &mut [f32]) -> io::Result<()> {
    let (_, rank) = header(block, factors)?;
    for chunk in block[2..].chunks_exact(factors.vector_bytes()) {
        let scale = f16::from_bits(u16::from_le_bytes([chunk[0], chunk[1]])).to_f32();
        if !scale.is_finite() || scale <= 0.0 {
            return Err(invalid("non-finite or non-positive factor scale"));
        }
        let invalid_code = match factors {
            Factors::Int8 => chunk[2..].contains(&128),
            Factors::Int6 => chunk[2..]
                .chunks_exact(3)
                .any(|packed| unpack6(packed).contains(&-32)),
        };
        if invalid_code {
            return Err(invalid("out-of-range factor code"));
        }
    }
    reconstruct(block, rank, factors, matrix);
    if !all_finite(matrix) {
        return Err(invalid("non-finite matrix value"));
    }
    Ok(())
}

/// Every value finite (no early exit, so the check vectorises).
fn all_finite(values: &[f32]) -> bool {
    values
        .iter()
        .fold(true, |finite, value| finite & value.is_finite())
}

fn reconstruct(block: &[u8], rank: usize, factors: Factors, matrix: &mut [f32]) {
    debug_assert_eq!(matrix.len(), HEADS * HEAD_SIZE * HEAD_SIZE);
    let mut left = [[0.0_f32; HEAD_SIZE]; MAX_RANK];
    let mut right = [[0.0_f32; HEAD_SIZE]; MAX_RANK];
    for head in 0..HEADS {
        for component in 0..rank {
            vector(block, rank, factors, head, component, &mut left[component]);
            vector(
                block,
                rank,
                factors,
                head,
                rank + component,
                &mut right[component],
            );
        }
        let base = head * HEAD_SIZE * HEAD_SIZE;
        product(
            &left[..rank],
            &right[..rank],
            &mut matrix[base..base + HEAD_SIZE * HEAD_SIZE],
        );
    }
}

/// out[row][column] = sum_i left_i[row] right_i[column], in component order
/// (every value of `out` is written, also for no components).
fn product(left: &[Vector], right: &[Vector], out: &mut [f32]) {
    for (row, out_row) in out.chunks_exact_mut(HEAD_SIZE).enumerate() {
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
    /// A stored low-rank block and its factor width (which may differ from
    /// the next write's: the basis is the decoded vectors).
    Block(&'a [u8], Factors),
    /// A non-zero matrix without factors (a state written in another format
    /// or held at float32): the full basis, answer count unknown (saturated).
    Matrix,
}

/// Encodes one layer's float update `matrix` (replaced by the stored
/// reconstruction) after a write with value vector `v` (`HEADS * HEAD_SIZE`)
/// and appends the block to `out`, with `factors` codes: `encode_layers`
/// with one write.
pub(super) fn encode_layer(
    matrix: &mut [f32],
    previous: Previous<'_>,
    v: &[f32],
    schedule: RankSchedule,
    factors: Factors,
    out: &mut Vec<u8>,
) {
    encode_layers::<HEADS>(
        &mut [LayerWrite {
            matrix,
            previous,
            v,
            out,
        }],
        schedule,
        factors,
    );
}

/// One layer write of a batch (`encode_layers`): the float update (replaced
/// by the stored reconstruction), the previous write's factors, the value
/// vector, and the buffer the block is appended to.
pub(super) struct LayerWrite<'a> {
    pub(super) matrix: &'a mut [f32],
    pub(super) previous: Previous<'a>,
    pub(super) v: &'a [f32],
    pub(super) out: &'a mut Vec<u8>,
}

/// Lanes of a batched write (`encode_layers::<BATCH_LANES>`): four layer
/// writes (four cards of a replay) at once.
pub(super) const BATCH_LANES: usize = 4 * HEADS;

/// Layer writes worth batching on this CPU: `BATCH_LANES / HEADS` with
/// 256-bit or wider vectors (x86-64 AVX2 or AVX-512), else one (with
/// 128-bit vectors more lanes only add work).
pub(super) fn batch_writes() -> usize {
    match simd() {
        Simd::Baseline => 1,
        #[cfg(target_arch = "x86_64")]
        Simd::Avx2 | Simd::Avx512 => BATCH_LANES / HEADS,
    }
}

/// Encodes independent layer writes (`encode_layer` each), `L / HEADS` at a
/// time: the heads of those writes are the lanes of one solve, so their
/// small eigen problems run together, lane by lane in SIMD registers. Every
/// write gets exactly the bytes and the matrix `encode_layer` gives it alone
/// (each lane does the same operations in the same order).
///
/// Everything lives in fixed-size stack arrays (no allocation on the
/// factored path); the stored matrix is the product of the rounded factors
/// as they are written (the values a decode of the block gives).
pub(super) fn encode_layers<const L: usize>(
    writes: &mut [LayerWrite<'_>],
    schedule: RankSchedule,
    factors: Factors,
) {
    const { assert!(L >= HEADS && L.is_multiple_of(HEADS)) };
    for batch in writes.chunks_mut(L / HEADS) {
        encode_batch::<L>(batch, schedule, factors);
    }
}

/// `encode_batch_impl` compiled for the widest vector unit available (the
/// same bits at every width: no operation is reordered or fused).
fn encode_batch<const L: usize>(
    writes: &mut [LayerWrite<'_>],
    schedule: RankSchedule,
    factors: Factors,
) {
    #[cfg(target_arch = "x86_64")]
    {
        #[target_feature(enable = "avx2")]
        fn avx2<const L: usize>(
            writes: &mut [LayerWrite<'_>],
            schedule: RankSchedule,
            factors: Factors,
        ) {
            encode_batch_impl::<L>(writes, schedule, factors)
        }
        #[target_feature(enable = "avx512f")]
        fn avx512<const L: usize>(
            writes: &mut [LayerWrite<'_>],
            schedule: RankSchedule,
            factors: Factors,
        ) {
            encode_batch_impl::<L>(writes, schedule, factors)
        }
        match simd() {
            // SAFETY: the CPU has the feature (checked by `simd`).
            Simd::Avx512 => return unsafe { avx512::<L>(writes, schedule, factors) },
            // SAFETY: as above.
            Simd::Avx2 => return unsafe { avx2::<L>(writes, schedule, factors) },
            Simd::Baseline => {}
        }
    }
    encode_batch_impl::<L>(writes, schedule, factors)
}

#[inline(always)]
fn encode_batch_impl<const L: usize>(
    writes: &mut [LayerWrite<'_>],
    schedule: RankSchedule,
    factors: Factors,
) {
    debug_assert!(writes.len() * HEADS <= L);
    let vector_bytes = factors.vector_bytes();
    let mut left = [[[0.0_f32; HEAD_SIZE]; MAX_RANK]; L];
    let mut right = [[[0.0_f32; HEAD_SIZE]; MAX_RANK]; L];
    let mut basis = [[[0.0_f32; HEAD_SIZE]; MAX_BASIS]; L];
    let mut m0 = [0_usize; L];
    // Per lane: the rank of the factored solve (0: no factored solve).
    let mut solve_rank = [0_usize; L];
    let mut ranks = [0_usize; L];
    let mut starts = [0_usize; L];
    for (index, write) in writes.iter_mut().enumerate() {
        let (answers, previous_rank) = match write.previous {
            Previous::Fresh => (1, 0),
            Previous::Block(block, width) => match header(block, width) {
                Ok((answers, rank)) => (answers.saturating_add(1), rank),
                Err(_) => (u8::MAX, 0),
            },
            Previous::Matrix => (u8::MAX, 0),
        };
        let full_basis = match write.previous {
            Previous::Matrix => true,
            Previous::Block(block, width) => header(block, width).is_err(),
            Previous::Fresh => false,
        };
        debug_assert_eq!(write.matrix.len(), HEADS * HEAD_SIZE * HEAD_SIZE);
        let rank = schedule.rank(answers);
        let out = &mut *write.out;
        let start = out.len();
        out.push(answers);
        out.push(rank as u8);
        out.resize(start + block_len(rank, factors), 0);
        ranks[index] = rank;
        starts[index] = start;
        sanitize(write.matrix);
        let lanes = index * HEADS..(index + 1) * HEADS;
        if full_basis {
            for (lane, head_matrix) in lanes.zip(write.matrix.chunks_exact(HEAD_SIZE * HEAD_SIZE)) {
                factor_head_general(
                    head_matrix,
                    HEAD_SIZE,
                    None,
                    rank,
                    left[lane].as_flattened_mut(),
                    right[lane].as_flattened_mut(),
                );
            }
        } else {
            debug_assert_eq!(write.v.len(), HEADS * HEAD_SIZE);
            for (head, lane) in lanes.enumerate() {
                let vectors = &mut basis[lane];
                if let Previous::Block(block, width) = write.previous {
                    for (component, slot) in vectors[..previous_rank].iter_mut().enumerate() {
                        vector(block, previous_rank, width, head, component, slot);
                    }
                }
                let value = &mut vectors[previous_rank];
                value.copy_from_slice(&write.v[head * HEAD_SIZE..(head + 1) * HEAD_SIZE]);
                sanitize(value);
                m0[lane] = previous_rank + 1;
                solve_rank[lane] = rank;
            }
        }
    }
    {
        let lanes = writes.len() * HEADS;
        let mut matrices: [&[f32]; L] = [&[]; L];
        for (index, write) in writes.iter().enumerate() {
            for (head, head_matrix) in write.matrix.chunks_exact(HEAD_SIZE * HEAD_SIZE).enumerate()
            {
                matrices[index * HEADS + head] = head_matrix;
            }
        }
        factor_heads::<HEAD_SIZE, L>(
            &matrices[..lanes],
            &basis,
            &m0,
            &solve_rank,
            &mut left,
            &mut right,
        );
    }
    for (index, write) in writes.iter_mut().enumerate() {
        let (rank, start) = (ranks[index], starts[index]);
        let out = &mut *write.out;
        for (head, head_matrix) in write
            .matrix
            .chunks_exact_mut(HEAD_SIZE * HEAD_SIZE)
            .enumerate()
        {
            let lane = index * HEADS + head;
            let (left, right) = (&mut left[lane][..rank], &mut right[lane][..rank]);
            let mut at = start + 2 + head * 2 * rank * vector_bytes;
            for vector in left.iter_mut().chain(right.iter_mut()) {
                quantize_vector(vector, factors, &mut out[at..at + vector_bytes]);
                at += vector_bytes;
            }
            product(left, right, head_matrix);
        }
    }
}

/// Encodes a matrix that no write at this precision produced (a state held
/// at float32 or stored in another layout): an all-zero matrix as answer
/// count 0 and rank 0, any other on the full basis with the answer count
/// unknown (saturated).
pub(super) fn encode_existing(
    matrix: &mut [f32],
    schedule: RankSchedule,
    factors: Factors,
    out: &mut Vec<u8>,
) {
    if matrix.iter().all(|value| *value == 0.0) {
        out.extend_from_slice(&[0, 0]);
        matrix.fill(0.0);
        return;
    }
    encode_layer(matrix, Previous::Matrix, &[], schedule, factors, out);
}

/// Non-finite values (never written by a finite model) become finite so
/// the stored state stays decodable: NaN as 0, infinities as +-f32::MAX.
fn sanitize(values: &mut [f32]) {
    if all_finite(values) {
        return;
    }
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

/// Rounds `values` to `factors` codes with one float16 scale (written to
/// `out`: scale, then the codes) and replaces them with the decoded values.
/// Int6 is the int8 arithmetic with limit 31 (the scorer's
/// `quant_vectors(x, dim, 31)`), the codes then bit-packed.
#[inline(always)]
pub(super) fn quantize_vector(values: &mut [f32], factors: Factors, out: &mut [u8]) {
    let limit = factors.limit();
    // The largest magnitude (the maximum of a set: any order gives it; NaN
    // is skipped either way), eight running maxima at a time.
    let mut maxima = [0.0_f32; 8];
    let chunks = values.chunks_exact(8);
    let rest = chunks.remainder();
    for chunk in chunks {
        for (maximum, value) in maxima.iter_mut().zip(chunk) {
            *maximum = maximum.max(value.abs());
        }
    }
    let maximum = rest
        .iter()
        .map(|v| v.abs())
        .chain(maxima)
        .fold(0.0_f32, f32::max);
    let scale = f16_scale(maximum, limit);
    out[..2].copy_from_slice(&scale.to_bits().to_le_bytes());
    let scale = scale.to_f32();
    match factors {
        Factors::Int8 => {
            for (value, code) in values.iter_mut().zip(&mut out[2..]) {
                let q = quantize(*value, scale, limit);
                *code = q as u8;
                *value = q as f32 * scale;
            }
        }
        Factors::Int6 => {
            for (values, packed) in values.chunks_exact_mut(4).zip(out[2..].chunks_exact_mut(3)) {
                let codes: [i8; 4] = std::array::from_fn(|i| quantize(values[i], scale, limit));
                packed.copy_from_slice(&pack6(codes));
                for (value, code) in values.iter_mut().zip(codes) {
                    *value = code as f32 * scale;
                }
            }
        }
    }
}

/// `factor_heads` for one head and a head size given at run time (the tests'
/// vectors cover both engines' head sizes); other head sizes and the full basis
/// use the general solve.
#[cfg(test)]
pub(super) fn factor_head(
    matrix: &[f32],
    k: usize,
    basis: Option<&[f32]>,
    rank: usize,
    left_out: &mut [f32],
    right_out: &mut [f32],
) {
    fn fixed<const K: usize>(
        matrix: &[f32],
        basis: &[f32],
        rank: usize,
        left_out: &mut [f32],
        right_out: &mut [f32],
    ) {
        let mut vectors = [[[0.0_f32; K]; MAX_BASIS]];
        for (vector, values) in vectors[0].iter_mut().zip(basis.chunks_exact(K)) {
            vector.copy_from_slice(values);
        }
        let mut left = [[[0.0_f32; K]; MAX_RANK]];
        let mut right = [[[0.0_f32; K]; MAX_RANK]];
        factor_heads::<K, 1>(
            &[matrix],
            &vectors,
            &[basis.len() / K],
            &[rank],
            &mut left,
            &mut right,
        );
        left_out[..rank * K].copy_from_slice(left[0][..rank].as_flattened());
        right_out[..rank * K].copy_from_slice(right[0][..rank].as_flattened());
    }
    match basis {
        Some(basis)
            if rank <= MAX_RANK && (k..=MAX_BASIS * k).contains(&basis.len()) && k == 16 =>
        {
            fixed::<16>(matrix, basis, rank, left_out, right_out)
        }
        Some(basis)
            if rank <= MAX_RANK && (k..=MAX_BASIS * k).contains(&basis.len()) && k == 32 =>
        {
            fixed::<32>(matrix, basis, rank, left_out, right_out)
        }
        _ => factor_head_general(matrix, k, basis, rank, left_out, right_out),
    }
}

/// One small symmetric problem (the leading `n x n` block is used).
type Small = [[f64; MAX_BASIS]; MAX_BASIS];

/// The rank-`rank[lane]` factors of each lane's `K x K` matrix
/// (`matrices[lane]`, row = value index) whose column space lies in the span
/// of that lane's first `m0[lane]` basis vectors, by decreasing singular
/// value; zero for components beyond the basis. Writes `left_out[lane]` and
/// `right_out[lane]` components `0..rank[lane]`; a lane of rank 0 is left
/// untouched. The lanes are the heads of one or more layer writes.
///
/// Per lane, the same arithmetic as `factor_head_general` on a basis,
/// operation for operation and in the same order (so the same bits), with
/// stack arrays and compile-time sizes. The lanes' eigen problems of the
/// same size run together (`eigh_groups`), so their serial Jacobi rotations
/// run side by side in SIMD registers.
#[inline(always)]
fn factor_heads<const K: usize, const L: usize>(
    matrices: &[&[f32]],
    basis: &[[[f32; K]; MAX_BASIS]],
    m0: &[usize],
    rank: &[usize],
    left_out: &mut [[[f32; K]; MAX_RANK]],
    right_out: &mut [[[f32; K]; MAX_RANK]],
) {
    let lanes = matrices.len();
    debug_assert!(lanes <= L && basis.len() >= lanes && m0.len() >= lanes && rank.len() >= lanes);
    let mut sizes = [0_usize; L];
    // P: the basis vectors, each normalised (zero vectors stay zero).
    let mut pn = [[[0.0_f64; K]; MAX_BASIS]; L];
    let mut gram = [[[0.0_f64; MAX_BASIS]; MAX_BASIS]; L];
    for lane in 0..lanes {
        let (m0, rank) = (m0[lane], rank[lane]);
        debug_assert!(rank <= MAX_RANK && matrices[lane].len() == K * K);
        for vector in left_out[lane][..rank]
            .iter_mut()
            .chain(&mut right_out[lane][..rank])
        {
            vector.fill(0.0);
        }
        if rank == 0 {
            continue;
        }
        debug_assert!((1..=MAX_BASIS).contains(&m0));
        sizes[lane] = m0;
        let pn = &mut pn[lane][..m0];
        for (vector, values) in pn.iter_mut().zip(&basis[lane][..m0]) {
            for (x, &value) in vector.iter_mut().zip(values) {
                *x = value as f64;
            }
            let norm = vector.iter().map(|x| x * x).sum::<f64>().sqrt();
            if norm > 0.0 {
                for x in vector.iter_mut() {
                    *x /= norm;
                }
            }
        }
        gram_fixed(pn, &mut gram[lane]);
    }
    let (lam, w) = eigh_groups::<L>(&gram, &sizes[..lanes]);
    // Q: orthonormal basis vectors of the kept directions; B = Q^T S.
    let mut q = [[[0.0_f64; K]; MAX_BASIS]; L];
    let mut b = [[[0.0_f64; K]; MAX_BASIS]; L];
    let mut bbt = [[[0.0_f64; MAX_BASIS]; MAX_BASIS]; L];
    let mut kept = [0_usize; L];
    for lane in 0..lanes {
        let m0 = sizes[lane];
        if m0 == 0 {
            continue;
        }
        let (lam, w, pn) = (&lam[lane], &w[lane], &pn[lane]);
        let top = lam[..m0].iter().copied().fold(0.0_f64, f64::max);
        let q = &mut q[lane];
        let mut m = 0;
        for (j, &value) in lam[..m0].iter().enumerate() {
            if value > top * KEEP_TOL && value > 0.0 {
                let inv = value.sqrt().recip();
                let column = &mut q[m];
                for i in 0..m0 {
                    let weight = w[i][j] * inv;
                    if weight != 0.0 {
                        for (c, &x) in column.iter_mut().zip(&pn[i]) {
                            *c += weight * x;
                        }
                    }
                }
                m += 1;
            }
        }
        kept[lane] = m;
        // B row by row of S (each value still sums the rows in order), so
        // the m vectors' sums advance together.
        let b = &mut b[lane][..m];
        for (r, row) in matrices[lane].chunks_exact(K).take(K).enumerate() {
            let row: [f64; K] = std::array::from_fn(|c| row[c] as f64);
            for (bj, qj) in b.iter_mut().zip(&*q) {
                let weight = qj[r];
                if weight != 0.0 {
                    for (value, &s) in bj.iter_mut().zip(&row) {
                        *value += weight * s;
                    }
                }
            }
        }
        gram_fixed(b, &mut bbt[lane]);
    }
    // The eigen problems of B B^T.
    let (s2, u) = eigh_groups::<L>(&bbt, &kept[..lanes]);
    for lane in 0..lanes {
        let m = kept[lane];
        if m == 0 {
            continue;
        }
        let (s2, u, q, b) = (&s2[lane], &u[lane], &q[lane], &b[lane]);
        let mut order = [0_usize; MAX_BASIS];
        for (index, slot) in order.iter_mut().enumerate() {
            *slot = index;
        }
        let order = &mut order[..m];
        order.sort_by(|&x, &y| s2[y].total_cmp(&s2[x]));
        for (component, &column) in order.iter().take(rank[lane]).enumerate() {
            let mut left = [0.0_f64; K];
            let mut right = [0.0_f64; K];
            for j in 0..m {
                let weight = u[j][column];
                for (value, &x) in left.iter_mut().zip(&q[j]) {
                    *value += weight * x;
                }
                for (value, &x) in right.iter_mut().zip(&b[j]) {
                    *value += weight * x;
                }
            }
            let sigma = right.iter().map(|x| x * x).sum::<f64>().sqrt();
            let g = sigma.sqrt();
            let divisor = if g > 0.0 { g } else { 1.0 };
            for ((out, &x), (out_right, &y)) in left_out[lane][component]
                .iter_mut()
                .zip(&left)
                .zip(right_out[lane][component].iter_mut().zip(&right))
            {
                *out = (x * g) as f32;
                *out_right = (y / divisor) as f32;
            }
        }
    }
}

fn dot_fixed<const K: usize>(a: &[f64; K], b: &[f64; K]) -> f64 {
    a.iter().zip(b).map(|(x, y)| x * y).sum()
}

/// `out[i][j] = out[j][i]` = the dot product of `vectors[i]` and
/// `vectors[j]` (j <= i).
#[inline(always)]
fn gram_fixed<const K: usize>(vectors: &[[f64; K]], out: &mut Small) {
    for (i, a) in vectors.iter().enumerate() {
        for (j, b) in vectors[..=i].iter().enumerate() {
            let dot = dot_fixed(a, b);
            out[i][j] = dot;
            out[j][i] = dot;
        }
    }
}

/// `eigh` of the leading `sizes[lane] x sizes[lane]` block (0 = none, up to
/// `MAX_BASIS`) of each lane's problem: eigenvalues, and eigenvectors as
/// columns (zeros beyond the size and for lanes of size 0). The lanes of
/// one size run together, up to 16 at a time.
fn eigh_groups<const L: usize>(
    problems: &[Small; L],
    sizes: &[usize],
) -> ([[f64; MAX_BASIS]; L], [Small; L]) {
    let mut values = [[0.0_f64; MAX_BASIS]; L];
    let mut vectors = [[[0.0_f64; MAX_BASIS]; MAX_BASIS]; L];
    for size in 1..=MAX_BASIS {
        let mut members = [0_usize; L];
        let mut count = 0;
        for (lane, &lane_size) in sizes.iter().enumerate() {
            if lane_size == size {
                members[count] = lane;
                count += 1;
            }
        }
        for block in members[..count].chunks(16) {
            eigh_block(problems, size, block, &mut values, &mut vectors);
        }
    }
    (values, vectors)
}

/// `eigh` of the leading `n x n` block of the problems `members` (at most
/// 16), in one Jacobi run of 4, 8 or 16 lanes.
fn eigh_block(
    problems: &[Small],
    n: usize,
    members: &[usize],
    values: &mut [[f64; MAX_BASIS]],
    vectors: &mut [Small],
) {
    fn run<const N: usize, const B: usize>(
        problems: &[Small],
        members: &[usize],
        values: &mut [[f64; MAX_BASIS]],
        vectors: &mut [Small],
    ) {
        let mut a = [[[0.0_f64; B]; N]; N];
        for (slot, &member) in members.iter().enumerate() {
            for (row, values) in a.iter_mut().zip(&problems[member]) {
                for (entry, &value) in row.iter_mut().zip(values) {
                    entry[slot] = value;
                }
            }
        }
        let mut active = [false; B];
        active[..members.len()].fill(true);
        let v = jacobi_dispatch(&mut a, active);
        for (slot, &member) in members.iter().enumerate() {
            for i in 0..N {
                values[member][i] = a[i][i][slot];
                for j in 0..N {
                    vectors[member][i][j] = v[i][j][slot];
                }
            }
        }
    }
    macro_rules! sizes {
        ($($n:literal)*) => {
            match (n, members.len()) {
                $(
                    ($n, 0..=4) => run::<$n, 4>(problems, members, values, vectors),
                    ($n, 5..=8) => run::<$n, 8>(problems, members, values, vectors),
                    ($n, 9..=16) => run::<$n, 16>(problems, members, values, vectors),
                )*
                _ => unreachable!("eigen problem of size {n} with {} lanes", members.len()),
            }
        };
    }
    sizes!(1 2 3 4 5 6 7 8 9);
}

/// `eigh`'s cyclic Jacobi rotations on each active lane's `N x N` problem
/// (lane-minor: `a[i][j][lane]`; in place: the diagonal ends as the
/// eigenvalues; returns the eigenvectors as columns). Every lane runs
/// exactly `eigh`'s sequence (stop rule, sweep cap, rotation order,
/// operation order), so each gives `eigh`'s bits. The lanes share the loop
/// and sit side by side in memory, so the compiler runs them in SIMD
/// registers: every lane computes every rotation, and a lane that `eigh`
/// would not rotate (inactive, or a zero pivot) keeps its old values
/// (a select, never an identity rotation, which could flip a zero's sign).
/// The widest vector unit of this CPU the codec uses (x86-64: checked once
/// at run time; the solve gives the same bits at every width).
#[derive(Clone, Copy, PartialEq, Eq)]
enum Simd {
    Baseline,
    #[cfg(target_arch = "x86_64")]
    Avx2,
    #[cfg(target_arch = "x86_64")]
    Avx512,
}

#[cfg(target_arch = "x86_64")]
fn simd() -> Simd {
    use std::sync::atomic::AtomicU8;
    use std::sync::atomic::Ordering;
    static LEVEL: AtomicU8 = AtomicU8::new(0);
    let level = match LEVEL.load(Ordering::Relaxed) {
        0 => {
            #[allow(unused_mut)]
            let mut level = if std::is_x86_feature_detected!("avx512f") {
                3
            } else if std::is_x86_feature_detected!("avx2") {
                2
            } else {
                1
            };
            // Tests and benchmarks can cap the width.
            #[cfg(test)]
            if let Ok(cap) = std::env::var("ANKI_RWKV_LOWRANK_SIMD") {
                level = level.min(match cap.as_str() {
                    "baseline" => 1,
                    "avx2" => 2,
                    _ => 3,
                });
            }
            LEVEL.store(level, Ordering::Relaxed);
            level
        }
        level => level,
    };
    match level {
        3 => Simd::Avx512,
        2 => Simd::Avx2,
        _ => Simd::Baseline,
    }
}

#[cfg(not(target_arch = "x86_64"))]
fn simd() -> Simd {
    Simd::Baseline
}

/// `jacobi_lanes` compiled for the widest vector unit available.
fn jacobi_dispatch<const N: usize, const B: usize>(
    a: &mut [[[f64; B]; N]; N],
    active: [bool; B],
) -> [[[f64; B]; N]; N] {
    #[cfg(target_arch = "x86_64")]
    {
        #[target_feature(enable = "avx2")]
        fn avx2<const N: usize, const B: usize>(
            a: &mut [[[f64; B]; N]; N],
            active: [bool; B],
        ) -> [[[f64; B]; N]; N] {
            jacobi_lanes(a, active)
        }
        #[target_feature(enable = "avx512f")]
        fn avx512<const N: usize, const B: usize>(
            a: &mut [[[f64; B]; N]; N],
            active: [bool; B],
        ) -> [[[f64; B]; N]; N] {
            jacobi_lanes(a, active)
        }
        match simd() {
            // SAFETY: the CPU has the feature (checked by `simd`).
            Simd::Avx512 => return unsafe { avx512(a, active) },
            // SAFETY: as above.
            Simd::Avx2 => return unsafe { avx2(a, active) },
            Simd::Baseline => {}
        }
    }
    jacobi_lanes(a, active)
}

#[inline(always)]
fn jacobi_lanes<const N: usize, const B: usize>(
    a: &mut [[[f64; B]; N]; N],
    mut active: [bool; B],
) -> [[[f64; B]; N]; N] {
    let mut v = [[[0.0_f64; B]; N]; N];
    for (i, row) in v.iter_mut().enumerate() {
        row[i] = [1.0; B];
    }
    let mut last_off = [f64::INFINITY; B];
    for _sweep in 0..60 {
        // The sums of `eigh` (same terms, same order; the terms are
        // squares, so the starting zero's sign does not matter).
        let mut off = [-0.0_f64; B];
        for (i, row) in a.iter().enumerate() {
            for (j, entry) in row.iter().enumerate() {
                if j != i {
                    for (sum, &value) in off.iter_mut().zip(entry) {
                        *sum += value * value;
                    }
                }
            }
        }
        let mut diagonal = [-0.0_f64; B];
        for (i, row) in a.iter().enumerate() {
            for (sum, &value) in diagonal.iter_mut().zip(&row[i]) {
                *sum += value * value;
            }
        }
        for lane in 0..B {
            if !active[lane] {
                continue;
            }
            let off = off[lane];
            if off <= diagonal[lane] * 1e-32 || off == 0.0 || off >= last_off[lane] {
                active[lane] = false;
            } else {
                last_off[lane] = off;
            }
        }
        if !active.contains(&true) {
            break;
        }
        for p in 0..N {
            for q in p + 1..N {
                let mut c = [0.0_f64; B];
                let mut s = [0.0_f64; B];
                let mut rotate = [false; B];
                for lane in 0..B {
                    let apq = a[p][q][lane];
                    let app = a[p][p][lane];
                    let aqq = a[q][q][lane];
                    let theta = (aqq - app) / (2.0 * apq);
                    let t = theta.signum() / (theta.abs() + (theta * theta + 1.0).sqrt());
                    let t = if theta == 0.0 { 1.0 } else { t };
                    let cosine = (t * t + 1.0).sqrt().recip();
                    c[lane] = cosine;
                    s[lane] = t * cosine;
                    rotate[lane] = active[lane] & (apq != 0.0);
                }
                if !rotate.contains(&true) {
                    continue;
                }
                for row in a.iter_mut() {
                    for lane in 0..B {
                        let arp = row[p][lane];
                        let arq = row[q][lane];
                        let (c, s) = (c[lane], s[lane]);
                        let (np, nq) = (c * arp - s * arq, s * arp + c * arq);
                        row[p][lane] = if rotate[lane] { np } else { arp };
                        row[q][lane] = if rotate[lane] { nq } else { arq };
                    }
                }
                let (before_q, from_q) = a.split_at_mut(q);
                for (apr, aqr) in before_q[p].iter_mut().zip(&mut from_q[0]) {
                    for lane in 0..B {
                        let (old_apr, old_aqr) = (apr[lane], aqr[lane]);
                        let (c, s) = (c[lane], s[lane]);
                        let (np, nq) = (c * old_apr - s * old_aqr, s * old_apr + c * old_aqr);
                        apr[lane] = if rotate[lane] { np } else { old_apr };
                        aqr[lane] = if rotate[lane] { nq } else { old_aqr };
                    }
                }
                for row in v.iter_mut() {
                    for lane in 0..B {
                        let vrp = row[p][lane];
                        let vrq = row[q][lane];
                        let (c, s) = (c[lane], s[lane]);
                        let (np, nq) = (c * vrp - s * vrq, s * vrp + c * vrq);
                        row[p][lane] = if rotate[lane] { np } else { vrp };
                        row[q][lane] = if rotate[lane] { nq } else { vrq };
                    }
                }
            }
        }
    }
    v
}

/// The rank-`rank` factors of one head's matrix (`k x k`, row = value
/// index) whose column space lies in the span of `basis` (vectors of `k`
/// values; `None` = every direction). Writes `rank` left vectors and `rank`
/// right vectors of `k` float32 values (zero for components beyond the
/// basis), by decreasing singular value. The general solve (any head size
/// and basis size, allocating): the full basis of a state without factors.
fn factor_head_general(
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

#[cfg(test)]
#[path = "lowrank_reference.rs"]
mod reference;

#[cfg(test)]
mod tests {
    use std::io::Read;

    use super::*;

    fn bits(values: &[f32]) -> Vec<u32> {
        values.iter().map(|value| value.to_bits()).collect()
    }

    /// One synthetic layer write on `matrix`: S' = S diag(w) + v k^T per
    /// head (w in 0.5..1 per column), with the value vector `v`. Some
    /// answers use extreme or degenerate inputs.
    fn synthetic_write(matrix: &[f32], seed: &mut u64, answer: u32) -> (Vec<f32>, Vec<f32>) {
        let decay: Vec<f32> = random(seed, HEADS * HEAD_SIZE, 0.5)
            .into_iter()
            .map(|x| 0.75 + x)
            .collect();
        let scale = match answer % 7 {
            0 => 1e-3,
            3 => 40.0,
            _ => 2.0,
        };
        let mut v = random(seed, HEADS * HEAD_SIZE, scale);
        let mut key = random(seed, HEADS * HEAD_SIZE, 2.0);
        match answer {
            40 => v.iter_mut().for_each(|x| *x *= 1e30),
            60 => v.fill(0.0),
            70 => v.iter_mut().for_each(|x| *x *= 1e-33),
            75 => key.fill(0.0),
            // The new value vector repeats the previous direction.
            90 => v[..HEAD_SIZE].copy_from_slice(&matrix[..HEAD_SIZE]),
            _ => {}
        }
        let mut next = vec![0.0_f32; matrix.len()];
        for head in 0..HEADS {
            for row in 0..HEAD_SIZE {
                for column in 0..HEAD_SIZE {
                    let index = head * HEAD_SIZE * HEAD_SIZE + row * HEAD_SIZE + column;
                    next[index] = matrix[index] * decay[head * HEAD_SIZE + column]
                        + v[head * HEAD_SIZE + row] * key[head * HEAD_SIZE + column];
                }
            }
        }
        if answer == 50 {
            next[3] = f32::NAN;
            next[5] = f32::INFINITY;
            next[HEAD_SIZE * HEAD_SIZE + 1] = f32::NEG_INFINITY;
        }
        if answer == 80 {
            next.fill(0.0);
        }
        (next, v)
    }

    /// The previous block of a write (`unreadable`: a block whose header
    /// does not parse, written on the full basis).
    fn choose(block: &[u8], unreadable: bool, factors: Factors) -> Previous<'_> {
        if unreadable {
            Previous::Block(&[1], factors)
        } else if block.is_empty() {
            Previous::Fresh
        } else {
            Previous::Block(block, factors)
        }
    }

    fn choose_frozen(block: &[u8], unreadable: bool) -> reference::Previous<'_> {
        if unreadable {
            reference::Previous::Block(&[1])
        } else if block.is_empty() {
            reference::Previous::Fresh
        } else {
            reference::Previous::Block(block)
        }
    }

    /// The allocation-free codec stores the same bytes and the same matrix,
    /// bit for bit, as the codec merged into the release candidate (the
    /// frozen copy in `lowrank_reference.rs`): long chains for every
    /// schedule shape, from a fresh state, from a dense matrix without
    /// factors and from an unreadable previous block, with extreme, zero,
    /// tiny, non-finite and repeated inputs, past the 255-answer saturation.
    #[test]
    fn rwkv_state_lowrank_codec_matches_the_frozen_codec_bit_for_bit() {
        let size = HEADS * HEAD_SIZE * HEAD_SIZE;
        let mut schedules = vec![
            RankSchedule::DEFAULT,
            RankSchedule::parse("1.3.8").unwrap(),
            RankSchedule::parse("8.20.2").unwrap(),
        ];
        schedules.extend((1..=MAX_RANK.min(HEAD_SIZE) as u8).map(RankSchedule::uniform));
        let mut writes = 0;
        for (index, &schedule) in schedules.iter().enumerate() {
            for start in 0..3 {
                let mut seed = 1_000 + 10 * index as u64 + start;
                let mut matrix = vec![0.0_f32; size];
                let (mut new_block, mut old_block) = (Vec::new(), Vec::new());
                if start == 1 {
                    // A dense matrix without factors: the full basis.
                    let mut new_matrix = random(&mut seed, size, 3.0);
                    let mut old_matrix = new_matrix.clone();
                    encode_existing(&mut new_matrix, schedule, Factors::Int8, &mut new_block);
                    reference::encode_existing(&mut old_matrix, schedule, &mut old_block);
                    assert_eq!(new_block, old_block);
                    assert_eq!(bits(&new_matrix), bits(&old_matrix));
                    matrix = new_matrix;
                }
                for answer in 1..=300_u32 {
                    let (next, v) = synthetic_write(&matrix, &mut seed, answer);
                    let unreadable = start == 2 && answer == 120;
                    let (mut new_matrix, mut old_matrix) = (next.clone(), next);
                    let (mut new_out, mut old_out) = (Vec::new(), Vec::new());
                    encode_layer(
                        &mut new_matrix,
                        choose(&new_block, unreadable, Factors::Int8),
                        &v,
                        schedule,
                        Factors::Int8,
                        &mut new_out,
                    );
                    reference::encode_layer(
                        &mut old_matrix,
                        choose_frozen(&old_block, unreadable),
                        &v,
                        schedule,
                        &mut old_out,
                    );
                    assert_eq!(
                        new_out, old_out,
                        "{schedule:?} start {start} answer {answer}"
                    );
                    assert_eq!(bits(&new_matrix), bits(&old_matrix));
                    let mut new_decoded = vec![1.0_f32; size];
                    let mut old_decoded = vec![2.0_f32; size];
                    decode_block(&new_out, Factors::Int8, &mut new_decoded).unwrap();
                    reference::decode_block(&old_out, &mut old_decoded).unwrap();
                    assert_eq!(bits(&new_decoded), bits(&old_decoded));
                    assert_eq!(bits(&new_decoded), bits(&new_matrix));
                    new_block = new_out;
                    old_block = old_out;
                    matrix = new_matrix;
                    writes += 1;
                }
            }
        }
        println!("{writes} layer writes identical (head size {HEAD_SIZE})");
    }

    fn compare_layer<const K: usize>(layer: &[(Vec<f32>, Vec<f32>)], rank: usize) {
        let m0 = layer[0].1.len() / K;
        let mut basis = vec![[[0.0_f32; K]; MAX_BASIS]; layer.len()];
        let mut matrix = Vec::new();
        for (vectors, (head_matrix, head_basis)) in basis.iter_mut().zip(layer) {
            for (vector, values) in vectors.iter_mut().zip(head_basis.chunks_exact(K)) {
                vector.copy_from_slice(values);
            }
            matrix.extend_from_slice(head_matrix);
        }
        let mut left = vec![[[7.0_f32; K]; MAX_RANK]; layer.len()];
        let mut right = vec![[[7.0_f32; K]; MAX_RANK]; layer.len()];
        let matrices: Vec<&[f32]> = matrix.chunks_exact(K * K).collect();
        let (m0s, ranks) = (vec![m0; layer.len()], vec![rank; layer.len()]);
        factor_heads::<K, 16>(&matrices, &basis, &m0s, &ranks, &mut left, &mut right);
        for (head, (head_matrix, head_basis)) in layer.iter().enumerate() {
            let (mut l, mut r) = (vec![9.0; rank * K], vec![9.0; rank * K]);
            reference::factor_head(head_matrix, K, Some(head_basis), rank, &mut l, &mut r);
            assert_eq!(bits(left[head][..rank].as_flattened()), bits(&l));
            assert_eq!(bits(right[head][..rank].as_flattened()), bits(&r));
        }
    }

    /// The fixed-size solve gives the frozen general solve's factors bit for
    /// bit at both head sizes: every write of the scorer's vectors (from the
    /// scorer's previous factors) and random bases of every size (1 to
    /// `MAX_BASIS` vectors, every rank) with zero, repeated and nearly
    /// dependent vectors, alone and as the heads of one layer.
    #[test]
    fn rwkv_state_lowrank_solve_matches_the_frozen_solve_bit_for_bit() {
        let compare = |matrix: &[f32], k: usize, basis: &[f32], rank: usize| {
            let (mut l1, mut r1) = (vec![7.0; rank * k], vec![7.0; rank * k]);
            let (mut l2, mut r2) = (vec![9.0; rank * k], vec![9.0; rank * k]);
            factor_head(matrix, k, Some(basis), rank, &mut l1, &mut r1);
            reference::factor_head(matrix, k, Some(basis), rank, &mut l2, &mut r2);
            assert_eq!(bits(&l1), bits(&l2), "k={k} rank={rank}");
            assert_eq!(bits(&r1), bits(&r2), "k={k} rank={rank}");
        };
        let mut solves = 0;
        for case in cases() {
            let (k, heads) = (case.k, case.heads);
            let mut last: Vec<Option<&Entry>> = vec![None; case.entities];
            for step in &case.steps {
                for entry in step {
                    for head in 0..heads {
                        let (state, mut basis) = match last[entry.entity] {
                            Some(previous) => {
                                let span = previous.rank * k;
                                let left = &previous.left[head * span..(head + 1) * span];
                                let right = &previous.right[head * span..(head + 1) * span];
                                (outer(left, right, k, previous.rank), left.to_vec())
                            }
                            None => (vec![0.0; k * k], Vec::new()),
                        };
                        let inputs = head_inputs(entry, heads, k, head);
                        basis.extend_from_slice(&inputs[4 * k..5 * k]);
                        compare(&update(&state, &inputs, k), k, &basis, entry.rank);
                        solves += 1;
                    }
                    last[entry.entity] = Some(entry);
                }
            }
        }
        let mut seed = 5;
        for k in [16, 32] {
            for m0 in 1..=MAX_BASIS {
                for rank in 1..=MAX_RANK {
                    let mut layer = Vec::new();
                    for variant in 0..HEADS {
                        let mut basis = random(&mut seed, m0 * k, 2.0);
                        match variant {
                            1 if m0 > 1 => {
                                let (first, rest) = basis.split_at_mut(k);
                                rest[(m0 - 2) * k..].copy_from_slice(first);
                            }
                            2 if m0 > 1 => {
                                let noise = random(&mut seed, k, 1e-4);
                                let (first, rest) = basis.split_at_mut(k);
                                for ((x, &y), &e) in
                                    rest[(m0 - 2) * k..].iter_mut().zip(&*first).zip(&noise)
                                {
                                    *x = y + e;
                                }
                            }
                            3 => basis[..k].fill(0.0),
                            _ => {}
                        }
                        let right = random(&mut seed, m0 * k, 2.0);
                        let mut matrix = outer(&basis, &right, k, m0);
                        let decay = random(&mut seed, k, 0.5);
                        for row in matrix.chunks_exact_mut(k) {
                            for (x, &w) in row.iter_mut().zip(&decay) {
                                *x *= 0.75 + w;
                            }
                        }
                        compare(&matrix, k, &basis, rank);
                        solves += 1;
                        layer.push((matrix, basis));
                    }
                    // The heads of one layer solved together (lanes that
                    // keep different numbers of directions and stop after
                    // different sweeps).
                    match k {
                        16 => compare_layer::<16>(&layer, rank),
                        _ => compare_layer::<32>(&layer, rank),
                    }
                }
            }
        }
        println!("{solves} head solves identical");
    }

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
        /// None: float32 factors (`f`).
        factors: Option<Factors>,
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

    /// The scorer's codec vectors: int8 and float32 factors
    /// (`lowrank-state-codec.json.gz`) and int6 factors
    /// (`lowrank-state-codec-int6.json.gz`).
    fn cases() -> Vec<Case> {
        let mut cases = read_cases(include_bytes!("testdata/lowrank-state-codec.json.gz"));
        cases.extend(read_cases(include_bytes!(
            "testdata/lowrank-state-codec-int6.json.gz"
        )));
        cases
    }

    fn read_cases(gzipped: &[u8]) -> Vec<Case> {
        let mut data = Vec::new();
        flate2::read::GzDecoder::new(gzipped)
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
                factors: match case["quant"].as_str().unwrap() {
                    "q8" => Some(Factors::Int8),
                    "q6" => Some(Factors::Int6),
                    "f" => None,
                    other => panic!("factor format {other}"),
                },
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

    /// One write of one head: the Rust factors (rounded to `factors` codes
    /// unless None).
    fn write(
        state: &[f32],
        previous_left: &[f32],
        inputs: &[f32],
        k: usize,
        rank: usize,
        factors: Option<Factors>,
    ) -> (Vec<f32>, Vec<f32>) {
        let next = update(state, inputs, k);
        let mut basis = previous_left.to_vec();
        basis.extend_from_slice(&inputs[4 * k..5 * k]);
        let mut left = vec![0.0; rank * k];
        let mut right = vec![0.0; rank * k];
        factor_head(&next, k, Some(&basis), rank, &mut left, &mut right);
        if let Some(factors) = factors {
            quantize_head_size(&mut left, k, factors);
            quantize_head_size(&mut right, k, factors);
        }
        let stored = outer(&left, &right, k, rank);
        (stored, left)
    }

    /// `quantize_vector` on vectors of `k` values (the tests' head size,
    /// which may differ from this engine's).
    fn quantize_head_size(vectors: &mut [f32], k: usize, factors: Factors) {
        let limit = factors.limit();
        for vector in vectors.chunks_exact_mut(k) {
            let maximum = vector.iter().map(|v| v.abs()).fold(0.0_f32, f32::max);
            let scale = f16_scale(maximum, limit).to_f32();
            for value in vector.iter_mut() {
                *value = quantize(*value, scale, limit) as f32 * scale;
            }
        }
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
                        let (stored, _) =
                            write(&state, &previous_left, &inputs, k, entry.rank, case.factors);
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
                label(case.factors)
            );
            match case.factors {
                Some(factors) => {
                    // Int6 codes are four times coarser: the same noise
                    // flips a code as often, each flip moves 4x more.
                    let steps = if factors == Factors::Int6 { 4.0 } else { 1.0 };
                    assert!(
                        mean <= 2e-4 * steps && worst <= 1e-2 * steps,
                        "{mean} {worst}"
                    );
                    assert!(exact * 20 >= count * 19, "{exact} of {count} identical");
                }
                None => assert!(worst <= 1e-5, "{worst}"),
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
                            case.factors,
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
                label(case.factors)
            );
            match case.factors {
                Some(Factors::Int8) => assert!(mean <= 3e-3 && worst <= 3e-2, "{mean} {worst}"),
                Some(Factors::Int6) => assert!(mean <= 1.2e-2 && worst <= 1.2e-1, "{mean} {worst}"),
                None => assert!(worst <= 1e-4, "{worst}"),
            }
        }
    }

    fn label(factors: Option<Factors>) -> &'static str {
        match factors {
            Some(Factors::Int8) => "q8",
            Some(Factors::Int6) => "q6",
            None => "f",
        }
    }

    /// Batched writes (`encode_layers`, several cards per solve) store the
    /// same bytes and the same matrix, bit for bit, as one write at a time
    /// (`encode_layer`), at both factor widths and for 1, 2 and 4 cards per
    /// solve: cards that start fresh, from a dense matrix without factors
    /// and from an unreadable block, joining and leaving the batch at
    /// different answer counts (so one batch mixes ranks and basis sizes,
    /// and batches are partial), with extreme, zero, non-finite and repeated
    /// inputs, past the 255-answer saturation.
    #[test]
    fn rwkv_state_lowrank_batched_codec_matches_one_write_at_a_time() {
        type Write = (Vec<u8>, bool, Vec<f32>, Vec<f32>, Vec<u8>);
        fn previous(block: &[u8], unreadable: bool, factors: Factors) -> Previous<'_> {
            if unreadable && block.is_empty() {
                Previous::Matrix
            } else {
                choose(block, unreadable, factors)
            }
        }
        fn batched<const L: usize>(writes: &mut [Write], schedule: RankSchedule, factors: Factors) {
            let mut layer_writes: Vec<LayerWrite<'_>> = writes
                .iter_mut()
                .map(|(block, unreadable, matrix, v, out)| LayerWrite {
                    previous: previous(block, *unreadable, factors),
                    matrix,
                    v,
                    out,
                })
                .collect();
            encode_layers::<L>(&mut layer_writes, schedule, factors);
        }
        let size = HEADS * HEAD_SIZE * HEAD_SIZE;
        let schedules = [
            RankSchedule::DEFAULT,
            RankSchedule::parse("1.3.8").unwrap(),
            RankSchedule::uniform(3),
        ];
        let cards = 9_usize;
        let answers = 270_u32;
        let mut writes = 0;
        for factors in [Factors::Int8, Factors::Int6] {
            for (index, &schedule) in schedules.iter().enumerate() {
                for lanes in [HEADS, 2 * HEADS, BATCH_LANES] {
                    let mut seeds: Vec<u64> = (0..cards as u64)
                        .map(|card| 7_000 + 97 * index as u64 + card)
                        .collect();
                    // Card 1 starts from a dense matrix without factors.
                    let mut matrices: Vec<Vec<f32>> = (0..cards)
                        .map(|card| {
                            if card == 1 {
                                random(&mut seeds[1], size, 3.0)
                            } else {
                                vec![0.0; size]
                            }
                        })
                        .collect();
                    let mut blocks: Vec<Vec<u8>> = vec![Vec::new(); cards];
                    // Card c joins at step 3 c and leaves 2 c answers early.
                    for step in 0..answers + 3 * cards as u32 {
                        let mut batch: Vec<Write> = Vec::new();
                        let mut members = Vec::new();
                        for card in 0..cards {
                            let first = 3 * card as u32;
                            if step < first || step >= first + answers - 2 * card as u32 {
                                continue;
                            }
                            let answer = step - first + 1;
                            let (mut next, v) =
                                synthetic_write(&matrices[card], &mut seeds[card], answer);
                            // Card 1's first write: the dense matrix itself.
                            let dense = card == 1 && answer == 1;
                            if dense {
                                next.copy_from_slice(&matrices[card]);
                            }
                            let unreadable = dense || (card == 2 && answer == 120);
                            batch.push((blocks[card].clone(), unreadable, next, v, Vec::new()));
                            members.push(card);
                        }
                        let mut single = batch.clone();
                        for (block, unreadable, matrix, v, out) in &mut single {
                            let previous = previous(block, *unreadable, factors);
                            encode_layer(matrix, previous, v, schedule, factors, out);
                        }
                        match lanes {
                            HEADS => batched::<HEADS>(&mut batch, schedule, factors),
                            BATCH_LANES => batched::<BATCH_LANES>(&mut batch, schedule, factors),
                            _ => batched::<{ 2 * HEADS }>(&mut batch, schedule, factors),
                        }
                        for ((card, one), many) in members.into_iter().zip(single).zip(batch) {
                            assert_eq!(
                                one.4, many.4,
                                "{factors:?} {schedule:?} lanes {lanes} card {card} step {step}"
                            );
                            assert_eq!(bits(&one.2), bits(&many.2));
                            let mut decoded = vec![1.0_f32; size];
                            decode_block(&many.4, factors, &mut decoded).unwrap();
                            assert_eq!(bits(&decoded), bits(&many.2));
                            blocks[card] = many.4;
                            matrices[card] = many.2;
                            writes += 1;
                        }
                    }
                }
            }
        }
        println!("{writes} batched layer writes identical (head size {HEAD_SIZE})");
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
        for factors in [Factors::Int8, Factors::Int6] {
            block_round_trip_and_growth(factors);
        }
        assert_eq!(block_len(2, Factors::Int8), 2 + HEADS * 4 * (2 + HEAD_SIZE));
        assert_eq!(
            block_len(2, Factors::Int6),
            2 + HEADS * 4 * (2 + HEAD_SIZE * 6 / 8)
        );
    }

    fn block_round_trip_and_growth(factors: Factors) {
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
                Previous::Block(&block, factors)
            };
            let mut out = Vec::new();
            encode_layer(
                &mut next,
                previous,
                &v,
                RankSchedule::DEFAULT,
                factors,
                &mut out,
            );
            let (answers, rank) = header(&out, factors).unwrap();
            assert_eq!(answers as u32, answer.min(255));
            assert_eq!(rank, RankSchedule::DEFAULT.rank(answers));
            assert_eq!(out.len(), block_len(rank, factors));
            let mut decoded = vec![1.0_f32; size];
            decode_block(&out, factors, &mut decoded).unwrap();
            assert_eq!(decoded, next, "{factors:?} answer {answer}");
            assert!(next.iter().all(|x| x.is_finite()));
            block = out;
            matrix = next;
        }
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
        for factors in [Factors::Int8, Factors::Int6] {
            assert!(decode_block(&[1], factors, &mut matrix).is_err());
            assert!(decode_block(&[1, 9], factors, &mut matrix).is_err());
            let mut block = vec![0_u8; block_len(1, factors)];
            block[1] = 1;
            assert!(
                decode_block(&block, factors, &mut matrix).is_err(),
                "zero scale"
            );
            for chunk in block[2..].chunks_exact_mut(factors.vector_bytes()) {
                chunk[..2].copy_from_slice(&f16::ONE.to_bits().to_le_bytes());
            }
            decode_block(&block, factors, &mut matrix).unwrap();
            let good = block.clone();
            match factors {
                Factors::Int8 => block[4] = 128,
                // -32 (0b100000) as the first code.
                Factors::Int6 => block[4] = 0b0010_0000,
            }
            assert!(
                decode_block(&block, factors, &mut matrix).is_err(),
                "{factors:?} out-of-range code"
            );
            assert!(decode_block(&good[..good.len() - 1], factors, &mut matrix).is_err());
        }
        // The other width's block has the wrong length.
        let int8 = vec![0_u8; block_len(1, Factors::Int8)];
        let mut block = int8.clone();
        block[1] = 1;
        assert!(decode_block(&block, Factors::Int6, &mut matrix).is_err());
    }

    /// Int6 packing: four codes in three bytes, little-endian two's
    /// complement (the training repository's `pack`), every code of
    /// [-31, 31] in every position.
    #[test]
    fn rwkv_state_lowrank_int6_packing() {
        assert_eq!(pack6([1, 0, 0, 0]), [1, 0, 0]);
        assert_eq!(pack6([0, 1, 0, 0]), [64, 0, 0]);
        assert_eq!(pack6([-1, 0, 0, 0]), [63, 0, 0]);
        assert_eq!(pack6([0, 0, 0, -1]), [0, 0, 0b1111_1100]);
        assert_eq!(pack6([-31, 31, -2, 5]), [0xE1, 0xE7, 0x17]);
        for code in -31_i8..=31 {
            for position in 0..4 {
                let mut codes = [3_i8, -7, 0, 30];
                codes[position] = code;
                assert_eq!(unpack6(&pack6(codes)), codes);
            }
        }
    }

    /// The int6 rounding of a factor vector equals the scorer's
    /// (`quant_vectors(x, -1, 31)`) bit for bit on synthetic vectors
    /// (random, exact rounding ties, zero, float16 underflow and overflow):
    /// the same float16 scale, the same codes, the same decoded values, and
    /// the stored bytes are the training repository's bit packing of them.
    #[test]
    fn rwkv_state_lowrank_int6_rounding_matches_the_scorer() {
        let mut data = Vec::new();
        flate2::read::GzDecoder::new(
            &include_bytes!("testdata/lowrank-state-codec-int6.json.gz")[..],
        )
        .read_to_end(&mut data)
        .unwrap();
        let value: serde_json::Value = serde_json::from_slice(&data).unwrap();
        let vectors = value["quant_vectors"].as_array().unwrap();
        let mut checked = [0_usize; 2];
        for vector in vectors {
            let k = vector["head_size"].as_u64().unwrap() as usize;
            let mut values = floats(&vector["values"]);
            let rounded = floats(&vector["rounded"]);
            let packed = data_encoding::BASE64
                .decode(vector["packed"].as_str().unwrap().as_bytes())
                .unwrap();
            let codes: Vec<i8> = vector["codes"]
                .as_array()
                .unwrap()
                .iter()
                .map(|code| code.as_i64().unwrap() as i8)
                .collect();
            let scale_bits = vector["scale_bits"].as_u64().unwrap() as u16;
            assert_eq!(values.len(), k);
            let mut out = vec![0_u8; 2 + k / 4 * 3];
            quantize_vector(&mut values, Factors::Int6, &mut out);
            // Bit for bit except the sign of zero: the scorer's value of a
            // code 0 keeps the input's sign (-0.0), a stored code has none.
            let unsigned_zero = |values: &[f32]| -> Vec<u32> {
                values
                    .iter()
                    .map(|&value| if value == 0.0 { 0 } else { value.to_bits() })
                    .collect()
            };
            assert_eq!(unsigned_zero(&values), unsigned_zero(&rounded), "k={k}");
            assert_eq!(out[..2], scale_bits.to_le_bytes(), "k={k}");
            assert_eq!(out[2..], packed[..], "k={k}");
            let decoded: Vec<i8> = out[2..].chunks_exact(3).flat_map(unpack6).collect();
            assert_eq!(decoded, codes);
            checked[usize::from(k == 32)] += 1;
        }
        assert!(checked.iter().all(|&count| count >= 25), "{checked:?}");
    }

    /// Manual micro-benchmark of the codec alone (release build, one
    /// thread): one layer's write (`encode_layer` on a previous block, the
    /// factored solve, the rounding and the reconstruction) and one layer's
    /// read (`decode_block`), at rank 2 (a card below 16 answers) and rank
    /// 4, for this engine's head layout; the frozen int8 codec of the
    /// release candidate (`reference`), this one with int8 factors and this
    /// one with int6 factors back to back.
    ///
    /// ```sh
    /// cargo test --release -p anki --lib rwkv_state_lowrank_codec_micro_benchmark -- --ignored --nocapture
    /// ```
    #[test]
    #[ignore]
    fn rwkv_state_lowrank_codec_micro_benchmark() {
        // (previous block or empty for a fresh state, matrix, v, out)
        type Encode = fn(&[u8], &mut [f32], &[f32], &mut Vec<u8>);
        type Decode = fn(&[u8], &mut [f32]) -> bool;
        fn current(factors: Factors) -> impl Fn(&[u8], &mut [f32], &[f32], &mut Vec<u8>) {
            move |previous, matrix, v, out| {
                let previous = if previous.is_empty() {
                    Previous::Fresh
                } else {
                    Previous::Block(previous, factors)
                };
                encode_layer(matrix, previous, v, RankSchedule::DEFAULT, factors, out)
            }
        }
        let codecs: [(&str, Factors, Encode, Decode); 3] = [
            (
                "frozen int8",
                Factors::Int8,
                |previous, matrix, v, out| {
                    let previous = if previous.is_empty() {
                        reference::Previous::Fresh
                    } else {
                        reference::Previous::Block(previous)
                    };
                    reference::encode_layer(matrix, previous, v, RankSchedule::DEFAULT, out)
                },
                |block, matrix| reference::decode_block(block, matrix).is_ok(),
            ),
            (
                "current int8",
                Factors::Int8,
                |previous, matrix, v, out| current(Factors::Int8)(previous, matrix, v, out),
                |block, matrix| decode_block(block, Factors::Int8, matrix).is_ok(),
            ),
            (
                "current int6",
                Factors::Int6,
                |previous, matrix, v, out| current(Factors::Int6)(previous, matrix, v, out),
                |block, matrix| decode_block(block, Factors::Int6, matrix).is_ok(),
            ),
        ];
        let size = HEADS * HEAD_SIZE * HEAD_SIZE;
        // (answers before the timed writes, label): 64 timed writes each,
        // all below 16 answers for rank 2 (a fresh chain per write) and
        // all at rank 4 from answer 41.
        for (answers, label) in [(1_u32, "rank 2"), (40, "rank 4")] {
            for (name, factors, encode, decode) in codecs {
                let mut seed = 11;
                let mut writes = Vec::new();
                for _chain in 0..if answers == 1 { 8 } else { 1 } {
                    let mut block: Vec<u8> = Vec::new();
                    let mut matrix = vec![0.0_f32; size];
                    let timed = if answers == 1 { 8 } else { 64 };
                    for answer in 1..=answers + timed {
                        let mut next: Vec<f32> = matrix.iter().map(|x| x * 0.9).collect();
                        let v = random(&mut seed, HEADS * HEAD_SIZE, 2.0);
                        let key = random(&mut seed, HEADS * HEAD_SIZE, 2.0);
                        for head in 0..HEADS {
                            for row in 0..HEAD_SIZE {
                                for column in 0..HEAD_SIZE {
                                    next[head * HEAD_SIZE * HEAD_SIZE
                                        + row * HEAD_SIZE
                                        + column] +=
                                        v[head * HEAD_SIZE + row] * key[head * HEAD_SIZE + column];
                                }
                            }
                        }
                        if answer > answers {
                            writes.push((block.clone(), next.clone(), v.clone()));
                        }
                        let mut out = Vec::new();
                        current(factors)(&block, &mut next, &v, &mut out);
                        block = out;
                        matrix = next;
                    }
                }
                let rounds = 2_000;
                {
                    let mut out = Vec::with_capacity(block_len(MAX_RANK, factors));
                    let mut scratch = vec![0.0_f32; size];
                    let started = std::time::Instant::now();
                    let mut checksum = 0.0_f32;
                    for _ in 0..rounds {
                        for (previous, next, v) in &writes {
                            scratch.copy_from_slice(next);
                            out.clear();
                            encode(previous, &mut scratch, v, &mut out);
                            checksum += scratch[0];
                        }
                    }
                    let encode_ns =
                        started.elapsed().as_secs_f64() * 1e9 / (rounds * writes.len()) as f64;
                    let stored: Vec<&Vec<u8>> = writes
                        .iter()
                        .map(|(previous, _, _)| previous)
                        .filter(|previous| !previous.is_empty())
                        .collect();
                    let started = std::time::Instant::now();
                    for _ in 0..rounds {
                        for previous in &stored {
                            assert!(decode(previous, &mut scratch));
                            checksum += scratch[0];
                        }
                    }
                    let decode_ns =
                        started.elapsed().as_secs_f64() * 1e9 / (rounds * stored.len()) as f64;
                    println!(
                    "heads={HEADS} head_size={HEAD_SIZE} {label} {name}: encode_layer {encode_ns:.0} ns, decode_block {decode_ns:.0} ns per layer (checksum {checksum:.3})"
                );
                }
            }
        }
    }

    /// Manual micro-benchmark of the batched codec (release build, one
    /// thread): one layer's write per card, 1, 2 or 4 cards per solve
    /// (`encode_layers::<4>`, `<8>`, `<16>`), at rank 2 and rank 4, both
    /// factor widths; the time is per layer write.
    ///
    /// ```sh
    /// cargo test --release -p anki --lib rwkv_state_lowrank_batched_codec_micro_benchmark -- --ignored --nocapture
    /// ```
    #[test]
    #[ignore]
    fn rwkv_state_lowrank_batched_codec_micro_benchmark() {
        fn run<const L: usize>(
            writes: &[(Vec<u8>, Vec<f32>, Vec<f32>)],
            factors: Factors,
            rounds: usize,
        ) -> f64 {
            let size = HEADS * HEAD_SIZE * HEAD_SIZE;
            let per = L / HEADS;
            let mut scratch = vec![vec![0.0_f32; size]; per];
            let mut outs = vec![Vec::with_capacity(block_len(MAX_RANK, factors)); per];
            let mut checksum = 0.0_f32;
            let started = std::time::Instant::now();
            for _ in 0..rounds {
                for batch in writes.chunks(per) {
                    let mut layer_writes: Vec<LayerWrite<'_>> = batch
                        .iter()
                        .zip(scratch.iter_mut().zip(outs.iter_mut()))
                        .map(|((previous, next, v), (matrix, out))| {
                            matrix.copy_from_slice(next);
                            out.clear();
                            LayerWrite {
                                matrix,
                                previous: if previous.is_empty() {
                                    Previous::Fresh
                                } else {
                                    Previous::Block(previous, factors)
                                },
                                v,
                                out,
                            }
                        })
                        .collect();
                    encode_layers::<L>(&mut layer_writes, RankSchedule::DEFAULT, factors);
                    checksum += scratch[0][0];
                }
            }
            let ns = started.elapsed().as_secs_f64() * 1e9 / (rounds * writes.len()) as f64;
            std::hint::black_box(checksum);
            ns
        }
        let size = HEADS * HEAD_SIZE * HEAD_SIZE;
        for (answers, label) in [(1_u32, "rank 2"), (40, "rank 4")] {
            for factors in [Factors::Int8, Factors::Int6] {
                let mut seed = 11;
                let mut writes = Vec::new();
                for _chain in 0..if answers == 1 { 8 } else { 1 } {
                    let mut block: Vec<u8> = Vec::new();
                    let mut matrix = vec![0.0_f32; size];
                    let timed = if answers == 1 { 8 } else { 64 };
                    for answer in 1..=answers + timed {
                        let mut next: Vec<f32> = matrix.iter().map(|x| x * 0.9).collect();
                        let v = random(&mut seed, HEADS * HEAD_SIZE, 2.0);
                        let key = random(&mut seed, HEADS * HEAD_SIZE, 2.0);
                        for head in 0..HEADS {
                            for row in 0..HEAD_SIZE {
                                for column in 0..HEAD_SIZE {
                                    next[head * HEAD_SIZE * HEAD_SIZE
                                        + row * HEAD_SIZE
                                        + column] +=
                                        v[head * HEAD_SIZE + row] * key[head * HEAD_SIZE + column];
                                }
                            }
                        }
                        if answer > answers {
                            writes.push((block.clone(), next.clone(), v.clone()));
                        }
                        let mut out = Vec::new();
                        let previous = if block.is_empty() {
                            Previous::Fresh
                        } else {
                            Previous::Block(&block, factors)
                        };
                        encode_layer(
                            &mut next,
                            previous,
                            &v,
                            RankSchedule::DEFAULT,
                            factors,
                            &mut out,
                        );
                        block = out;
                        matrix = next;
                    }
                }
                let rounds = 2_000;
                let one = run::<HEADS>(&writes, factors, rounds);
                let two = run::<{ 2 * HEADS }>(&writes, factors, rounds);
                let four = run::<BATCH_LANES>(&writes, factors, rounds);
                println!(
                    "heads={HEADS} head_size={HEAD_SIZE} {label} {factors:?}: encode per layer write: 1 card {one:.0} ns, 2 cards {two:.0} ns, 4 cards {four:.0} ns"
                );
            }
        }
    }

    /// Manual micro-benchmark of the batched Jacobi solve alone: 64 random
    /// problems of size 3 and 5, in blocks of 1, 4, 8 or 16 lanes, and the
    /// general `eigh` (the time is per problem; `ANKI_RWKV_LOWRANK_SIMD` =
    /// `baseline` or `avx2` caps the vector width).
    ///
    /// ```sh
    /// cargo test --release -p anki --lib rwkv_state_lowrank_jacobi_micro_benchmark -- --ignored --nocapture --test-threads 1
    /// ```
    #[test]
    #[ignore]
    fn rwkv_state_lowrank_jacobi_micro_benchmark() {
        let mut seed = 5;
        for n in [3_usize, 5] {
            let problems: Vec<Small> = (0..64)
                .map(|_| {
                    let x = random(&mut seed, n * 16, 2.0);
                    let mut small = [[0.0_f64; MAX_BASIS]; MAX_BASIS];
                    for i in 0..n {
                        for j in 0..n {
                            small[i][j] = (0..16)
                                .map(|c| x[i * 16 + c] as f64 * x[j * 16 + c] as f64)
                                .sum();
                        }
                    }
                    small
                })
                .collect();
            for lanes in [1_usize, 4, 8, 16] {
                let mut values = vec![[0.0_f64; MAX_BASIS]; 64];
                let mut vectors = vec![[[0.0_f64; MAX_BASIS]; MAX_BASIS]; 64];
                let members: Vec<usize> = (0..64).collect();
                let rounds = 2_000;
                let started = std::time::Instant::now();
                for _ in 0..rounds {
                    for block in members.chunks(lanes) {
                        eigh_block(&problems, n, block, &mut values, &mut vectors);
                    }
                }
                let ns = started.elapsed().as_secs_f64() * 1e9 / (rounds * 64) as f64;
                std::hint::black_box(&values);
                println!("jacobi n={n} lanes={lanes}: {ns:.0} ns per problem");
            }
            let rounds = 2_000;
            let started = std::time::Instant::now();
            for _ in 0..rounds {
                for problem in &problems {
                    let flat: Vec<f64> = (0..n).flat_map(|i| problem[i][..n].to_vec()).collect();
                    std::hint::black_box(eigh(&flat, n));
                }
            }
            let ns = started.elapsed().as_secs_f64() * 1e9 / (rounds * 64) as f64;
            println!("jacobi n={n} scalar eigh: {ns:.0} ns per problem");
        }
    }
}
