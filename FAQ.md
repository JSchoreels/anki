# RWKV Fork FAQ

This FAQ collects public questions about this fork's RWKV features, including
posts that mention the fork or JSchoreels. Source questions are quoted and
linked below. Related questions are summarized first. For a deeper explanation
of RWKV and this fork's implementation, see [RWKV FAQ](RWKV_FAQ.md).

Last reviewed: 2026-11-21.

## Common questions

### How do I enable RWKV-Instant?

In the desktop fork, open **Deck Options → Scheduler → Review scheduler** and
select **RWKV-Instant** for the relevant preset. **Fallback : Due-Date
Calculator** selects the model that writes stored intervals and due dates for
statistics and clients without RWKV support. Instant selects reviews using
current recall predictions rather than those stored due dates.

Source: [Q1](#q1--getting-started).
Details: [Where do I select the scheduler?](RWKV_FAQ.md#where-do-i-select-the-scheduler)

### Why does the due count change after each answer? Will switching create a backlog?

RWKV-Instant reassesses recall predictions as you review. An answer can change
predictions for other cards, so cards can enter or leave the eligible pool
during a session. Switching from FSRS can therefore reveal more or fewer
eligible reviews. The displayed count is a changing snapshot, not a fixed
checklist. Better recall prediction does not guarantee fewer reviews.

If you want a predictable stopping point despite those changes, use a daily
review limit as explained in the next answer.

Source: [Q3](#q3--changing-due-counts-and-backlogs).
Details: [RWKV-Instant and RWKV-Curve](RWKV_FAQ.md#how-do-rwkv-instant-and-rwkv-curve-differ)
and [workload](RWKV_FAQ.md#does-rwkv-guarantee-fewer-reviews-than-fsrs).

### Should I limit my daily reviews? Why and how?

A daily maximum is useful if you want to keep reviews within a workload you can
sustain. Instant's eligible review count can increase during a session, so
finishing the displayed count is not a fixed daily target. A limit gives you a
stopping point; it does not guarantee that every eligible card has been reviewed
or automatically adjust desired retention or intervals to fit the cap.

Set **Daily Limits → Maximum reviews/day** to your chosen maximum, such as
**150**. This limits reviews rather than every card presentation: learning steps
and new cards have separate rules, and enabling **Same-day reviews ignore review
limit** allows eligible repeats beyond the maximum.

**Minimum reviews per day** is an optional floor, not a cap. It can pull
additional cards forward when Instant would otherwise offer fewer reviews.

Source: [Q2](#q2--daily-review-limits).
Details: [How does the fork handle daily review limits?](RWKV_FAQ.md#how-does-the-fork-handle-daily-review-limits)

### Can I sync with AnkiWeb and keep reviewing on other devices?

Yes, AnkiWeb synchronization works. RWKV's desktop state cache and live queue
scores remain local. A client without RWKV support uses the synchronized due
dates and its own supported scheduler rather than running desktop Instant's
live selection. Instant's fallback calculator supplies stored intervals and
due dates. Sync compatibility therefore does not mean every device uses the
same scheduling algorithm.

Source: [Q5](#q5--sync-and-other-devices).
Details: [RWKV review scheduler](docs/rwkv.MD#rwkv-review-scheduler)
and [local state storage](docs/rwkv.MD#settings-and-storage).

### Should reverse cards use sibling burying or RWKV ordering?

Sibling burying remains controlled by Anki's normal settings. Enable the
relevant toggles when you want sibling cards deferred. **Ascending
retrievability** orders cards by predicted recall; with precomputation disabled,
this order waits for updated predictions after an answer. Those settings affect
selection and responsiveness, but do not guarantee that siblings will be skipped
or replace sibling burying.

Source: [Q4](#q4--reverse-cards-and-sibling-burying).
Details: [Sibling burying](RWKV_FAQ.md#does-rwkv-override-standard-sibling-burying)
and [queue refresh behavior](docs/rwkv.MD).

## Source questions

### Q1 — Getting started

> Does anyone here have a guide on how to approach it?

u/Ready-Bumblebee-6220, 2026-11-20,
[Recommended settings for RWKV Instant on Anki?](https://www.reddit.com/r/Anki/comments/1y0u0h1/recommended_settings_for_rwkv_instant_on_anki/)

Answer: [How do I enable RWKV-Instant?](#how-do-i-enable-rwkv-instant)

### Q3 — Changing due counts and backlogs

> Will there be a huge backlog after enabling it?i tested it and the review count changes after every single review so I'm not sure how to tell

u/Ready-Bumblebee-6220, 2026-11-20,
[Recommended settings for RWKV Instant on Anki?](https://www.reddit.com/r/Anki/comments/1y0u0h1/recommended_settings_for_rwkv_instant_on_anki/)

Answer: [Why does the due count change after each answer? Will switching create a backlog?](#why-does-the-due-count-change-after-each-answer-will-switching-create-a-backlog)

### Q2 — Daily review limits

> I would like to have a maximum of 150 reviews per day (since RWKV can give a huge amount of cards so I'd rather limit it)

u/Ready-Bumblebee-6220, 2026-11-20,
[Recommended settings for RWKV Instant on Anki?](https://www.reddit.com/r/Anki/comments/1y0u0h1/recommended_settings_for_rwkv_instant_on_anki/)

Answer: [Should I limit my daily reviews? Why and how?](#should-i-limit-my-daily-reviews-why-and-how)

### Q5 — Sync and other devices

> Can this version sync with ankiweb? And if it can, since its pc only, can you tell me how it handle merging different platforms running current fsrs versions? Ty

u/kronpas, 2026-11-19,
[Original sync question in Open Reply to LMSherlock from the Anki Team](https://www.reddit.com/r/Anki/comments/1y0lor6/comment/19sw7p1gno48z/)

Answer: [Can I sync with AnkiWeb and keep reviewing on other devices?](#can-i-sync-with-ankiweb-and-keep-reviewing-on-other-devices)

### Q4 — Reverse cards and sibling burying

> Would you recommend enabling all 3 sibling-burying toggles, or disabling "Precompute next card" + setting the review sort order to "Ascending retrievability"?

u/ozonep2, 2026-11-20,
[Original reverse-card question in Recommended settings for RWKV Instant on Anki?](https://www.reddit.com/r/Anki/comments/1y0u0h1/comment/19sxq0vwlrc69/)

Answer: [Should reverse cards use sibling burying or RWKV ordering?](#should-reverse-cards-use-sibling-burying-or-rwkv-ordering)
