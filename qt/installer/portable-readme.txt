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

You can move the complete folder after quitting Anki. Keep the application and
"Anki Portable Data" together when moving it.

The normal installed Anki and this portable copy can run at the same time. They
do not share local data. If both copies sign in to the same AnkiWeb account,
they can still exchange collection changes through sync.

The portable edition checks for updates automatically unless you disable update
checks in Preferences. Tools > Check for Updates also checks manually. Accepting
an update downloads and prepares the matching portable archive, then closes
Anki normally, replaces the application, and restarts it automatically. Your
"Anki Portable Data" folder stays in place. If installation or early bootstrap fails,
the updater restores the previous application. Downloads, update logs and the
previous application are kept under "Anki Portable Data/updates".

Older builds need one manual upgrade to gain automatic replacement: extract the
new portable version outside the old "Anki Portable" folder, quit Anki, and move
your existing "Anki Portable Data" folder into the new folder, replacing its
empty data folder. Confirm your profiles are present before deleting the old
application folder.

This portable edition separates Anki's application data; it is not an operating
system security sandbox. Add-ons retain the permissions of your operating
system account.
