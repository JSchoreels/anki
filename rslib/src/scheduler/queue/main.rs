// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::collections::HashMap;

use super::CardQueues;
use crate::prelude::*;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct MainQueueEntry {
    pub id: CardId,
    pub mtime: TimestampSecs,
    pub kind: MainQueueEntryKind,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum MainQueueEntryKind {
    New,
    Review,
    InterdayLearning,
}

impl CardQueues {
    /// Remove the head of the main queue, and update counts.
    pub(super) fn pop_main(&mut self) -> Option<MainQueueEntry> {
        self.main.pop_front().inspect(|head| {
            match head.kind {
                MainQueueEntryKind::New => self.counts.new -= 1,
                MainQueueEntryKind::Review => self.counts.review -= 1,
                MainQueueEntryKind::InterdayLearning => {
                    // the bug causing learning counts to go below zero should
                    // hopefully be fixed at this point, but ensure we don't wrap
                    // if it isn't
                    self.counts.learning = self.counts.learning.saturating_sub(1)
                }
            };
        })
    }

    /// Add an undone entry to the top of the main queue.
    pub(super) fn push_main(&mut self, entry: MainQueueEntry) {
        match entry.kind {
            MainQueueEntryKind::New => self.counts.new += 1,
            MainQueueEntryKind::Review => self.counts.review += 1,
            MainQueueEntryKind::InterdayLearning => self.counts.learning += 1,
        };
        self.main.push_front(entry);
    }

    pub(crate) fn resort_review_entries_by_retrievability(
        &mut self,
        scores: &HashMap<CardId, f32>,
        descending: bool,
    ) {
        let pinned_top_card = self.shown_top_card.filter(|card_id| {
            self.main
                .front()
                .map(|entry| {
                    entry.id == *card_id && matches!(entry.kind, MainQueueEntryKind::Review)
                })
                .unwrap_or(false)
        });
        let mut review_entries: Vec<_> = self
            .main
            .iter()
            .copied()
            .filter(|entry| {
                matches!(entry.kind, MainQueueEntryKind::Review)
                    && Some(entry.id) != pinned_top_card
            })
            .collect();
        review_entries.sort_by(|a, b| {
            let score_a = scores.get(&a.id).copied().filter(|score| score.is_finite());
            let score_b = scores.get(&b.id).copied().filter(|score| score.is_finite());
            match (score_a, score_b) {
                (Some(score_a), Some(score_b)) => {
                    let ord = score_a.total_cmp(&score_b);
                    let ord = if descending { ord.reverse() } else { ord };
                    ord.then_with(|| a.id.cmp(&b.id))
                }
                (Some(_), None) => std::cmp::Ordering::Less,
                (None, Some(_)) => std::cmp::Ordering::Greater,
                (None, None) => std::cmp::Ordering::Equal,
            }
        });

        let mut review_entries = review_entries.into_iter();
        for entry in &mut self.main {
            if matches!(entry.kind, MainQueueEntryKind::Review) && Some(entry.id) != pinned_top_card
            {
                *entry = review_entries.next().unwrap();
            }
        }
    }
}
