# Review Fuzz Configuration

Review fuzz is stored as collection-level config under the `reviewFuzz` key.
When the key is missing or invalid, Anki uses the built-in defaults:

- enabled: `true`
- base: `1.0`
- short factor: `0.15`
- mid factor: `0.10`
- long factor: `0.05`

Deck preset `review_fuzz_*` fields are legacy compatibility data. They may be
present in old collections or sync payloads, but scheduler code must not use
them to choose intervals. Normal reviews, filtered deck rescheduling, load
balancing, and FSRS rescheduling all read the collection-level value.

Changing collection-level review fuzz only causes existing cards to be
rescheduled when the deck-options update requests FSRS rescheduling. Otherwise,
the new value applies to future scheduling decisions.
