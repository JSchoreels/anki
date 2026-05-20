// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use fsrs::FSRS;

use crate::card::CardType;
use crate::card::FsrsMemoryState;
use crate::prelude::*;
use crate::revlog::RevlogEntry;
use crate::scheduler::fsrs::memory_state::fsrs_current_retrievability_for_params;
use crate::scheduler::fsrs::memory_state::fsrs_item_for_memory_state;
use crate::scheduler::fsrs::memory_state::fsrs_memory_state_for_params;
use crate::scheduler::fsrs::params::ignore_revlogs_before_ms_from_config;
use crate::scheduler::timing::is_unix_epoch_timestamp;

impl Collection {
    pub fn card_stats(&mut self, cid: CardId) -> Result<anki_proto::stats::CardStatsResponse> {
        let card = self.storage.get_card(cid)?.or_not_found(cid)?;
        let note = self
            .storage
            .get_note(card.note_id)?
            .or_not_found(card.note_id)?;
        let nt = self
            .get_notetype(note.notetype_id)?
            .or_not_found(note.notetype_id)?;
        let deck = self
            .storage
            .get_deck(card.deck_id)?
            .or_not_found(card.deck_id)?;
        let revlog = self.storage.get_revlog_entries_for_card(card.id)?;

        let (average_secs, total_secs) = average_and_total_secs_strings(&revlog);
        let timing = self.timing_today()?;

        let last_review_time = if let Some(last_review_time) = card.last_review_time {
            last_review_time
        } else {
            let mut new_card = card.clone();
            let last_review_time = self
                .storage
                .time_of_last_review(card.id)?
                .unwrap_or_default();

            new_card.last_review_time = Some(last_review_time);

            self.storage.update_card(&new_card)?;
            last_review_time
        };

        let seconds_elapsed = timing.now.elapsed_secs_since(last_review_time) as u32;

        let original_deck = if card.original_deck_id == DeckId(0) {
            deck.clone()
        } else {
            self.storage
                .get_deck(card.original_deck_id)?
                .or_not_found(card.original_deck_id)?
        };
        let config_id = original_deck.config_id().unwrap();
        let preset = self
            .get_deck_config(config_id, true)?
            .or_not_found(config_id.to_string())?;

        let fsrs_retrievability =
            card.memory_state
                .zip(Some(seconds_elapsed))
                .map(|(state, seconds)| {
                    fsrs_current_retrievability_for_params(
                        preset.fsrs_params(),
                        state.stability_internal,
                        seconds as f32 / 86_400.0,
                    )
                });
        Ok(anki_proto::stats::CardStatsResponse {
            card_id: card.id.into(),
            note_id: card.note_id.into(),
            deck: deck.human_name(),
            added: card.id.as_secs().0,
            first_review: revlog
                .iter()
                .find(|entry| entry.has_rating())
                .map(|entry| entry.id.as_secs().0),
            // last_review_time is not used to ensure cram revlogs are included.
            latest_review: revlog
                .iter()
                .rfind(|entry| entry.has_rating())
                .map(|entry| entry.id.as_secs().0),
            due_date: self.due_date(&card)?,
            due_position: self.position(&card),
            interval: card.interval,
            ease: card.ease_factor as u32,
            reviews: card.reps,
            lapses: card.lapses,
            average_secs,
            total_secs,
            card_type: nt.get_template(card.template_idx)?.name.clone(),
            notetype: nt.name.clone(),
            revlog: self.stats_revlog_entries_with_memory_state(&card, last_review_time, revlog)?,
            memory_state: card.memory_state.map(Into::into),
            fsrs_retrievability: fsrs_retrievability.transpose()?,
            custom_data: card.custom_data,
            fsrs_params: preset.fsrs_params().to_vec(),
            preset: preset.name,
            original_deck: if original_deck != deck {
                Some(original_deck.human_name())
            } else {
                None
            },
            desired_retention: card.desired_retention,
        })
    }

    pub fn get_review_logs(&mut self, cid: CardId) -> Result<anki_proto::stats::ReviewLogs> {
        let revlogs = self.storage.get_revlog_entries_for_card(cid)?;
        Ok(anki_proto::stats::ReviewLogs {
            entries: revlogs.iter().rev().map(stats_revlog_entry).collect(),
        })
    }

    fn due_date(&mut self, card: &Card) -> Result<Option<i64>> {
        Ok(match card.ctype {
            CardType::New => None,
            CardType::Review | CardType::Learn | CardType::Relearn => {
                let due = if card.original_due != 0 {
                    card.original_due
                } else {
                    card.due
                };
                if !is_unix_epoch_timestamp(due) {
                    let days_remaining = due - (self.timing_today()?.days_elapsed as i32);
                    let mut due_timestamp = TimestampSecs::now();
                    due_timestamp.0 += (days_remaining as i64) * 86_400;
                    Some(due_timestamp.0)
                } else {
                    Some(due as i64)
                }
            }
        })
    }

    fn position(&mut self, card: &Card) -> Option<i32> {
        if let Some(original_pos) = card.original_position {
            return Some(original_pos as i32);
        }
        match card.ctype {
            CardType::New => Some(card.due),
            _ => None,
        }
    }

    fn stats_revlog_entries_with_memory_state(
        self: &mut Collection,
        card: &Card,
        last_review_time: TimestampSecs,
        revlog: Vec<RevlogEntry>,
    ) -> Result<Vec<anki_proto::stats::card_stats_response::StatsRevlogEntry>> {
        let deck_id = card.original_deck_id.or(card.deck_id);
        let deck = self.get_deck(deck_id)?.or_not_found(card.deck_id)?;
        let conf_id = DeckConfigId(deck.normal()?.config_id);
        let config = self
            .storage
            .get_deck_config(conf_id)?
            .or_not_found(conf_id)?;
        let historical_retention = config.inner.historical_retention;
        let params = config.fsrs_params();
        let fsrs = FSRS::new(params)?;
        let next_day_at = self.timing_today()?.next_day_at;
        let ignore_before = ignore_revlogs_before_ms_from_config(&config)?;

        let mut result = Vec::new();
        if let Some(item) = fsrs_item_for_memory_state(
            &fsrs,
            params,
            revlog.clone(),
            next_day_at,
            historical_retention,
            ignore_before,
        )? {
            let memory_states = fsrs.historical_memory_states(item.item, item.starting_state)?;
            let mut revlog_index = 0;
            for entry in revlog {
                let mut stats_entry = stats_revlog_entry(&entry);
                let memory_state: Option<FsrsMemoryState> = if revlog_index >= memory_states.len() {
                    // The removed revlog is in the end of the revlog, so we use the last memory
                    // state
                    Some(fsrs_memory_state_for_params(
                        params,
                        memory_states[memory_states.len() - 1],
                    )?)
                } else if entry.id == item.filtered_revlogs[revlog_index].id {
                    revlog_index += 1;
                    Some(fsrs_memory_state_for_params(
                        params,
                        memory_states[revlog_index - 1],
                    )?)
                } else if revlog_index == 0 {
                    // The removed revlog is in the start of the revlog, so we don't have a memory
                    // state for it
                    None
                } else {
                    // The removed revlog is in the middle of the revlog, so we use the memory
                    // state for the previous revlog entry
                    Some(fsrs_memory_state_for_params(
                        params,
                        memory_states[revlog_index],
                    )?)
                };
                stats_entry.memory_state = memory_state.map(|s| s.into());
                result.push(stats_entry);
            }
            Ok(with_current_memory_state_on_latest_review(
                card.memory_state,
                last_review_time,
                result.into_iter().rev().collect(),
            ))
        } else {
            Ok(with_current_memory_state_on_latest_review(
                card.memory_state,
                last_review_time,
                revlog.iter().rev().map(stats_revlog_entry).collect(),
            ))
        }
    }
}

fn with_current_memory_state_on_latest_review(
    memory_state: Option<FsrsMemoryState>,
    last_review_time: TimestampSecs,
    mut entries: Vec<anki_proto::stats::card_stats_response::StatsRevlogEntry>,
) -> Vec<anki_proto::stats::card_stats_response::StatsRevlogEntry> {
    if let Some(memory_state) = memory_state {
        if let Some(entry) = entries
            .iter_mut()
            .find(|entry| entry.button_chosen > 0 && entry.time == last_review_time.0)
        {
            entry.memory_state = Some(memory_state.into());
        }
    }
    entries
}

fn average_and_total_secs_strings(revlog: &[RevlogEntry]) -> (f32, f32) {
    let normal_answer_count = revlog.iter().filter(|r| r.has_rating()).count();
    let total_secs: f32 = revlog
        .iter()
        .map(|entry| (entry.taken_millis as f32) / 1000.0)
        .sum();
    if normal_answer_count == 0 || total_secs == 0.0 {
        (0.0, 0.0)
    } else {
        (total_secs / normal_answer_count as f32, total_secs)
    }
}

fn stats_revlog_entry(
    entry: &RevlogEntry,
) -> anki_proto::stats::card_stats_response::StatsRevlogEntry {
    anki_proto::stats::card_stats_response::StatsRevlogEntry {
        time: entry.id.as_secs().0,
        review_kind: entry.review_kind.into(),
        button_chosen: entry.button_chosen as u32,
        interval: entry.interval_secs(),
        ease: entry.ease_factor,
        taken_secs: entry.taken_millis as f32 / 1000.,
        memory_state: None,
        last_interval: entry.last_interval_secs(),
    }
}

#[cfg(test)]
mod test {
    use anki_proto::deck_config::deck_configs_for_update::current_deck::Limits;
    use anki_proto::deck_config::UpdateDeckConfigsMode;

    use super::*;
    use crate::card::FsrsMemoryState;
    use crate::deckconfig::FsrsVersion;
    use crate::deckconfig::UpdateDeckConfigsRequest;
    use crate::revlog::RevlogEntry;
    use crate::revlog::RevlogReviewKind;
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
    fn stats() -> Result<()> {
        let mut col = Collection::new();

        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note = nt.new_note();
        col.add_note(&mut note, DeckId(1))?;

        let cid = col.search_cards("", SortMode::NoOrder)?[0];
        let _report = col.card_stats(cid)?;
        //println!("report {}", report);

        Ok(())
    }

    #[test]
    fn card_stats_retrievability_uses_selected_model_curve() -> Result<()> {
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

        let report = col.card_stats(cid)?;
        let expected = fsrs_current_retrievability_for_params(&params, stability, elapsed_days)?;
        assert_eq!(
            report.fsrs_retrievability.map(|v| format!("{v:.6}")),
            Some(format!("{expected:.6}"))
        );
        Ok(())
    }

    #[test]
    fn card_stats_latest_revlog_uses_current_memory_state() -> Result<()> {
        let mut col = Collection::new();
        let params = fsrs7_params_for_retrievability_test();
        set_selected_fsrs7_params(&mut col, params.clone())?;

        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note = nt.new_note();
        col.add_note(&mut note, DeckId(1))?;

        let cid = col.search_cards("", SortMode::NoOrder)?[0];
        let last_review_time = TimestampSecs::now();
        let stability = 0.0101;
        let stability_internal = 0.0733;
        let mut card = col.storage.get_card(cid)?.unwrap();
        card.memory_state = Some(FsrsMemoryState {
            stability,
            stability_internal,
            difficulty: 9.168,
        });
        card.last_review_time = Some(last_review_time);
        card.decay = Some(params[27]);
        col.storage.update_card(&card)?;
        col.storage.add_revlog_entry(
            &RevlogEntry {
                id: RevlogId(last_review_time.0 * 1000),
                cid,
                usn: Usn(0),
                button_chosen: 3,
                review_kind: RevlogReviewKind::Learning,
                ..Default::default()
            },
            false,
        )?;

        let report = col.card_stats(cid)?;
        let latest = report.revlog[0].memory_state.as_ref().unwrap();
        assert!((latest.stability - stability).abs() < 0.0001);
        assert!((latest.stability_internal.unwrap() - stability_internal).abs() < 0.0001);
        Ok(())
    }
}
