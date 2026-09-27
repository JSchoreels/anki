# RWKV FAQ

This FAQ summarizes the RWKV questions and answers discussed in the following
Discord conversations and Reddit discussion. It is a practical discussion
summary, not a formal algorithm specification. Behavior can differ between
RWKV variants, add-ons, forks, and versions; observations and opinions from the
threads are marked as such.

Last reviewed: 2026-09-27.

## Sources

- [TheMoeWay: Release Anki: RWKV Edition](https://discord.com/channels/617136488840429598/1525841501622370476)
- [Anki: RWKV thread](https://discord.com/channels/368267295601983490/1526305928603766784)
- [Anki: #fsrs-discussion](https://discord.com/channels/368267295601983490/1347982145418694747)
- [Reddit: After ~6 months of work on my fork, integrating FSRS7 and RWKV](https://www.reddit.com/r/Anki/comments/1w0p9qc/after_6_months_of_work_on_my_fork_integrating/)
- [SRS benchmark: RWKV features](https://github.com/open-spaced-repetition/srs-benchmark#features-note)

## Table of contents

- [Terminology](#terminology)
- [1. What RWKV learns and adapts to](#1-what-rwkv-learns-and-adapts-to)
- [2. Decks, presets, and collection scope](#2-decks-presets-and-collection-scope)
- [3. RWKV-Instant versus RWKV-Curve](#3-rwkv-instant-versus-rwkv-curve)
- [4. Retention, efficiency, and workload](#4-retention-efficiency-and-workload)
- [5. Calibration graphs and Search Stats Extended](#5-calibration-graphs-and-search-stats-extended)
- [6. Review deletion, state, and rebuilds](#6-review-deletion-state-and-rebuilds)
- [7. Performance, compatibility, and implementation caveats](#7-performance-compatibility-and-implementation-caveats)
- [8. Code-based answers to previously open questions](#8-code-based-answers-to-previously-open-questions)
  - [How much and how quickly do manually graded cards from a large filtered-deck backlog affect RWKV scheduling?](#how-much-and-how-quickly-do-manually-graded-cards-from-a-large-filtered-deck-backlog-affect-rwkv-scheduling)
  - [Why did disabling “Skip learning/relearning queues with FSRS/RWKV” for one preset appear to affect other presets as well?](#why-did-disabling-skip-learningrelearning-queues-with-fsrsrwkv-for-one-preset-appear-to-affect-other-presets-as-well)
  - [Does the first answer on a new card contribute to the calibration graph in the same way as a later review?](#does-the-first-answer-on-a-new-card-contribute-to-the-calibration-graph-in-the-same-way-as-a-later-review)
  - [How should learning/relearning steps be interpreted when reading the calibration graph?](#how-should-learningrelearning-steps-be-interpreted-when-reading-the-calibration-graph)
  - [Can an explicit daily cap, such as 300 reviews, be met by adapting intervals or desired retention rather than merely limiting the queue?](#can-an-explicit-daily-cap-such-as-300-reviews-be-met-by-adapting-intervals-or-desired-retention-rather-than-merely-limiting-the-queue)
  - [Should auto-burying be enabled or disabled when using RWKV-Instant?](#should-auto-burying-be-enabled-or-disabled-when-using-rwkv-instant)

## Terminology

- **R** means estimated retrievability: the probability that a card will be
  recalled at a given moment.
- **DR** means desired retention: the target retrievability used by an
  interval-based scheduler.
- **RWKV-Instant** is the more dynamic variant discussed in the threads. It
  does not use the displayed interval as its primary scheduling signal and can
  ask for a card again sooner than its displayed interval suggests.
- **RWKV-Curve** is the interval-producing variant. It is intended to feel
  closer to a conventional scheduler while retaining RWKV's learned behavior.

## 1. What RWKV learns and adapts to

### What is RWKV, conceptually?

The most useful mental model from the discussion is that RWKV learned both an
FSRS-like spaced-repetition algorithm and an optimizer for that algorithm. The
shared, pretrained part learns how to update a learner model; the per-user
state is then updated by the user's review stream.

The [SRS benchmark's RWKV description](https://github.com/open-spaced-repetition/srs-benchmark#features-note)
adds a more technical view: RWKV is a modified recurrent architecture that
combines properties of an RNN and a Transformer. It can process the complete
review history across all cards, using grades and intervals together with
features such as review duration, the number of new and reviewed cards that
day, sibling-card information, deck and preset hierarchy, and calendar
context. The benchmark describes RWKV as trained across users and evaluated on
held-out users rather than optimized separately for each user. RWKV-Instant
directly predicts recall probability immediately before a review instead of
using a traditional forgetting curve, so some predictions can look
counter-intuitive, such as never reaching exactly 100% or increasing with
time.

An informal explanation in the thread described this as two kinds of weights:

- shared, slow weights that implement the learned update strategy; and
- fast state associated with the learner, cards, notes, decks, and presets.

This explains why RWKV can be pretrained on many users without simply copying
those users' memories into a new collection. The two-network explanation was a
discussion mental model rather than a formal implementation specification.

### Does RWKV need to be trained separately on my collection?

The thread's answer was no in the FSRS-optimization sense. RWKV is pretrained
and then adapts from the review history it sees in the current collection. It
does not require a separate per-user parameter optimization step before it can
make predictions.

### What information does RWKV use besides grades and intervals?

The Reddit discussion lists several inputs that can be useful beyond the
interval and answer button used by FSRS-7: the card-to-note-to-deck-to-preset
hierarchy, review duration, the number of new and mature cards reviewed that
day, and calendar context such as the day of the week, month, or year.

The 10,000-user dataset discussed there does not contain card text, images, or
audio, so the model trained on that dataset does not use card content. Card and
deck creation dates, collection age, and hour of day were mentioned as possible
additional features under development, not as guaranteed inputs in every build.

### Does it work for people who are much better or worse than average?

The discussion argued that this is one of RWKV's strengths: it is pretrained
on many users, but its state is adaptable enough to learn an individual's
behavior. One participant summarized the claim as RWKV still predicting
`p(recall)` better than FSRS for unusual users. This is a claim from the
discussion, not a guarantee for every collection.

### Would training on more users automatically make RWKV better?

No simple conclusion was reached. The thread noted that the released model was
trained on about 5,000 users, and that using all available users would remove
held-out users needed for an independent benchmark and overfitting check. An
earlier experiment using more users was reported as not being better so far;
the discussion suggested that additional input features might matter more than
just adding users.

The Reddit follow-up described a benchmark split in which RWKV is trained on
5,000 users and evaluated on the other 5,000, with the two halves swapped for
the second run. That is an out-of-sample check against user-level overfitting,
but it is still not a guarantee that every collection will benefit equally.

## 2. Decks, presets, and collection scope

### Do unrelated decks contaminate each other?

Not in the simple sense that every deck is treated as the same subject. The
thread says RWKV knows which deck and preset a review belongs to, and that
different decks can have different retrievability landscapes even within one
collection.

Some information is still useful across decks. For example, a bad day,
fatigue, or a difficult review session can affect performance across subjects.
That is different from assuming that failing equations means a learner will
fail Mandarin.

### Can RWKV handle decks with consistently different difficulty?

The discussion says that RWKV uses the deck as well as the preset, so an easier
B2 language deck and a harder A1 deck are not treated as identical sources of
reviews. That should help it adapt to different difficulty levels, although it
does not guarantee perfect isolation or equal performance for every deck.

### Can I force RWKV to ignore a deck?

The linked discussions did not identify a setting that explicitly discounts a
deck. The practical answer was that RWKV's deck/preset awareness already
mitigates much of the problem. If complete isolation is required, use a
separate Anki profile rather than relying on deck content being unrelated.

### Why did another deck appear to have no reviews?

One user traced this to the deck's **minimum reviews** setting not being set
for the other decks. That was a queue-configuration issue, not evidence that
RWKV had permanently hidden the reviews.

## 3. RWKV-Instant versus RWKV-Curve

| Variant          | What the discussion emphasized                                                                                                 | Main trade-off                                                                                                            |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------- |
| **RWKV-Instant** | Chooses review behavior from the model's current state rather than treating the displayed interval as authoritative.           | More adaptive and potentially efficient, but less predictable; a card can return soon even after showing a long interval. |
| **RWKV-Curve**   | Produces conventional-looking intervals and was described as a balance between FSRS-7 accuracy and Instant's unusual behavior. | Easier to understand, but the discussion considered it less able to react to failures at very low retention.              |

### Why can the due count change during a review session?

This is expected to be more visible with RWKV-Instant. A review can modestly
update the model's expectations for other cards in the same deck or preset, so
the due count can grow or shrink after a review. The Reddit discussion also
described seeing the same word several times in one day, even after pressing
Good, when Instant considered it problematic; the frequency of such short-term
reviews can be adjusted in the fork's settings.

The practical advice in that discussion was to start with the workload one
would normally do with FSRS. If the queue keeps growing, reduce new cards,
suspend some leeches, or continue reviewing rather than assuming that the
initial due count is fixed.

### What does the Hard button mean with RWKV?

**Again means fail; Hard means the answer was correct but difficult.** Using
Hard as a substitute for Again teaches the scheduler the wrong signal. The
discussion also reported that RWKV-Curve could sometimes produce unintuitive
Hard/Good/Easy relationships. Some fork versions expose a toggle enforcing
`Hard < Good < Easy`; the discussion called that a temporary workaround, so
the exact behavior is version-dependent.

### Which variant should I use if I want something stronger than FSRS but still familiar?

The thread's answer was **RWKV-Curve**. It was described as the more balanced
choice for people who want interval lengths and a relatively conventional due
queue. This is a usability recommendation from the discussion, not a universal
ranking of the algorithms.

### Why did a new card show an interval of 3.69 years?

The answer depends on the scheduler actually producing that interval:

- **RWKV-Instant does not use that interval as its main signal**, so the card
  can be shown again soon despite the displayed multi-year interval.
- The interval may instead have come from **FSRS or RWKV-Curve**. The thread
  considered FSRS more able to adapt to a low DR after re-optimization, while
  RWKV-Curve was considered less able to shorten such an interval after a
  failure.

Therefore, a surprising interval alone is not enough to conclude that Instant
has abandoned the card or that the Dynamic DR add-on must be removed.

### Does Dynamic Desired Retention work with RWKV?

Yes, according to the author describing their own setup in `#fsrs-discussion`.
The compatibility still depends on the fork and add-on versions, so verify the
hook is available in the build being used.

### Can desired retention vary by card type?

The Reddit post describes a Dynamic Preset Selection add-on that can assign
different desired-retention values to different kinds of cards, for example by
using a field such as word frequency. This is an add-on and fork feature, not a
property guaranteed by RWKV itself.

## 4. Retention, efficiency, and workload

### Should I use a high or low desired retention?

The discussion described a trade-off rather than a universally correct value:

- Lower R generally gives more coverage and higher efficiency, but less control
  over which cards keep returning and a lower guarantee of what will be
  retained.
- Higher R gives tighter control over retention, but requires substantially
  more review time and reduces the amount of material that can be covered.

A suggested compromise was to use a higher DR for core material and a lower
DR, such as roughly 70% or even 50% for a fast reviewer, for less important
background knowledge. The numbers were personal guidance from the discussion,
not recommended defaults.

### What cards produce the largest expected gain from a review?

The thread proposed ranking a card using an expected interval gain such as:

```text
(p * interval_if_good + (1 - p) * interval_if_again) / current_interval
```

However, Hard and Easy make this more complicated because the scheduler needs a
probability for each possible answer button. Using only historical
Hard/Good/Easy frequencies was considered too crude, while asking another
algorithm for those probabilities would mix models.

The practical rule given in the discussion was that young, easy cards tend to
produce the best gains. Cards with high stability and high difficulty were
described as the least attractive to review repeatedly. More analysis of this
trade-off is linked from the thread to
[fsrs-workload-analysis](https://github.com/JSchoreels/fsrs-workload-analysis).

### Can a bad day create a workload spiral?

Potentially. If fatigue, lack of sleep, or another temporary condition causes a
run of failures, a stateful scheduler may respond by asking for more reviews
when the learner is least able to handle them. The concrete precaution
suggested in the thread was to set a daily maximum review limit when using
RWKV.

### Why can RWKV feel slower than FSRS?

This was reported as a user experience, not a guarantee: some users felt that
RWKV promoted cards more slowly, while they remembered more. One comparison
described 80% DR with RWKV as feeling roughly like 92% DR with FSRS. Backlogged
cards that had already failed could also feel particularly persistent.

The appropriate interpretation is that review count, intervals, and subjective
retention can move in different directions. The displayed interval alone does
not describe what RWKV-Instant will do next.

## 5. Calibration graphs and Search Stats Extended

### What does a 90% bucket in the calibration graph mean?

The graph groups reviews by the model's predicted recall probability and then
compares those predictions with the observed outcomes. A bucket is therefore a
calibration view, not a permanent label attached to a card.

The thread gave two important details:

1. Under FSRS, re-optimizing a preset can move old reviews between buckets
   because the new parameters would assign them different predictions.
2. RWKV has no equivalent optimize step, but the graph can still replay the
   current RWKV model over old reviews. Anki does not reliably record which
   scheduler produced each historical review, so the graph shows what the
   current RWKV model would have predicted for the past, not necessarily what
   the model predicted on the original review date.

That is why a card appearing in a 90% bucket today does not imply that it was
also in the 90% bucket yesterday.

## 6. Review deletion, state, and rebuilds

### What happens if I delete a card that has review history?

The discussion's implementation explanation was:

- RWKV state should reflect the complete review stream, so deleting a reviewed
  card makes the cached state technically stale.
- In the current behavior discussed, the deleted card stays out of the history,
  but its state is kept until the next RWKV rebuild at application launch.
- The next rebuild removes the deleted card's history. Until then, a residual
  effect may remain, although a quick experiment reported little change in the
  resulting predictions.

The reported startup rebuild was about 18 seconds in that discussion. Treat
that timing as version- and collection-dependent.

The Reddit discussion separately says that reviews from deleted cards may still
be useful as information to RWKV. That is distinct from whether a deleted card
remains in the active collection history; the exact training and rebuild
behavior should be verified against the fork version in use.

### Can moving cards, decks, or presets change other cards' due dates?

According to the Reddit discussion, moving a card to another deck, changing a
deck's parent, or assigning a different preset can cause RWKV to recalculate
card and note states and therefore the due-ness of all cards. A resulting
increase or decrease in the due count is an implementation behavior to expect
in the discussed fork, not a promise that every RWKV integration behaves this
way.

### Should deleted reviews be included when training the model?

An experiment described in the thread found a small benchmark advantage to
including deleted reviews during training, even when evaluation excluded them.
The reported reduction in log loss was about 0.13–0.15%, and the conclusion
was to keep deleted reviews in training when optimizing benchmark loss. That
training result does not mean a deleted card should remain in a user's active
collection history after a rebuild.

## 7. Performance, compatibility, and implementation caveats

### Are there precautions for large collections?

One reply warned that the particular SoundJona implementation could require a
large amount of RAM for a large collection and could introduce noticeable lag
when refreshing R between reviews for RWKV-Instant. The reply recommended
considering another implementation or waiting for one with a more suitable
state-management strategy.

This is an implementation-specific warning, not a general hardware
requirement for every RWKV build.

### Does it work on mobile?

The Reddit post describes the fork as primarily intended for desktop review.
AnkiWeb synchronization works, but the mobile client falls back to scheduling
the next reviews with FSRS-6 rather than FSRS-7 or RWKV. Mobile and desktop
therefore should not be expected to produce identical scheduling behavior.

### Can I try the fork without manually migrating my collection?

The post author suggested either replacing the normal installation or
installing the fork separately and syncing through AnkiWeb. This is
fork- and version-specific operational advice, so make a backup before testing
and verify the current fork's install and sync instructions.

### Is the fork an official, stable Anki release?

No. The Reddit post explicitly describes it as an unofficial, experimental
build intended to test scheduling ideas in real use. Use the official Anki
release when stability is the priority.

### Is RWKV ready to behave like ordinary Anki scheduling?

The thread repeatedly characterized RWKV-Instant as a substantial change in how
reviews are presented. The cautious practical position was to keep backups,
use a daily review cap, and expect a less predictable queue. RWKV-Curve was
described as the more familiar compromise for users who want conventional
intervals.

## 8. Code-based answers to previously open questions

The answers below are derived from the current repository implementation rather
than inferred from the discussions. Code paths are repository-relative; line
numbers may move as the code changes.

### How much and how quickly do manually graded cards from a large filtered-deck backlog affect RWKV scheduling?

They are included as RWKV inputs when they produce eligible revlog rows. The
historical query accepts revlog types 0 through 5 and excludes only filtered
rows with `type = 3` and `factor = 0`. Filtered rows are mapped to the
`FILTERED` RWKV state and replayed.

For live reviews, the answer path updates the RWKV backend immediately. The
Grade Now path can apply a batch of answered inputs, and each transition updates
the card, note, deck, preset, and global states before clearing prediction
caches. Later predictions therefore see the batch in order. The code does not
define a fixed numerical weight or a delay such as “one filtered review changes
R by X”; the magnitude depends on the trained runtime and the shared states.
That exact magnitude still requires a runtime experiment.

Code: `qt/aqt/rwkv_scheduler.py:18473-18539`, `qt/aqt/rwkv_scheduler.py:4695-4888`,
`qt/aqt/rwkv_scheduler.py:2322-2435`.

### Why did disabling “Skip learning/relearning queues with FSRS/RWKV” for one preset appear to affect other presets as well?

Because the switch is stored as a collection-wide boolean, not inside each
`DeckConfig`. The deck-options update request reads and writes
`BoolKey::FsrsLearningQueuesDisabled` at collection scope, and the scheduler
later reads that same global value when building card states. The UI exposes the
switch while editing deck options, but saving it changes the shared setting for
all presets.

This is a configuration-scope issue, not evidence that RWKV copied one preset's
learned state into another.

Code: `rslib/src/deckconfig/update.rs:130-138`,
`rslib/src/deckconfig/update.rs:444-458`,
`rslib/src/scheduler/answering/mod.rs:784-820`,
`rslib/src/scheduler/states/mod.rs:142-150`.

### Does the first answer on a new card contribute to the calibration graph in the same way as a later review?

For the repository's RWKV calibration cache, yes: an eligible first learning
answer is included and receives a pre-answer retrievability prediction. The
historical replay recognizes a learning-start row as `LEARN_START`, and the
calibration rebuild records a prediction before applying each answered input.
The history builder keeps the retained learning sequence for each card, so an
older superseded learning sequence may not be replayed.

It is not the same model context as a mature review. When enabled, the first
answer can use elapsed time since card creation as a query feature. The recurrent
state update then removes that creation-age value, while later reviews use the
elapsed time since the previous review. The default configuration enables this
card-creation feature. An external graph can still apply its own filtering.

Code: `qt/aqt/rwkv_scheduler.py:1351-1397`,
`qt/aqt/rwkv_scheduler.py:18090-18220`,
`qt/aqt/rwkv_scheduler.py:18682-18743`,
`rslib/src/deckconfig/mod.rs:40-46`.

### How should learning/relearning steps be interpreted when reading the calibration graph?

The replay gives each eligible revlog row an explicit state:

- raw review kind `0`: `LEARN_START` for the retained sequence's first step,
  then `LEARNING` for later learning steps;
- raw review kind `1`: `REVIEW`;
- raw review kind `2`: `RELEARNING`;
- raw review kind `3`: `FILTERED`;
- raw review kind `4`: `MANUAL`;
- raw review kind `5`: `RESCHEDULED`.

The model also receives queue/state labels: learning uses the learning queue,
relearning uses the day-learning/relearning queue, and the other kinds use the
review queue. Calibration recording nevertheless stores only the revlog ID,
prediction, sample role, and fold index. It does not store the state label in
the cache row, so a graph that needs to separate learning, relearning, and
mature reviews must join the prediction cache back to `revlog.type`. A learning
or relearning point is therefore a calibration result for that step's answer,
not automatically a mature-review calibration point.

Code: `qt/aqt/rwkv_scheduler.py:73-82`,
`qt/aqt/rwkv_scheduler.py:1351-1397`,
`qt/aqt/rwkv_scheduler.py:18736-18760`,
`rslib/src/storage/revlog/mod.rs:362-398`.

### Can an explicit daily cap, such as 300 reviews, be met by adapting intervals or desired retention rather than merely limiting the queue?

Not automatically in the current code. The live queue ranks or filters RWKV
candidates and then applies the normal remaining review limits; gathering stops
when the limit is reached. The workload simulator also treats `review_limit`
as a truncation value while sweeping fixed desired-retention targets. No path
feeds the observed count back into desired retention or rewrites intervals to
solve for a requested cap.

The RWKV-specific `minimum_reviews_per_day` setting is a floor used by Instant
ordering, not a ceiling. Also note that same-day-review exemptions and the
“new cards ignore review limit” option can intentionally bypass parts of the
ordinary cap.

Code: `rslib/src/decks/limits.rs:91-115`,
`rslib/src/decks/limits.rs:190-228`,
`rslib/src/scheduler/queue/builder/gathering.rs:83-190`,
`qt/aqt/rwkv_scheduler.py:12871-12955`,
`qt/aqt/rwkv_scheduler.py:13290-13329`.

### Should auto-burying be enabled or disabled when using RWKV-Instant?

The code does not make this an RWKV decision. After an answer, the scheduler
always checks the deck's normal bury settings. `BuryMode` is built only from
`bury_new`, `bury_reviews`, and `bury_interday_learning`; there is no
RWKV-Instant branch that lets the model choose whether siblings are buried.

Therefore, enabling `bury_reviews` continues to bury review siblings according
to the ordinary queue rules, while disabling it allows them to remain available.
The repository cannot support the Reddit guess that auto-bury should be off as a
correctness rule; that remains a user-policy choice to test.

Code: `rslib/src/scheduler/answering/mod.rs:499-500`,
`rslib/src/scheduler/answering/mod.rs:563-568`,
`rslib/src/scheduler/queue/builder/burying.rs:45-68`,
`rslib/src/scheduler/bury_and_suspend.rs:132-151`.
