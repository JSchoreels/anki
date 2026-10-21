# Portable application updates

Portable builds use the same automatic update preference and Tools > Check for
Updates action as installed Anki. Release lookup explicitly selects a portable
archive for the running application's platform and CPU architecture. Windows
and macOS use ZIP; Linux uses `.tar.zst`. Installed builds retain their normal
assets. Neither edition falls back to the other edition's download.

## Download, stage, close, replace, restart

After the user accepts the prompt, Anki downloads and verifies the archive with
the existing backend progress and SHA-256 check. Downloads and update jobs stay
under the current copy's `Anki Portable Data/updates`, using
`ANKI_PORTABLE_ROOT` from its launcher.

The backend extracts into a fresh job directory. The updater checks the incoming
manifest's protocol, platform and CPU, the portable marker and launcher, and
that links stay inside the application payload. An archive may contain only an
empty portable data directory. The downloaded build must support the helper
bootstrap protocol. Staging, validation, insufficient-space and helper-start
failures leave the running application open.

The helper runs from the staged application's bundled runtime, using the current
updater's plain Python source. This avoids requiring system Python or relying on
old bytecode remaining compatible with a future bundled interpreter. It starts
without importing Qt or opening a profile and signals readiness before the UI
requests normal Anki shutdown. Normal close still saves the profile and follows
the usual sync/backup flow.

The helper holds an installation lock and waits for the old process to exit.
Cancellation or a shutdown that has not finished after ten minutes aborts before
application replacement. Windows observes a process handle; it does not signal
or terminate the old process. Transient Windows file-lock failures are retried
for up to ten seconds after exit.

Only application-owned top-level entries from `anki-portable-update.json` are
moved into the job's `previous` directory. New entries are copied from staging
to the existing portable application folder. Removed application entries do not
linger. User-added top-level files remain in place; a collision with a newly
introduced application entry stops the update instead of overwriting that file.
**The existing `Anki Portable Data` directory is never moved, copied or replaced.**
Profiles, collections, media, add-ons, preferences, backups and caches keep their
paths throughout the update.

The helper launches the new application from its original location. The portable
bootstrap acknowledges successful runtime/Qt imports **before** calling
`aqt.run()` or opening profile metadata. Copy or launch failure, or a new process
exiting before this acknowledgement, restores the previous application and
relaunches it. Once profile loading can begin, automatic rollback is disabled:
the newer application may migrate data that an older version cannot safely open.
A new process that remains alive without acknowledging within two minutes is
left running with the backup retained. Rollback never replaces files belonging
to a live new process.

## Recovery and retained files

Each job contains `plan.json`, `state`, `helper.log`, the staged payload and the
previous application files. After a confirmed successful update, older completed,
cancelled or rolled-back jobs for this portable root are removed. The latest
job and its previous application remain available. Incomplete/unrecognized jobs
are retained for troubleshooting. Space is needed for staging and the new copy
in addition to the existing application; staging checks available space before
requesting shutdown.

An update failure is reported when Anki next opens. If an I/O error or process
interruption prevents installation/rollback from completing before profile startup,
quit every application using
that portable folder and restore the entries from the job's `previous` directory
to the portable application folder. Leave `Anki Portable Data` in place.

Older portable builds lacking this bootstrap/manifest protocol need one manual
upgrade before they can install updates automatically: extract the new archive
outside the old portable folder, quit Anki, and move the existing data folder
into the new portable folder, replacing its empty data folder.

## Packaging and compatibility

The portable build bundles `qt/aqt/portable_update.py` as plain
`portable-update-helper.py` outside `app_packages`, before signing. Packaging
records the distributed top-level entries, platform and CPU in the protocol-1
manifest. Future launchers must keep handling `ANKI_PORTABLE_UPDATE_PLAN` before
profile setup; this invokes the job's helper source. Normal restarts remove this
helper-mode flag, use `ANKI_PORTABLE_UPDATE_RESTART` to bypass the active
installation lock for the updater's own child, and use
`ANKI_PORTABLE_UPDATE_ACK` for bootstrap acknowledgement before opening profiles.
Other manual starts return before profile setup while the installation lock is
held.

`LatestReleaseRequest.portable` remains opt-in and defaults to false.
`ExtractPortableUpdate` is additive and refuses an existing destination.
Extraction uses the existing ZIP/Zstandard crates and the workspace's TAR
library, preserving executable permissions and internal links. Filename suffixes
must continue matching `qt/tools/build_installer.py` for all six supported
platform/architecture pairs.
