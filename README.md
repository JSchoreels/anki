# Anki FSRS7 Fork

This is an unofficial Anki desktop build focused on trying FSRS7 scheduling
changes before they are available in the official Anki app.

It is not maintained by Ankitects and should not be confused with the official
Anki desktop app. The original upstream README is preserved in
[OFFICIAL_README.md](./OFFICIAL_README.md).

## What This Fork Is For

This fork is for users who want to test newer FSRS behavior and compare how it
affects their reviews, study time, and retention choices.

Compared with official Anki, it adds more FSRS7-focused options and analysis
tools in deck options. These are intended to help you understand trade-offs
before changing how your cards are scheduled.

Use this build if you are comfortable trying experimental scheduling behavior.
Use official Anki if you prefer the most stable and broadly supported desktop
release.

## What It Adds

This fork extends the FSRS tools in deck options so you can inspect the impact
of FSRS7 settings before committing to them:

- The experimental FSRS simulator has extra views for reviews, review time,
  memorized cards, and efficiency, so you can compare retention choices by both
  workload and expected learning outcome.
- The experimental "Help Me Decide" flow uses the simulator to estimate how
  different desired retention values affect daily review load, time spent, and
  memorized cards.
- FSRS7 optimization and evaluation can include same-day review history, which
  helps the model learn from short-term relearning behavior.
- You can also disable same-day review history for FSRS7 optimize/evaluate when
  you want to compare against long-term review behavior only.
- The "Allow same day review for (re)learning steps" option lets FSRS schedule
  short-term learning/relearning reviews on the same day.
- The "Skip learning/relearning queues with FSRS/RWKV" option lets FSRS/RWKV
  schedule review states directly instead of using the learning/relearning
  queues.

## Before You Use It

This fork changes scheduling behavior. Before relying on it for an important
collection, make sure you are comfortable with the experimental nature of the
build and keep normal Anki backups.

Some add-ons may expect official Anki version strings or official scheduling
behavior. If an add-on behaves unexpectedly, check whether there is a compatible
version listed below.

## FSRS Preset Versions

The deck options "Optimize All Presets" action optimizes each preset using that
preset's current FSRS version. It does not convert all presets to FSRS7. For
example, an FSRS6 preset stays FSRS6 and receives optimized FSRS6 parameters,
while an FSRS7 preset stays FSRS7 and receives optimized FSRS7 parameters.

If you want to mass-change existing presets to FSRS7 before optimizing them, use
[Change FSRS Version for Presets](https://ankiweb.net/shared/info/1542952656?cb=1778427918643).

## Compatible Add-ons

The following add-ons have been adapted for this fork:

- [Change FSRS Version for Presets](https://ankiweb.net/shared/info/1542952656?cb=1778427918643):
  use this if you want to change many existing presets to FSRS7 before running
  Optimize All Presets in this fork.
- [Search Stats Extended (Sound's Fork)](https://ankiweb.net/shared/info/1339555413?cb=1778336951277):
  search statistics adapted and tested primarily against this FSRS7 fork.
- [FSRS Helper (Sound's Fork)](https://ankiweb.net/shared/info/218829258?cb=1778337495548):
  FSRS helper add-on adapted to work with this fork's FSRS7 behavior.
- [Dynamic Desired Retention](https://ankiweb.net/shared/info/160848019?cb=1778705771363):
  add-on for overriding desired retention on cards matched by ordered Anki
  search rules, such as `Frequency<1000`.
- [FSRS Dynamic Preset Selection](https://ankiweb.net/shared/info/1798968356?cb=1779797013168):
  add-on for selecting card-specific FSRS presets from ordered Anki search
  rules, decoupling FSRS parameters from deck preset assignment.
- [AnkiConnect Extended](https://ankiweb.net/shared/info/1635024181?cb=1778339678165):
  required when other tools use AnkiConnect against this fork, because the
  upstream AnkiConnect version parsing does not handle the `+fsrs7` build
  suffix correctly.

### Dynamic Desired Retention and FSRS Helper

This fork exposes a reviewer hook that lets add-ons override desired retention
for the current card before Anki computes FSRS scheduling states. Dynamic
Desired Retention uses that hook during normal reviews.

FSRS Helper uses a separate reschedule path. Its adapted fork calls Dynamic
Desired Retention's Python resolver before recomputing a card interval, so
`Reschedule All Cards` can use the same ordered search rules as normal review.
If Dynamic Desired Retention is not installed or does not expose the resolver,
FSRS Helper falls back to the deck or preset desired retention.

## License

This fork keeps Anki's original license: [LICENSE](./LICENSE).
