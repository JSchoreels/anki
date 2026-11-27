// Copyright: Anki Software, LLC and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use super::CardStateUpdater;
use super::RevlogEntryPartial;
use crate::card::CardType;
use crate::prelude::*;
use crate::scheduler::states::CardState;
use crate::scheduler::states::RelearnState;

impl CardStateUpdater {
    pub(super) fn apply_relearning_state(
        &mut self,
        current: CardState,
        next: RelearnState,
    ) -> Result<RevlogEntryPartial> {
        self.card.interval = next.review.scheduled_days;
        self.card.remaining_steps = next.learning.remaining_steps;
        self.card.ctype = CardType::Relearn;
        self.card.lapses = next.review.lapses;
        self.card.ease_factor = (next.review.ease_factor * 1000.0).round() as u16;
        if let Some(position) = current.new_position() {
            self.card.original_position = Some(position)
        }
        self.card.memory_state = self.memory_state_for_storage(next.learning.memory_state)?;

        self.schedule_step(next.learning.scheduled_secs);

        Ok(RevlogEntryPartial::new(
            current,
            next.into(),
            self.card
                .memory_state
                .map(|d| d.difficulty_shifted())
                .unwrap_or(next.review.ease_factor),
            self.secs_until_rollover(),
        ))
    }
}
