// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Use the live queue's eligibility and deck-limit rules in Instant forecasts.

use std::collections::HashMap;
use std::io;

use anki_proto::deck_config::deck_config::config::ReviewCardOrder;

use crate::deckconfig::DeckConfig;
use crate::deckconfig::DeckConfigId;
use crate::decks::limits::LimitKind;
use crate::decks::limits::LimitTreeMap;
use crate::decks::Deck;
use crate::decks::DeckId;
use crate::scheduler::rwkv::rwkv_review_score_eligibility;
use crate::scheduler::rwkv::rwkv_review_score_eligibility_ignoring_retention;
use crate::scheduler::rwkv::RwkvReviewCandidateMetadata;
use crate::scheduler::rwkv::RwkvReviewScoreEligibility;

#[derive(Clone, Copy)]
pub(super) struct InstantCandidate {
    pub card_id: i64,
    pub deck_id: i64,
    pub score: f32,
    pub target: f32,
    pub instant_enabled: bool,
    pub due_day: i64,
    pub interval_days: i64,
    pub ease_factor: i64,
    pub reviewed_today: bool,
    pub elapsed_seconds: Option<u32>,
    pub intervening_reviews: Option<u32>,
}

#[derive(Clone, Copy)]
pub(super) struct InstantReviewPolicy {
    pub same_day_allowed: bool,
    pub min_intervening: u32,
    pub min_elapsed_seconds: u32,
    pub order: ReviewCardOrder,
    pub shuffle: i64,
    pub day: i64,
}

pub(super) fn instant_due(card: &InstantCandidate, policy: InstantReviewPolicy) -> bool {
    if !card.instant_enabled {
        return card.due_day <= policy.day;
    }
    let metadata = RwkvReviewCandidateMetadata {
        target_retention: card.target,
        reviewed_today: card.reviewed_today,
        same_day_review_allowed: policy.same_day_allowed,
        elapsed_secs_since_last_review: card.elapsed_seconds,
        current_deck_id: DeckId(card.deck_id),
        source_deck_id: DeckId(card.deck_id),
        fsrs_due_today: false,
    };
    rwkv_review_score_eligibility(
        card.score,
        &metadata,
        policy.min_intervening,
        policy.min_elapsed_seconds,
        card.intervening_reviews,
        Some(card.target),
    ) == RwkvReviewScoreEligibility::Eligible
}

#[cfg(test)]
pub(super) fn instant_review_candidates(
    cards: &[InstantCandidate],
    decks: &[Deck],
    configs: &HashMap<DeckConfigId, DeckConfig>,
    today: u32,
    new_cards_ignore_review_limit: bool,
    policy: InstantReviewPolicy,
) -> io::Result<Vec<usize>> {
    if decks.is_empty() {
        return Err(invalid("Instant forecast has no study deck"));
    }
    let mut limits = LimitTreeMap::build(decks, configs, today, new_cards_ignore_review_limit);
    select_reviews(cards, decks, configs, &mut limits, policy)
}

/// Build the queues in the same limit-reservation order as Anki: interday
/// learning, reviews, then new cards. Returned indices refer to each input.
#[allow(clippy::too_many_arguments)]
pub(super) fn instant_study_candidates(
    reviews: &[InstantCandidate],
    day_learning_cards: &[InstantCandidate],
    new_decks: &[i64],
    decks: &[Deck],
    configs: &HashMap<DeckConfigId, DeckConfig>,
    today: u32,
    new_cards_ignore_review_limit: bool,
    policy: InstantReviewPolicy,
) -> io::Result<(Vec<usize>, Vec<usize>, Vec<usize>)> {
    if decks.is_empty() {
        return Err(invalid("Instant forecast has no study deck"));
    }
    let mut limits = LimitTreeMap::build(decks, configs, today, new_cards_ignore_review_limit);
    let mut learning = Vec::new();
    for (index, card) in day_learning_cards.iter().enumerate() {
        let deck = DeckId(card.deck_id);
        let exempt = ignores_review_limit(card, decks, configs)?;
        if exempt
            || !limits
                .limit_reached(deck, LimitKind::Review)
                .map_err(map_error)?
        {
            learning.push(index);
            if !exempt {
                limits.reserve_review(deck, DeckId(0)).map_err(map_error)?;
            }
        }
    }
    let review = select_reviews(reviews, decks, configs, &mut limits, policy)?;
    let mut new = Vec::new();
    for (index, deck) in new_decks.iter().copied().enumerate() {
        let deck = DeckId(deck);
        if !limits
            .limit_reached(deck, LimitKind::New)
            .map_err(map_error)?
        {
            new.push(index);
            limits.reserve_new_card(deck).map_err(map_error)?;
        }
    }
    Ok((learning, review, new))
}

fn select_reviews(
    cards: &[InstantCandidate],
    decks: &[Deck],
    configs: &HashMap<DeckConfigId, DeckConfig>,
    limits: &mut LimitTreeMap,
    policy: InstantReviewPolicy,
) -> io::Result<Vec<usize>> {
    let mut eligible = Vec::new();
    let mut above_target = Vec::new();
    for (index, card) in cards.iter().enumerate() {
        if !card.instant_enabled {
            if card.due_day <= policy.day {
                eligible.push(index);
            }
            continue;
        }
        let metadata = RwkvReviewCandidateMetadata {
            target_retention: card.target,
            reviewed_today: card.reviewed_today,
            same_day_review_allowed: policy.same_day_allowed,
            elapsed_secs_since_last_review: card.elapsed_seconds,
            current_deck_id: DeckId(card.deck_id),
            source_deck_id: DeckId(card.deck_id),
            fsrs_due_today: false,
        };
        let allowed = rwkv_review_score_eligibility(
            card.score,
            &metadata,
            policy.min_intervening,
            policy.min_elapsed_seconds,
            card.intervening_reviews,
            Some(card.target),
        );
        if allowed == RwkvReviewScoreEligibility::Eligible {
            eligible.push(index);
        } else if rwkv_review_score_eligibility_ignoring_retention(
            card.score,
            &metadata,
            policy.min_intervening,
            policy.min_elapsed_seconds,
            card.intervening_reviews,
        ) == RwkvReviewScoreEligibility::Eligible
        {
            above_target.push(index);
        }
    }
    let deck_order = decks
        .iter()
        .enumerate()
        .map(|(index, deck)| (deck.id.0, index as i64))
        .collect::<HashMap<_, _>>();
    let priority = |index: &usize| {
        let card = &cards[*index];
        let rank = deck_order.get(&card.deck_id).copied().unwrap_or(i64::MAX);
        match policy.order {
            ReviewCardOrder::DeckThenDay => (rank, card.due_day, card.card_id),
            ReviewCardOrder::DayThenDeck => (card.due_day, rank, card.card_id),
            _ => instant_review_priority(card, policy),
        }
    };
    let compare = |left: &usize, right: &usize| {
        let a = &cards[*left];
        let b = &cards[*right];
        let scores = match policy.order {
            ReviewCardOrder::RetrievabilityAscending => Some(a.score.total_cmp(&b.score)),
            ReviewCardOrder::RetrievabilityDescending => Some(b.score.total_cmp(&a.score)),
            ReviewCardOrder::RelativeOverdueness => {
                Some((a.score / a.target.max(0.0001)).total_cmp(&(b.score / b.target.max(0.0001))))
            }
            _ => None,
        };
        scores
            .map(|order| order.then_with(|| a.card_id.cmp(&b.card_id)))
            .unwrap_or_else(|| priority(left).cmp(&priority(right)))
    };
    eligible.sort_by(compare);
    let mut selected = Vec::new();
    for index in eligible {
        let card = &cards[index];
        let exempt = ignores_review_limit(card, decks, configs)?;
        if exempt
            || !limits
                .limit_reached(DeckId(card.deck_id), LimitKind::Review)
                .map_err(map_error)?
        {
            selected.push(index);
            if !exempt {
                limits
                    .reserve_review(DeckId(card.deck_id), DeckId(0))
                    .map_err(map_error)?;
            }
        }
    }
    above_target.sort_by(|left, right| {
        let score = |card: &InstantCandidate| {
            if policy.order == ReviewCardOrder::RelativeOverdueness {
                card.score / card.target.max(0.0001)
            } else {
                card.score
            }
        };
        score(&cards[*left])
            .total_cmp(&score(&cards[*right]))
            .then_with(|| cards[*left].card_id.cmp(&cards[*right].card_id))
    });
    for index in above_target {
        let card = &cards[index];
        let deck = DeckId(card.deck_id);
        if !ignores_review_limit(card, decks, configs)?
            && limits
                .rwkv_review_minimum_remaining(deck)
                .map_err(map_error)?
            && !limits
                .limit_reached(deck, LimitKind::Review)
                .map_err(map_error)?
        {
            selected.push(index);
            limits.reserve_review(deck, DeckId(0)).map_err(map_error)?;
        }
    }
    selected.sort_by(compare);
    Ok(selected)
}

pub(super) fn ignores_review_limit(
    card: &InstantCandidate,
    decks: &[Deck],
    configs: &HashMap<DeckConfigId, DeckConfig>,
) -> io::Result<bool> {
    let deck = decks
        .iter()
        .find(|deck| deck.id.0 == card.deck_id)
        .ok_or_else(|| invalid("Instant forecast card is outside the study deck"))?;
    Ok(card.reviewed_today
        && deck
            .config_id()
            .and_then(|id| configs.get(&id))
            .is_some_and(|config| config.inner.same_day_reviews_ignore_review_limit))
}

pub(super) fn instant_review_priority(
    card: &InstantCandidate,
    policy: InstantReviewPolicy,
) -> (i64, i64, i64) {
    let score = (card.score * 1_000_000.0).round() as i64;
    let relative = (card.score / card.target.max(0.0001) * 1_000_000.0).round() as i64;
    let (primary, secondary) = match policy.order {
        ReviewCardOrder::RetrievabilityAscending => (score, card.due_day),
        ReviewCardOrder::RetrievabilityDescending => (-score, card.due_day),
        ReviewCardOrder::RelativeOverdueness => (relative, card.due_day),
        ReviewCardOrder::IntervalsAscending => (card.interval_days, card.due_day),
        ReviewCardOrder::IntervalsDescending => (-card.interval_days, card.due_day),
        ReviewCardOrder::EaseAscending => (card.ease_factor, card.due_day),
        ReviewCardOrder::EaseDescending => (-card.ease_factor, card.due_day),
        ReviewCardOrder::Added => (card.card_id, 0),
        ReviewCardOrder::ReverseAdded => (-card.card_id, 0),
        ReviewCardOrder::DeckThenDay => (card.deck_id, card.due_day),
        ReviewCardOrder::DayThenDeck => (card.due_day, card.deck_id),
        ReviewCardOrder::Random => (
            (super::simulation_unit_hash(card.card_id, policy.shuffle, 0) * i64::MAX as f64) as i64,
            0,
        ),
        ReviewCardOrder::Day => (card.due_day, 0),
    };
    (primary, secondary, card.card_id)
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn map_error(error: impl std::fmt::Display) -> io::Error {
    invalid(&error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::decks::NativeDeckName;

    fn card(card_id: i64, deck_id: i64, score: f32) -> InstantCandidate {
        InstantCandidate {
            card_id,
            deck_id,
            score,
            target: 0.9,
            instant_enabled: true,
            due_day: 0,
            interval_days: 10,
            ease_factor: 2500,
            reviewed_today: false,
            elapsed_seconds: Some(86_400),
            intervening_reviews: Some(10),
        }
    }

    fn deck(id: i64, name: &str, config: i64) -> Deck {
        let mut deck = Deck::new_normal();
        deck.id = DeckId(id);
        deck.name = NativeDeckName::from_human_name(name);
        deck.normal_mut().unwrap().config_id = config;
        deck
    }

    fn config(id: i64, minimum: u32, maximum: u32) -> DeckConfig {
        let mut config = DeckConfig {
            id: DeckConfigId(id),
            ..Default::default()
        };
        config.inner.rwkv_review_instant_order_enabled = true;
        config.inner.rwkv_review_minimum_reviews_per_day = minimum;
        config.inner.reviews_per_day = maximum;
        config
    }

    fn policy() -> InstantReviewPolicy {
        InstantReviewPolicy {
            same_day_allowed: false,
            min_intervening: 5,
            min_elapsed_seconds: 30,
            order: ReviewCardOrder::RetrievabilityAscending,
            shuffle: 0,
            day: 0,
        }
    }

    #[test]
    fn minimum_pulls_only_lowest_above_target_cards() {
        let decks = [deck(1, "Parent", 1)];
        let configs = HashMap::from([(DeckConfigId(1), config(1, 2, 20))]);
        let cards = [card(1, 1, 0.70), card(2, 1, 0.96), card(3, 1, 0.93)];
        assert_eq!(
            instant_review_candidates(&cards, &decks, &configs, 0, true, policy()).unwrap(),
            vec![0, 2]
        );
    }

    #[test]
    fn parent_maximum_caps_child_decks() {
        let decks = [deck(1, "Parent", 1), deck(2, "Parent::Child", 2)];
        let configs = HashMap::from([
            (DeckConfigId(1), config(1, 0, 1)),
            (DeckConfigId(2), config(2, 2, 20)),
        ]);
        let cards = [card(1, 2, 0.7), card(2, 2, 0.8)];
        assert_eq!(
            instant_review_candidates(&cards, &decks, &configs, 0, true, policy()).unwrap(),
            vec![0]
        );
    }

    #[test]
    fn minimum_does_not_bypass_spacing_or_same_day_rules() {
        let decks = [deck(1, "Parent", 1)];
        let configs = HashMap::from([(DeckConfigId(1), config(1, 2, 20))]);
        let mut recent = card(2, 1, 0.91);
        recent.elapsed_seconds = Some(10);
        let mut repeated = card(3, 1, 0.92);
        repeated.reviewed_today = true;
        let cards = [card(1, 1, 0.7), recent, repeated, card(4, 1, 0.95)];
        assert_eq!(
            instant_review_candidates(&cards, &decks, &configs, 0, true, policy()).unwrap(),
            vec![0, 3]
        );
    }

    #[test]
    fn maximum_still_allows_an_exempt_same_day_review() {
        let mut root = deck(1, "Parent", 1);
        root.common.review_studied = 1;
        let mut cfg = config(1, 0, 1);
        cfg.inner.same_day_reviews_ignore_review_limit = true;
        let configs = HashMap::from([(DeckConfigId(1), cfg)]);
        let mut repeated = card(2, 1, 0.7);
        repeated.reviewed_today = true;
        let mut policy = policy();
        policy.same_day_allowed = true;
        assert_eq!(
            instant_review_candidates(
                &[card(1, 1, 0.5), repeated],
                &[root],
                &configs,
                0,
                true,
                policy
            )
            .unwrap(),
            vec![1]
        );
    }

    #[test]
    fn new_cards_respect_the_ignore_review_limit_toggle() {
        let decks = [deck(1, "Parent", 1)];
        let mut preset = config(1, 0, 1);
        preset.inner.new_per_day = 1;
        let configs = HashMap::from([(DeckConfigId(1), preset)]);
        let reviews = [card(1, 1, 0.7)];
        let shared =
            instant_study_candidates(&reviews, &[], &[1], &decks, &configs, 0, false, policy())
                .unwrap();
        let separate =
            instant_study_candidates(&reviews, &[], &[1], &decks, &configs, 0, true, policy())
                .unwrap();
        assert_eq!(shared.1, vec![0]);
        assert!(shared.2.is_empty());
        assert_eq!(separate.1, vec![0]);
        assert_eq!(separate.2, vec![0]);
    }

    #[test]
    fn interday_learning_reserves_review_capacity() {
        let decks = [deck(1, "Parent", 1)];
        let configs = HashMap::from([(DeckConfigId(1), config(1, 0, 1))]);
        let result = instant_study_candidates(
            &[card(1, 1, 0.5)],
            &[card(2, 1, 0.9)],
            &[],
            &decks,
            &configs,
            0,
            true,
            policy(),
        )
        .unwrap();
        assert_eq!(result.0, vec![0]);
        assert!(result.1.is_empty());
    }

    #[test]
    fn normal_decks_use_due_dates_instead_of_instant_thresholds() {
        let decks = [deck(1, "Parent", 1)];
        let configs = HashMap::from([(DeckConfigId(1), config(1, 0, 10))]);
        let mut future = card(1, 1, 0.1);
        future.instant_enabled = false;
        future.due_day = 5;
        assert!(
            instant_review_candidates(&[future], &decks, &configs, 0, true, policy())
                .unwrap()
                .is_empty()
        );
        let mut later = policy();
        later.day = 5;
        assert_eq!(
            instant_review_candidates(&[future], &decks, &configs, 5, true, later).unwrap(),
            vec![0]
        );
    }

    #[test]
    fn searches_across_separate_decks_keep_independent_maximums() {
        use prost::Message;
        let deck_bytes = [deck(1, "Alpha", 1), deck(2, "Beta", 2)]
            .into_iter()
            .map(|deck| anki_proto::decks::Deck::from(deck).encode_to_vec())
            .collect();
        let config_bytes = [config(1, 0, 1), config(2, 0, 1)]
            .into_iter()
            .map(|config| anki_proto::deck_config::DeckConfig::from(config).encode_to_vec())
            .collect();
        let (decks, configs) =
            crate::rwkv::decode_instant_forecast_decks(deck_bytes, config_bytes).unwrap();
        let configs = configs
            .into_iter()
            .map(|config| (config.id, config))
            .collect();
        let cards = [card(1, 1, 0.5), card(2, 2, 0.6)];
        assert_eq!(
            instant_review_candidates(&cards, &decks, &configs, 0, true, policy()).unwrap(),
            vec![0, 1]
        );
    }
}
