# Portable application updates

Portable builds use the same automatic update preference and Tools > Check for
Updates action as installed Anki. The release check requests portable assets
explicitly and selects the archive for the running application's platform and
CPU architecture. Windows and macOS use ZIP archives; Linux uses `.tar.zst`.
Installed builds continue to select their normal installer/download assets.
Neither edition falls back to the other edition when its asset is missing.

After the user accepts the update prompt, Anki downloads the archive with the
existing backend progress, cancellation, and SHA-256 verification. Downloads
and verified-download reuse stay in the current copy's
`Anki Portable Data/updates` directory, using `ANKI_PORTABLE_ROOT` from the
portable launcher. Old downloads are cleaned up only in that directory.

On completion, Anki opens the download directory and displays these steps:

1. Extract the archive outside the current `Anki Portable` folder. In particular,
   do not extract it beside the download inside the existing data folder.
2. Quit every Anki instance using the existing portable data folder.
3. Move the existing `Anki Portable Data` folder into the extracted
   `Anki Portable` folder, replacing the archive's empty data folder.
4. Open the new application and confirm the profiles are present before
   deleting the old application folder.

The download flow does not launch a normal installer, quit Anki, extract over
the running application, or move collection/profile data. The user completes
the replacement after quitting; profiles, collections, media, add-ons,
preferences, backups, and caches travel with the existing data folder.

Portable builds released before this flow need one manual upgrade to gain
automatic notifications and the manual update action. This feature does not
retroactively enable update checks in those builds.

The backend `LatestReleaseRequest.portable` field defaults to false, preserving
the behavior of existing callers. The frontend sets it from the portable
launcher flag. Release filenames must continue to match
`qt/tools/build_installer.py`; the backend tests cover both editions for all
six supported platform/architecture combinations and missing-asset behavior.
