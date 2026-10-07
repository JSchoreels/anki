// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

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
    /// Remove the first eligible main entry, and update counts. Withheld
    /// learning cards stay queued until their repeat minimums are met.
    pub(super) fn pop_main(&mut self) -> Option<MainQueueEntry> {
        let position = self
            .main
            .iter()
            .position(|entry| !self.rwkv_blocks_learning_card(entry.id))?;
        self.main.remove(position).inspect(|head| {
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
}
