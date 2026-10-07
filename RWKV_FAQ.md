# RWKV FAQ

This FAQ explains RWKV as a spaced-repetition algorithm and summarizes what
the RWKV support in this Anki fork currently does. It uses the benchmark
description, the linked message-level discussions, and the repository
implementation. It intentionally leaves out individual bug reports,
temporary workarounds, and
subjective comparisons unless they clarify a stable behavior.

Behavior can differ between RWKV variants, add-ons, forks, and versions. The
fork-specific statements below describe the current repository and may change
as the code changes.

Last reviewed: 2026-09-27.

## Sources

- [TheMoeWay: deck-scope question](https://discord.com/channels/617136488840429598/1525841501622370476/1551521915452002356)
- [TheMoeWay: calibration-bucket question](https://discord.com/channels/617136488840429598/1525841501622370476/1552174896539238511)
- [TheMoeWay: RWKV-Instant interval explanation](https://discord.com/channels/617136488840429598/1525841501622370476/1553715420295462957)
- [Anki: RWKV-Curve/Instant question](https://discord.com/channels/368267295601983490/1526305928603766784/1552363440973025311)
- [Anki: deck/preset context reply](https://discord.com/channels/368267295601983490/1526305928603766784/1552364924376850545)
- [Anki: mobile-support reply](https://discord.com/channels/368267295601983490/1347982145418694747/1553731488997052537)
- [Reddit: fork description](https://www.reddit.com/r/Anki/comments/1w0p9qc/comment/paqq1oe/)
- [SRS benchmark: RWKV features](https://github.com/open-spaced-repetition/srs-benchmark#features-note)

## Table of contents

- [Terminology](#terminology)
- [1. What RWKV is and how it learns](#1-what-rwkv-is-and-how-it-learns)
  - [What is RWKV?](#what-is-rwkv)
  - [Does RWKV train itself on my collection?](#does-rwkv-train-itself-on-my-collection)
  - [What features does RWKV use?](#what-features-does-rwkv-use)
  - [Does RWKV use card content?](#does-rwkv-use-card-content)
  - [Does RWKV adapt to different users and subjects?](#does-rwkv-adapt-to-different-users-and-subjects)
- [2. RWKV-Instant and RWKV-Curve](#2-rwkv-instant-and-rwkv-curve)
  - [How do RWKV-Instant and RWKV-Curve differ?](#how-do-rwkv-instant-and-rwkv-curve-differ)
  - [How do intervals and desired retention fit in?](#how-do-intervals-and-desired-retention-fit-in)
  - [How do answer buttons affect the model?](#how-do-answer-buttons-affect-the-model)
- [3. Collection, deck, and preset scope](#3-collection-deck-and-preset-scope)
  - [What scope does the model use?](#what-scope-does-the-model-use)
  - [Can decks and presets have different behavior?](#can-decks-and-presets-have-different-behavior)
  - [What happens when a card or preset moves?](#what-happens-when-a-card-or-preset-moves)
- [4. Scheduling, limits, and workload](#4-scheduling-limits-and-workload)
  - [How does the fork handle daily review limits?](#how-does-the-fork-handle-daily-review-limits)
  - [Can RWKV-Instant repeat a card when same-day reviews are disabled?](#can-rwkv-instant-repeat-a-card-when-same-day-reviews-are-disabled)
  - [How do the same-day settings interact?](#how-do-the-same-day-settings-interact)
  - [Does RWKV override standard sibling burying?](#does-rwkv-override-standard-sibling-burying)
  - [Does RWKV guarantee fewer reviews than FSRS?](#does-rwkv-guarantee-fewer-reviews-than-fsrs)
- [5. Calibration and prediction history](#5-calibration-and-prediction-history)
  - [What does a calibration bucket mean?](#what-does-a-calibration-bucket-mean)
  - [Does the first answer on a new card count?](#does-the-first-answer-on-a-new-card-count)
  - [How are learning and relearning steps represented?](#how-are-learning-and-relearning-steps-represented)
- [6. History and state in this fork](#6-history-and-state-in-this-fork)
  - [Which review events enter the RWKV history?](#which-review-events-enter-the-rwkv-history)
  - [How is RWKV state rebuilt?](#how-is-rwkv-state-rebuilt)
- [7. Platform and implementation scope](#7-platform-and-implementation-scope)
  - [Does the fork run on mobile?](#does-the-fork-run-on-mobile)
  - [Is this an official Anki release?](#is-this-an-official-anki-release)
  - [What are the resource and performance trade-offs?](#what-are-the-resource-and-performance-trade-offs)
  - [What does this fork add to Anki?](#what-does-this-fork-add-to-anki)
- [Repository code references](#repository-code-references)

## Terminology

- **R** means estimated retrievability: the probability that a card will be
  recalled at a given moment.
- **Desired retention (DR)** is a target retrievability used by interval-based
  scheduling. It is a scheduling target, not a new set of model weights.
- **Model state** is the information carried forward between review events. In
  a recurrent model, it is what lets the next prediction depend on prior
  history.
- **RWKV-Instant** is the more dynamic variant. It predicts recall directly and
  does not treat the displayed interval as its only scheduling signal.
- **RWKV-Curve** is the interval-producing variant. It presents the model's
  behavior through a more conventional forgetting-curve-style schedule.

## 1. What RWKV is and how it learns

### What is RWKV?

RWKV is a neural-network-based spaced-repetition scheduler. The SRS benchmark
describes its modified RWKV architecture as combining properties of recurrent
neural networks and Transformers. In a scheduler, the model processes review
events and produces a recall prediction or another scheduling signal.

The important difference from a small formula that only transforms the current
card's interval and grade is that RWKV can carry information from a longer
review history. In the benchmark, that history includes all cards in the
collection. The exact inputs and scheduling behavior depend on the RWKV model
variant and the integration using it.

### Does RWKV train itself on my collection?

Not in normal use. The model weights are trained beforehand. When the fork
loads RWKV for a collection, it replays the available review history and
updates model state; it does not run a per-user gradient-optimization step
before making predictions.

This distinction is useful:

- **Pretraining** learns shared model weights from many users.
- **State updates** adapt those weights to the current collection's review
  history.

State adaptation can make the model personal without copying another user's
memories into the collection. The exact state representation is an
implementation detail, not a claim that the model has one independent neural
network per user.

### What features does RWKV use?

The benchmark describes RWKV as using interval lengths and grades together
with features such as:

- review duration;
- the number of new and reviewed cards completed that day;
- sibling-card information;
- deck and preset hierarchy; and
- calendar context such as the day, month, and year.

The available features are model- and build-dependent. A feature listed by the
benchmark should not be assumed to exist in every RWKV fork or every model
version.

### Does RWKV use card content?

The benchmark dataset contains review metadata rather than card text, images,
or audio, so the benchmarked RWKV model does not learn from card content. The
fork's scheduling inputs are review events and collection metadata, not a
semantic analysis of the words or images on a card.

### Does RWKV adapt to different users and subjects?

Yes, through the combination of shared pretrained weights and collection
history. The model can use learner-level signals while also receiving card,
note, deck, and preset context. This allows it to adapt without requiring a
separate parameter-optimization run for each user.

That does not guarantee better predictions for every collection. Benchmark
results are population-level measurements, and individual behavior, data
quality, and the model version still matter.

## 2. RWKV-Instant and RWKV-Curve

| Variant          | Scheduling behavior                                                                                           | Main consequence                                                                                                           |
| ---------------- | ------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| **RWKV-Instant** | Predicts the probability of recall immediately before a review and orders cards from the current model state. | The displayed interval is not authoritative; a card may return sooner or later than a conventional scheduler would choose. |
| **RWKV-Curve**   | Uses RWKV predictions to produce interval-based, forgetting-curve-style scheduling.                           | Intervals and due dates are easier to interpret, while the model still uses its learned state.                             |

### How do RWKV-Instant and RWKV-Curve differ?

RWKV-Instant predicts recall directly at the moment a review is considered. It
does not assume a traditional monotonic forgetting curve, so its outputs can
look unusual: it may not reach exactly 100%, and a predicted recall
probability can sometimes increase with time when other state features change.

RWKV-Curve exposes the model through interval-producing behavior. It is the
closer fit for a user who wants a conventional interval display while still
using RWKV's learned representation. This distinction is also illustrated in
the [RWKV-Curve/Instant discussion](https://discord.com/channels/368267295601983490/1526305928603766784/1552363440973025311).

### How do intervals and desired retention fit in?

An interval is an important input and output for conventional scheduling, but
it is not RWKV-Instant's only signal. Instant uses the current predicted recall
and queue policy to decide which cards to present; the displayed interval does
not by itself determine the next review. The [fork's interval explanation](https://discord.com/channels/617136488840429598/1525841501622370476/1553715420295462957)
describes the same separation between Instant and interval-producing
schedulers.

The fork contains hooks for desired-retention overrides and reads desired
retention from the relevant scheduling context. An add-on or configuration can
provide different targets for different cards or decks when its integration
matches the fork. Changing desired retention changes scheduling targets; it
does not retrain the RWKV weights.

### How do answer buttons affect the model?

Again, Hard, Good, and Easy are review outcomes supplied to the scheduler.
Again represents a failure; Hard, Good, and Easy represent increasing levels
of successful recall. Using Hard as a substitute for Again gives the model a
different signal from the one intended by Anki's grading semantics.

The fork also distinguishes learning, relearning, filtered, manually
rescheduled, and ordinary review events. Their state labels and queue handling
are described in the implementation sections below.

## 3. Collection, deck, and preset scope

### What scope does the model use?

The benchmark describes RWKV as reading the review history of all cards. The
fork also carries context for cards, notes, decks, presets, and collection-wide
state. As a result, a review in one deck can contribute to shared learner
context while deck and preset features tell the model where that review came
from. This collection-wide behavior is the subject of the [deck-scope question](https://discord.com/channels/617136488840429598/1525841501622370476/1551521915452002356),
and a separate [deck/preset discussion](https://discord.com/channels/368267295601983490/1526305928603766784/1552364924376850545)
highlights the contextual features.

This is not the same as treating all subjects as identical. It means that the
model can combine general learner signals with subject- and deck-specific
context.

### Can decks and presets have different behavior?

Yes. Deck and preset hierarchy are available to the model, and Anki's normal
deck and preset settings continue to apply. Desired-retention and review-limit
values can also be resolved from the applicable deck or preset.

There is no general promise that decks are statistically independent. If strict
isolation is required, use separate profiles or another explicit isolation
boundary rather than relying only on deck names.

### What happens when a card or preset moves?

Moving a card, changing its deck hierarchy, or changing its preset changes the
context used by the scheduler. The fork can therefore recalculate dependent
card, note, deck, or preset state and change predictions or due ordering.
That is a consequence of a stateful, hierarchy-aware scheduler; it is not a
permanent deletion of review history.

## 4. Scheduling, limits, and workload

### Where do I select the scheduler?

In Deck Options, use **Scheduler → Review scheduler** to choose FSRS-6,
FSRS-7, RWKV-Curve, or RWKV-Instant for the preset. Instant adds a separate
**Fallback : Due-Date Calculator**: choose FSRS or Curve for stored due dates
used by mobile sync and statistics. Instant ignores those dates when selecting
reviews. The separate Curve and Instant enable switches have
been replaced by these selectors. Older FSRS versions are no longer offered;
existing saved choices are preserved until you explicitly select a current model.

**Machine Learning Based scheduling** remains collection-wide. Choosing a model
enables it; other presets keep their own model choices. Turning it off restores
SM2 for native intervals, while the existing per-preset RWKV settings remain
independent.

### How does the fork handle daily review limits?

The fork still uses Anki's normal review limits and queue truncation. RWKV
orders or filters candidates, after which the applicable limits determine how
many cards are gathered. The current code does not solve for a requested daily
cap by automatically changing desired retention or rewriting intervals.

The RWKV-specific `minimum_reviews_per_day` setting is a floor used by Instant
ordering, not a maximum. Same-day-review exemptions and the option that lets
new cards ignore the review limit can also make the final count differ from a
simple cap.

### Can RWKV-Instant repeat a card when same-day reviews are disabled?

Turning off **Allow same day review for (re)learning steps** prevents
RWKV-Instant from showing a card already answered that scheduler day, with or
without FSRS. This also applies when **Skip learning/relearning queues with
FSRS/RWKV** is enabled. With the switch on, both repeat-spacing minimums must
still be met. The former RWKV-specific same-day switch has been removed; its
saved value no longer blocks repeats.

**Allow same day review for (re)learning steps** defaults to on. An explicitly
saved off choice is preserved. It also controls generated short-term
learning/relearning intervals for FSRS-7 and RWKV-Curve.
With empty steps, or after the final configured step, turning it off schedules
generated intervals as review states of at least one day. Configured steps still
apply while learning/relearning queues are enabled; **Skip learning/relearning
queues with FSRS/RWKV** bypasses those steps as well.

If you want manual steps such as `1m 10m` for learning or `10m` for relearning to
run, leave **Skip learning/relearning queues with FSRS/RWKV** off (its default).
Turning it on with FSRS active bypasses these delays for new, learning, review,
and relearning cards; answers schedule Review states directly. The configured
step lists are kept. Turning it off makes them available again, but cards already
graduated to Review are not automatically moved back into learning. With FSRS
disabled, SM2 still follows its configured steps regardless of this bypass.

Same-day Again answers do not add another lapse or trigger leech handling,
regardless of queue skipping or scheduler. This also covers rescheduling filtered
decks and **Grade Now**. A review card's first answer of a scheduler day still
adds a lapse when answered Again; later answers that day do not, even after an
earlier successful answer. All answers remain in review history and update the
model.

### How do the same-day settings interact?

FSRS-7 and RWKV-Curve decide answer intervals, while RWKV-Instant independently
decides whether a review card can enter the queue. They share **Allow same day
review for (re)learning steps**, which is shown when FSRS or Instant is enabled.
The decision tree shows both paths. When Instant is enabled, its spacing
minimums also apply to learning/relearning selection, including Learn ahead:

```mermaid
flowchart TD
    A{"Scheduling path"}

    A -->|FSRS-7 / RWKV-Curve| B{"Skip learning/relearning queues On?"}
    B -->|Yes| C["Schedule directly as Review<br/>Minimum interval: 1 day"]
    B -->|No| D{"Does a configured learning/relearning<br/>step apply to this answer?"}
    D -->|Yes| E["Use the configured step delay"]
    D -->|No| F{"Allow same day review for<br/>(re)learning steps On?"}
    F -->|No| C
    F -->|Yes| G{"Generated interval below 12 hours?"}
    G -->|Yes| H["Use learning/relearning queue<br/>with the generated delay"]
    G -->|No| C

    E --> Q{"RWKV-Instant enabled?"}
    H --> Q
    Q -->|No| P
    Q -->|Yes| L

    A -->|RWKV-Instant| I{"Allow same day review for<br/>(re)learning steps On?"}
    I -->|No| J["Same-day RWKV review repeat blocked"]
    I -->|Yes| L{"Minimum other answers reached<br/>since this card's last answer?"}
    L -->|No| M["Wait for enough other answers"]
    L -->|Yes| N{"Minimum seconds elapsed<br/>since this card's last answer?"}
    N -->|No| O["Wait for enough elapsed time"]
    N -->|Yes| P["Card eligible for selection<br/>Other queue conditions still apply"]
```

The **Same-Day Repeats** row sets minimum other reviews and elapsed seconds
between repeats. Both must be satisfied; a value of `0`
removes that minimum. Their defaults are **5 other answers** and **30 seconds**.
For example, values of **3** and **90** require both three other answers and
90 elapsed seconds before the card can repeat while RWKV-Instant is enabled.
This includes learning/relearning cards, even when a step is already due or
Learn ahead would offer it early. Waiting preserves the card's saved interval
and learning steps. Learning cards continue to follow those intervals; Instant's
recall threshold is not added to their selection rules.

**Allow same day review for (re)learning steps** defaults to on and is the sole
same-day repeat switch for Instant. Its saved off choice is preserved; the
former per-preset RWKV switch is ignored. The spacing guards and other queue
conditions must still be satisfied.
Configured learning/relearning steps may still repeat while queues are enabled.
Enabling **Skip learning/relearning queues with FSRS/RWKV** bypasses these
steps; RWKV-Instant can still admit an eligible same-day repeat even when the
card's stored review due day is in the future.

### Does RWKV override standard sibling burying?

No. The fork uses Anki's normal bury settings: `bury_new`, `bury_reviews`, and
`bury_interday_learning`. RWKV-Instant does not decide independently whether
sibling cards are buried. The deck configuration remains the source of that
policy.

### Does RWKV guarantee fewer reviews than FSRS?

No. Predictive accuracy and workload are different measurements. A model can
predict recall well without producing a fixed number of reviews, and
RWKV-Instant can request short-term reviews when its current state predicts
that a card is at risk.

The fork therefore does not provide a universal conversion such as “RWKV at
90% equals FSRS at 90%,” nor does it guarantee a particular review count for a
given desired-retention value.

## 5. Calibration and prediction history

### What does a calibration bucket mean?

A calibration graph groups reviews by predicted recall probability and compares
the predictions with the observed outcomes. A 90% bucket means that the
reviews in that bucket were predicted near 90%; it is not a permanent label
attached to a card. This is the interpretation behind the [calibration-bucket question](https://discord.com/channels/617136488840429598/1525841501622370476/1552174896539238511)
in the discussion.

For RWKV, a graph can replay the current model over historical reviews. Anki
does not reliably record which scheduler produced every historical prediction,
so a replay shows what the current model would predict for the past, not
necessarily what it predicted on the original review date.

### Does the first answer on a new card count?

In this fork's RWKV calibration cache, an eligible first learning answer is
included and receives a prediction before the answer is applied. It is not the
same model context as a mature review: when enabled, the first answer can use
elapsed time since card creation, while later reviews use elapsed time since
the previous review.

The history builder retains the learning sequence used for replay, so an older
superseded sequence may not be replayed. This is a property of the fork's
calibration history, not a statement that all external graphs use the same
filtering rules.

### How are learning and relearning steps represented?

The replay assigns explicit states to learning, review, relearning, filtered,
manual, and rescheduled events. Learning and relearning events also use their
corresponding queues; mature reviews use the review queue.

The prediction cache records the revlog identifier, prediction, sample role,
and fold information, but not every state label directly in the cache row. A
graph that needs to separate learning, relearning, and mature reviews should
join the cache to the corresponding revlog data.

## 6. History and state in this fork

### Which review events enter the RWKV history?

The current replay query accepts revlog types 0 through 5 and excludes filtered
rows with `type = 3` and `factor = 0`. Eligible learning, review, relearning,
filtered, manual, and rescheduled events can therefore affect the replayed
state.

The code does not assign a fixed numerical weight such as “one filtered review
changes R by X.” The effect depends on the trained model, the preceding state,
and the other features in the review sequence.

### How is RWKV state rebuilt?

The fork reconstructs RWKV state by replaying eligible history in order and
refreshing the prediction cache. Live answers update the relevant model state
immediately; a rebuild is the mechanism used to bring cached state back in
line with the collection history after a substantial change.

The exact time and memory cost depend on the collection, the model, and the
runtime. The repository does not define one fixed rebuild time for all users.

## 7. Platform and implementation scope

### Does the fork run on mobile?

The [fork description](https://www.reddit.com/r/Anki/comments/1w0p9qc/comment/paqq1oe/)
is desktop-focused. A [mobile-support reply](https://discord.com/channels/368267295601983490/1347982145418694747/1553731488997052537)
reports that AnkiWeb sync works, while the mobile client falls back to FSRS-6
scheduling rather than running the desktop fork's FSRS-7 or RWKV behavior.
Verify the current fork release before relying on that behavior, because
mobile support is version-specific.

### Is this an official Anki release?

No. The [fork description](https://www.reddit.com/r/Anki/comments/1w0p9qc/comment/paqq1oe/)
describes it as an unofficial, experimental build for testing scheduling ideas
in real use. It should not be treated as a promise about the behavior or
support policy of official Anki releases.

### What are the resource and performance trade-offs?

RWKV keeps and updates more state than a small formula-only scheduler, and
replaying a large collection can require noticeable time or memory. The exact
cost depends on the model implementation, collection size, and hardware; this
FAQ does not assign a universal hardware requirement.

### What does this fork add to Anki?

The current fork integrates RWKV with Anki's existing collection and scheduler
model. In practical terms, it provides:

- RWKV-Instant and RWKV-Curve scheduling modes;
- replay and incremental updates over review history;
- deck, preset, card, note, and collection context;
- calibration and prediction-history support;
- desired-retention and review-limit integration; and
- normal Anki learning, relearning, review, queue-limit, and sibling-burying
  behavior around the RWKV prediction layer.

These are integration capabilities, not guarantees that every model version
will improve retention, reduce workload, or behave like FSRS.

## Repository code references

The fork-specific statements above are primarily grounded in these repository
areas:

- `qt/aqt/rwkv_scheduler.py` for model inputs, replay, prediction caching,
  desired-retention resolution, and calibration handling;
- `rslib/src/rwkv/` for RWKV history queries and state processing;
- `rslib/src/storage/revlog/` for revlog filtering and review kinds;
- `rslib/src/decks/limits.rs` for daily limits and RWKV minimum-review
  handling; and
- `rslib/src/scheduler/answering/` and
  `rslib/src/scheduler/queue/builder/` for answer transitions, queueing, and
  sibling burying.
