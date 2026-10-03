# Reviewer Scheduling States

The reviewer keeps the currently displayed card's scheduling choices in
`Reviewer._v3.states`. When a desired-retention hook is registered, the reviewer
first fetches the queued card without scheduling states, computes the desired
retention for the card, then reads the scheduling states for that retention.

While that read is pending, the temporary empty `SchedulingStates` value must not
be used to render answer buttons or build an answer. Empty scheduling states
decode as a default new-card state in the backend, which makes review cards fail
the backend current-state check when answered.

Reviewer answer-button rendering waits for populated scheduling states. The
answer path also populates missing states before building the answer, so delayed
callbacks cannot persist an empty placeholder.

The active backend card queue caches the FSRS enabled and short-term-with-steps
flags. Queue construction loads them from collection config; changes to either
key, removal, and undo refresh the cached value without discarding a preserved
queue. Scheduling-state requests without an active queue read collection config
directly.

When scheduling states are requested with a queued card, the returned card and
answer states come from the same calculation. Missing FSRS memory fields are
filled on that returned card without saving it. Requests that skip scheduling
states still return the stored card unchanged for the desired-retention hook.

Card Info reconstructs missing memory state only when FSRS is enabled, using the
card's effective preset. The reconstructed state, desired retention, and decay
are used for the report without being saved to the card, so opening Card Info
does not invalidate the pending review answer.

Again adds a lapse to a review card only on its first answer of the scheduler day.
Later answers that day preserve its lapse count and do not trigger leech tagging
or suspension, even if the earlier answer was successful. This applies across
schedulers, regardless of learning/relearning queue skipping, including
rescheduling filtered decks and Grade Now. Learning/relearning answers already
preserve the lapse count, and preview-only filtered reviews do not count as
scheduling answers. The scheduler uses the configured day rollover and the card's
last answer time, falling back to review history when that time is missing.
