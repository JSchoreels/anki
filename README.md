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

## Compatible Add-ons

The following add-on has been adapted for this fork:

- [FSRS Helper / adapted add-on](https://ankiweb.net/shared/info/1339555413?cb=1778336951277)

## Building

Use the same development flow as upstream Anki. See
[OFFICIAL_README.md](./OFFICIAL_README.md) and
[docs/development.md](./docs/development.md) for build and development details.

Unsigned internal installer builds are produced through the release workflow on
this fork's `main` branch.

## License

This fork keeps Anki's original license: [LICENSE](./LICENSE).
