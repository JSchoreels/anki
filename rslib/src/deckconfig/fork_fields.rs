// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use serde::Deserialize;
use serde::Serialize;
use serde_json::Map;
use serde_json::Value;

use super::DeckConfigInner;
use super::FsrsVersion;

const FORK_FIELDS_KEY: &str = "jschoreels.fsrs";
const FSRS_MINIMUM_INTERVAL_SECS_DEFAULT: u32 = 1;
const DYNAMIC_DR_MIN_DEFAULT: f32 = 0.30;
const DYNAMIC_DR_MAX_DEFAULT: f32 = 0.995;

#[derive(Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
struct ForkDeckConfigFields {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    fsrs_params_7: Option<Vec<f32>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    fsrs_minimum_interval_secs: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    fsrs_dynamic_desired_retention_enabled: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    fsrs_dynamic_desired_retention_params: Option<Vec<f32>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    fsrs_dynamic_desired_retention_weights: Option<Vec<f32>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    fsrs_dynamic_desired_retention_avg_drs: Option<Vec<f32>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    fsrs_dynamic_desired_retention_fsrs_eq_weights: Option<Vec<f32>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    fsrs_dynamic_desired_retention_fsrs_eq_drs: Option<Vec<f32>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    fsrs_dynamic_desired_retention_min: Option<f32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    fsrs_dynamic_desired_retention_max: Option<f32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    fsrs_dynamic_desired_retention_clamp: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    fsrs_version: Option<i32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    review_fuzz_base: Option<f32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    review_fuzz_factor_short: Option<f32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    review_fuzz_factor_mid: Option<f32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    review_fuzz_factor_long: Option<f32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    review_fuzz_enabled: Option<bool>,
}

impl ForkDeckConfigFields {
    fn from_config(config: &DeckConfigInner) -> Self {
        Self {
            fsrs_params_7: non_empty_vec(&config.fsrs_params_7),
            fsrs_minimum_interval_secs: non_default(
                config.fsrs_minimum_interval_secs,
                FSRS_MINIMUM_INTERVAL_SECS_DEFAULT,
            ),
            fsrs_dynamic_desired_retention_enabled: true_only(
                config.fsrs_dynamic_desired_retention_enabled,
            ),
            fsrs_dynamic_desired_retention_params: non_empty_vec(
                &config.fsrs_dynamic_desired_retention_params,
            ),
            fsrs_dynamic_desired_retention_weights: non_empty_vec(
                &config.fsrs_dynamic_desired_retention_weights,
            ),
            fsrs_dynamic_desired_retention_avg_drs: non_empty_vec(
                &config.fsrs_dynamic_desired_retention_avg_drs,
            ),
            fsrs_dynamic_desired_retention_fsrs_eq_weights: non_empty_vec(
                &config.fsrs_dynamic_desired_retention_fsrs_eq_weights,
            ),
            fsrs_dynamic_desired_retention_fsrs_eq_drs: non_empty_vec(
                &config.fsrs_dynamic_desired_retention_fsrs_eq_drs,
            ),
            fsrs_dynamic_desired_retention_min: non_default_f32(
                config.fsrs_dynamic_desired_retention_min,
                DYNAMIC_DR_MIN_DEFAULT,
            ),
            fsrs_dynamic_desired_retention_max: non_default_f32(
                config.fsrs_dynamic_desired_retention_max,
                DYNAMIC_DR_MAX_DEFAULT,
            ),
            fsrs_dynamic_desired_retention_clamp: true_only(
                config.fsrs_dynamic_desired_retention_clamp,
            ),
            fsrs_version: non_default(config.fsrs_version, FsrsVersion::Seven as i32),
            review_fuzz_base: config.review_fuzz_base,
            review_fuzz_factor_short: config.review_fuzz_factor_short,
            review_fuzz_factor_mid: config.review_fuzz_factor_mid,
            review_fuzz_factor_long: config.review_fuzz_factor_long,
            review_fuzz_enabled: config.review_fuzz_enabled,
        }
    }

    fn apply_to_config(self, config: &mut DeckConfigInner) {
        if let Some(value) = self.fsrs_params_7 {
            config.fsrs_params_7 = value;
        }
        if let Some(value) = self.fsrs_minimum_interval_secs {
            config.fsrs_minimum_interval_secs = value;
        }
        if let Some(value) = self.fsrs_dynamic_desired_retention_enabled {
            config.fsrs_dynamic_desired_retention_enabled = value;
        }
        if let Some(value) = self.fsrs_dynamic_desired_retention_params {
            config.fsrs_dynamic_desired_retention_params = value;
        }
        if let Some(value) = self.fsrs_dynamic_desired_retention_weights {
            config.fsrs_dynamic_desired_retention_weights = value;
        }
        if let Some(value) = self.fsrs_dynamic_desired_retention_avg_drs {
            config.fsrs_dynamic_desired_retention_avg_drs = value;
        }
        if let Some(value) = self.fsrs_dynamic_desired_retention_fsrs_eq_weights {
            config.fsrs_dynamic_desired_retention_fsrs_eq_weights = value;
        }
        if let Some(value) = self.fsrs_dynamic_desired_retention_fsrs_eq_drs {
            config.fsrs_dynamic_desired_retention_fsrs_eq_drs = value;
        }
        if let Some(value) = self.fsrs_dynamic_desired_retention_min {
            config.fsrs_dynamic_desired_retention_min = value;
        }
        if let Some(value) = self.fsrs_dynamic_desired_retention_max {
            config.fsrs_dynamic_desired_retention_max = value;
        }
        if let Some(value) = self.fsrs_dynamic_desired_retention_clamp {
            config.fsrs_dynamic_desired_retention_clamp = value;
        }
        if let Some(value) = self.fsrs_version {
            config.fsrs_version = value;
        }
        if let Some(value) = self.review_fuzz_base {
            config.review_fuzz_base = Some(value);
        }
        if let Some(value) = self.review_fuzz_factor_short {
            config.review_fuzz_factor_short = Some(value);
        }
        if let Some(value) = self.review_fuzz_factor_mid {
            config.review_fuzz_factor_mid = Some(value);
        }
        if let Some(value) = self.review_fuzz_factor_long {
            config.review_fuzz_factor_long = Some(value);
        }
        if let Some(value) = self.review_fuzz_enabled {
            config.review_fuzz_enabled = Some(value);
        }
    }

    fn is_empty(&self) -> bool {
        serde_json::to_value(self)
            .ok()
            .and_then(|value| value.as_object().map(Map::is_empty))
            .unwrap_or(true)
    }
}

pub(crate) fn restore_fork_fields_from_other(config: &mut DeckConfigInner) {
    if let Some(fields) = fork_fields_from_other(&config.other) {
        fields.apply_to_config(config);
    }

    if config.fsrs_minimum_interval_secs == 0 {
        config.fsrs_minimum_interval_secs = FSRS_MINIMUM_INTERVAL_SECS_DEFAULT;
    }
    if config.fsrs_dynamic_desired_retention_min == 0.0 {
        config.fsrs_dynamic_desired_retention_min = DYNAMIC_DR_MIN_DEFAULT;
    }
    if config.fsrs_dynamic_desired_retention_max == 0.0 {
        config.fsrs_dynamic_desired_retention_max = DYNAMIC_DR_MAX_DEFAULT;
    }
}

pub(crate) fn deck_config_inner_for_storage(config: &DeckConfigInner) -> DeckConfigInner {
    let mut storage_config = config.clone();
    let fields = ForkDeckConfigFields::from_config(&storage_config);
    storage_config.other = other_with_fork_fields(&storage_config.other, fields);
    clear_numbered_fork_fields(&mut storage_config);
    storage_config
}

fn fork_fields_from_other(other: &[u8]) -> Option<ForkDeckConfigFields> {
    let value: Value = serde_json::from_slice(other).ok()?;
    serde_json::from_value(value.get(FORK_FIELDS_KEY)?.clone()).ok()
}

fn other_with_fork_fields(other: &[u8], fields: ForkDeckConfigFields) -> Vec<u8> {
    let mut object = serde_json::from_slice::<Value>(other)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .unwrap_or_default();

    if fields.is_empty() {
        object.remove(FORK_FIELDS_KEY);
    } else if let Ok(value) = serde_json::to_value(fields) {
        object.insert(FORK_FIELDS_KEY.to_string(), value);
    }

    if object.is_empty() {
        Vec::new()
    } else {
        serde_json::to_vec(&object).unwrap_or_default()
    }
}

fn clear_numbered_fork_fields(config: &mut DeckConfigInner) {
    config.fsrs_params_7.clear();
    config.fsrs_minimum_interval_secs = 0;
    config.fsrs_dynamic_desired_retention_enabled = false;
    config.fsrs_dynamic_desired_retention_params.clear();
    config.fsrs_dynamic_desired_retention_weights.clear();
    config.fsrs_dynamic_desired_retention_avg_drs.clear();
    config
        .fsrs_dynamic_desired_retention_fsrs_eq_weights
        .clear();
    config.fsrs_dynamic_desired_retention_fsrs_eq_drs.clear();
    config.fsrs_dynamic_desired_retention_min = 0.0;
    config.fsrs_dynamic_desired_retention_max = 0.0;
    config.fsrs_dynamic_desired_retention_clamp = false;
    config.fsrs_version = FsrsVersion::Seven as i32;
    config.review_fuzz_base = None;
    config.review_fuzz_factor_short = None;
    config.review_fuzz_factor_mid = None;
    config.review_fuzz_factor_long = None;
    config.review_fuzz_enabled = None;
}

fn non_empty_vec(values: &[f32]) -> Option<Vec<f32>> {
    (!values.is_empty()).then(|| values.to_vec())
}

fn non_default<T: Copy + PartialEq>(value: T, default: T) -> Option<T> {
    (value != default).then_some(value)
}

fn non_default_f32(value: f32, default: f32) -> Option<f32> {
    ((value - default).abs() > f32::EPSILON).then_some(value)
}

fn true_only(value: bool) -> Option<bool> {
    value.then_some(value)
}

#[cfg(test)]
mod tests {
    use prost::Message;
    use serde_json::Value;

    use super::*;

    fn config_with_fork_fields() -> DeckConfigInner {
        DeckConfigInner {
            fsrs_params_7: vec![0.1; 35],
            fsrs_minimum_interval_secs: 42,
            fsrs_dynamic_desired_retention_enabled: true,
            fsrs_dynamic_desired_retention_params: vec![1.0; 15],
            fsrs_dynamic_desired_retention_weights: vec![0.0, 15.0],
            fsrs_dynamic_desired_retention_avg_drs: vec![0.8, 0.9],
            fsrs_dynamic_desired_retention_fsrs_eq_weights: vec![3.0],
            fsrs_dynamic_desired_retention_fsrs_eq_drs: vec![0.85],
            fsrs_dynamic_desired_retention_min: 0.31,
            fsrs_dynamic_desired_retention_max: 0.96,
            fsrs_dynamic_desired_retention_clamp: true,
            fsrs_version: FsrsVersion::Six as i32,
            review_fuzz_base: Some(1.2),
            review_fuzz_factor_short: Some(0.2),
            review_fuzz_factor_mid: Some(0.1),
            review_fuzz_factor_long: Some(0.05),
            review_fuzz_enabled: Some(false),
            ..Default::default()
        }
    }

    #[test]
    fn storage_clears_numbered_fork_fields() {
        let config = config_with_fork_fields();
        let storage_config = deck_config_inner_for_storage(&config);

        assert!(storage_config.fsrs_params_7.is_empty());
        assert_eq!(storage_config.fsrs_minimum_interval_secs, 0);
        assert!(!storage_config.fsrs_dynamic_desired_retention_enabled);
        assert!(storage_config
            .fsrs_dynamic_desired_retention_params
            .is_empty());
        assert_eq!(storage_config.fsrs_version, FsrsVersion::Seven as i32);
        assert_eq!(storage_config.review_fuzz_base, None);

        let other: Value = serde_json::from_slice(&storage_config.other).unwrap();
        assert!(other.get(FORK_FIELDS_KEY).is_some());
    }

    #[test]
    fn storage_other_restores_fork_fields() {
        let config = config_with_fork_fields();
        let storage_config = deck_config_inner_for_storage(&config);
        let mut decoded =
            DeckConfigInner::decode(storage_config.encode_to_vec().as_slice()).unwrap();

        restore_fork_fields_from_other(&mut decoded);

        assert_eq!(decoded.fsrs_params_7, config.fsrs_params_7);
        assert_eq!(
            decoded.fsrs_dynamic_desired_retention_params,
            config.fsrs_dynamic_desired_retention_params
        );
        assert_eq!(
            decoded.fsrs_dynamic_desired_retention_clamp,
            config.fsrs_dynamic_desired_retention_clamp
        );
        assert_eq!(decoded.fsrs_version, config.fsrs_version);
        assert_eq!(decoded.review_fuzz_base, config.review_fuzz_base);
    }

    #[test]
    fn legacy_numbered_fields_remain_readable() {
        let mut config = config_with_fork_fields();
        restore_fork_fields_from_other(&mut config);

        assert_eq!(config.fsrs_params_7, vec![0.1; 35]);
        assert_eq!(config.fsrs_version, FsrsVersion::Six as i32);
        assert_eq!(config.fsrs_dynamic_desired_retention_min, 0.31);
    }
}
