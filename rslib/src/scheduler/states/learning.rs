// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use super::fsrs_interval_as_secs;
use super::interval_kind::IntervalKind;
use super::CardState;
use super::ReviewState;
use super::SchedulingStates;
use super::StateContext;
use crate::card::FsrsMemoryState;
use crate::revlog::RevlogReviewKind;

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct LearnState {
    pub remaining_steps: u32,
    pub scheduled_secs: u32,
    pub elapsed_secs: u32,
    pub memory_state: Option<FsrsMemoryState>,
}

impl LearnState {
    pub(crate) fn interval_kind(self) -> IntervalKind {
        IntervalKind::InSecs(self.scheduled_secs)
    }

    pub(crate) fn revlog_kind(self) -> RevlogReviewKind {
        RevlogReviewKind::Learning
    }

    pub(crate) fn next_states(self, ctx: &StateContext) -> SchedulingStates {
        SchedulingStates {
            current: self.into(),
            again: self.answer_again(ctx),
            hard: self.answer_hard(ctx),
            good: self.answer_good(ctx),
            easy: self.answer_easy(ctx).into(),
            dynamic_desired_retentions: None,
            dynamic_desired_retention_enabled: false,
        }
    }

    fn answer_again(self, ctx: &StateContext) -> CardState {
        let memory_state = ctx.fsrs_next_states.as_ref().map(|s| s.again.memory.into());
        if ctx.fsrs_uses_learning_queues() {
            if let Some(again_delay) = ctx.steps.again_delay_secs_learn() {
                return LearnState {
                    remaining_steps: ctx.steps.remaining_for_failed(),
                    scheduled_secs: again_delay,
                    elapsed_secs: 0,
                    memory_state,
                }
                .into();
            }
        }
        {
            let (minimum, maximum) = ctx.min_and_max_review_intervals(1);
            let (interval, short_term) = if let Some(states) = &ctx.fsrs_next_states {
                (
                    states.again.interval,
                    ctx.fsrs_uses_short_term_learning_queue() && states.again.interval < 0.5,
                )
            } else {
                (ctx.graduating_interval_good as f32, false)
            };

            if short_term {
                LearnState {
                    remaining_steps: ctx.steps.remaining_for_failed(),
                    scheduled_secs: fsrs_interval_as_secs(interval, ctx.fsrs_minimum_interval_secs),
                    elapsed_secs: 0,
                    memory_state,
                }
                .into()
            } else {
                let (scheduled_days, fuzz_delta_days) =
                    ctx.with_review_fuzz_and_delta(interval.round().max(1.0), minimum, maximum);
                ReviewState {
                    scheduled_days,
                    fuzz_delta_days,
                    ease_factor: ctx.initial_ease_factor,
                    memory_state,
                    ..Default::default()
                }
                .into()
            }
        }
    }

    fn answer_hard(self, ctx: &StateContext) -> CardState {
        let memory_state = ctx.fsrs_next_states.as_ref().map(|s| s.hard.memory.into());
        if ctx.fsrs_uses_learning_queues() {
            if let Some(hard_delay) = ctx.steps.hard_delay_secs(self.remaining_steps) {
                return LearnState {
                    scheduled_secs: hard_delay,
                    elapsed_secs: 0,
                    memory_state,
                    ..self
                }
                .into();
            }
        }
        {
            let (minimum, maximum) = ctx.min_and_max_review_intervals(1);
            let (interval, short_term) = if let Some(states) = &ctx.fsrs_next_states {
                (
                    states.hard.interval,
                    ctx.fsrs_uses_short_term_learning_queue() && states.hard.interval < 0.5,
                )
            } else {
                (ctx.graduating_interval_good as f32, false)
            };

            if short_term {
                LearnState {
                    remaining_steps: 0,
                    scheduled_secs: fsrs_interval_as_secs(interval, ctx.fsrs_minimum_interval_secs),
                    elapsed_secs: 0,
                    memory_state,
                }
                .into()
            } else {
                let (scheduled_days, fuzz_delta_days) =
                    ctx.with_review_fuzz_and_delta(interval.round().max(1.0), minimum, maximum);
                ReviewState {
                    scheduled_days,
                    fuzz_delta_days,
                    ease_factor: ctx.initial_ease_factor,
                    memory_state,
                    ..Default::default()
                }
                .into()
            }
        }
    }

    fn answer_good(self, ctx: &StateContext) -> CardState {
        let memory_state = ctx.fsrs_next_states.as_ref().map(|s| s.good.memory.into());
        if ctx.fsrs_uses_learning_queues() {
            if let Some(good_delay) = ctx.steps.good_delay_secs(self.remaining_steps) {
                return LearnState {
                    remaining_steps: ctx.steps.remaining_for_good(self.remaining_steps),
                    scheduled_secs: good_delay,
                    elapsed_secs: 0,
                    memory_state,
                }
                .into();
            }
        }
        {
            let (minimum, maximum) = ctx.min_and_max_review_intervals(1);
            let (interval, short_term) = if let Some(states) = &ctx.fsrs_next_states {
                (
                    states.good.interval,
                    ctx.fsrs_uses_short_term_learning_queue() && states.good.interval < 0.5,
                )
            } else {
                (ctx.graduating_interval_good as f32, false)
            };

            if short_term {
                LearnState {
                    remaining_steps: 0,
                    scheduled_secs: fsrs_interval_as_secs(interval, ctx.fsrs_minimum_interval_secs),
                    elapsed_secs: 0,
                    memory_state,
                }
                .into()
            } else {
                let (scheduled_days, fuzz_delta_days) =
                    ctx.with_review_fuzz_and_delta(interval.round().max(1.0), minimum, maximum);
                ReviewState {
                    scheduled_days,
                    fuzz_delta_days,
                    ease_factor: ctx.initial_ease_factor,
                    memory_state,
                    ..Default::default()
                }
                .into()
            }
        }
    }

    fn answer_easy(self, ctx: &StateContext) -> ReviewState {
        let (mut minimum, maximum) = ctx.min_and_max_review_intervals(1);
        let interval = if let Some(states) = &ctx.fsrs_next_states {
            let good = ctx.with_review_fuzz(states.good.interval, minimum, maximum);
            minimum = good + 1;
            states.easy.interval.round().max(1.0) as u32
        } else {
            ctx.graduating_interval_easy
        };
        let (scheduled_days, fuzz_delta_days) =
            ctx.with_review_fuzz_and_delta(interval as f32, minimum, maximum);
        ReviewState {
            scheduled_days,
            fuzz_delta_days,
            ease_factor: ctx.initial_ease_factor,
            memory_state: ctx.fsrs_next_states.as_ref().map(|s| s.easy.memory.into()),
            ..Default::default()
        }
    }
}

#[cfg(test)]
mod test {
    use fsrs::ItemState;
    use fsrs::MemoryState;
    use fsrs::NextStates;

    use super::*;
    use crate::scheduler::states::steps::LearningSteps;

    fn fsrs_item_state(interval: f32) -> ItemState {
        ItemState {
            interval,
            memory: MemoryState {
                stability: 0.1,
                difficulty: 5.0,
                stability_fast: 0.1,
            },
        }
    }

    #[test]
    fn fsrs_short_term_intervals_are_at_least_one_second() {
        let mut ctx = StateContext::defaults_for_testing();
        ctx.steps = LearningSteps::new(&[]);
        ctx.fsrs_allow_short_term = true;
        ctx.fsrs_short_term_with_steps_enabled = true;
        ctx.fsrs_minimum_interval_secs = 5;
        ctx.fsrs_next_states = Some(NextStates {
            again: fsrs_item_state(0.000001),
            hard: fsrs_item_state(0.000001),
            good: fsrs_item_state(0.000001),
            easy: fsrs_item_state(1.0),
        });

        let state = LearnState {
            remaining_steps: 0,
            scheduled_secs: 0,
            elapsed_secs: 0,
            memory_state: None,
        };
        let next = state.next_states(&ctx);

        let CardState::Normal(super::super::NormalState::Learning(again)) = next.again else {
            panic!("Again should stay in learning");
        };
        assert_eq!(again.scheduled_secs, 5);

        let CardState::Normal(super::super::NormalState::Learning(hard)) = next.hard else {
            panic!("Hard should stay in learning");
        };
        assert_eq!(hard.scheduled_secs, 5);

        let CardState::Normal(super::super::NormalState::Learning(good)) = next.good else {
            panic!("Good should stay in learning");
        };
        assert_eq!(good.scheduled_secs, 5);
    }

    #[test]
    fn fsrs_short_term_can_follow_configured_learning_steps() {
        let mut ctx = StateContext::defaults_for_testing();
        ctx.steps = LearningSteps::new(&[1.0]);
        ctx.fsrs_allow_short_term = true;
        ctx.fsrs_short_term_with_steps_enabled = true;
        ctx.fsrs_minimum_interval_secs = 5;
        ctx.fsrs_next_states = Some(NextStates {
            again: fsrs_item_state(0.000001),
            hard: fsrs_item_state(0.000001),
            good: fsrs_item_state(0.000001),
            easy: fsrs_item_state(1.0),
        });

        let state = LearnState {
            remaining_steps: 1,
            scheduled_secs: 60,
            elapsed_secs: 0,
            memory_state: None,
        };
        let next = state.next_states(&ctx);

        let CardState::Normal(super::super::NormalState::Learning(good)) = next.good else {
            panic!("Good should stay in short-term learning after final configured step");
        };
        assert_eq!(good.remaining_steps, 0);
        assert_eq!(good.scheduled_secs, 5);

        let followup = good.next_states(&ctx);
        let CardState::Normal(super::super::NormalState::Learning(hard)) = followup.hard else {
            panic!("Hard should stay in FSRS short-term learning");
        };
        assert_eq!(hard.remaining_steps, 0);
        assert_eq!(hard.scheduled_secs, 5);
    }
}
