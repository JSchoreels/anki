// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use anki_proto::stats::graphs_response::Retrievability;

use crate::prelude::TimestampSecs;
use crate::scheduler::timing::SchedTimingToday;
use crate::stats::graphs::eases::percent_to_bin;
use crate::stats::graphs::GraphsContext;

impl GraphsContext {
    /// (SM-2, FSRS)
    pub(super) fn retrievability(&self) -> Retrievability {
        let mut retrievability = Retrievability::default();
        let mut card_with_retrievability_count: usize = 0;
        let timing = SchedTimingToday {
            days_elapsed: self.days_elapsed,
            now: TimestampSecs::now(),
            next_day_at: self.next_day_start,
        };
        // note id -> (sum, count)
        let mut note_retrievability: std::collections::HashMap<i64, (f32, u32)> =
            std::collections::HashMap::new();
        for card in &self.cards {
            let entry = note_retrievability
                .entry(card.note_id.0)
                .or_insert((0.0, 0));
            entry.1 += 1;
            if let Some(state) = card.memory_state {
                let elapsed_seconds = card.seconds_since_last_review(&timing).unwrap_or_default();
                let deck_id = card.original_deck_id.or(card.deck_id);
                let Some(fsrs) = self.fsrs_by_deck.get(&deck_id) else {
                    entry.0 += 0.0;
                    continue;
                };
                let r =
                    fsrs.current_retrievability(state.into(), elapsed_seconds as f32 / 86_400.0);

                *retrievability
                    .retrievability
                    .entry(percent_to_bin(r * 100.0, 1))
                    .or_insert_with(Default::default) += 1;
                retrievability.sum_by_card += r;
                card_with_retrievability_count += 1;
                entry.0 += r;
            } else {
                entry.0 += 0.0;
            }
        }
        if card_with_retrievability_count != 0 {
            retrievability.average =
                retrievability.sum_by_card * 100.0 / card_with_retrievability_count as f32;
        }
        retrievability.sum_by_note = note_retrievability
            .values()
            .map(|(sum, count)| sum / *count as f32)
            .sum();
        retrievability
    }
}

#[cfg(test)]
mod tests {
    use anki_proto::deck_config::deck_configs_for_update::current_deck::Limits;
    use anki_proto::deck_config::UpdateDeckConfigsMode;
    use fsrs::MemoryState;
    use fsrs::FSRS;

    use crate::card::FsrsMemoryState;
    use crate::deckconfig::FsrsVersion;
    use crate::deckconfig::UpdateDeckConfigsRequest;
    use crate::prelude::*;
    use crate::search::SortMode;

    fn fsrs7_params_for_retrievability_test() -> Vec<f32> {
        vec![
            0.4843, 3.0562, 10.9946, 32.7202, 5.6296, 0.5900, 3.1230, 2.4679, 0.2733, 1.4895,
            0.4868, 0.0010, 0.8082, 0.1723, 0.6389, 1.5767, 0.8918, 0.3341, 3.5942, 0.3455, 0.0022,
            0.2834, 2.6418, 0.5604, 1.3042, 2.5054, 0.9376, 0.0611, 0.0830, 0.6339, 0.9846, 0.2485,
            0.6014, 0.0545, 0.0,
        ]
    }

    fn set_selected_fsrs7_params(col: &mut Collection, params: Vec<f32>) -> Result<()> {
        let output = col.get_deck_configs_for_update(DeckId(1))?;
        let mut input = UpdateDeckConfigsRequest {
            target_deck_id: DeckId(1),
            configs: output
                .all_config
                .into_iter()
                .map(|c| c.config.unwrap().into())
                .collect(),
            removed_config_ids: vec![],
            mode: UpdateDeckConfigsMode::Normal,
            card_state_customizer: String::new(),
            limits: Limits::default(),
            new_cards_ignore_review_limit: false,
            apply_all_parent_limits: false,
            fsrs: true,
            load_balancer_enabled: false,
            fsrs_short_term_with_steps_enabled: false,
            fsrs_learning_queues_disabled: false,
            fsrs_reschedule: false,
            fsrs_health_check: true,
            review_fuzz_config: Default::default(),
        };
        input.configs[0].inner.fsrs_version = FsrsVersion::Seven as i32;
        input.configs[0].inner.fsrs_params_7 = params;
        col.update_deck_configs(input)?;
        Ok(())
    }

    #[test]
    fn retrievability_graph_uses_selected_model_curve() -> Result<()> {
        let mut col = Collection::new();
        let params = fsrs7_params_for_retrievability_test();
        set_selected_fsrs7_params(&mut col, params.clone())?;

        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note = nt.new_note();
        col.add_note(&mut note, DeckId(1))?;
        let cid = col.search_cards("", SortMode::NoOrder)?[0];

        let mut card = col.storage.get_card(cid)?.unwrap();
        let stability = 42.0;
        let elapsed_days = 120.0;
        let timing = col.timing_today()?;
        card.memory_state = Some(FsrsMemoryState {
            stability,
            stability_internal: stability,
            difficulty: 5.0,
        });
        card.last_review_time = Some(timing.now.adding_secs(-(elapsed_days as i64) * 86_400));
        card.decay = Some(params[27]);
        col.storage.update_card(&card)?;

        let graphs = col.graph_data_for_search("", 365)?;
        let actual = graphs.retrievability.unwrap().average;
        let expected = FSRS::new(&params)?.current_retrievability(
            MemoryState {
                stability,
                difficulty: 5.0,
            },
            elapsed_days,
        ) * 100.0;

        assert_eq!(format!("{actual:.3}"), format!("{expected:.3}"));
        Ok(())
    }
}
