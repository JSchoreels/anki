# Statistics Data Notes

## Future Due

The Future Due graph reads review-card due buckets from the backend
`future_due` stats response. Bucket keys are relative days from today: negative
keys are review cards due before today, `0` is due today, and positive keys are
future due days.

The displayed Backlog count is the sum of all negative due-day buckets already
present in that response. It is independent of the graph's Backlog checkbox,
which only controls whether those negative buckets are drawn in the histogram.
