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
