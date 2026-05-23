# Collection Shutdown

When the main window is closed while a profile is loaded, Anki first closes
secondary windows, optionally syncs media and the collection, then unloads the
collection.

The unload path performs collection data operations on the GUI thread:

- reads the profile's last optimize timestamp
- may update that timestamp after optimizing
- reads `pragma quick_check` before closing
- may create a collection backup
- closes the collection database

Shutdown must not start those close-time collection operations while a
background `QueryOp` or `CollectionOp` is still active. Those operations
serialize access to the collection backend, and a synchronous close-time read on
the GUI thread can otherwise block until the background operation releases the
collection. The main-window close handler therefore defers profile unload until
the background operation count reaches zero.
