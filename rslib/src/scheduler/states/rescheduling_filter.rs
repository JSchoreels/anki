// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use super::interval_kind::IntervalKind;
use super::normal::NormalState;
use super::CardState;
use super::SchedulingStates;
use super::StateContext;
use crate::revlog::RevlogReviewKind;

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ReschedulingFilterState {
    pub original_state: NormalState,
}

impl ReschedulingFilterState {
    pub(crate) fn interval_kind(self) -> IntervalKind {
        self.original_state.interval_kind()
    }

    pub(crate) fn revlog_kind(self) -> RevlogReviewKind {
        self.original_state.revlog_kind()
    }

    pub(crate) fn next_states(self, ctx: &StateContext) -> SchedulingStates {
        let normal = self.original_state.next_states(ctx);
        if ctx.in_filtered_deck {
            let return_steps = ctx.uses_review_queue_for_steps();
            SchedulingStates {
                current: self.into(),
                again: maybe_wrap(normal.again, return_steps),
                hard: maybe_wrap(normal.hard, return_steps),
                good: maybe_wrap(normal.good, return_steps),
                easy: maybe_wrap(normal.easy, return_steps),
                dynamic_desired_retentions: normal.dynamic_desired_retentions,
                dynamic_desired_retention_enabled: normal.dynamic_desired_retention_enabled,
            }
        } else {
            // card is marked as filtered, but not in a filtered deck; convert to normal
            normal
        }
    }
}

/// Cards that go into Review return to their original deck.
fn maybe_wrap(state: CardState, return_steps: bool) -> CardState {
    match state {
        CardState::Normal(normal) => {
            if return_steps || matches!(normal, NormalState::Review(_)) {
                normal.into()
            } else {
                ReschedulingFilterState {
                    original_state: normal,
                }
                .into()
            }
        }
        CardState::Filtered(_) => {
            unreachable!()
        }
    }
}
