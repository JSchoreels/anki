ANKI PORTABLE
=============

Keep this entire "Anki Portable" folder together.

On macOS, before the first launch, use Finder to move the complete extracted
"Anki Portable" folder from Downloads to a writable location such as your home
folder. Then open "Anki Portable.app" from the moved folder. If you open the app
before moving it, macOS Gatekeeper may run it from a read-only temporary
location where it cannot use the adjacent data folder.

Start Anki with the file for your platform:

  macOS:  Anki Portable.app
  Windows: Anki Portable.exe
  Linux:  anki

Anki creates and uses only the "Anki Portable Data" folder beside the
application for its preferences, profiles, collections, media, add-ons,
backups, logs, and temporary files.

You can move the complete folder after quitting Anki. To update, extract the
new portable version outside the current "Anki Portable" folder, quit Anki,
and move your existing "Anki Portable Data" folder into the new folder before
deleting the old copy.

The normal installed Anki and this portable copy can run at the same time. They
do not share local data. If both copies sign in to the same AnkiWeb account,
they can still exchange collection changes through sync.

The portable edition checks for updates automatically unless you disable update
checks in Preferences. Tools > Check for Updates also checks manually. Accepting
an update downloads the matching portable archive, verifies its checksum, and
opens its folder with instructions for the replacement steps above. Downloads
stay in "Anki Portable Data/updates". The updater does not run the standard
installer or replace files while Anki is running.

Older portable builds without update checks need one manual upgrade before
they can offer this update path.

This portable edition separates Anki's application data; it is not an operating
system security sandbox. Add-ons retain the permissions of your operating
system account.
