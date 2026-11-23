# RWKV Fork FAQ

This FAQ collects community questions about this fork's RWKV features. Source
questions are quoted and linked below. Related questions are summarized first.
For a deeper explanation of RWKV and this fork's implementation, see
[RWKV FAQ](RWKV_FAQ.md).

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

### How should I interpret the due count in RWKV-Instant?

The due count is a snapshot of the cards currently eligible for review under
the latest predictions and scheduling rules. As you answer, RWKV updates its
assessment of your memory, which can change the eligibility of other cards.

**Each review provides new evidence about your current memory, while the
practice itself can also change what you remember.**

Conventional FSRS chooses a card's next interval using the information available
when that card was last answered. RWKV-Instant can reassess its eligibility
using subsequent reviews of other cards, including your performance during
today's session. FSRS can still calculate retrievability for today; the
difference is the information used to update that prediction, not simply when
the calculation runs.

For example, after a two-week break, RWKV-Instant might initially identify
10,000 cards as needing review. If your first 500 answers go better than
expected, that provides evidence for revising predictions for the remaining
cards. Some may move above your desired-retention threshold without being
reviewed themselves. If your answers go worse than expected, the count can
increase instead.

This combines two effects: the model learns more about your present condition,
and reviewing may improve your actual recall. A changing prediction does not
directly establish which memory connections changed.

**A count of 3,000 does not necessarily mean you must answer 3,000 times to
finish.** Some cards may stop being eligible as predictions change, while
others may become eligible. The count could fall much faster than one card per
answer—or increase. The example of “3,000 to zero in 500 reviews” illustrates
that possibility; it is not a measured result or a guarantee.

Consequently, an increase from 1,000 to 3,000 due cards does not establish that
your actual daily workload has tripled. It shows that the schedulers currently
assess your review needs differently.

You can choose a sustainable stopping point, just as with FSRS. Reviewing less
can affect the retention you achieve or how many cards you can maintain at your
target retention. For a predictable daily workload, see
[Should I limit my daily reviews? Why and how?](#should-i-limit-my-daily-reviews-why-and-how)

Prediction accuracy is also distinct from workload. The benchmark evaluates
predictions against recorded review outcomes; RWKV does not earn better scores
by assigning extra reviews during that evaluation. A percentage improvement in
prediction error does not directly translate into the same percentage increase
in retention.
[Benchmark methodology](https://github.com/open-spaced-repetition/srs-benchmark#evaluation)

**In the Browser, `is:due` selects review cards the way the deck list counts
them.** For review cards whose preset uses RWKV-Instant, `is:due` uses the same
eligibility; other cards use their due dates. The deck list can still show more
reviews: it adds the preset's daily minimum of RWKV reviews, which `is:due`
does not include. On a parent deck whose subdecks use other presets, the counts
can also differ, because the deck list applies the parent's preset to every
subdeck. To search by due dates only, use `is:fsrs:due`.

Sources: [Q3](#q3--changing-due-counts-and-backlogs)
and [Q6](#q6--interpreting-due-counts-and-workload).
Details: [RWKV-Instant and RWKV-Curve](RWKV_FAQ.md#how-do-rwkv-instant-and-rwkv-curve-differ),
[fresh review information](RWKV_FAQ.md#why-does-fresh-review-information-matter),
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

### Can I donate my review history for research?

A way to donate review history is in development. The plan is to make uploads
easy directly from the desktop fork. We’ll share more details in the coming
weeks.

Source: [Q7](#q7--donating-review-history).

### Can I compare two schedulers in the same collection?

The fork has no built-in randomized comparison. To run one manually, randomly
split your deck into two subdecks, give each its own preset and scheduler, and
use the same desired retention. Study each subdeck separately, since starting
from the parent can apply the parent’s settings to review selection.

Track both reviews per card and actual recall rates. The same desired retention
can yield different recall rates, and RWKV can use reviews from both groups as
shared collection history.

Source: [Q8](#q8--comparing-schedulers).

## Source questions

### Q1 — Getting started

> Does anyone here have a guide on how to approach it?

Reddit, 2026-11-20 —
[source post](https://www.reddit.com/r/Anki/comments/1y0u0h1/recommended_settings_for_rwkv_instant_on_anki/)

Answer: [How do I enable RWKV-Instant?](#how-do-i-enable-rwkv-instant)

### Q3 — Changing due counts and backlogs

> Will there be a huge backlog after enabling it?i tested it and the review count changes after every single review so I'm not sure how to tell

Reddit, 2026-11-20 —
[source post](https://www.reddit.com/r/Anki/comments/1y0u0h1/recommended_settings_for_rwkv_instant_on_anki/)

Answer: [How should I interpret the due count in RWKV-Instant?](#how-should-i-interpret-the-due-count-in-rwkv-instant)

### Q6 — Interpreting due counts and workload

> It went from 1k to 3k

> I thought the point of rwkv is that you can just stop whenever you want, instead of having a fixed amount of workload per day?

Anki Discord, 2026-11-21 —
[source discussion](https://discord.com/channels/368267295601983490/1347982145418694747/1573680088665432259)

Answer: [How should I interpret the due count in RWKV-Instant?](#how-should-i-interpret-the-due-count-in-rwkv-instant)

### Q2 — Daily review limits

> I would like to have a maximum of 150 reviews per day (since RWKV can give a huge amount of cards so I'd rather limit it)

Reddit, 2026-11-20 —
[source post](https://www.reddit.com/r/Anki/comments/1y0u0h1/recommended_settings_for_rwkv_instant_on_anki/)

Answer: [Should I limit my daily reviews? Why and how?](#should-i-limit-my-daily-reviews-why-and-how)

### Q5 — Sync and other devices

> Can this version sync with ankiweb? And if it can, since its pc only, can you tell me how it handle merging different platforms running current fsrs versions? Ty

Reddit, 2026-11-19 —
[source comment](https://www.reddit.com/r/Anki/comments/1y0lor6/comment/19sw7p1gno48z/)

Answer: [Can I sync with AnkiWeb and keep reviewing on other devices?](#can-i-sync-with-ankiweb-and-keep-reviewing-on-other-devices)

### Q4 — Reverse cards and sibling burying

> Would you recommend enabling all 3 sibling-burying toggles, or disabling "Precompute next card" + setting the review sort order to "Ascending retrievability"?

Reddit, 2026-11-20 —
[source comment](https://www.reddit.com/r/Anki/comments/1y0u0h1/comment/19sxq0vwlrc69/)

Answer: [Should reverse cards use sibling burying or RWKV ordering?](#should-reverse-cards-use-sibling-burying-or-rwkv-ordering)

### Q7 — Donating review history

> Hello! I use your fork. Just wondering, do you know if there's any way I can donate my review history for study?

Reddit, 2026-11-22 —
[source comment](https://www.reddit.com/r/Anki/comments/1y1uivb/comment/19t0qo8e0upog/)

Answer: [Can I donate my review history for research?](#can-i-donate-my-review-history-for-research)

### Q8 — Comparing schedulers

> does any fork let two schedulers run side by side in one collection, with cards split at random and the same desired retention?

Reddit —
[source comment](https://www.reddit.com/r/Anki/comments/1xws8kt/comment/19so9ibzv744i/)

Answer: [Can I compare two schedulers in the same collection?](#can-i-compare-two-schedulers-in-the-same-collection)
