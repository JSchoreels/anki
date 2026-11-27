// Copyright: Anki Software, LLC and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Rolling per-user recalibration of the timed answer-time probabilities
//! (SM175): "recalled / Good or Easy / Easy AND answered within S s"
//! (the whole time on the card, reading the back included).
//!
//! The rolling recalibration on the 732 set (SM125) validated this scheme
//! for five models, the Big models (SM128) and the six-layer Tiny (SM140)
//! for the others: per event (each grade set and each S separately), the
//! raw probability p is mapped through sigmoid(a logit(p) + b). The user's
//! time-labelled reviews (a recorded time, not the card's first review) are
//! cut into 7-day blocks counted from the user's first one; each block is
//! predicted with (a, b) fitted on the user's latest 30,000 such reviews of
//! strictly earlier days, and the raw model is kept until 1,000 of them
//! exist. An event with fewer than 20 positives or 20 negatives in the fit
//! rows keeps the identity map; S at or above the user's cap stays raw. A
//! repair keeps the numbers coherent: non-decreasing in S, Easy within Good
//! or Easy within recalled, and each timed event at most the raw P(set).
//! Grade-only probabilities are never changed.
//!
//! The arithmetic follows the reference (`recal_user`, `fit_platt_batch`,
//! `repair` of the training repository's `scripts/sm125_recal.py`) step by
//! step, including the batched Newton loop over every event of a block;
//! `rwkv_time_recal_matches_the_reference` checks it on synthetic users.

use std::ops::Range;

use super::answer_time::AnswerTimePrediction;

/// Thresholds S of the reference grid, in seconds.
pub const ANSWER_TIME_RECAL_SECONDS: [u32; 10] = [2, 3, 4, 5, 6, 8, 10, 15, 20, 30];
const S_COUNT: usize = ANSWER_TIME_RECAL_SECONDS.len();
/// Grade sets in the reference's column order: recalled (Hard, Good or
/// Easy), Good or Easy, Easy. Grades are 0-based (0 = Again).
const SET_GRADES: [&[usize]; 3] = [&[1, 2, 3], &[2, 3], &[3]];
const SETS: usize = 3;
/// 3 grade-only events, then 10 thresholds per set.
const COLUMNS: usize = SETS + SETS * S_COUNT;
const EPS: f64 = 1e-6;
const A_RANGE: (f64, f64) = (0.05, 20.0);
const B_RANGE: (f64, f64) = (-15.0, 15.0);
const NEWTON_ITERATIONS: usize = 50;
const NEWTON_TOLERANCE: f64 = 1e-12;
/// The answer-time cap of the reference labels (Anki's default maximum
/// answer seconds).
const TIME_CAP_MILLIS: i64 = 60_000;
const TIME_LOW_CAP_SHARE: f64 = 0.005;

/// Window and sample rules of the rolling fit.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct AnswerTimeRecalConfig {
    /// Block length in Anki days.
    pub roll_days: i64,
    /// Fit rows: at most this many of the latest earlier reviews.
    pub roll_rows: usize,
    /// Earlier reviews needed before recalibrating (else raw).
    pub min_rows: usize,
    /// Positives and negatives an event needs in the fit rows (else the
    /// identity map).
    pub min_class: usize,
}

/// The validated scheme (SM125, SM128, SM140).
pub const ANSWER_TIME_RECAL_SHIPPED: AnswerTimeRecalConfig = AnswerTimeRecalConfig {
    roll_days: 7,
    roll_rows: 30_000,
    min_rows: 1_000,
    min_class: 20,
};

const fn timed_column(set: usize, threshold: usize) -> usize {
    SETS + set * S_COUNT + threshold
}

/// One query's event probabilities: P(set) and P(set and answered within
/// S s) for each set of [`SET_GRADES`] and each S.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct TimedEvents {
    /// P(recalled), P(Good or Easy), P(Easy): grade only.
    pub grade: [f64; SETS],
    /// [set][threshold] of [`ANSWER_TIME_RECAL_SECONDS`].
    pub timed: [[f64; S_COUNT]; SETS],
}

impl TimedEvents {
    /// The raw events of a head prediction: sum over the set's grades of
    /// P(grade) x P(t <= S | grade), the latter linear in ln time inside the
    /// bin (the reference's `cdf_at`). Each value is rounded to f32, as the
    /// reference stored its per-review outputs.
    pub fn from_prediction(prediction: &AnswerTimePrediction) -> Self {
        let buttons = prediction.button_probabilities.map(f64::from);
        let mut under = [[0.0_f64; 4]; S_COUNT];
        for (index, &seconds) in ANSWER_TIME_RECAL_SECONDS.iter().enumerate() {
            let values = prediction
                .probabilities_under_seconds_f64(f64::from(seconds))
                .expect("grid thresholds are inside the head's range");
            under[index] = values;
        }
        let round = |value: f64| f64::from(value as f32);
        Self {
            grade: std::array::from_fn(|set| {
                round(SET_GRADES[set].iter().map(|&grade| buttons[grade]).sum())
            }),
            timed: std::array::from_fn(|set| {
                std::array::from_fn(|threshold| {
                    round(
                        SET_GRADES[set]
                            .iter()
                            .map(|&grade| buttons[grade] * under[threshold][grade])
                            .sum(),
                    )
                })
            }),
        }
    }

    fn columns(&self) -> [f64; COLUMNS] {
        let mut out = [0.0; COLUMNS];
        out[..SETS].copy_from_slice(&self.grade);
        for set in 0..SETS {
            out[timed_column(set, 0)..timed_column(set, 0) + S_COUNT]
                .copy_from_slice(&self.timed[set]);
        }
        out
    }

    fn from_columns(columns: &[f64; COLUMNS]) -> Self {
        Self {
            grade: std::array::from_fn(|set| columns[set]),
            timed: std::array::from_fn(|set| {
                std::array::from_fn(|threshold| columns[timed_column(set, threshold)])
            }),
        }
    }

    /// The value at `seconds` of the grid for each set (recalled, Good or
    /// Easy, Easy), if `seconds` is on the grid.
    pub fn within_seconds(&self, seconds: f64) -> Option<[f64; SETS]> {
        let index = ANSWER_TIME_RECAL_SECONDS
            .iter()
            .position(|&value| f64::from(value) == seconds)?;
        Some(std::array::from_fn(|set| self.timed[set][index]))
    }
}

/// One time-labelled review of the user's history with the model's raw
/// events before it.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct TimedRow {
    /// Anki day of the review.
    pub day: i64,
    /// 0-based grade (0 = Again).
    pub grade: u8,
    /// Recorded answer time (front + back), capped by Anki.
    pub duration_millis: i64,
    pub raw: TimedEvents,
}

impl TimedRow {
    fn label(&self, column: usize) -> f64 {
        let (set, seconds) = if column < SETS {
            (column, None)
        } else {
            let index = column - SETS;
            (
                index / S_COUNT,
                Some(ANSWER_TIME_RECAL_SECONDS[index % S_COUNT]),
            )
        };
        let in_set = SET_GRADES[set].contains(&(self.grade as usize));
        let in_time = seconds.is_none_or(|s| self.duration_millis <= 1000 * i64::from(s));
        if in_set && in_time {
            1.0
        } else {
            0.0
        }
    }
}

/// The reference's user cap (`time_user_cap`): 60 s, unless the user's
/// longest recorded time is lower and holds at least 0.5% (and 20) of the
/// recorded times (a lower "maximum answer seconds").
pub fn answer_time_user_cap_millis(durations: impl IntoIterator<Item = i64>) -> i64 {
    let mut top = 0;
    let mut at_top = 0_u64;
    let mut timed = 0_u64;
    for duration in durations.into_iter().filter(|&value| value > 0) {
        timed += 1;
        if duration > top {
            top = duration;
            at_top = 1;
        } else if duration == top {
            at_top += 1;
        }
    }
    if timed == 0 {
        return TIME_CAP_MILLIS;
    }
    if top < TIME_CAP_MILLIS && at_top as f64 / timed as f64 >= TIME_LOW_CAP_SHARE && at_top >= 20 {
        top
    } else {
        TIME_CAP_MILLIS
    }
}

/// Whether each column is scored for a user with this cap: grade-only
/// always, S only below the cap (the reference's `valid_cols`).
fn valid_columns(cap_millis: i64) -> [bool; COLUMNS] {
    std::array::from_fn(|column| {
        column < SETS
            || cap_millis > 1000 * i64::from(ANSWER_TIME_RECAL_SECONDS[(column - SETS) % S_COUNT])
    })
}

fn logit(p: f64) -> f64 {
    let p = p.clamp(EPS, 1.0 - EPS);
    p.ln() - (-p).ln_1p()
}

fn sigmoid(z: f64) -> f64 {
    0.5 * (1.0 + (0.5 * z).tanh())
}

/// numpy's `logaddexp(0, z)`.
fn softplus(z: f64) -> f64 {
    if z > 0.0 {
        z + (-z).exp().ln_1p()
    } else {
        z.exp().ln_1p()
    }
}

fn mean(values: impl Iterator<Item = f64>, n: usize) -> f64 {
    values.sum::<f64>() / n as f64
}

fn platt_loss(w: [f64; 2], x: &[f64], y: &[f64]) -> f64 {
    mean(
        x.iter().zip(y).map(|(&x, &y)| {
            let z = w[0] * x + w[1];
            softplus(z) - y * z
        }),
        x.len(),
    )
}

/// The reference's `fit_platt_batch`: per event (a, b) minimising the mean
/// LogLoss of sigmoid(a x + b) by Newton with per-event step halving, every
/// event of a block in one loop (the halving stops for all events once one
/// event's step reaches 1e-4).
fn fit_platt_batch(x: &[Vec<f64>], y: &[Vec<f64>], w0: &[[f64; 2]]) -> Vec<[f64; 2]> {
    let k = x.len();
    let mut w = w0.to_vec();
    let mut cur: Vec<f64> = (0..k).map(|e| platt_loss(w[e], &x[e], &y[e])).collect();
    let mut active = vec![true; k];
    for _ in 0..NEWTON_ITERATIONS {
        let rows: Vec<usize> = (0..k).filter(|&e| active[e]).collect();
        if rows.is_empty() {
            break;
        }
        let mut step = Vec::with_capacity(rows.len());
        for &e in &rows {
            let (xe, ye, we) = (&x[e], &y[e], w[e]);
            let n = xe.len();
            let (mut g0, mut g1, mut h11, mut h12, mut h22) = (0.0, 0.0, 0.0, 0.0, 0.0);
            for (&xi, &yi) in xe.iter().zip(ye) {
                let q = sigmoid(we[0] * xi + we[1]);
                let r = q * (1.0 - q);
                g0 += (q - yi) * xi;
                g1 += q - yi;
                h11 += r * xi * xi;
                h12 += r * xi;
                h22 += r;
            }
            let n = n as f64;
            let (g0, g1) = (g0 / n, g1 / n);
            let (h11, h12, h22) = (h11 / n + 1e-9, h12 / n, h22 / n + 1e-9);
            let det = h11 * h22 - h12 * h12;
            step.push([(h22 * g0 - h12 * g1) / det, (h11 * g1 - h12 * g0) / det]);
        }
        let mut lam = vec![1.0_f64; rows.len()];
        let mut new_w: Vec<[f64; 2]> = rows
            .iter()
            .zip(&step)
            .map(|(&e, s)| [w[e][0] - s[0], w[e][1] - s[1]])
            .collect();
        let mut new_loss: Vec<f64> = rows
            .iter()
            .zip(&new_w)
            .map(|(&e, nw)| platt_loss(*nw, &x[e], &y[e]))
            .collect();
        let mut bad: Vec<bool> = rows
            .iter()
            .enumerate()
            .map(|(i, &e)| new_loss[i] > cur[e] + 1e-15)
            .collect();
        while bad.iter().any(|&b| b) && lam.iter().copied().fold(f64::INFINITY, f64::min) > 1e-4 {
            for (i, &e) in rows.iter().enumerate() {
                if bad[i] {
                    lam[i] /= 2.0;
                    new_w[i] = [w[e][0] - lam[i] * step[i][0], w[e][1] - lam[i] * step[i][1]];
                    new_loss[i] = platt_loss(new_w[i], &x[e], &y[e]);
                }
            }
            for (i, &e) in rows.iter().enumerate() {
                bad[i] = new_loss[i] > cur[e] + 1e-15 && lam[i] > 1e-4;
            }
        }
        for (i, &e) in rows.iter().enumerate() {
            let stuck = new_loss[i] > cur[e] + 1e-15;
            if stuck {
                active[e] = false;
                continue;
            }
            let converged = (cur[e] - new_loss[i]).abs() < NEWTON_TOLERANCE;
            w[e] = new_w[i];
            cur[e] = new_loss[i];
            if converged {
                active[e] = false;
            }
        }
    }
    w
}

/// The fit of one block: per column the map applied to it, `None` for a
/// column that stays raw (S at or above the cap).
struct BlockFit {
    maps: [Option<[f64; 2]>; COLUMNS],
    fitted: [bool; COLUMNS],
}

/// Fits every valid column on `rows` (the block's fit rows), warm-started
/// from `warm` and updating it for the fitted columns, as the reference.
fn fit_block(
    rows: &[TimedRow],
    valid: &[bool; COLUMNS],
    config: AnswerTimeRecalConfig,
    warm: &mut [[f64; 2]; COLUMNS],
) -> BlockFit {
    let n = rows.len();
    let mut maps = [None; COLUMNS];
    let mut fitted = [false; COLUMNS];
    let mut columns = Vec::new();
    for column in 0..COLUMNS {
        if !valid[column] {
            continue;
        }
        maps[column] = Some([1.0, 0.0]);
        let positives = rows.iter().filter(|row| row.label(column) > 0.5).count();
        if positives >= config.min_class && n - positives >= config.min_class {
            columns.push(column);
        }
    }
    if !columns.is_empty() {
        let raw: Vec<[f64; COLUMNS]> = rows.iter().map(|row| row.raw.columns()).collect();
        let x: Vec<Vec<f64>> = columns
            .iter()
            .map(|&column| raw.iter().map(|values| logit(values[column])).collect())
            .collect();
        let y: Vec<Vec<f64>> = columns
            .iter()
            .map(|&column| rows.iter().map(|row| row.label(column)).collect())
            .collect();
        let w0: Vec<[f64; 2]> = columns.iter().map(|&column| warm[column]).collect();
        let fits = fit_platt_batch(&x, &y, &w0);
        for (&column, fit) in columns.iter().zip(fits) {
            let clamped = [
                fit[0].clamp(A_RANGE.0, A_RANGE.1),
                fit[1].clamp(B_RANGE.0, B_RANGE.1),
            ];
            warm[column] = clamped;
            maps[column] = Some(clamped);
            fitted[column] = true;
        }
    }
    BlockFit { maps, fitted }
}

/// The reference's `repair`: running maximum over increasing S per set,
/// then Easy <= Good or Easy <= recalled at every S, then each timed event
/// capped by the raw P(set); grade-only values are the raw ones.
fn repair(columns: &mut [f64; COLUMNS], raw: &[f64; COLUMNS]) {
    for set in [2, 1, 0] {
        for threshold in 1..S_COUNT {
            let previous = columns[timed_column(set, threshold - 1)];
            let value = &mut columns[timed_column(set, threshold)];
            *value = value.max(previous);
        }
    }
    for (low, high) in [(2, 1), (1, 0)] {
        for threshold in 0..S_COUNT {
            let low_value = columns[timed_column(low, threshold)];
            let value = &mut columns[timed_column(high, threshold)];
            *value = value.max(low_value);
        }
    }
    for set in 0..SETS {
        for threshold in 0..S_COUNT {
            let value = &mut columns[timed_column(set, threshold)];
            *value = value.min(raw[set]);
        }
        columns[set] = raw[set];
    }
}

fn apply_maps(raw: &TimedEvents, maps: &[Option<[f64; 2]>; COLUMNS]) -> TimedEvents {
    let raw_columns = raw.columns();
    let mut out = raw_columns;
    for column in SETS..COLUMNS {
        if let Some([a, b]) = maps[column] {
            out[column] = sigmoid(a * logit(raw_columns[column]) + b);
        }
    }
    repair(&mut out, &raw_columns);
    TimedEvents::from_columns(&out)
}

/// The rows of each block: (applied rows, fit rows), as the reference's
/// `blocks_of` on time-sorted days.
#[cfg(test)]
fn blocks(days: &[i64], config: AnswerTimeRecalConfig) -> Vec<(Range<usize>, Range<usize>)> {
    let Some(&first) = days.first() else {
        return vec![];
    };
    let mut out = vec![];
    let mut start = 0;
    while start < days.len() {
        let block = (days[start] - first).div_euclid(config.roll_days);
        let end = start
            + days[start..]
                .iter()
                .take_while(|&&day| (day - first).div_euclid(config.roll_days) == block)
                .count();
        out.push((start..end, start.saturating_sub(config.roll_rows)..start));
        start = end;
    }
    out
}

/// The reference's whole rolling scheme on one user's time-sorted rows
/// (`recal_user`'s repaired arm): per row the events and whether it was
/// recalibrated. Rows before `min_rows` earlier reviews keep the raw values
/// (repaired as in the reference). Each block's fit is warm-started from
/// the previous one. The runtime only needs today's block
/// ([`AnswerTimeRecalibration::fit`]); this replay is the parity reference.
#[cfg(test)]
pub fn answer_time_rolling_recalibration(
    rows: &[TimedRow],
    cap_millis: i64,
    config: AnswerTimeRecalConfig,
) -> (Vec<TimedEvents>, Vec<bool>) {
    let days: Vec<i64> = rows.iter().map(|row| row.day).collect();
    debug_assert!(days.windows(2).all(|pair| pair[0] <= pair[1]));
    let valid = valid_columns(cap_millis);
    let mut warm = [[1.0, 0.0]; COLUMNS];
    let mut out: Vec<TimedEvents> = rows
        .iter()
        .map(|row| {
            let raw = row.raw.columns();
            let mut columns = raw;
            repair(&mut columns, &raw);
            TimedEvents::from_columns(&columns)
        })
        .collect();
    let mut active = vec![false; rows.len()];
    for (applied, fit) in blocks(&days, config) {
        if fit.len() < config.min_rows {
            continue;
        }
        let block = fit_block(&rows[fit], &valid, config, &mut warm);
        // Grade-only columns are fitted in the reference too (they share the
        // Newton loop) but never applied.
        let mut maps = block.maps;
        maps[..SETS].fill(None);
        for index in applied {
            out[index] = apply_maps(&rows[index].raw, &maps);
            active[index] = true;
        }
    }
    (out, active)
}

/// Which time-labelled rows the fit for `today` reads.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AnswerTimeRecalWindow {
    /// Indexes into the time-sorted time-labelled rows.
    pub fit: Range<usize>,
    /// First day of today's block; every fit row is on an earlier day.
    pub block_start_day: i64,
    /// First day of the next block, when the fit must be redone.
    pub block_end_day: i64,
}

/// The fit window of today's block, or `None` while the user has fewer
/// than `min_rows` time-labelled reviews before it (the raw model is used).
/// `days` are the time-sorted days of the time-labelled reviews.
pub fn answer_time_recal_window(
    days: &[i64],
    today: i64,
    config: AnswerTimeRecalConfig,
) -> Option<AnswerTimeRecalWindow> {
    let &first = days.first()?;
    if today < first {
        return None;
    }
    let block_start_day = first + (today - first).div_euclid(config.roll_days) * config.roll_days;
    let end = days.partition_point(|&day| day < block_start_day);
    let start = end.saturating_sub(config.roll_rows);
    (end - start >= config.min_rows).then_some(AnswerTimeRecalWindow {
        fit: start..end,
        block_start_day,
        block_end_day: block_start_day + config.roll_days,
    })
}

/// A fitted recalibration for one user and block.
#[derive(Clone, Debug, PartialEq)]
pub struct AnswerTimeRecalibration {
    /// (a, b) per [set][threshold]; `None` keeps the raw value (S at or
    /// above the user's cap). An event without enough positives and
    /// negatives has the identity map (1, 0).
    pub maps: [[Option<[f64; 2]>; S_COUNT]; SETS],
    /// Whether each event was fitted (not the identity fallback).
    pub fitted: [[bool; S_COUNT]; SETS],
    pub fit_rows: usize,
    pub cap_millis: i64,
    pub block_start_day: i64,
    pub block_end_day: i64,
}

const ENCODED_VERSION: f64 = 1.0;
const ENCODED_HEADER: usize = 5;
const ENCODED_LEN: usize = ENCODED_HEADER + 4 * SETS * S_COUNT;

impl AnswerTimeRecalibration {
    /// Fits today's block on its fit rows (`rows[window.fit]`), each event
    /// from the identity start (the reference warm-starts from the previous
    /// block; Newton reaches the same optimum).
    pub fn fit(
        rows: &[TimedRow],
        window: &AnswerTimeRecalWindow,
        cap_millis: i64,
        config: AnswerTimeRecalConfig,
    ) -> Self {
        let fit_rows = &rows[window.fit.clone()];
        let mut warm = [[1.0, 0.0]; COLUMNS];
        let block = fit_block(fit_rows, &valid_columns(cap_millis), config, &mut warm);
        Self {
            maps: std::array::from_fn(|set| {
                std::array::from_fn(|threshold| block.maps[timed_column(set, threshold)])
            }),
            fitted: std::array::from_fn(|set| {
                std::array::from_fn(|threshold| block.fitted[timed_column(set, threshold)])
            }),
            fit_rows: fit_rows.len(),
            cap_millis,
            block_start_day: window.block_start_day,
            block_end_day: window.block_end_day,
        }
    }

    /// The recalibrated, repaired events of a query; grade-only values are
    /// returned unchanged.
    pub fn apply(&self, raw: &TimedEvents) -> TimedEvents {
        let mut maps = [None; COLUMNS];
        for set in 0..SETS {
            for threshold in 0..S_COUNT {
                maps[timed_column(set, threshold)] = self.maps[set][threshold];
            }
        }
        let mut out = apply_maps(raw, &maps);
        out.grade = raw.grade;
        out
    }

    /// A flat encoding for the desktop glue: version, block start, block
    /// end, fit rows, cap, then per [set][threshold] (has map, fitted, a, b).
    pub fn encode(&self) -> Vec<f64> {
        let mut out = vec![
            ENCODED_VERSION,
            self.block_start_day as f64,
            self.block_end_day as f64,
            self.fit_rows as f64,
            self.cap_millis as f64,
        ];
        for set in 0..SETS {
            for threshold in 0..S_COUNT {
                let map = self.maps[set][threshold];
                let [a, b] = map.unwrap_or([1.0, 0.0]);
                out.extend([
                    f64::from(u8::from(map.is_some())),
                    f64::from(u8::from(self.fitted[set][threshold])),
                    a,
                    b,
                ]);
            }
        }
        out
    }

    pub fn decode(values: &[f64]) -> Option<Self> {
        if values.len() != ENCODED_LEN
            || values[0] != ENCODED_VERSION
            || !values.iter().all(|value| value.is_finite())
        {
            return None;
        }
        let entry = |set: usize, threshold: usize| {
            let start = ENCODED_HEADER + 4 * (set * S_COUNT + threshold);
            &values[start..start + 4]
        };
        Some(Self {
            maps: std::array::from_fn(|set| {
                std::array::from_fn(|threshold| {
                    let e = entry(set, threshold);
                    (e[0] != 0.0).then_some([e[2], e[3]])
                })
            }),
            fitted: std::array::from_fn(|set| {
                std::array::from_fn(|threshold| entry(set, threshold)[1] != 0.0)
            }),
            block_start_day: values[1] as i64,
            block_end_day: values[2] as i64,
            fit_rows: values[3] as usize,
            cap_millis: values[4] as i64,
        })
    }
}

#[cfg(test)]
mod tests {
    use std::io::Read;

    use super::*;

    #[derive(serde::Deserialize)]
    struct Config {
        roll_days: i64,
        roll_rows: usize,
        min_rows: usize,
        min_class: usize,
    }

    #[derive(serde::Deserialize)]
    struct User {
        name: String,
        config: Config,
        cap_millis: i64,
        day: Vec<i64>,
        grade: Vec<u8>,
        duration_millis: Vec<i64>,
        raw: Vec<Vec<f32>>,
        cal: Vec<Vec<f64>>,
        active: Vec<u8>,
    }

    #[derive(serde::Deserialize)]
    struct Vectors {
        s_grid: Vec<u32>,
        cols: Vec<(String, u32)>,
        users: Vec<User>,
    }

    fn vectors() -> Vectors {
        let mut text = String::new();
        flate2::read::GzDecoder::new(&include_bytes!("testdata/time-recal-vectors.json.gz")[..])
            .read_to_string(&mut text)
            .unwrap();
        serde_json::from_str(&text).unwrap()
    }

    fn events(values: &[f64]) -> TimedEvents {
        TimedEvents::from_columns(&values.try_into().unwrap())
    }

    fn rows(user: &User) -> Vec<TimedRow> {
        (0..user.day.len())
            .map(|index| TimedRow {
                day: user.day[index],
                grade: user.grade[index],
                duration_millis: user.duration_millis[index],
                raw: events(
                    &user.raw[index]
                        .iter()
                        .map(|&value| f64::from(value))
                        .collect::<Vec<_>>(),
                ),
            })
            .collect()
    }

    fn config(user: &User) -> AnswerTimeRecalConfig {
        AnswerTimeRecalConfig {
            roll_days: user.config.roll_days,
            roll_rows: user.config.roll_rows,
            min_rows: user.config.min_rows,
            min_class: user.config.min_class,
        }
    }

    fn max_difference(a: &TimedEvents, b: &[f64]) -> f64 {
        a.columns()
            .iter()
            .zip(b)
            .map(|(x, y)| (x - y).abs())
            .fold(0.0, f64::max)
    }

    #[test]
    fn rwkv_time_recal_matches_the_reference() {
        let vectors = vectors();
        assert_eq!(vectors.s_grid, ANSWER_TIME_RECAL_SECONDS.to_vec());
        let names = ["Rec", "GE", "E"];
        for (column, (set, seconds)) in vectors.cols.iter().enumerate() {
            let (expected_set, expected_seconds) = if column < SETS {
                (column, 0)
            } else {
                (
                    (column - SETS) / S_COUNT,
                    ANSWER_TIME_RECAL_SECONDS[(column - SETS) % S_COUNT],
                )
            };
            assert_eq!(
                (set.as_str(), *seconds),
                (names[expected_set], expected_seconds)
            );
        }
        let shipped = vectors
            .users
            .iter()
            .find(|user| user.name == "standard")
            .unwrap();
        assert_eq!(config(shipped), ANSWER_TIME_RECAL_SHIPPED);
        for user in &vectors.users {
            let rows = rows(user);
            let config = config(user);
            let (out, active) = answer_time_rolling_recalibration(&rows, user.cap_millis, config);
            let mut worst = 0.0_f64;
            for (index, events) in out.iter().enumerate() {
                assert_eq!(
                    active[index],
                    user.active[index] != 0,
                    "{} row {index}",
                    user.name
                );
                worst = worst.max(max_difference(events, &user.cal[index]));
            }
            assert!(worst < 1e-6, "{}: largest difference {worst:e}", user.name);
            println!(
                "{}: {} rows, {} recalibrated, largest difference vs the reference {worst:.2e}",
                user.name,
                rows.len(),
                active.iter().filter(|&&a| a).count()
            );

            // The deployment fit (today's block only, identity start) on every
            // row before the last gives the reference's value of the last row.
            let last = rows.len() - 1;
            let days: Vec<i64> = rows[..last].iter().map(|row| row.day).collect();
            let window = answer_time_recal_window(&days, rows[last].day, config);
            assert_eq!(window.is_some(), active[last], "{}", user.name);
            if let Some(window) = window {
                assert!(rows[window.fit.clone()]
                    .iter()
                    .all(|row| row.day < rows[last].day));
                let fit =
                    AnswerTimeRecalibration::fit(&rows[..last], &window, user.cap_millis, config);
                let applied = fit.apply(&rows[last].raw);
                let difference = max_difference(&applied, &user.cal[last]);
                assert!(
                    difference < 1e-6,
                    "{}: deployment {difference:e}",
                    user.name
                );
                println!(
                    "{}: deployment fit, last row, difference {difference:.2e}",
                    user.name
                );
                assert_eq!(AnswerTimeRecalibration::decode(&fit.encode()), Some(fit));
            }
        }
    }

    fn synthetic_rows(n: usize, per_day: usize) -> Vec<TimedRow> {
        // A deterministic, miscalibrated synthetic user: labels follow a
        // shifted copy of the raw probabilities.
        let mut seed = 175_u64;
        let mut next = move || {
            seed = seed
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            (seed >> 11) as f64 / (1_u64 << 53) as f64
        };
        (0..n)
            .map(|index| {
                let recall = 0.6 + 0.35 * next();
                let easy = 0.1 * next();
                let good = (recall - 0.1) * (0.5 + 0.4 * next()) + easy;
                let grade_values = [recall, good.min(recall), easy.min(good)];
                let fast = 0.2 + 0.6 * next();
                let timed = std::array::from_fn(|set| {
                    std::array::from_fn(|threshold| {
                        let share = 1.0 - (1.0 - fast).powf(1.0 + threshold as f64 * 0.6);
                        f64::from((grade_values[set] * share) as f32)
                    })
                });
                let grade = if next() < recall {
                    if next() < 0.9 {
                        2
                    } else {
                        3
                    }
                } else {
                    0
                };
                TimedRow {
                    day: 1000 + (index / per_day) as i64,
                    grade,
                    duration_millis: (1000.0 + 9000.0 * next() * next()) as i64,
                    raw: TimedEvents {
                        grade: grade_values.map(|v| f64::from(v as f32)),
                        timed,
                    },
                }
            })
            .collect()
    }

    #[test]
    fn rwkv_time_recal_fits_only_on_earlier_reviews() {
        let config = ANSWER_TIME_RECAL_SHIPPED;
        let rows = synthetic_rows(3_000, 40);
        let (before, active) = answer_time_rolling_recalibration(&rows, 60_000, config);
        assert!(active.iter().any(|&a| a));
        // Change every label from day D on: no earlier output moves.
        let cut_day = rows[2_200].day;
        let mut changed = rows.clone();
        for row in changed.iter_mut().filter(|row| row.day >= cut_day) {
            row.grade = if row.grade == 0 { 3 } else { 0 };
            row.duration_millis = 59_000 - row.duration_millis;
        }
        let (after, _) = answer_time_rolling_recalibration(&changed, 60_000, config);
        let cut = rows.partition_point(|row| row.day < cut_day);
        for index in 0..cut {
            for (x, y) in before[index].columns().iter().zip(after[index].columns()) {
                assert_eq!(x.to_bits(), y.to_bits(), "row {index}");
            }
        }
        // Later blocks see the change.
        assert!((cut..rows.len()).any(|index| before[index] != after[index]));

        // The deployment fit for today ignores today's block entirely.
        let today = rows.last().unwrap().day;
        let days: Vec<i64> = rows.iter().map(|row| row.day).collect();
        let window = answer_time_recal_window(&days, today, config).unwrap();
        assert!(rows[window.fit.clone()]
            .iter()
            .all(|row| row.day < window.block_start_day));
        assert!(window.block_start_day <= today && today < window.block_end_day);
        let fit = AnswerTimeRecalibration::fit(&rows, &window, 60_000, config);
        let mut later = rows.clone();
        for row in later
            .iter_mut()
            .filter(|row| row.day >= window.block_start_day)
        {
            row.grade = 0;
        }
        assert_eq!(
            AnswerTimeRecalibration::fit(&later, &window, 60_000, config),
            fit
        );
    }

    #[test]
    fn rwkv_time_recal_keeps_raw_with_short_history() {
        let config = ANSWER_TIME_RECAL_SHIPPED;
        let rows = synthetic_rows(1_000, 37);
        let days: Vec<i64> = rows.iter().map(|row| row.day).collect();
        let later = days[999] + 30;
        // 999 earlier time-labelled reviews: no fit, the raw model is used.
        assert!(answer_time_recal_window(&days[..999], later, config).is_none());
        assert!(answer_time_recal_window(&[], later, config).is_none());
        // The clock moved before the first review: no fit.
        assert!(answer_time_recal_window(&days, days[0] - 1, config).is_none());
        // 1,000 are enough.
        let window = answer_time_recal_window(&days, later, config).unwrap();
        assert_eq!(window.fit, 0..1_000);
        // In the rolling scheme no row of a 999-review user is recalibrated,
        // and the values are the raw ones (only the repair applies).
        let (out, active) = answer_time_rolling_recalibration(&rows[..999], 60_000, config);
        assert!(active.iter().all(|&a| !a));
        for (row, events) in rows.iter().zip(&out) {
            assert_eq!(events.grade, row.raw.grade);
            let mut repaired = row.raw.columns();
            let raw = repaired;
            repair(&mut repaired, &raw);
            assert_eq!(events.columns(), repaired);
        }
    }

    #[test]
    fn rwkv_time_recal_never_changes_grade_only_outputs() {
        let config = ANSWER_TIME_RECAL_SHIPPED;
        let rows = synthetic_rows(2_500, 30);
        let days: Vec<i64> = rows.iter().map(|row| row.day).collect();
        let today = days[2_400];
        let window = answer_time_recal_window(&days, today, config).unwrap();
        let fit = AnswerTimeRecalibration::fit(&rows, &window, 60_000, config);
        assert!(fit.fitted.iter().flatten().any(|&f| f));
        for row in &rows[2_400..] {
            let out = fit.apply(&row.raw);
            for (x, y) in out.grade.iter().zip(row.raw.grade) {
                assert_eq!(x.to_bits(), y.to_bits());
            }
            // The repair keeps the timed events coherent.
            for set in 0..SETS {
                for threshold in 1..S_COUNT {
                    assert!(out.timed[set][threshold] >= out.timed[set][threshold - 1]);
                }
                for threshold in 0..S_COUNT {
                    assert!(out.timed[set][threshold] <= row.raw.grade[set]);
                }
            }
            for threshold in 0..S_COUNT {
                assert!(out.timed[2][threshold] <= out.timed[1][threshold]);
                assert!(out.timed[1][threshold] <= out.timed[0][threshold]);
            }
        }
        // A user cap of 20 s keeps S = 20 s and 30 s raw.
        let capped = AnswerTimeRecalibration::fit(&rows, &window, 20_000, config);
        let out = capped.apply(&rows[2_450].raw);
        assert!(capped
            .maps
            .iter()
            .all(|set| set[8].is_none() && set[9].is_none()));
        assert!(capped
            .maps
            .iter()
            .all(|set| set[..8].iter().all(Option::is_some)));
        let _ = out;
    }

    #[test]
    fn rwkv_time_recal_user_cap_follows_the_reference() {
        assert_eq!(answer_time_user_cap_millis([]), 60_000);
        assert_eq!(answer_time_user_cap_millis([0, 0]), 60_000);
        let mut durations = vec![3_000; 3_000];
        durations.extend([20_000; 20]);
        assert_eq!(answer_time_user_cap_millis(durations.clone()), 20_000);
        // Below 0.5% of the recorded times: the default cap.
        durations.extend([3_000; 2_000]);
        assert_eq!(answer_time_user_cap_millis(durations), 60_000);
        assert_eq!(answer_time_user_cap_millis([20_000; 19]), 60_000);
    }

    #[test]
    fn rwkv_time_recal_events_follow_the_head() {
        let mut probabilities = [[0.0; super::super::ANSWER_TIME_BINS]; 4];
        for grade in &mut probabilities {
            grade[5] = 0.25;
            grade[8] = 0.5;
            grade[12] = 0.25;
        }
        let prediction = AnswerTimePrediction {
            probabilities,
            button_probabilities: [0.1, 0.2, 0.6, 0.1],
        };
        let events = TimedEvents::from_prediction(&prediction);
        for (value, expected) in events.grade.iter().zip([0.9, 0.7, 0.1]) {
            assert!((value - expected).abs() < 1e-6);
        }
        for (index, &seconds) in ANSWER_TIME_RECAL_SECONDS.iter().enumerate() {
            let under = prediction
                .probabilities_under_seconds(f64::from(seconds))
                .unwrap();
            let expected =
                0.2 * f64::from(under[1]) + 0.6 * f64::from(under[2]) + 0.1 * f64::from(under[3]);
            assert!((events.timed[0][index] - expected).abs() < 1e-6);
        }
        assert_eq!(
            events.within_seconds(4.0),
            Some([0, 1, 2].map(|s| events.timed[s][2]))
        );
        assert_eq!(events.within_seconds(7.0), None);
    }
}
