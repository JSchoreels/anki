// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::collections::HashMap;
use std::collections::HashSet;
use std::time::Instant;

use fsrs::DEFAULT_PARAMETERS;
use fsrs::FSRS;
use serde::Deserialize;
use serde::Serialize;

use crate::card::Card;
use crate::deckconfig::DeckConfig;
use crate::deckconfig::DeckConfigId;
use crate::deckconfig::FsrsVersion;
use crate::decks::Deck;
use crate::prelude::*;
use crate::scheduler::fsrs::dynamic_desired_retention::DynamicDesiredRetention;
use crate::scheduler::fsrs::dynamic_desired_retention::DynamicDesiredRetentionFields;
use crate::scheduler::fsrs::params::ignore_revlogs_before_date_to_ms;
use crate::search::FieldSearchMode;
use crate::search::Node;
use crate::search::PropertyKind;
use crate::search::SearchNode;
use crate::search::SortMode;
use crate::search::TryIntoSearch;

pub(crate) const FSRS_PRESET_OVERLAY_CONFIG_KEY: &str = "fsrsPresetOverlay";
const OUTDATED_FSRS7_PREVIEW_PARAM_COUNT: usize = 35;

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub(crate) enum FsrsPresetId {
    DeckConfig(DeckConfigId),
    Addon(String),
}

#[derive(Debug, Clone)]
pub(crate) struct FsrsPreset {
    pub id: FsrsPresetId,
    pub name: String,
    pub fsrs_version: FsrsVersion,
    pub params: Vec<f32>,
    pub desired_retention: f32,
    pub dynamic_desired_retention: Option<DynamicDesiredRetention>,
    pub historical_retention: f32,
    pub ignore_revlogs_before_date: String,
}

#[derive(Debug, Clone, Default)]
pub(crate) struct FsrsPresetOverlayCache {
    presets: HashMap<String, FsrsPreset>,
    rules: Vec<ResolvedFsrsPresetRule>,
    simulator_rules: Vec<FsrsPresetSimulatorRule>,
    card_to_preset: HashMap<CardId, String>,
    cards_without_preset: HashSet<CardId>,
}

#[derive(Debug, Clone)]
struct ResolvedFsrsPresetRule {
    preset_id: String,
    search: String,
    node: Node,
}

impl FsrsPresetOverlayCache {
    pub(crate) fn clear_card_matches(&mut self) {
        self.card_to_preset.clear();
        self.cards_without_preset.clear();
    }
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub(crate) struct FsrsPresetOverlay {
    #[serde(default)]
    pub presets: Vec<AddonFsrsPreset>,
    #[serde(default)]
    pub rules: Vec<FsrsPresetRule>,
    #[serde(default)]
    pub simulator_rules: Vec<FsrsPresetSimulatorRule>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub(crate) struct AddonFsrsPreset {
    pub id: String,
    pub name: String,
    #[serde(default)]
    pub fsrs_version: AddonFsrsVersion,
    pub params: Vec<f32>,
    pub desired_retention: f32,
    pub historical_retention: f32,
    #[serde(default)]
    pub ignore_revlogs_before_date: String,
    #[serde(default)]
    pub fsrs_dynamic_desired_retention_enabled: bool,
    #[serde(default)]
    pub fsrs_dynamic_desired_retention_params: Vec<f32>,
    #[serde(default)]
    pub fsrs_dynamic_desired_retention_weights: Vec<f32>,
    #[serde(default)]
    pub fsrs_dynamic_desired_retention_avg_drs: Vec<f32>,
    #[serde(default)]
    pub fsrs_dynamic_desired_retention_fsrs_eq_weights: Vec<f32>,
    #[serde(default)]
    pub fsrs_dynamic_desired_retention_fsrs_eq_drs: Vec<f32>,
    #[serde(default)]
    pub fsrs_dynamic_desired_retention_fixed_target_weights: Vec<f32>,
    #[serde(default)]
    pub fsrs_dynamic_desired_retention_fixed_target_drs: Vec<f32>,
    #[serde(default)]
    pub fsrs_dynamic_desired_retention_min: f32,
    #[serde(default)]
    pub fsrs_dynamic_desired_retention_max: f32,
    #[serde(default)]
    pub fsrs_dynamic_desired_retention_clamp: bool,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum AddonFsrsVersion {
    #[default]
    Seven,
    Six,
    Five,
    Four,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct FsrsPresetRule {
    pub search: String,
    pub preset_id: String,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub(crate) struct FsrsPresetSimulatorRule {
    pub preset_id: String,
    #[serde(default)]
    pub search: Option<String>,
    #[serde(default)]
    pub min_reps: Option<u32>,
    #[serde(default)]
    pub max_reps: Option<u32>,
    #[serde(default)]
    pub min_interval_days: Option<f32>,
    #[serde(default)]
    pub max_interval_days: Option<f32>,
}

impl FsrsPreset {
    pub(crate) fn from_deck_config(config: &DeckConfig, deck: &Deck) -> Result<Self> {
        let fsrs_version =
            FsrsVersion::try_from(config.inner.fsrs_version).unwrap_or(FsrsVersion::Seven);
        let dynamic_desired_retention = if fsrs_version == FsrsVersion::Seven {
            DynamicDesiredRetention::from_deck_config(&config.inner)?
        } else {
            None
        };
        Ok(Self {
            id: FsrsPresetId::DeckConfig(config.id),
            name: config.name.clone(),
            fsrs_version,
            params: config.fsrs_params().to_vec(),
            desired_retention: deck.effective_desired_retention(config),
            dynamic_desired_retention,
            historical_retention: config.inner.historical_retention,
            ignore_revlogs_before_date: config.inner.ignore_revlogs_before_date.clone(),
        })
    }

    pub(crate) fn fsrs(&self) -> Result<FSRS> {
        Ok(FSRS::new(&self.params)?)
    }

    pub(crate) fn ignore_revlogs_before_ms(&self) -> Result<TimestampMillis> {
        ignore_revlogs_before_date_to_ms(&self.ignore_revlogs_before_date)
    }
}

impl AddonFsrsVersion {
    fn into_fsrs_version(self) -> FsrsVersion {
        match self {
            AddonFsrsVersion::Seven => FsrsVersion::Seven,
            AddonFsrsVersion::Six => FsrsVersion::Six,
            AddonFsrsVersion::Five => FsrsVersion::Five,
            AddonFsrsVersion::Four => FsrsVersion::Four,
        }
    }
}

impl AddonFsrsPreset {
    fn into_fsrs_preset(self) -> Result<FsrsPreset> {
        require!(
            self.id.starts_with("addon:"),
            "add-on FSRS preset id must start with addon:"
        );
        let fsrs_version = self.fsrs_version.into_fsrs_version();
        let params = if fsrs_version == FsrsVersion::Seven
            && self.params.len() == OUTDATED_FSRS7_PREVIEW_PARAM_COUNT
        {
            tracing::warn!(
                preset_id = %self.id,
                "ignored outdated 35-parameter FSRS-7 add-on preset overlay params"
            );
            DEFAULT_PARAMETERS.to_vec()
        } else {
            self.params
        };
        FSRS::new(&params)?;
        let dynamic_desired_retention = if fsrs_version == FsrsVersion::Seven
            && self.fsrs_dynamic_desired_retention_enabled
        {
            Some(DynamicDesiredRetention::from_fields(
                DynamicDesiredRetentionFields {
                    policy_params: self.fsrs_dynamic_desired_retention_params,
                    calibration_weights: self.fsrs_dynamic_desired_retention_weights,
                    calibration_avg_drs: self.fsrs_dynamic_desired_retention_avg_drs,
                    fsrs_equivalent_weights: self.fsrs_dynamic_desired_retention_fsrs_eq_weights,
                    fsrs_equivalent_drs: self.fsrs_dynamic_desired_retention_fsrs_eq_drs,
                    fixed_target_weights: self.fsrs_dynamic_desired_retention_fixed_target_weights,
                    fixed_target_drs: self.fsrs_dynamic_desired_retention_fixed_target_drs,
                    retention_min: self.fsrs_dynamic_desired_retention_min,
                    retention_max: self.fsrs_dynamic_desired_retention_max,
                    clamp_target: self.fsrs_dynamic_desired_retention_clamp,
                    max_interval_days: None,
                },
            )?)
        } else {
            None
        };
        Ok(FsrsPreset {
            id: FsrsPresetId::Addon(self.id),
            name: self.name,
            fsrs_version,
            params,
            desired_retention: self.desired_retention,
            dynamic_desired_retention,
            historical_retention: self.historical_retention,
            ignore_revlogs_before_date: self.ignore_revlogs_before_date,
        })
    }
}

fn node_uses_exact_fsrs_metric(node: &Node) -> bool {
    match node {
        Node::Not(inner) => node_uses_exact_fsrs_metric(inner),
        Node::Group(nodes) => nodes.iter().any(node_uses_exact_fsrs_metric),
        Node::Search(SearchNode::Property {
            kind:
                PropertyKind::Retrievability(_)
                | PropertyKind::Stability(_)
                | PropertyKind::Difficulty(_),
            ..
        }) => true,
        _ => false,
    }
}

fn node_uses_first_grade(node: &Node) -> bool {
    match node {
        Node::Not(inner) => node_uses_first_grade(inner),
        Node::Group(nodes) => nodes.iter().any(node_uses_first_grade),
        Node::Search(SearchNode::FirstGrade(_)) => true,
        _ => false,
    }
}

fn node_uses_regex(node: &Node) -> bool {
    match node {
        Node::Not(inner) => node_uses_regex(inner),
        Node::Group(nodes) => nodes.iter().any(node_uses_regex),
        Node::Search(
            SearchNode::Regex(_)
            | SearchNode::Tag {
                mode: FieldSearchMode::Regex,
                ..
            }
            | SearchNode::SingleField {
                mode: FieldSearchMode::Regex,
                ..
            },
        ) => true,
        _ => false,
    }
}

impl Collection {
    pub(crate) fn fsrs_presets_for_cards(
        &mut self,
        cards: &[Card],
    ) -> Result<HashMap<CardId, FsrsPreset>> {
        let start = Instant::now();
        let mut presets_by_card = self.fsrs_overlay_presets_for_cards(cards)?;
        let overlay_matches = presets_by_card.len();
        let fallback_start = Instant::now();
        let decks_by_id = self.storage.get_decks_map()?;
        let configs_by_id = self.storage.get_deck_config_map()?;

        for card in cards {
            if presets_by_card.contains_key(&card.id) {
                continue;
            }
            let deck_id = card.original_deck_id.or(card.deck_id);
            let deck = decks_by_id.get(&deck_id).or_not_found(deck_id)?;
            let config_id = deck.config_id().or_invalid("home deck is filtered")?;
            let config = configs_by_id.get(&config_id).or_not_found(config_id)?;
            presets_by_card.insert(card.id, FsrsPreset::from_deck_config(config, deck)?);
        }

        tracing::debug!(
            cards = cards.len(),
            overlay_matches,
            fallback_matches = cards.len() - overlay_matches,
            fallback_elapsed_ms = fallback_start.elapsed().as_secs_f64() * 1000.0,
            elapsed_ms = start.elapsed().as_secs_f64() * 1000.0,
            "resolved FSRS presets for card batch"
        );

        Ok(presets_by_card)
    }

    pub(crate) fn fsrs_preset_for_card(&mut self, card: &Card) -> Result<FsrsPreset> {
        self.fsrs_preset_overlay_cache()?;
        if let Some(preset) = self
            .state
            .fsrs_preset_overlay_cache
            .as_ref()
            .and_then(|cache| cache.card_to_preset.get(&card.id))
            .and_then(|preset_id| {
                self.state
                    .fsrs_preset_overlay_cache
                    .as_ref()
                    .and_then(|cache| cache.presets.get(preset_id))
            })
        {
            return Ok(preset.clone());
        }

        let no_overlay_match = self
            .state
            .fsrs_preset_overlay_cache
            .as_ref()
            .map(|cache| cache.cards_without_preset.contains(&card.id))
            .unwrap_or_default();
        if !no_overlay_match {
            let start = Instant::now();
            let rules = self
                .state
                .fsrs_preset_overlay_cache
                .as_ref()
                .map(|cache| cache.rules.clone())
                .unwrap_or_default();
            for rule in rules {
                if self.fsrs_preset_rule_matches_card(card.id, rule.node)? {
                    let preset = self
                        .state
                        .fsrs_preset_overlay_cache
                        .as_ref()
                        .and_then(|cache| cache.presets.get(&rule.preset_id))
                        .or_invalid("FSRS preset rule references an unknown preset")?
                        .clone();
                    if let Some(cache) = self.state.fsrs_preset_overlay_cache.as_mut() {
                        cache.card_to_preset.insert(card.id, rule.preset_id);
                    }
                    tracing::debug!(
                        card_id = card.id.0,
                        preset_id = ?preset.id,
                        elapsed_ms = start.elapsed().as_secs_f64() * 1000.0,
                        "resolved FSRS preset overlay rule for card"
                    );
                    return Ok(preset);
                }
            }
            if let Some(cache) = self.state.fsrs_preset_overlay_cache.as_mut() {
                if !cache.rules.is_empty() {
                    cache.cards_without_preset.insert(card.id);
                }
            }
            if !self
                .state
                .fsrs_preset_overlay_cache
                .as_ref()
                .map(|cache| cache.rules.is_empty())
                .unwrap_or(true)
            {
                tracing::debug!(
                    card_id = card.id.0,
                    elapsed_ms = start.elapsed().as_secs_f64() * 1000.0,
                    "no FSRS preset overlay rule matched card"
                );
            }
        }

        let deck_id = card.original_deck_id.or(card.deck_id);
        let deck = self.storage.get_deck(deck_id)?.or_not_found(deck_id)?;
        self.fsrs_preset_for_deck(&deck)
    }

    pub(crate) fn fsrs_preset_for_deck(&mut self, deck: &Deck) -> Result<FsrsPreset> {
        let config_id = deck.config_id().or_invalid("home deck is filtered")?;
        let config = self
            .storage
            .get_deck_config(config_id)?
            .or_not_found(config_id)?;
        FsrsPreset::from_deck_config(&config, deck)
    }

    pub(crate) fn fsrs_preset_simulator_rules(
        &mut self,
    ) -> Result<Vec<(FsrsPresetSimulatorRule, FsrsPreset)>> {
        let cache = self.fsrs_preset_overlay_cache()?;
        cache
            .simulator_rules
            .iter()
            .map(|rule| {
                let preset = cache
                    .presets
                    .get(&rule.preset_id)
                    .or_invalid("FSRS simulator preset rule references an unknown preset")?
                    .clone();
                Ok((rule.clone(), preset))
            })
            .collect()
    }

    fn fsrs_preset_overlay_cache(&mut self) -> Result<&FsrsPresetOverlayCache> {
        if self.state.fsrs_preset_overlay_cache.is_none() {
            let start = Instant::now();
            let cache = self.build_fsrs_preset_overlay_cache()?;
            tracing::debug!(
                presets = cache.presets.len(),
                rules = cache.rules.len(),
                elapsed_ms = start.elapsed().as_secs_f64() * 1000.0,
                "built FSRS preset overlay rule cache"
            );
            self.state.fsrs_preset_overlay_cache = Some(cache);
        }
        Ok(self.state.fsrs_preset_overlay_cache.as_ref().unwrap())
    }

    fn fsrs_preset_rule_matches_card(&mut self, card_id: CardId, rule_node: Node) -> Result<bool> {
        let node = Node::Group(vec![
            Node::Search(SearchNode::CardIds(card_id.to_string())),
            Node::And,
            rule_node,
        ]);
        Ok(!self.search_cards(node, SortMode::NoOrder)?.is_empty())
    }

    fn build_fsrs_preset_overlay_cache(&mut self) -> Result<FsrsPresetOverlayCache> {
        let Some(overlay) =
            self.get_config_optional::<FsrsPresetOverlay, _>(FSRS_PRESET_OVERLAY_CONFIG_KEY)
        else {
            return Ok(FsrsPresetOverlayCache::default());
        };

        self.build_fsrs_preset_overlay_cache_from_overlay(overlay)
    }

    fn fsrs_overlay_presets_for_cards(
        &mut self,
        cards: &[Card],
    ) -> Result<HashMap<CardId, FsrsPreset>> {
        let cache = self.fsrs_preset_overlay_cache()?;
        if cache.rules.is_empty() || cards.is_empty() {
            return Ok(HashMap::new());
        }

        let start = Instant::now();
        let presets = cache.presets.clone();
        let rules = cache.rules.clone();
        let uses_first_grade = rules.iter().any(|rule| node_uses_first_grade(&rule.node));
        let mut presets_by_card = HashMap::new();
        let mut unresolved = HashSet::new();
        let mut cached_non_matches = 0;

        for card in cards {
            if let Some(preset_id) = cache.card_to_preset.get(&card.id) {
                let preset = presets
                    .get(preset_id)
                    .or_invalid("FSRS preset rule references an unknown preset")?
                    .clone();
                presets_by_card.insert(card.id, preset);
            } else if cache.cards_without_preset.contains(&card.id) {
                cached_non_matches += 1;
            } else {
                unresolved.insert(card.id);
            }
        }
        tracing::debug!(
            cards = cards.len(),
            cached_matches = presets_by_card.len(),
            cached_non_matches,
            unresolved = unresolved.len(),
            uses_first_grade,
            elapsed_ms = start.elapsed().as_secs_f64() * 1000.0,
            "checked cached FSRS preset overlay card matches"
        );

        if !unresolved.is_empty() {
            let table_start = Instant::now();
            let unresolved_cards: Vec<CardId> = unresolved.iter().copied().collect();
            self.storage.setup_fsrs_preset_search_cards_table()?;
            self.storage
                .set_fsrs_preset_search_table_to_card_ids(&unresolved_cards)?;
            tracing::debug!(
                cards = unresolved_cards.len(),
                elapsed_ms = table_start.elapsed().as_secs_f64() * 1000.0,
                "built FSRS preset unresolved card table"
            );
            if uses_first_grade {
                let first_grade_start = Instant::now();
                self.storage.setup_fsrs_preset_first_grades_table()?;
                tracing::debug!(
                    cards = unresolved_cards.len(),
                    elapsed_ms = first_grade_start.elapsed().as_secs_f64() * 1000.0,
                    "built FSRS preset first-grade table"
                );
            }
        }

        let mut cache_updates = Vec::new();
        let mut evaluated_rule_searches = HashSet::new();
        for (rule_index, rule) in rules.into_iter().enumerate() {
            if unresolved.is_empty() {
                break;
            }
            let rule_uses_regex = node_uses_regex(&rule.node);
            let rule_search = rule.search;
            let rule_preset_id = rule.preset_id;
            if !evaluated_rule_searches.insert(rule_search.clone()) {
                tracing::debug!(
                    rule_index,
                    preset_id = rule_preset_id,
                    search = rule_search,
                    uses_regex = rule_uses_regex,
                    matched = 0,
                    remaining = unresolved.len(),
                    "skipped duplicate FSRS preset overlay rule search for card batch"
                );
                continue;
            }
            let preset = presets
                .get(&rule_preset_id)
                .or_invalid("FSRS preset rule references an unknown preset")?
                .clone();
            let mut matched_card_ids = Vec::new();
            let rule_start = Instant::now();
            for card_id in
                self.search_cards_in_fsrs_preset_search_table(rule.node, uses_first_grade)?
            {
                if unresolved.remove(&card_id) {
                    presets_by_card.insert(card_id, preset.clone());
                    cache_updates.push((card_id, rule_preset_id.clone()));
                    matched_card_ids.push(card_id);
                }
            }
            self.storage
                .remove_fsrs_preset_search_table_card_ids(&matched_card_ids)?;
            tracing::debug!(
                rule_index,
                preset_id = rule_preset_id,
                search = rule_search,
                uses_regex = rule_uses_regex,
                matched = matched_card_ids.len(),
                remaining = unresolved.len(),
                elapsed_ms = rule_start.elapsed().as_secs_f64() * 1000.0,
                "resolved FSRS preset overlay rule for card batch"
            );
        }
        if uses_first_grade {
            self.storage.clear_fsrs_preset_first_grades_table()?;
        }
        self.storage.clear_fsrs_preset_search_cards_table()?;

        if let Some(cache) = self.state.fsrs_preset_overlay_cache.as_mut() {
            cache.card_to_preset.extend(cache_updates);
            cache.cards_without_preset.extend(unresolved);
        }
        tracing::debug!(
            cards = cards.len(),
            matched = presets_by_card.len(),
            elapsed_ms = start.elapsed().as_secs_f64() * 1000.0,
            "resolved FSRS preset overlay rules for card batch"
        );
        Ok(presets_by_card)
    }

    pub(crate) fn validate_fsrs_preset_overlay_json(&mut self, value: &[u8]) -> Result<()> {
        let overlay: FsrsPresetOverlay = serde_json::from_slice(value)?;
        self.build_fsrs_preset_overlay_cache_from_overlay(overlay)?;
        Ok(())
    }

    fn build_fsrs_preset_overlay_cache_from_overlay(
        &mut self,
        overlay: FsrsPresetOverlay,
    ) -> Result<FsrsPresetOverlayCache> {
        let mut presets = HashMap::new();
        for preset in overlay.presets {
            let id = preset.id.clone();
            let preset = preset.into_fsrs_preset()?;
            presets.insert(id, preset);
        }

        let mut rules = Vec::new();
        for rule in overlay.rules {
            require!(
                presets.contains_key(&rule.preset_id),
                "FSRS preset rule references an unknown preset"
            );
            let node = rule.search.try_into_search()?;
            require!(
                !node_uses_exact_fsrs_metric(&node),
                "FSRS preset rule searches must not use prop:r, prop:s, or prop:d"
            );
            rules.push(ResolvedFsrsPresetRule {
                preset_id: rule.preset_id,
                search: rule.search,
                node,
            });
        }
        for rule in &overlay.simulator_rules {
            require!(
                presets.contains_key(&rule.preset_id),
                "FSRS simulator preset rule references an unknown preset"
            );
            if let Some(search) = rule.search.as_ref() {
                let node = search.try_into_search()?;
                require!(
                    !node_uses_exact_fsrs_metric(&node),
                    "FSRS simulator preset rule searches must not use prop:r, prop:s, or prop:d"
                );
            }
            require!(
                rule.min_reps
                    .zip(rule.max_reps)
                    .map_or(true, |(min, max)| min <= max),
                "FSRS simulator preset rule has invalid rep bounds"
            );
            require!(
                rule.min_interval_days
                    .into_iter()
                    .chain(rule.max_interval_days)
                    .all(|value| value.is_finite()),
                "FSRS simulator preset rule has invalid interval bounds"
            );
            require!(
                rule.min_interval_days
                    .zip(rule.max_interval_days)
                    .map_or(true, |(min, max)| min <= max),
                "FSRS simulator preset rule has invalid interval bounds"
            );
        }

        Ok(FsrsPresetOverlayCache {
            presets,
            rules,
            simulator_rules: overlay.simulator_rules,
            card_to_preset: HashMap::new(),
            cards_without_preset: HashSet::new(),
        })
    }
}

#[cfg(test)]
mod test {
    use fsrs::FSRS6_DEFAULT_PARAMETERS;

    use super::*;
    use crate::card::CardQueue;
    use crate::card::CardType;
    use crate::card::FsrsMemoryState;
    use crate::deckconfig::DeckConfigId;
    use crate::scheduler::fsrs::memory_state::fsrs_current_retrievability_for_params;
    use crate::tests::NoteAdder;

    #[test]
    fn fsrs_preset_is_derived_from_deck_config() -> Result<()> {
        let mut col = Collection::new();
        let params = vec![2.0; 21];
        col.update_default_deck_config(|config| {
            config.fsrs_version = FsrsVersion::Six as i32;
            config.fsrs_params_6 = params.clone();
            config.desired_retention = 0.82;
            config.historical_retention = 0.73;
            config.ignore_revlogs_before_date = "2024-01-02".into();
        });
        NoteAdder::basic(&mut col).add(&mut col);

        let card = col.get_first_card();
        let preset = col.fsrs_preset_for_card(&card)?;

        assert_eq!(preset.id, FsrsPresetId::DeckConfig(DeckConfigId(1)));
        assert_eq!(preset.fsrs_version, FsrsVersion::Six);
        assert_eq!(preset.params, params);
        assert_eq!(preset.desired_retention, 0.82);
        assert_eq!(preset.historical_retention, 0.73);
        assert_eq!(preset.ignore_revlogs_before_date, "2024-01-02");
        Ok(())
    }

    #[test]
    fn dynamic_desired_retention_is_fsrs7_only() -> Result<()> {
        let mut col = Collection::new();
        col.update_default_deck_config(|config| {
            config.fsrs_version = FsrsVersion::Six as i32;
            config.fsrs_dynamic_desired_retention_enabled = true;
            config.fsrs_dynamic_desired_retention_params = vec![0.0; 15];
            config.fsrs_dynamic_desired_retention_weights = vec![0.0, 15.0];
            config.fsrs_dynamic_desired_retention_avg_drs = vec![0.9, 0.8];
            config.fsrs_dynamic_desired_retention_min = 0.75;
            config.fsrs_dynamic_desired_retention_max = 0.95;
        });
        NoteAdder::basic(&mut col).add(&mut col);

        let card = col.get_first_card();
        let preset = col.fsrs_preset_for_card(&card)?;

        assert_eq!(preset.fsrs_version, FsrsVersion::Six);
        assert!(preset.dynamic_desired_retention.is_none());
        Ok(())
    }

    #[test]
    fn addon_fsrs_preset_uses_dynamic_desired_retention_fields() -> Result<()> {
        let preset = AddonFsrsPreset {
            id: "addon:test:dynamic-dr".into(),
            name: "Dynamic DR".into(),
            fsrs_version: AddonFsrsVersion::Seven,
            params: vec![2.0; 34],
            desired_retention: 0.82,
            historical_retention: 0.72,
            ignore_revlogs_before_date: String::new(),
            fsrs_dynamic_desired_retention_enabled: true,
            fsrs_dynamic_desired_retention_params: vec![0.0; 15],
            fsrs_dynamic_desired_retention_weights: vec![0.0, 15.0],
            fsrs_dynamic_desired_retention_avg_drs: vec![0.9, 0.8],
            fsrs_dynamic_desired_retention_fsrs_eq_weights: vec![0.0, 15.0],
            fsrs_dynamic_desired_retention_fsrs_eq_drs: vec![0.91, 0.82],
            fsrs_dynamic_desired_retention_fixed_target_weights: vec![64.0, 16.0],
            fsrs_dynamic_desired_retention_fixed_target_drs: vec![0.8, 0.9],
            fsrs_dynamic_desired_retention_min: 0.3,
            fsrs_dynamic_desired_retention_max: 0.995,
            fsrs_dynamic_desired_retention_clamp: false,
        }
        .into_fsrs_preset()?;

        let dynamic_dr = preset.dynamic_desired_retention.unwrap();
        assert_eq!(
            dynamic_dr.fixed_target_calibration(),
            &[(64.0, 0.8), (16.0, 0.9)]
        );
        Ok(())
    }

    #[test]
    fn fsrs_preset_overlay_exposes_simulator_rules() -> Result<()> {
        let mut col = Collection::new();
        col.set_config(
            FSRS_PRESET_OVERLAY_CONFIG_KEY,
            &FsrsPresetOverlay {
                presets: vec![AddonFsrsPreset {
                    id: "addon:test:rep-route".into(),
                    name: "Rep Route".into(),
                    fsrs_version: AddonFsrsVersion::Six,
                    params: vec![1.0; 21],
                    desired_retention: 0.81,
                    historical_retention: 0.71,
                    ignore_revlogs_before_date: String::new(),
                    ..Default::default()
                }],
                rules: Vec::new(),
                simulator_rules: vec![FsrsPresetSimulatorRule {
                    preset_id: "addon:test:rep-route".into(),
                    search: Some("tag:medical".into()),
                    min_reps: Some(3),
                    max_reps: Some(7),
                    min_interval_days: Some(10.0),
                    max_interval_days: Some(30.0),
                }],
            },
        )?;

        let routes = col.fsrs_preset_simulator_rules()?;
        assert_eq!(routes.len(), 1);
        assert_eq!(routes[0].0.search.as_deref(), Some("tag:medical"));
        assert_eq!(routes[0].0.min_reps, Some(3));
        assert_eq!(routes[0].0.max_reps, Some(7));
        assert_eq!(routes[0].0.min_interval_days, Some(10.0));
        assert_eq!(routes[0].0.max_interval_days, Some(30.0));
        assert_eq!(
            routes[0].1.id,
            FsrsPresetId::Addon("addon:test:rep-route".into())
        );
        Ok(())
    }

    #[test]
    fn fsrs_preset_overlay_rejects_invalid_simulator_rule_bounds() {
        let mut col = Collection::new();
        let result = col.build_fsrs_preset_overlay_cache_from_overlay(FsrsPresetOverlay {
            presets: vec![AddonFsrsPreset {
                id: "addon:test:rep-route".into(),
                name: "Rep Route".into(),
                fsrs_version: AddonFsrsVersion::Six,
                params: vec![1.0; 21],
                desired_retention: 0.81,
                historical_retention: 0.71,
                ignore_revlogs_before_date: String::new(),
                ..Default::default()
            }],
            rules: Vec::new(),
            simulator_rules: vec![FsrsPresetSimulatorRule {
                preset_id: "addon:test:rep-route".into(),
                min_reps: Some(8),
                max_reps: Some(7),
                ..Default::default()
            }],
        });

        assert!(result.is_err());
    }

    #[test]
    fn fsrs_preset_overlay_rejects_invalid_simulator_rule_interval_bounds() {
        let mut col = Collection::new();
        let result = col.build_fsrs_preset_overlay_cache_from_overlay(FsrsPresetOverlay {
            presets: vec![AddonFsrsPreset {
                id: "addon:test:interval-route".into(),
                name: "Interval Route".into(),
                fsrs_version: AddonFsrsVersion::Six,
                params: vec![1.0; 21],
                desired_retention: 0.81,
                historical_retention: 0.71,
                ignore_revlogs_before_date: String::new(),
                ..Default::default()
            }],
            rules: Vec::new(),
            simulator_rules: vec![FsrsPresetSimulatorRule {
                preset_id: "addon:test:interval-route".into(),
                min_interval_days: Some(30.0),
                max_interval_days: Some(10.0),
                ..Default::default()
            }],
        });

        assert!(result.is_err());
    }

    #[test]
    fn fsrs_preset_overlay_rejects_invalid_simulator_rule_search() {
        let mut col = Collection::new();
        let result = col.build_fsrs_preset_overlay_cache_from_overlay(FsrsPresetOverlay {
            presets: vec![AddonFsrsPreset {
                id: "addon:test:route".into(),
                name: "Route".into(),
                fsrs_version: AddonFsrsVersion::Six,
                params: vec![1.0; 21],
                desired_retention: 0.81,
                historical_retention: 0.71,
                ignore_revlogs_before_date: String::new(),
                ..Default::default()
            }],
            rules: Vec::new(),
            simulator_rules: vec![FsrsPresetSimulatorRule {
                preset_id: "addon:test:route".into(),
                search: Some("prop:r>0.9".into()),
                ..Default::default()
            }],
        });

        assert!(result.is_err());
    }

    #[test]
    fn fsrs_preset_overlay_uses_first_matching_rule() -> Result<()> {
        let mut col = Collection::new();
        NoteAdder::basic(&mut col)
            .fields(&["front", "back"])
            .add(&mut col);
        let card = col.get_first_card();
        col.set_config(
            FSRS_PRESET_OVERLAY_CONFIG_KEY,
            &FsrsPresetOverlay {
                presets: vec![
                    AddonFsrsPreset {
                        id: "addon:test:first".into(),
                        name: "First".into(),
                        fsrs_version: AddonFsrsVersion::Six,
                        params: vec![1.0; 21],
                        desired_retention: 0.81,
                        historical_retention: 0.71,
                        ignore_revlogs_before_date: String::new(),
                        ..Default::default()
                    },
                    AddonFsrsPreset {
                        id: "addon:test:second".into(),
                        name: "Second".into(),
                        fsrs_version: AddonFsrsVersion::Seven,
                        params: vec![2.0; 34],
                        desired_retention: 0.82,
                        historical_retention: 0.72,
                        ignore_revlogs_before_date: String::new(),
                        ..Default::default()
                    },
                ],
                rules: vec![
                    FsrsPresetRule {
                        search: "front".into(),
                        preset_id: "addon:test:first".into(),
                    },
                    FsrsPresetRule {
                        search: "front".into(),
                        preset_id: "addon:test:second".into(),
                    },
                ],
                simulator_rules: Vec::new(),
            },
        )?;

        let preset = col.fsrs_preset_for_card(&card)?;

        assert_eq!(preset.id, FsrsPresetId::Addon("addon:test:first".into()));
        assert_eq!(preset.name, "First");
        assert_eq!(preset.fsrs_version, FsrsVersion::Six);
        assert_eq!(preset.params, vec![1.0; 21]);
        assert_eq!(preset.desired_retention, 0.81);
        assert_eq!(preset.historical_retention, 0.71);
        Ok(())
    }

    #[test]
    fn fsrs_preset_overlay_replaces_outdated_fsrs7_preview_params() -> Result<()> {
        let mut col = Collection::new();
        NoteAdder::basic(&mut col)
            .fields(&["front", "back"])
            .add(&mut col);
        let card = col.get_first_card();

        col.set_config(
            FSRS_PRESET_OVERLAY_CONFIG_KEY,
            &FsrsPresetOverlay {
                presets: vec![AddonFsrsPreset {
                    id: "addon:test:old-preview".into(),
                    name: "Old Preview".into(),
                    fsrs_version: AddonFsrsVersion::Seven,
                    params: vec![1.0; OUTDATED_FSRS7_PREVIEW_PARAM_COUNT],
                    desired_retention: 0.82,
                    historical_retention: 0.72,
                    ignore_revlogs_before_date: String::new(),
                    ..Default::default()
                }],
                rules: vec![FsrsPresetRule {
                    search: "front".into(),
                    preset_id: "addon:test:old-preview".into(),
                }],
                simulator_rules: Vec::new(),
            },
        )?;

        let preset = col.fsrs_preset_for_card(&card)?;
        assert_eq!(
            preset.id,
            FsrsPresetId::Addon("addon:test:old-preview".into())
        );
        assert_eq!(preset.fsrs_version, FsrsVersion::Seven);
        assert_eq!(preset.params, DEFAULT_PARAMETERS.to_vec());
        Ok(())
    }

    #[test]
    fn fsrs_preset_overlay_rejects_fsrs_property_rules() -> Result<()> {
        for search in ["prop:r<0.9", "prop:s>1", "prop:d>0.5"] {
            let mut col = Collection::new();
            NoteAdder::basic(&mut col).add(&mut col);
            assert!(col
                .set_config(
                    FSRS_PRESET_OVERLAY_CONFIG_KEY,
                    &FsrsPresetOverlay {
                        presets: vec![AddonFsrsPreset {
                            id: "addon:test:first".into(),
                            name: "First".into(),
                            fsrs_version: AddonFsrsVersion::Six,
                            params: vec![1.0; 21],
                            desired_retention: 0.81,
                            historical_retention: 0.71,
                            ignore_revlogs_before_date: String::new(),
                            ..Default::default()
                        }],
                        rules: vec![FsrsPresetRule {
                            search: search.into(),
                            preset_id: "addon:test:first".into(),
                        }],
                        simulator_rules: Vec::new(),
                    },
                )
                .is_err());
        }
        Ok(())
    }

    #[test]
    fn fsrs_preset_overlay_rejects_unknown_rule_preset() -> Result<()> {
        let mut col = Collection::new();
        NoteAdder::basic(&mut col).add(&mut col);

        assert!(col
            .set_config(
                FSRS_PRESET_OVERLAY_CONFIG_KEY,
                &FsrsPresetOverlay {
                    presets: vec![AddonFsrsPreset {
                        id: "addon:test:first".into(),
                        name: "First".into(),
                        fsrs_version: AddonFsrsVersion::Six,
                        params: vec![1.0; 21],
                        desired_retention: 0.81,
                        historical_retention: 0.71,
                        ignore_revlogs_before_date: String::new(),
                        ..Default::default()
                    }],
                    rules: vec![FsrsPresetRule {
                        search: "front".into(),
                        preset_id: "addon:test:missing".into(),
                    }],
                    simulator_rules: Vec::new(),
                },
            )
            .is_err());
        Ok(())
    }

    #[test]
    fn exact_retrievability_search_uses_overlay_params() -> Result<()> {
        let mut col = Collection::new();
        let deck_params = FSRS6_DEFAULT_PARAMETERS.to_vec();
        let mut overlay_params = deck_params.clone();
        overlay_params[20] += 0.2;
        col.update_default_deck_config(|config| {
            config.fsrs_version = FsrsVersion::Six as i32;
            config.fsrs_params_6 = deck_params.clone();
        });
        let note = NoteAdder::basic(&mut col).add(&mut col);
        col.add_tags_to_notes(&[note.id], "medical")?;

        let mut card = col.get_first_card();
        let stability = 10.0;
        let elapsed_days = 5.0;
        card.ctype = CardType::Review;
        card.queue = CardQueue::Review;
        card.interval = 10;
        card.due = col.timing_today()?.days_elapsed as i32;
        card.memory_state = Some(FsrsMemoryState {
            stability,
            stability_internal: stability,
            stability_fast: None,
            difficulty: 5.0,
        });
        card.last_review_time = Some(TimestampSecs::now().adding_secs(-5 * 86_400));
        col.storage.update_card(&card)?;

        let deck_r = fsrs_current_retrievability_for_params(&deck_params, stability, elapsed_days)?;
        let overlay_r =
            fsrs_current_retrievability_for_params(&overlay_params, stability, elapsed_days)?;
        assert_ne!(deck_r, overlay_r);
        let threshold = (deck_r + overlay_r) / 2.0;
        let query = if overlay_r > deck_r {
            format!("prop:r>{threshold}")
        } else {
            format!("prop:r<{threshold}")
        };

        assert_eq!(col.search_cards(&query, SortMode::NoOrder)?, Vec::new());

        col.set_config(
            FSRS_PRESET_OVERLAY_CONFIG_KEY,
            &FsrsPresetOverlay {
                presets: vec![AddonFsrsPreset {
                    id: "addon:test:medical".into(),
                    name: "Medical".into(),
                    fsrs_version: AddonFsrsVersion::Six,
                    params: overlay_params,
                    desired_retention: 0.81,
                    historical_retention: 0.71,
                    ignore_revlogs_before_date: String::new(),
                    ..Default::default()
                }],
                rules: vec![FsrsPresetRule {
                    search: "tag:medical".into(),
                    preset_id: "addon:test:medical".into(),
                }],
                simulator_rules: Vec::new(),
            },
        )?;

        assert_eq!(col.search_cards(&query, SortMode::NoOrder)?, vec![card.id]);
        Ok(())
    }

    #[test]
    fn fsrs_preset_overlay_card_matches_refresh_after_card_membership_changes() -> Result<()> {
        let mut col = Collection::new();
        let note = NoteAdder::basic(&mut col).add(&mut col);
        let card = col.get_first_card();
        col.set_config(
            FSRS_PRESET_OVERLAY_CONFIG_KEY,
            &FsrsPresetOverlay {
                presets: vec![AddonFsrsPreset {
                    id: "addon:test:tagged".into(),
                    name: "Tagged".into(),
                    fsrs_version: AddonFsrsVersion::Six,
                    params: vec![1.0; 21],
                    desired_retention: 0.81,
                    historical_retention: 0.71,
                    ignore_revlogs_before_date: String::new(),
                    ..Default::default()
                }],
                rules: vec![FsrsPresetRule {
                    search: "tag:medical".into(),
                    preset_id: "addon:test:tagged".into(),
                }],
                simulator_rules: Vec::new(),
            },
        )?;

        assert!(matches!(
            col.fsrs_preset_for_card(&card)?.id,
            FsrsPresetId::DeckConfig(_)
        ));

        col.add_tags_to_notes(&[note.id], "medical")?;
        let preset = col.fsrs_preset_for_card(&card)?;

        assert_eq!(preset.id, FsrsPresetId::Addon("addon:test:tagged".into()));
        Ok(())
    }

    #[test]
    fn fsrs_preset_overlay_cache_stores_rules_without_materializing_cards() -> Result<()> {
        let mut col = Collection::new();
        NoteAdder::basic(&mut col)
            .fields(&["front", "back"])
            .add(&mut col);

        col.set_config(
            FSRS_PRESET_OVERLAY_CONFIG_KEY,
            &FsrsPresetOverlay {
                presets: vec![AddonFsrsPreset {
                    id: "addon:test:front".into(),
                    name: "Front".into(),
                    fsrs_version: AddonFsrsVersion::Six,
                    params: vec![1.0; 21],
                    desired_retention: 0.81,
                    historical_retention: 0.71,
                    ignore_revlogs_before_date: String::new(),
                    ..Default::default()
                }],
                rules: vec![FsrsPresetRule {
                    search: "front".into(),
                    preset_id: "addon:test:front".into(),
                }],
                simulator_rules: Vec::new(),
            },
        )?;

        let cache = col.fsrs_preset_overlay_cache()?;

        assert_eq!(cache.presets.len(), 1);
        assert_eq!(cache.rules.len(), 1);
        assert!(cache.card_to_preset.is_empty());
        assert!(cache.cards_without_preset.is_empty());
        Ok(())
    }

    #[test]
    fn fsrs_preset_overlay_batch_matches_selected_cards() -> Result<()> {
        let mut col = Collection::new();
        let tagged_note = NoteAdder::basic(&mut col).add(&mut col);
        NoteAdder::basic(&mut col)
            .fields(&["other", "back"])
            .add(&mut col);
        col.add_tags_to_notes(&[tagged_note.id], "medical")?;
        col.set_config(
            FSRS_PRESET_OVERLAY_CONFIG_KEY,
            &FsrsPresetOverlay {
                presets: vec![AddonFsrsPreset {
                    id: "addon:test:tagged".into(),
                    name: "Tagged".into(),
                    fsrs_version: AddonFsrsVersion::Six,
                    params: vec![1.0; 21],
                    desired_retention: 0.81,
                    historical_retention: 0.71,
                    ignore_revlogs_before_date: String::new(),
                    ..Default::default()
                }],
                rules: vec![FsrsPresetRule {
                    search: "tag:medical".into(),
                    preset_id: "addon:test:tagged".into(),
                }],
                simulator_rules: Vec::new(),
            },
        )?;

        let cids = col.search_cards("", SortMode::NoOrder)?;
        let cards = col.all_cards_for_ids(&cids, false)?;
        let presets = col.fsrs_presets_for_cards(&cards)?;

        assert_eq!(presets.len(), 2);
        for card in cards {
            let preset = presets.get(&card.id).unwrap();
            if card.note_id == tagged_note.id {
                assert_eq!(preset.id, FsrsPresetId::Addon("addon:test:tagged".into()));
            } else {
                assert!(matches!(preset.id, FsrsPresetId::DeckConfig(_)));
            }
        }
        let cache = col.state.fsrs_preset_overlay_cache.as_ref().unwrap();
        assert_eq!(cache.card_to_preset.len(), 1);
        assert_eq!(cache.cards_without_preset.len(), 1);
        Ok(())
    }

    #[test]
    fn fsrs_preset_overlay_batch_preserves_first_match_for_duplicate_searches() -> Result<()> {
        let mut col = Collection::new();
        let tagged_note = NoteAdder::basic(&mut col).add(&mut col);
        col.add_tags_to_notes(&[tagged_note.id], "medical")?;
        col.set_config(
            FSRS_PRESET_OVERLAY_CONFIG_KEY,
            &FsrsPresetOverlay {
                presets: vec![
                    AddonFsrsPreset {
                        id: "addon:test:first".into(),
                        name: "First".into(),
                        fsrs_version: AddonFsrsVersion::Six,
                        params: vec![1.0; 21],
                        desired_retention: 0.81,
                        historical_retention: 0.71,
                        ignore_revlogs_before_date: String::new(),
                        ..Default::default()
                    },
                    AddonFsrsPreset {
                        id: "addon:test:second".into(),
                        name: "Second".into(),
                        fsrs_version: AddonFsrsVersion::Six,
                        params: vec![1.0; 21],
                        desired_retention: 0.82,
                        historical_retention: 0.72,
                        ignore_revlogs_before_date: String::new(),
                        ..Default::default()
                    },
                ],
                rules: vec![
                    FsrsPresetRule {
                        search: "tag:medical".into(),
                        preset_id: "addon:test:first".into(),
                    },
                    FsrsPresetRule {
                        search: "tag:medical".into(),
                        preset_id: "addon:test:second".into(),
                    },
                ],
                simulator_rules: Vec::new(),
            },
        )?;

        let cards = col.all_cards_for_search("")?;
        let presets = col.fsrs_presets_for_cards(&cards)?;
        let tagged_card = cards
            .iter()
            .find(|card| card.note_id == tagged_note.id)
            .unwrap();

        assert_eq!(
            presets.get(&tagged_card.id).unwrap().id,
            FsrsPresetId::Addon("addon:test:first".into())
        );
        let cache = col.state.fsrs_preset_overlay_cache.as_ref().unwrap();
        assert_eq!(
            cache.card_to_preset.get(&tagged_card.id).unwrap(),
            "addon:test:first"
        );
        Ok(())
    }

    #[test]
    fn stats_preset_batch_skips_cards_without_memory_state() -> Result<()> {
        let mut col = Collection::new();
        let tagged_note = NoteAdder::basic(&mut col).add(&mut col);
        let reviewed_note = NoteAdder::basic(&mut col)
            .fields(&["other", "back"])
            .add(&mut col);
        col.add_tags_to_notes(&[tagged_note.id], "medical")?;
        col.set_config(
            FSRS_PRESET_OVERLAY_CONFIG_KEY,
            &FsrsPresetOverlay {
                presets: vec![AddonFsrsPreset {
                    id: "addon:test:tagged".into(),
                    name: "Tagged".into(),
                    fsrs_version: AddonFsrsVersion::Six,
                    params: vec![1.0; 21],
                    desired_retention: 0.81,
                    historical_retention: 0.71,
                    ignore_revlogs_before_date: String::new(),
                    ..Default::default()
                }],
                rules: vec![FsrsPresetRule {
                    search: "tag:medical".into(),
                    preset_id: "addon:test:tagged".into(),
                }],
                simulator_rules: Vec::new(),
            },
        )?;

        let cards = col.all_cards_for_search("")?;
        let tagged_card = cards
            .iter()
            .find(|card| card.note_id == tagged_note.id)
            .unwrap();
        let mut reviewed_card = cards
            .iter()
            .find(|card| card.note_id == reviewed_note.id)
            .unwrap()
            .clone();
        reviewed_card.memory_state = Some(FsrsMemoryState {
            stability: 10.0,
            stability_internal: 10.0,
            stability_fast: None,
            difficulty: 5.0,
        });
        reviewed_card.last_review_time = Some(TimestampSecs::now().adding_secs(-86_400));
        col.storage.update_card(&reviewed_card)?;

        let _ = col.graph_data_for_search("", 365)?;

        let cache = col.state.fsrs_preset_overlay_cache.as_ref().unwrap();
        assert!(!cache.card_to_preset.contains_key(&tagged_card.id));
        assert!(!cache.cards_without_preset.contains(&tagged_card.id));
        assert!(cache.cards_without_preset.contains(&reviewed_card.id));
        Ok(())
    }

    #[test]
    fn fsrs_preset_overlay_cache_keeps_rules_after_card_membership_changes() -> Result<()> {
        let mut col = Collection::new();
        let note = NoteAdder::basic(&mut col).add(&mut col);
        let card = col.get_first_card();
        col.set_config(
            FSRS_PRESET_OVERLAY_CONFIG_KEY,
            &FsrsPresetOverlay {
                presets: vec![AddonFsrsPreset {
                    id: "addon:test:tagged".into(),
                    name: "Tagged".into(),
                    fsrs_version: AddonFsrsVersion::Six,
                    params: vec![1.0; 21],
                    desired_retention: 0.81,
                    historical_retention: 0.71,
                    ignore_revlogs_before_date: String::new(),
                    ..Default::default()
                }],
                rules: vec![FsrsPresetRule {
                    search: "tag:medical".into(),
                    preset_id: "addon:test:tagged".into(),
                }],
                simulator_rules: Vec::new(),
            },
        )?;
        col.fsrs_preset_overlay_cache()?;

        col.add_tags_to_notes(&[note.id], "medical")?;
        let cache = col.state.fsrs_preset_overlay_cache.as_ref().unwrap();

        assert_eq!(cache.rules.len(), 1);
        assert!(cache.card_to_preset.is_empty());
        assert!(cache.cards_without_preset.is_empty());
        assert_eq!(
            col.fsrs_preset_for_card(&card)?.id,
            FsrsPresetId::Addon("addon:test:tagged".into())
        );
        Ok(())
    }
}
