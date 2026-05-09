# Anki FSRS7 Fork

This repository is an unofficial fork of [Anki](https://apps.ankiweb.net) focused on
FSRS7 experimentation and internal builds.

It is not maintained by Ankitects and should not be confused with the official
Anki desktop app. The original upstream README is preserved in
[OFFICIAL_README.md](./OFFICIAL_README.md).

## Differences From Official Anki

Compared with upstream Anki, this fork adds FSRS7-oriented scheduling,
optimization, and simulator work. In particular, it includes:

- FSRS7 support backed by this fork's pinned `fsrs-rs` dependency.
- FSRS7 deck option data such as same-day optimization/evaluation settings.
- Separate evaluation search handling for FSRS optimization/evaluation.
- Additional simulator and "Help me decide" work around retention, time cost,
  and workload inspection.
- Internal build versioning with the `+fsrs7` suffix.

The goal is to make FSRS7 behavior easier to test before it is appropriate for
general official Anki distribution.

## What This Adds For Users

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
- The "Skip learning/relearning queues with FSRS" option lets FSRS schedule
  review states directly instead of using the learning/relearning queues.

## Compatible Add-ons

The following add-ons have been adapted for this fork:

- [Search Stats Extended (Sound's Fork)](https://ankiweb.net/shared/info/1339555413?cb=1778336951277):
  search statistics adapted and tested primarily against this FSRS7 fork.
- [FSRS Helper (Sound's Fork)](https://ankiweb.net/shared/info/218829258?cb=1778337495548):
  FSRS helper add-on adapted to work with this fork's FSRS7 behavior.
- [AnkiConnect Extended](https://ankiweb.net/shared/info/1635024181?cb=1778339678165):
  required when other tools use AnkiConnect against this fork, because the
  upstream AnkiConnect version parsing does not handle the `+fsrs7` build
  suffix correctly.

## Building

Use the same development flow as upstream Anki. See
[OFFICIAL_README.md](./OFFICIAL_README.md) and
[docs/development.md](./docs/development.md) for build and development details.

Unsigned internal installer builds are produced through the release workflow on
this fork's `main` branch.

## License

This fork keeps Anki's original license: [LICENSE](./LICENSE).
