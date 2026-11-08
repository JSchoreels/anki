// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Approximate strata of aggregate answer outcomes for the fast forecast.

use super::simulation_unit_hash;

pub(super) const PREVIEW_LIMIT: usize = 512;

/// Draw shared day-to-day variation, conditioned approximately on a low,
/// central, or high aggregate outcome over the whole forecast. The weights
/// estimate each day's contribution to answer-count variability.
pub(super) fn day_deviations(counts: &[usize], seed: u32, stratum: usize) -> Vec<f64> {
    let weights = counts
        .iter()
        .map(|n| (*n).max(1) as f64)
        .map(f64::sqrt)
        .collect::<Vec<_>>();
    let norms = weights.iter().map(|w| w * w).sum::<f64>();
    let noise = (0..counts.len())
        .map(|day| {
            let u1 =
                simulation_unit_hash(i64::from(seed), day as i64, 0x1A63).max(f64::MIN_POSITIVE);
            let u2 = simulation_unit_hash(i64::from(seed), day as i64, 0x6DB5);
            (-2.0 * u1.ln()).sqrt() * (std::f64::consts::TAU * u2).cos()
        })
        .collect::<Vec<_>>();
    let projection = weights.iter().zip(&noise).map(|(w, z)| w * z).sum::<f64>() / norms;
    let desired = stratum as f64 - 1.0;
    weights
        .iter()
        .zip(noise)
        .map(|(w, z)| z - w * projection + desired * w / norms.sqrt())
        .collect()
}

/// Approximate sequential conditioning on a target aggregate deviation.
/// The queue continues to use the unconditioned model probabilities.
pub(super) struct AnswerStratum {
    deviation: f64,
    expected_answers: usize,
    preview_variance: f64,
    observed: usize,
    observed_variance: f64,
    residual: f64,
}

impl AnswerStratum {
    pub(super) fn new(deviation: f64, expected_answers: usize, recalls: &[f32]) -> Self {
        let preview_variance = recalls
            .iter()
            .map(|r| {
                let p = f64::from(r.clamp(0.0, 1.0));
                p * (1.0 - p)
            })
            .sum::<f64>()
            / recalls.len().max(1) as f64;
        Self {
            deviation,
            expected_answers,
            preview_variance,
            observed: 0,
            observed_variance: 0.0,
            residual: 0.0,
        }
    }

    /// Conditional probability for one answer. The adjustment changes with
    /// observed outcomes and the remaining work; it is not a fixed R offset.
    pub(super) fn probability(&self, recall: f32, queued: usize) -> f32 {
        let p = f64::from(recall.clamp(0.0, 1.0));
        if p == 0.0 || p == 1.0 {
            return p as f32;
        }
        let remaining = self
            .expected_answers
            .saturating_sub(self.observed)
            .max(queued)
            .max(1) as f64;
        // Retain a small preview prior while adapting to the actual predictions.
        let variance_per_answer =
            (16.0 * self.preview_variance + self.observed_variance) / (16.0 + self.observed as f64);
        let remaining_variance = remaining * variance_per_answer;
        let target = self.deviation * (self.observed_variance + remaining_variance).sqrt();
        let tilt = ((target - self.residual) / remaining_variance.max(1.0)).clamp(-6.0, 6.0);
        (1.0 / (1.0 + (-((p / (1.0 - p)).ln() + tilt)).exp())) as f32
    }

    pub(super) fn observe(&mut self, recall: f32, success: bool) {
        let p = f64::from(recall.clamp(0.0, 1.0));
        self.observed += 1;
        self.observed_variance += p * (1.0 - p);
        self.residual += f64::from(u8::from(success)) - p;
    }

    pub(super) fn log_summary(&self, path: usize, day: usize) {
        let observed_z = if self.observed_variance > 0.0 {
            self.residual / self.observed_variance.sqrt()
        } else {
            0.0
        };
        tracing::debug!(
            path,
            day,
            answers = self.observed,
            expected_answers = self.expected_answers,
            target_z = self.deviation,
            observed_z,
            "RWKV Instant forecast answer stratum"
        );
    }
}

#[cfg(test)]
mod tests {
    use super::super::workload_comparison::paired_grade;
    use super::*;
    use crate::rwkv::RwkvWorkloadReviewModel;

    #[test]
    fn daily_deviations_have_the_requested_weighted_aggregate() {
        let counts = [100, 80, 100, 40, 100, 70, 100];
        let norm = (counts.iter().sum::<usize>() as f64).sqrt();
        let paths = (0..3)
            .map(|stratum| day_deviations(&counts, 42, stratum))
            .collect::<Vec<_>>();
        for (stratum, path) in paths.iter().enumerate() {
            let aggregate = path
                .iter()
                .zip(counts)
                .map(|(z, n)| z * (n as f64).sqrt())
                .sum::<f64>()
                / norm;
            assert!((aggregate - (stratum as f64 - 1.0)).abs() < 1e-10);
        }
        assert_eq!(paths[1], day_deviations(&counts, 42, 1));
        assert!(paths[1].iter().any(|z| *z < 0.0) && paths[1].iter().any(|z| *z > 0.0));
        assert!(paths[0].iter().any(|z| *z > 0.0));
        assert!(paths[2].iter().any(|z| *z < 0.0));
    }

    #[test]
    fn conditions_mixed_answers_on_their_aggregate_residual() {
        let recalls = (0..200)
            .map(|i| if i % 2 == 0 { 0.8 } else { 0.6 })
            .collect::<Vec<_>>();
        let model = RwkvWorkloadReviewModel {
            grade_seconds: [1.0; 4],
            bucket_probabilities: (0..20).map(|b| (b, [0.0, 0.0, 1.0, 0.0])).collect(),
        };
        let outcomes = [-1.0, 0.0, 1.0].map(|z| {
            let mut stratum = AnswerStratum::new(z, recalls.len(), &recalls);
            let mut successes = 0;
            for (i, &r) in recalls.iter().enumerate() {
                let p = stratum.probability(r, recalls.len() - i);
                let success = paired_grade(&model, p, i as i64, 0, 42) != 1;
                successes += usize::from(success);
                stratum.observe(r, success);
            }
            (
                stratum.residual / stratum.observed_variance.sqrt(),
                successes,
            )
        });
        assert!(
            outcomes[0].0 < outcomes[1].0 && outcomes[1].0 < outcomes[2].0,
            "{outcomes:?}"
        );
        for (index, (z, successes)) in outcomes.iter().enumerate() {
            assert!((z - (index as f64 - 1.0)).abs() < 0.6, "{outcomes:?}");
            assert!((1..recalls.len()).contains(successes));
        }
        let sampler = AnswerStratum::new(1.0, 2, &[0.0, 1.0]);
        assert_eq!(sampler.probability(0.0, 2), 0.0);
        assert_eq!(sampler.probability(1.0, 2), 1.0);
    }
}
