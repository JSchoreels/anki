// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use fsrs::CostAdrNextStates;
use fsrs::CostAdrPolicy;
use fsrs::ItemState;
use fsrs::NextStates;

use crate::deckconfig::DeckConfigInner;
use crate::prelude::*;

const COST_ADR_PARAMETER_COUNT: usize = 15;
const COST_WEIGHT_MIN: f32 = 0.0;
const COST_WEIGHT_MAX: f32 = 1024.0;
const RETENTION_MIN: f32 = 0.30;
const RETENTION_MAX: f32 = 0.995;

#[derive(Debug, Clone, PartialEq)]
pub(crate) struct DynamicDesiredRetention {
    policy_params: Vec<f32>,
    calibration: Vec<(f32, f32)>,
    max_interval_days: f32,
}

#[derive(Debug, Clone)]
pub(crate) struct DynamicDesiredRetentionStates {
    pub states: NextStates,
    pub desired_retentions: [f32; 4],
    pub cost_weight: f32,
}

impl DynamicDesiredRetention {
    pub(crate) fn from_deck_config(config: &DeckConfigInner) -> Result<Option<Self>> {
        if !config.fsrs_dynamic_desired_retention_enabled {
            return Ok(None);
        }

        require!(
            config.fsrs_dynamic_desired_retention_params.len() == COST_ADR_PARAMETER_COUNT,
            "Dynamic DR requires 15 SSP-MMC parameters"
        );
        require!(
            config
                .fsrs_dynamic_desired_retention_params
                .iter()
                .all(|value| value.is_finite()),
            "Dynamic DR parameters must be finite"
        );
        require!(
            config.fsrs_dynamic_desired_retention_weights.len()
                == config.fsrs_dynamic_desired_retention_avg_drs.len()
                && config.fsrs_dynamic_desired_retention_weights.len() >= 2,
            "Dynamic DR requires matching weight and average DR calibration arrays"
        );

        let mut calibration = config
            .fsrs_dynamic_desired_retention_weights
            .iter()
            .copied()
            .zip(
                config
                    .fsrs_dynamic_desired_retention_avg_drs
                    .iter()
                    .copied(),
            )
            .collect::<Vec<_>>();
        calibration.sort_by(|a, b| a.0.total_cmp(&b.0));
        require!(
            calibration.iter().all(|(weight, avg_dr)| {
                weight.is_finite()
                    && *weight >= 0.0
                    && avg_dr.is_finite()
                    && (0.0..=1.0).contains(avg_dr)
            }),
            "Dynamic DR calibration values must be finite"
        );

        Ok(Some(Self {
            policy_params: config.fsrs_dynamic_desired_retention_params.clone(),
            calibration,
            max_interval_days: config.maximum_review_interval as f32,
        }))
    }

    pub(crate) fn cost_weight_for_average_dr(&self, target: f32) -> Result<f32> {
        require!(
            target.is_finite() && (0.0..=1.0).contains(&target),
            "Dynamic DR target must be a retention value"
        );

        for pair in self.calibration.windows(2) {
            let (left_weight, left_dr) = pair[0];
            let (right_weight, right_dr) = pair[1];
            if (left_dr - target) * (right_dr - target) > 0.0 {
                continue;
            }
            if (left_dr - right_dr).abs() < f32::EPSILON {
                return Ok(left_weight);
            }
            let t = ((target - left_dr) / (right_dr - left_dr)).clamp(0.0, 1.0);
            let left_log = left_weight.ln_1p();
            let right_log = right_weight.ln_1p();
            return Ok((left_log + (right_log - left_log) * t).exp_m1());
        }

        invalid_input!("Dynamic DR target is outside the calibrated average DR range")
    }

    pub(crate) fn policy(&self) -> Result<CostAdrPolicy> {
        CostAdrPolicy::new_with_settings(
            self.policy_params.clone(),
            COST_WEIGHT_MIN,
            COST_WEIGHT_MAX,
            RETENTION_MIN,
            RETENTION_MAX,
            Some(self.max_interval_days),
        )
        .map_err(Into::into)
    }

    pub(crate) fn next_states(
        &self,
        fsrs: &fsrs::FSRS,
        current_memory_state: Option<fsrs::MemoryState>,
        target_average_dr: f32,
        days_elapsed: f32,
    ) -> Result<DynamicDesiredRetentionStates> {
        let cost_weight = self.cost_weight_for_average_dr(target_average_dr)?;
        let states =
            self.policy()?
                .next_states(fsrs, current_memory_state, cost_weight, days_elapsed)?;
        Ok(dynamic_states_from_cost_adr(states, cost_weight))
    }
}

fn dynamic_states_from_cost_adr(
    states: CostAdrNextStates,
    cost_weight: f32,
) -> DynamicDesiredRetentionStates {
    DynamicDesiredRetentionStates {
        desired_retentions: [
            states.again.desired_retention,
            states.hard.desired_retention,
            states.good.desired_retention,
            states.easy.desired_retention,
        ],
        states: NextStates {
            again: ItemState {
                memory: states.again.memory,
                interval: states.again.interval,
            },
            hard: ItemState {
                memory: states.hard.memory,
                interval: states.hard.interval,
            },
            good: ItemState {
                memory: states.good.memory,
                interval: states.good.interval,
            },
            easy: ItemState {
                memory: states.easy.memory,
                interval: states.easy.interval,
            },
        },
        cost_weight,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::deckconfig::DeckConfig;

    #[test]
    fn disabled_config_has_no_dynamic_dr() -> Result<()> {
        let config = DeckConfig::default();
        assert!(DynamicDesiredRetention::from_deck_config(&config.inner)?.is_none());
        Ok(())
    }

    #[test]
    fn enabled_config_requires_policy_and_calibration() {
        let mut config = DeckConfig::default();
        config.inner.fsrs_dynamic_desired_retention_enabled = true;
        assert!(DynamicDesiredRetention::from_deck_config(&config.inner).is_err());
    }

    #[test]
    fn cost_weight_uses_log_interpolation() -> Result<()> {
        let mut config = DeckConfig::default();
        config.inner.fsrs_dynamic_desired_retention_enabled = true;
        config.inner.fsrs_dynamic_desired_retention_params = vec![0.0; COST_ADR_PARAMETER_COUNT];
        config.inner.fsrs_dynamic_desired_retention_weights = vec![0.0, 15.0];
        config.inner.fsrs_dynamic_desired_retention_avg_drs = vec![0.9, 0.8];

        let dynamic_dr = DynamicDesiredRetention::from_deck_config(&config.inner)?.unwrap();
        assert!((dynamic_dr.cost_weight_for_average_dr(0.85)? - 3.0).abs() < 1e-5);
        Ok(())
    }
}
