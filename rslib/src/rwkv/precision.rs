// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Compact card/note state. Computation and the shared scopes remain f32.
//!
//! Two stored layouts share one magic:
//!
//! - Plain (`Int8`, `Int4`, precision bytes 8 / 4): one f32 scale per matrix
//!   row and float16 shift vectors.
//! - Compact (`Int8Compact`, `Int4Compact`, precision bytes 0x18 / 0x14): one
//!   float16 scale per matrix row (abs-max / limit, clamped to 65,504, 1.0
//!   when it rounds to zero) and int8 shift codes with one float16 scale per
//!   vector. This is the training repository's `hx8` state rounding
//!   (`int_row_quant(bits | F16_SCALE)`). The float16 clamp keeps every
//!   stored value finite, so a long, drifting card saturates instead of
//!   overflowing.
//!
//! A third layout (`LowRankInt8`, precision byte 0x28, and `LowRankInt6`,
//! 0x26) stores every card / note head as a rank-c product of int8 or int6
//! factors with the compact int8 shifts (`lowrank.rs`).

use std::borrow::Cow;
use std::io;

use half::f16;

use super::lowrank;
use super::lowrank::Factors;
use super::lowrank::RankSchedule;
use super::Cursor;
use super::LayerState;
use super::ModuleState;
use super::TimeState;
use super::D_MODEL;
use super::HEADS;
use super::HEAD_SIZE;

pub(super) const MAGIC: &[u8] = b"ARWKVMODSTATE2";

/// Compact shift vectors use the int8 rule of the matrix rows.
const SHIFT_LIMIT: f32 = 127.0;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum StatePrecision {
    #[default]
    F32,
    Int8,
    Int4,
    /// Runtime policy; modules resolve this to Int4 cards and Int8 notes.
    /// Kept for comparisons only: its f32 row scales can overflow on very
    /// long cards.
    Int4CardsInt8Notes,
    /// Module precision: int8 rows with float16 scales, int8 shifts.
    Int8Compact,
    /// Module precision: int4 rows with float16 scales, int8 shifts.
    Int4Compact,
    /// Runtime policy `compact_cards_int8`: Int8Compact cards and notes
    /// (the training repository's `w8+s8hx8`).
    CompactCardsInt8,
    /// Runtime policy `compact_cards_int4`: Int4Compact cards and
    /// Int8Compact notes (`w8+s4c8nhx8`).
    CompactCardsInt4,
    /// Module precision: rank-c int8 factors per head (rank by the
    /// schedule), int8 shifts with float16 scales (`lowrank.rs`). The stored
    /// bytes do not depend on the schedule; it only picks the next write's
    /// rank.
    LowRankInt8(RankSchedule),
    /// Module precision: LowRankInt8 with int6 factor codes (bit-packed,
    /// precision byte 0x26).
    LowRankInt6(RankSchedule),
    /// Runtime policy `compact_lowrank_int8[:spec]`: LowRankInt8 cards and
    /// notes (the training repository's `w8+lr<spec>q8x8`; default spec
    /// `2.16.4`, rank 2 below 16 answers and rank 4 from 16).
    CompactLowRank(RankSchedule),
    /// Runtime policy `compact_lowrank_int6[:spec]`: LowRankInt6 cards and
    /// notes (`w8+lr<spec>q6x8`).
    CompactLowRankInt6(RankSchedule),
    /// Runtime policy `compact_lowrank[:spec]`, the profile default since
    /// the 6-bit factors (SM162): the low-rank state with the factor width
    /// of the model's size, chosen when a model loads (`for_card_layers`):
    /// int6 factors for Big and Small (`w8+lr<spec>q6x8`), int8 factors for
    /// Tiny (`w8+lr<spec>q8x8`; the smaller shift vectors and factors
    /// experiment (SM152) kept Tiny on int8 factors).
    CompactLowRankBySize(RankSchedule),
}

/// The runtime policy string of the int8-factor low-rank state.
pub const LOWRANK_POLICY: &str = "compact_lowrank_int8";
/// The runtime policy string of the int6-factor low-rank state.
pub const LOWRANK_INT6_POLICY: &str = "compact_lowrank_int6";
/// The runtime policy string of the default low-rank state (factor width by
/// model size).
pub const LOWRANK_BY_SIZE_POLICY: &str = "compact_lowrank";

impl StatePrecision {
    pub fn parse(value: &str) -> io::Result<Self> {
        match value {
            "f32" => Ok(Self::F32),
            "int8" => Ok(Self::Int8),
            "cards_int4_notes_int8" => Ok(Self::Int4CardsInt8Notes),
            "compact_cards_int8" => Ok(Self::CompactCardsInt8),
            "compact_cards_int4" => Ok(Self::CompactCardsInt4),
            LOWRANK_POLICY => Ok(Self::CompactLowRank(RankSchedule::DEFAULT)),
            LOWRANK_INT6_POLICY => Ok(Self::CompactLowRankInt6(RankSchedule::DEFAULT)),
            LOWRANK_BY_SIZE_POLICY => Ok(Self::CompactLowRankBySize(RankSchedule::DEFAULT)),
            _ => {
                let policies: [(&str, fn(RankSchedule) -> Self); 3] = [
                    (LOWRANK_POLICY, Self::CompactLowRank),
                    (LOWRANK_INT6_POLICY, Self::CompactLowRankInt6),
                    (LOWRANK_BY_SIZE_POLICY, Self::CompactLowRankBySize),
                ];
                policies
                    .into_iter()
                    .find_map(|(policy, make)| {
                        value
                            .strip_prefix(policy)
                            .and_then(|rest| rest.strip_prefix(':'))
                            .and_then(RankSchedule::parse)
                            .map(make)
                    })
                    .ok_or_else(invalid_state)
            }
        }
    }

    /// This policy for a model with `card_layers` card layers: the
    /// size-dependent default resolves to int8 factors for Tiny (one card
    /// layer: the Tiny two-head and Instant-Only models) and to int6
    /// factors for Small (two) and Big (three); every other value is kept.
    pub fn for_card_layers(self, card_layers: usize) -> Self {
        match self {
            Self::CompactLowRankBySize(schedule) if card_layers <= 1 => {
                Self::CompactLowRank(schedule)
            }
            Self::CompactLowRankBySize(schedule) => Self::CompactLowRankInt6(schedule),
            precision => precision,
        }
    }

    /// The precision of the card and note modules under this runtime policy.
    /// Deck, preset and global states always stay f32.
    pub fn module_precisions(self) -> (Self, Self) {
        match self {
            Self::Int4CardsInt8Notes => (Self::Int4, Self::Int8),
            Self::CompactCardsInt8 => (Self::Int8Compact, Self::Int8Compact),
            Self::CompactCardsInt4 => (Self::Int4Compact, Self::Int8Compact),
            Self::CompactLowRank(schedule) => {
                (Self::LowRankInt8(schedule), Self::LowRankInt8(schedule))
            }
            Self::CompactLowRankInt6(schedule) => {
                (Self::LowRankInt6(schedule), Self::LowRankInt6(schedule))
            }
            // Resolved per model when it loads (`for_card_layers`); a direct
            // engine load without a model size takes int6 factors.
            Self::CompactLowRankBySize(schedule) => {
                Self::CompactLowRankInt6(schedule).module_precisions()
            }
            precision => (precision, precision),
        }
    }

    /// Compact shift vectors (float16 scale + int8 codes).
    fn is_compact(self) -> bool {
        matches!(
            self,
            Self::Int8Compact | Self::Int4Compact | Self::LowRankInt8(_) | Self::LowRankInt6(_)
        )
    }

    pub(super) fn is_lowrank(self) -> bool {
        self.lowrank().is_some()
    }

    /// The schedule and factor width of a low-rank module precision.
    fn lowrank(self) -> Option<(RankSchedule, Factors)> {
        match self {
            Self::LowRankInt8(schedule) => Some((schedule, Factors::Int8)),
            Self::LowRankInt6(schedule) => Some((schedule, Factors::Int6)),
            _ => None,
        }
    }

    /// Whether stored bytes of `self` can be kept for `other` (the low-rank
    /// bytes do not depend on the schedule, only on the factor width).
    fn same_layout(self, other: Self) -> bool {
        self == other
            || matches!(
                (self.lowrank(), other.lowrank()),
                (Some((_, a)), Some((_, b))) if a == b
            )
    }

    fn int8_codes(self) -> bool {
        match self {
            Self::Int8 | Self::Int8Compact => true,
            Self::Int4 | Self::Int4Compact => false,
            _ => unreachable!("{self:?} is not a module precision"),
        }
    }

    fn limit(self) -> f32 {
        if self.int8_codes() {
            127.0
        } else {
            7.0
        }
    }

    fn scale_bytes(self) -> usize {
        if self.is_compact() {
            2
        } else {
            4
        }
    }

    fn row_bytes(self) -> usize {
        self.scale_bytes()
            + if self.int8_codes() {
                HEAD_SIZE
            } else {
                HEAD_SIZE / 2
            }
    }

    fn code(self) -> u8 {
        match self {
            Self::Int8 => 8,
            Self::Int4 => 4,
            Self::Int8Compact => 0x18,
            Self::Int4Compact => 0x14,
            Self::LowRankInt8(_) => 0x28,
            Self::LowRankInt6(_) => 0x26,
            _ => unreachable!("{self:?} is not a module precision"),
        }
    }

    fn from_code(code: u8) -> io::Result<Self> {
        match code {
            8 => Ok(Self::Int8),
            4 => Ok(Self::Int4),
            0x18 => Ok(Self::Int8Compact),
            0x14 => Ok(Self::Int4Compact),
            0x28 => Ok(Self::LowRankInt8(RankSchedule::DEFAULT)),
            0x26 => Ok(Self::LowRankInt6(RankSchedule::DEFAULT)),
            _ => Err(invalid_state()),
        }
    }

    pub(super) fn round_matrix(self, values: &mut [f32], packed: &mut Option<PackedMatrix>) {
        if self == Self::F32 {
            return;
        }
        debug_assert!(
            !self.is_lowrank(),
            "low-rank writes use round_written_matrix"
        );
        let packed = packed.get_or_insert_with(|| PackedMatrix {
            precision: self,
            bytes: Vec::new(),
        });
        packed.precision = self;
        packed.bytes.clear();
        encode_matrix(self, values, &mut packed.bytes);
        decode_matrix(self, &packed.bytes, values);
    }

    /// Rounds a layer's matrix after a write exactly as storing and reloading
    /// it would. `packed` holds the previous write's bytes on entry (low-rank
    /// only: its factors are the basis of the solve) and this write's bytes
    /// on return; `previous` is the matrix before the write and `v` the
    /// write's value vector.
    pub(super) fn round_written_matrix(
        self,
        values: &mut [f32],
        packed: &mut Option<PackedMatrix>,
        previous: &[f32],
        v: &[f32],
    ) {
        let Some((schedule, factors)) = self.lowrank() else {
            self.round_matrix(values, packed);
            return;
        };
        let mut bytes = Vec::with_capacity(lowrank::block_len(4, factors));
        lowrank::encode_layer(
            values,
            lowrank_previous(packed.as_ref(), previous),
            v,
            schedule,
            factors,
            &mut bytes,
        );
        *packed = Some(PackedMatrix {
            precision: self,
            bytes,
        });
    }

    /// Rounds a shift vector exactly as storing and reloading it would.
    pub(super) fn round_shift(self, values: &mut [f32]) {
        if self == Self::F32 {
            return;
        }
        if self.is_compact() {
            if values.is_empty() {
                return;
            }
            let scale = f16_scale(abs_max(values), SHIFT_LIMIT).to_f32();
            for value in values {
                *value = quantize(*value, scale, SHIFT_LIMIT) as f32 * scale;
            }
        } else {
            for value in values {
                *value = f16::from_f32(*value).to_f32();
            }
        }
    }
}

impl ModuleState {
    pub(super) fn try_decoded(&self) -> io::Result<Cow<'_, Self>> {
        match &self.packed {
            Some(bytes) => decode(bytes).map(Cow::Owned),
            None => Ok(Cow::Borrowed(self)),
        }
    }

    /// The decoded state for a model pass that cannot report an error (the
    /// workload simulation and forecast). Every path that loads stored bytes
    /// validates them first, and the compact encoder always writes a
    /// decodable state, so this only drops a state the plain int4 / int8
    /// encoder wrote after overflowing: the pass sees fresh state there
    /// instead of panicking.
    pub(super) fn decoded_or_fresh(&self) -> Option<Cow<'_, Self>> {
        self.try_decoded().ok()
    }

    /// The decoded state. A state that fails to decode (for example one an
    /// overflowing plain int4 card produced earlier in the same replay) is an
    /// error, never a panic, so the caller can discard and rebuild the cache.
    pub(super) fn try_into_decoded(self) -> io::Result<Self> {
        match &self.packed {
            Some(bytes) => decode(bytes),
            None => Ok(self),
        }
    }

    pub(super) fn compact(self, precision: StatePrecision) -> Self {
        if precision == StatePrecision::F32 {
            return self;
        }
        let mut bytes = Vec::new();
        bytes.extend_from_slice(MAGIC);
        bytes.push(precision.code());
        super::write_u32(&mut bytes, self.layers.len() as u32);
        for layer in self.layers {
            if let Some(time) = layer.time {
                write_shift(precision, &mut bytes, &time.x_shift);
                super::write_u32(&mut bytes, time.matrix.len() as u32);
                if let Some(packed) = time
                    .packed_matrix
                    .filter(|packed| packed.precision.same_layout(precision))
                {
                    bytes.extend(packed.bytes);
                } else if let Some((schedule, factors)) = precision.lowrank() {
                    // A matrix without factors of this width (never written
                    // at this precision): factor it on the full basis.
                    let mut matrix = time.matrix;
                    lowrank::encode_existing(&mut matrix, schedule, factors, &mut bytes);
                } else {
                    encode_matrix(precision, &time.matrix, &mut bytes);
                }
            } else {
                super::write_u32(&mut bytes, 0);
                super::write_u32(&mut bytes, 0);
            }
            write_shift(
                precision,
                &mut bytes,
                layer.channel_shift.as_deref().unwrap_or(&[]),
            );
        }
        Self {
            layers: Vec::new(),
            packed: Some(bytes),
        }
    }
}

fn abs_max(values: &[f32]) -> f32 {
    values.iter().map(|v| v.abs()).fold(0.0_f32, f32::max)
}

/// The float16 scale rule of the training repository's `round_scale_f16`:
/// clamp to 65,504, round to nearest even, and use 1.0 when the result is 0
/// (all-zero vectors and float16 underflow).
pub(super) fn f16_scale(maximum: f32, limit: f32) -> f16 {
    let scale = f16::from_f32((maximum / limit).min(f16::MAX.to_f32()));
    if scale.to_f32() > 0.0 {
        scale
    } else {
        f16::ONE
    }
}

pub(super) fn quantize(value: f32, scale: f32, limit: f32) -> i8 {
    (value / scale).round_ties_even().clamp(-limit, limit) as i8
}

fn write_shift(precision: StatePrecision, out: &mut Vec<u8>, values: &[f32]) {
    super::write_u32(out, values.len() as u32);
    if precision.is_compact() {
        if values.is_empty() {
            return;
        }
        let scale = f16_scale(abs_max(values), SHIFT_LIMIT);
        out.extend_from_slice(&scale.to_bits().to_le_bytes());
        let scale = scale.to_f32();
        out.extend(
            values
                .iter()
                .map(|value| quantize(*value, scale, SHIFT_LIMIT) as u8),
        );
    } else {
        for value in values {
            out.extend_from_slice(&f16::from_f32(*value).to_bits().to_le_bytes());
        }
    }
}

fn non_finite_shift() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidData,
        "invalid RWKV compact state: non-finite shift",
    )
}

fn read_shift(precision: StatePrecision, cursor: &mut Cursor<'_>) -> io::Result<Vec<f32>> {
    let count = cursor.u32()? as usize;
    if count != 0 && count != D_MODEL {
        return Err(invalid_state());
    }
    if precision.is_compact() {
        if count == 0 {
            return Ok(Vec::new());
        }
        let scale = f16_bits(cursor.bytes(2)?);
        if !scale.is_finite() || scale <= 0.0 {
            return Err(non_finite_shift());
        }
        let codes = cursor.bytes(count)?;
        if codes.contains(&128) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "invalid RWKV compact state: out-of-range shift code",
            ));
        }
        return Ok(codes
            .iter()
            .map(|&code| code as i8 as f32 * scale)
            .collect());
    }
    cursor
        .bytes(count * 2)?
        .chunks_exact(2)
        .map(|bytes| {
            let value = f16_bits(bytes);
            if value.is_finite() {
                Ok(value)
            } else {
                Err(non_finite_shift())
            }
        })
        .collect()
}

fn f16_bits(bytes: &[u8]) -> f32 {
    f16::from_bits(u16::from_le_bytes(bytes[..2].try_into().unwrap())).to_f32()
}

fn row_scale(precision: StatePrecision, row: &[u8]) -> f32 {
    if precision.is_compact() {
        f16_bits(row)
    } else {
        f32::from_le_bytes(row[..4].try_into().unwrap())
    }
}

pub(super) fn decode(bytes: &[u8]) -> io::Result<ModuleState> {
    let mut cursor = Cursor::new(bytes);
    cursor.expect_magic(MAGIC)?;
    let precision = StatePrecision::from_code(cursor.u8()?)?;
    let count = cursor.u32()? as usize;
    if !(1..=4).contains(&count) {
        return Err(invalid_state());
    }
    let scale_bytes = precision.scale_bytes();
    let mut layers = Vec::with_capacity(count);
    for _ in 0..count {
        let x_shift = read_shift(precision, &mut cursor)?;
        let matrix_count = cursor.u32()? as usize;
        if matrix_count != 0 && matrix_count != HEADS * HEAD_SIZE * HEAD_SIZE {
            return Err(invalid_state());
        }
        if let Some((_, factors)) = precision.lowrank() {
            let time = if matrix_count == 0 {
                None
            } else {
                let head = cursor.bytes(2)?;
                let rank = head[1] as usize;
                if rank > lowrank::MAX_RANK.min(HEAD_SIZE) {
                    return Err(invalid_state());
                }
                let len = lowrank::block_len(rank, factors);
                let rest = cursor.bytes(len - 2)?;
                let mut block = Vec::with_capacity(len);
                block.extend_from_slice(head);
                block.extend_from_slice(rest);
                let mut matrix = vec![0.0; matrix_count];
                lowrank::decode_block(&block, factors, &mut matrix)?;
                Some((matrix, block))
            };
            let channel_shift = read_shift(precision, &mut cursor)?;
            if x_shift.is_empty() != time.is_none() {
                return Err(invalid_state());
            }
            layers.push(LayerState {
                time: time.map(|(matrix, bytes)| TimeState {
                    x_shift,
                    matrix,
                    packed_matrix: Some(PackedMatrix { precision, bytes }),
                }),
                channel_shift: (!channel_shift.is_empty()).then_some(channel_shift),
            });
            continue;
        }
        let packed = cursor.bytes(matrix_count / HEAD_SIZE * precision.row_bytes())?;
        for row in packed.chunks_exact(precision.row_bytes()) {
            let scale = row_scale(precision, row);
            if !scale.is_finite() || scale <= 0.0 {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "invalid RWKV compact state: non-finite or non-positive matrix scale",
                ));
            }
            let codes = &row[scale_bytes..];
            let invalid_code = if precision.int8_codes() {
                codes.contains(&128)
            } else {
                codes.iter().any(|byte| byte & 15 == 8 || byte >> 4 == 8)
            };
            if invalid_code {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "invalid RWKV compact state: out-of-range matrix code",
                ));
            }
        }
        let channel_shift = read_shift(precision, &mut cursor)?;
        if x_shift.is_empty() != (matrix_count == 0) {
            return Err(invalid_state());
        }
        let time = if matrix_count == 0 {
            None
        } else {
            let mut matrix = vec![0.0; matrix_count];
            decode_matrix(precision, packed, &mut matrix);
            if matrix.iter().any(|value| !value.is_finite()) {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "invalid RWKV compact state: non-finite matrix value",
                ));
            }
            Some(TimeState {
                x_shift,
                matrix,
                packed_matrix: Some(PackedMatrix {
                    precision,
                    bytes: packed.to_vec(),
                }),
            })
        };
        layers.push(LayerState {
            time,
            channel_shift: (!channel_shift.is_empty()).then_some(channel_shift),
        });
    }
    cursor.expect_end()?;
    Ok(ModuleState {
        layers,
        packed: None,
    })
}

/// Kept only while bulk replay holds a decoded layer, so its final packed
/// representation preserves the exact scales from the last answer.
#[derive(Clone)]
pub(super) struct PackedMatrix {
    precision: StatePrecision,
    bytes: Vec<u8>,
}

fn encode_matrix(precision: StatePrecision, values: &[f32], out: &mut Vec<u8>) {
    out.reserve(values.len() / HEAD_SIZE * precision.row_bytes());
    let limit = precision.limit();
    for row in values.chunks_exact(HEAD_SIZE) {
        let maximum = abs_max(row);
        let scale = if precision.is_compact() {
            let scale = f16_scale(maximum, limit);
            out.extend_from_slice(&scale.to_bits().to_le_bytes());
            scale.to_f32()
        } else {
            let scale = if maximum == 0.0 {
                1.0
            } else {
                (maximum / limit).max(f32::from_bits(1))
            };
            out.extend_from_slice(&scale.to_le_bytes());
            scale
        };
        let codes: [i8; HEAD_SIZE] = std::array::from_fn(|i| quantize(row[i], scale, limit));
        if precision.int8_codes() {
            out.extend_from_slice(&codes.map(|code| code as u8));
        } else {
            let packed: [u8; HEAD_SIZE / 2] = std::array::from_fn(|index| {
                (codes[2 * index] as u8 & 15) | ((codes[2 * index + 1] as u8 & 15) << 4)
            });
            out.extend_from_slice(&packed);
        }
    }
}

fn decode_matrix(precision: StatePrecision, bytes: &[u8], out: &mut [f32]) {
    let scale_bytes = precision.scale_bytes();
    let int8 = precision.int8_codes();
    for (row, encoded) in out
        .chunks_exact_mut(HEAD_SIZE)
        .zip(bytes.chunks_exact(precision.row_bytes()))
    {
        let scale = row_scale(precision, encoded);
        let codes = &encoded[scale_bytes..];
        for (column, value) in row.iter_mut().enumerate() {
            let code = if int8 {
                codes[column] as i8
            } else {
                let nibble = (codes[column / 2] >> (4 * (column % 2))) & 15;
                ((nibble as i8) << 4) >> 4
            };
            *value = code as f32 * scale;
        }
    }
}

/// The basis of a low-rank write: the previous write's block, or what the
/// previous matrix is when it has none.
pub(super) fn lowrank_previous<'a>(
    packed: Option<&'a PackedMatrix>,
    previous: &[f32],
) -> lowrank::Previous<'a> {
    match packed.and_then(|packed| Some((packed, packed.precision.lowrank()?.1))) {
        Some((packed, factors)) => lowrank::Previous::Block(&packed.bytes, factors),
        None if previous.iter().all(|value| *value == 0.0) => lowrank::Previous::Fresh,
        None => lowrank::Previous::Matrix,
    }
}

fn invalid_state() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidData,
        "invalid RWKV state precision or compact state",
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    const MODULE_PRECISIONS: [StatePrecision; 4] = [
        StatePrecision::Int8,
        StatePrecision::Int4,
        StatePrecision::Int8Compact,
        StatePrecision::Int4Compact,
    ];

    fn test_layer(value: f32) -> LayerState {
        LayerState {
            time: Some(TimeState {
                x_shift: vec![value; D_MODEL],
                matrix: vec![value; HEADS * HEAD_SIZE * HEAD_SIZE],
                packed_matrix: None,
            }),
            channel_shift: Some(vec![value; D_MODEL]),
        }
    }

    #[test]
    fn rwkv_state_precision_codes_use_signed_rows_and_even_rounding() {
        for precision in MODULE_PRECISIONS {
            let mut row = [0.0; HEAD_SIZE];
            row[..7].copy_from_slice(&[
                precision.limit(),
                -precision.limit(),
                0.5,
                1.5,
                -0.5,
                -1.5,
                0.0,
            ]);
            let mut packed = None;
            precision.round_matrix(&mut row, &mut packed);
            assert_eq!(
                &row[..7],
                &[
                    precision.limit(),
                    -precision.limit(),
                    0.0,
                    2.0,
                    0.0,
                    -2.0,
                    0.0
                ]
            );
            assert_eq!(packed.unwrap().bytes.len(), precision.row_bytes());
            let mut zero = [0.0; HEAD_SIZE];
            precision.round_matrix(&mut zero, &mut None);
            assert_eq!(zero, [0.0; HEAD_SIZE]);
        }
    }

    #[test]
    fn rwkv_state_precision_compact_policies_and_sizes() {
        assert_eq!(
            StatePrecision::parse("compact_cards_int8")
                .unwrap()
                .module_precisions(),
            (StatePrecision::Int8Compact, StatePrecision::Int8Compact)
        );
        assert_eq!(
            StatePrecision::parse("compact_cards_int4")
                .unwrap()
                .module_precisions(),
            (StatePrecision::Int4Compact, StatePrecision::Int8Compact)
        );
        assert_eq!(StatePrecision::Int8Compact.row_bytes(), 2 + HEAD_SIZE);
        assert_eq!(StatePrecision::Int4Compact.row_bytes(), 2 + HEAD_SIZE / 2);
        for precision in MODULE_PRECISIONS {
            assert_eq!(
                StatePrecision::from_code(precision.code()).unwrap(),
                precision
            );
        }
    }

    /// The float16 scale clamps at 65,504 (codes saturate at the limit) and
    /// falls back to 1.0 on underflow (codes 0), as `round_scale_f16` does.
    #[test]
    fn rwkv_state_precision_compact_scales_clamp_and_underflow() {
        for precision in [StatePrecision::Int8Compact, StatePrecision::Int4Compact] {
            let limit = precision.limit();
            let mut huge = [0.0; HEAD_SIZE];
            huge[0] = 1e9;
            huge[1] = -1e9;
            huge[2] = 65_504.0;
            let mut packed = None;
            precision.round_matrix(&mut huge, &mut packed);
            let bytes = packed.unwrap().bytes;
            assert_eq!(f16_bits(&bytes), 65_504.0);
            assert_eq!(huge[0], limit * 65_504.0);
            assert_eq!(huge[1], -limit * 65_504.0);
            assert_eq!(huge[2], 65_504.0);

            let mut tiny = [1e-9; HEAD_SIZE];
            tiny[1] = -1e-9;
            let mut packed = None;
            precision.round_matrix(&mut tiny, &mut packed);
            assert_eq!(f16_bits(&packed.unwrap().bytes), 1.0);
            assert_eq!(tiny, [0.0; HEAD_SIZE]);

            // Infinite inputs saturate instead of storing a non-finite scale.
            let mut infinite = [0.0; HEAD_SIZE];
            infinite[3] = f32::INFINITY;
            precision.round_matrix(&mut infinite, &mut None);
            assert_eq!(infinite[3], limit * 65_504.0);
            assert!(infinite.iter().all(|value| value.is_finite()));
        }
        // Shift vectors: int8 codes, one float16 scale per vector.
        let mut shift = vec![0.0; D_MODEL];
        shift[0] = 2.0;
        shift[1] = 1.0;
        shift[2] = -1e9;
        StatePrecision::Int8Compact.round_shift(&mut shift);
        assert_eq!(shift[0], 0.0);
        assert_eq!(shift[2], -127.0 * 65_504.0);
        let mut shift = vec![0.0; D_MODEL];
        shift[0] = 127.0;
        shift[1] = 0.5;
        shift[2] = 1.5;
        StatePrecision::Int4Compact.round_shift(&mut shift);
        assert_eq!(&shift[..3], &[127.0, 0.0, 2.0]);
    }

    /// Rounding a shift in replay equals storing and reloading it.
    #[test]
    fn rwkv_state_precision_shift_rounding_matches_the_stored_shift() {
        for precision in MODULE_PRECISIONS {
            let values: Vec<f32> = (0..D_MODEL)
                .map(|index| ((index * 37 % 101) as f32 - 50.0) * 0.731 + index as f32 * 1e-3)
                .collect();
            let mut rounded = values.clone();
            precision.round_shift(&mut rounded);
            let mut bytes = Vec::new();
            write_shift(precision, &mut bytes, &values);
            let mut cursor = Cursor::new(&bytes);
            assert_eq!(read_shift(precision, &mut cursor).unwrap(), rounded);
            cursor.expect_end().unwrap();
        }
    }

    #[test]
    fn rwkv_state_precision_rejects_corrupt_lengths_scales_and_codes() {
        let layer = test_layer(1.0);
        for precision in MODULE_PRECISIONS {
            let packed = ModuleState {
                layers: vec![layer.clone(); 2],
                packed: None,
            }
            .compact(precision);
            let bytes = packed.packed.unwrap();
            assert_eq!(bytes[MAGIC.len()], precision.code());
            assert!(decode(&bytes).is_ok());
            for length in [0, MAGIC.len(), bytes.len() - 1] {
                assert!(decode(&bytes[..length]).is_err());
            }
            let mut corrupt = bytes.clone();
            corrupt[MAGIC.len()] = 16;
            assert_eq!(
                decode(&corrupt).err().unwrap().kind(),
                io::ErrorKind::InvalidData
            );
            let shift_bytes = if precision.is_compact() {
                2 + D_MODEL
            } else {
                2 * D_MODEL
            };
            let scale_start = MAGIC.len() + 1 + 4 + 4 + shift_bytes + 4;
            corrupt = bytes.clone();
            if precision.is_compact() {
                corrupt[scale_start..scale_start + 2]
                    .copy_from_slice(&f16::NAN.to_bits().to_le_bytes());
            } else {
                corrupt[scale_start..scale_start + 4].copy_from_slice(&f32::NAN.to_le_bytes());
            }
            assert_eq!(
                decode(&corrupt).err().unwrap().kind(),
                io::ErrorKind::InvalidData
            );
            if precision.is_compact() {
                // Zero, negative and infinite float16 scales.
                for bits in [0u16, f16::NEG_ONE.to_bits(), f16::INFINITY.to_bits()] {
                    corrupt = bytes.clone();
                    corrupt[scale_start..scale_start + 2].copy_from_slice(&bits.to_le_bytes());
                    assert!(decode(&corrupt).is_err());
                    // The x-shift scale of the first layer.
                    corrupt = bytes.clone();
                    let shift_scale = MAGIC.len() + 1 + 4 + 4;
                    corrupt[shift_scale..shift_scale + 2].copy_from_slice(&bits.to_le_bytes());
                    assert!(decode(&corrupt).is_err());
                }
                // Shift code -128.
                corrupt = bytes.clone();
                corrupt[MAGIC.len() + 1 + 4 + 4 + 2] = 128;
                assert!(decode(&corrupt).is_err());
            }
            corrupt = bytes.clone();
            corrupt[scale_start + precision.scale_bytes()] =
                if precision.int8_codes() { 128 } else { 8 };
            assert_eq!(
                decode(&corrupt).err().unwrap().kind(),
                io::ErrorKind::InvalidData
            );
            corrupt = bytes.clone();
            corrupt[MAGIC.len() + 1..MAGIC.len() + 5].copy_from_slice(&u32::MAX.to_le_bytes());
            assert_eq!(
                decode(&corrupt).err().unwrap().kind(),
                io::ErrorKind::InvalidData
            );
            // Loading and querying must not re-quantize scales or codes.
            assert_eq!(
                decode(&bytes).unwrap().compact(precision).packed.unwrap(),
                bytes
            );
        }
    }

    /// Bytes of one card / note layer in each layout, against the training
    /// repository's byte model (SM136 `sm136-bytes.json`).
    #[test]
    fn rwkv_state_precision_compact_layer_bytes() {
        let rows = HEADS * HEAD_SIZE;
        for (precision, codes) in [
            (StatePrecision::Int8Compact, HEAD_SIZE),
            (StatePrecision::Int4Compact, HEAD_SIZE / 2),
        ] {
            for channel in [true, false] {
                let state = ModuleState {
                    layers: vec![LayerState {
                        channel_shift: channel.then(|| vec![0.5; D_MODEL]),
                        ..test_layer(0.5)
                    }],
                    packed: None,
                }
                .compact(precision);
                let shift = 4 + 2 + D_MODEL;
                assert_eq!(
                    state.packed.unwrap().len(),
                    MAGIC.len()
                        + 1
                        + 4
                        + shift
                        + 4
                        + rows * (2 + codes)
                        + if channel { shift } else { 4 }
                );
            }
        }
    }

    /// A state that cannot be decoded is an error for bulk replay, and model
    /// passes that cannot report one see fresh state.
    #[test]
    fn rwkv_state_precision_undecodable_state_is_an_error_not_a_panic() {
        let mut layer = test_layer(1.0);
        layer.time.as_mut().unwrap().x_shift[0] = 1e6;
        let state = ModuleState {
            layers: vec![layer],
            packed: None,
        }
        .compact(StatePrecision::Int4);
        assert!(state.try_decoded().is_err());
        assert!(state.decoded_or_fresh().is_none());
        assert_eq!(
            state.try_into_decoded().err().unwrap().to_string(),
            "invalid RWKV compact state: non-finite shift"
        );
        // The same overflow stays finite in the compact layout.
        let mut layer = test_layer(1.0);
        layer.time.as_mut().unwrap().x_shift[0] = 1e30;
        layer.time.as_mut().unwrap().matrix[5] = f32::INFINITY;
        for precision in [StatePrecision::Int8Compact, StatePrecision::Int4Compact] {
            let state = ModuleState {
                layers: vec![layer.clone()],
                packed: None,
            }
            .compact(precision);
            let decoded = state.try_into_decoded().unwrap();
            let time = decoded.layers[0].time.as_ref().unwrap();
            assert_eq!(time.x_shift[0], 127.0 * 65_504.0);
            assert_eq!(time.matrix[5], precision.limit() * 65_504.0);
        }
    }

    /// The bits of a value, with -0 as +0: the scorer's float codes keep the
    /// sign of a value that rounds to zero, the stored integer codes cannot,
    /// and the two zeros give the same results.
    fn bits(value: &f32) -> u32 {
        if *value == 0.0 {
            0
        } else {
            value.to_bits()
        }
    }

    fn codec_floats(text: &serde_json::Value) -> Vec<f32> {
        data_encoding::BASE64
            .decode(text.as_str().unwrap().as_bytes())
            .unwrap()
            .chunks_exact(4)
            .map(|bytes| f32::from_le_bytes(bytes.try_into().unwrap()))
            .collect()
    }

    /// Bit-exact parity with the training repository scorer's compact state
    /// rounding (`int_row_quant(bits | F16_SCALE)`, the big model's
    /// `_quantize_rows` / `_round_shift`) on synthetic states, written by
    /// `qt/tools/rwkv_compact_state_codec_vectors.py`.
    #[test]
    fn rwkv_state_precision_compact_codec_matches_the_scorer() {
        use std::io::Read;
        let mut text = Vec::new();
        flate2::read::GzDecoder::new(&include_bytes!("testdata/compact-state-codec.json.gz")[..])
            .read_to_end(&mut text)
            .unwrap();
        let vectors: serde_json::Value = serde_json::from_slice(&text).unwrap();
        let case = vectors["cases"]
            .as_array()
            .unwrap()
            .iter()
            .find(|case| case["head_size"] == HEAD_SIZE && case["d_model"] == D_MODEL)
            .expect("vectors for this engine's dimensions");
        let matrix = codec_floats(&case["matrix"]);
        assert_eq!(matrix.len() % HEAD_SIZE, 0);
        for (precision, key) in [
            (StatePrecision::Int8Compact, "int8"),
            (StatePrecision::Int4Compact, "int4"),
        ] {
            let mut actual = matrix.clone();
            let mut packed = None;
            precision.round_matrix(&mut actual, &mut packed);
            let expected = codec_floats(&case[key]);
            assert_eq!(actual.len(), expected.len());
            for (index, (a, b)) in actual.iter().zip(&expected).enumerate() {
                assert_eq!(
                    bits(a),
                    bits(b),
                    "{key} row {} column {}: {a} != {b}",
                    index / HEAD_SIZE,
                    index % HEAD_SIZE
                );
            }
            // Decoding the stored bytes gives the same values.
            let mut decoded = vec![0.0; matrix.len()];
            decode_matrix(precision, &packed.unwrap().bytes, &mut decoded);
            assert_eq!(
                decoded.iter().map(bits).collect::<Vec<_>>(),
                expected.iter().map(bits).collect::<Vec<_>>()
            );
        }
        let shifts = codec_floats(&case["shift"]);
        let expected = codec_floats(&case["shift_int8"]);
        for (vector, expected) in shifts
            .chunks_exact(D_MODEL)
            .zip(expected.chunks_exact(D_MODEL))
        {
            for precision in [StatePrecision::Int8Compact, StatePrecision::Int4Compact] {
                let mut actual = vector.to_vec();
                precision.round_shift(&mut actual);
                assert_eq!(
                    actual.iter().map(bits).collect::<Vec<_>>(),
                    expected.iter().map(bits).collect::<Vec<_>>()
                );
            }
        }
    }
}
