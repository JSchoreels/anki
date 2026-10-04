// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use itertools::Itertools;
use serde::Deserialize;
use serde::Serialize;
use serde_tuple::Serialize_tuple;
use tracing::debug;

use crate::card::Card;
use crate::card::CardQueue;
use crate::card::CardType;
use crate::notes::Note;
use crate::prelude::*;
use crate::revlog::RevlogEntry;
use crate::serde::deserialize_int_from_number;
use crate::storage::card::data::card_data_string;
use crate::storage::card::data::CardData;
use crate::sync::collection::normal::ClientSyncState;
use crate::sync::collection::normal::NormalSyncer;
use crate::sync::collection::protocol::EmptyInput;
use crate::sync::collection::protocol::SyncProtocol;
use crate::sync::collection::start::ServerSyncState;
use crate::sync::request::IntoSyncRequest;
use crate::tags::join_tags;
use crate::tags::split_tags;

/// The objects of a remote chunk that were newer than the local ones.
#[derive(Default)]
pub(in crate::sync) struct AppliedChunk {
    pub card_ids: Vec<CardId>,
    pub note_ids: Vec<NoteId>,
    /// A card was added, or moved to another note or effective deck, which
    /// changes the identity of its reviews.
    pub card_identity_changed: bool,
}

pub(in crate::sync) struct ChunkableIds {
    revlog: Vec<RevlogId>,
    cards: Vec<CardId>,
    notes: Vec<NoteId>,
}

#[derive(Serialize, Deserialize, Debug, Default)]
pub struct Chunk {
    #[serde(default)]
    pub done: bool,
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub revlog: Vec<RevlogEntry>,
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub cards: Vec<CardEntry>,
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub notes: Vec<NoteEntry>,
}

#[derive(Serialize_tuple, Deserialize, Debug)]
pub struct NoteEntry {
    pub id: NoteId,
    pub guid: String,
    #[serde(rename = "mid")]
    pub ntid: NotetypeId,
    #[serde(rename = "mod")]
    pub mtime: TimestampSecs,
    pub usn: Usn,
    pub tags: String,
    pub fields: String,
    pub sfld: String, // always empty
    pub csum: String, // always empty
    pub flags: u32,
    pub data: String,
}

#[derive(Serialize_tuple, Deserialize, Debug)]
pub struct CardEntry {
    pub id: CardId,
    pub nid: NoteId,
    pub did: DeckId,
    pub ord: u16,
    #[serde(deserialize_with = "deserialize_int_from_number")]
    pub mtime: TimestampSecs,
    pub usn: Usn,
    pub ctype: CardType,
    pub queue: CardQueue,
    #[serde(deserialize_with = "deserialize_int_from_number")]
    pub due: i32,
    #[serde(deserialize_with = "deserialize_int_from_number")]
    pub ivl: u32,
    pub factor: u16,
    pub reps: u32,
    pub lapses: u32,
    pub left: u32,
    #[serde(deserialize_with = "deserialize_int_from_number")]
    pub odue: i32,
    pub odid: DeckId,
    pub flags: u8,
    pub data: String,
}

impl NormalSyncer<'_> {
    pub(in crate::sync) async fn process_chunks_from_server(
        &mut self,
        state: &ClientSyncState,
    ) -> Result<()> {
        loop {
            let chunk = self.server.chunk(EmptyInput::request()).await?.json()?;

            debug!(
                done = chunk.done,
                cards = chunk.cards.len(),
                notes = chunk.notes.len(),
                revlog = chunk.revlog.len(),
                "received"
            );
            self.mark_remote_collection_changed(
                !chunk.cards.is_empty() || !chunk.notes.is_empty() || !chunk.revlog.is_empty(),
            );
            for entry in &chunk.revlog {
                self.record_remote_review(entry.id);
            }

            self.progress.update(false, |p| {
                p.remote_update += chunk.cards.len() + chunk.notes.len() + chunk.revlog.len()
            })?;

            let done = chunk.done;
            let applied = self.col.apply_chunk(chunk, state.pending_usn)?;
            // Reviews rewrite their card's scheduling fields, which is not a
            // change to the history the reviews belong to.
            self.mark_remote_non_review_collection_changed(
                applied.card_identity_changed || !applied.note_ids.is_empty(),
            );

            self.progress.check_cancelled()?;

            if done {
                return Ok(());
            }
        }
    }

    pub(in crate::sync) async fn send_chunks_to_server(
        &mut self,
        state: &ClientSyncState,
    ) -> Result<()> {
        let mut ids = self.col.get_chunkable_ids(state.pending_usn)?;

        loop {
            let chunk: Chunk = self.col.get_chunk(&mut ids, Some(state.server_usn))?;
            let done = chunk.done;

            debug!(
                done = chunk.done,
                cards = chunk.cards.len(),
                notes = chunk.notes.len(),
                revlog = chunk.revlog.len(),
                "sending"
            );

            self.progress.update(false, |p| {
                p.local_update += chunk.cards.len() + chunk.notes.len() + chunk.revlog.len()
            })?;

            self.server
                .apply_chunk(ApplyChunkRequest { chunk }.try_into_sync_request()?)
                .await?;

            self.progress.check_cancelled()?;

            if done {
                return Ok(());
            }
        }
    }
}

impl Collection {
    // Remote->local chunks
    //----------------------------------------------------------------

    /// pending_usn is used to decide whether the local objects are newer.
    /// If the provided objects are not modified locally, the USN inside
    /// the individual objects is used.
    pub(in crate::sync) fn apply_chunk(
        &mut self,
        chunk: Chunk,
        pending_usn: Usn,
    ) -> Result<AppliedChunk> {
        self.merge_revlog(chunk.revlog)?;
        let mut applied = self.merge_cards(chunk.cards, pending_usn)?;
        applied.note_ids = self.merge_notes(chunk.notes, pending_usn)?;
        let mut changed_card_ids = std::mem::take(&mut applied.card_ids);
        changed_card_ids.extend(self.storage.card_ids_of_notes(&applied.note_ids)?);
        self.forget_fsrs_preset_overlay_card_matches(&changed_card_ids);
        applied.card_ids = changed_card_ids;
        Ok(applied)
    }

    fn merge_revlog(&self, entries: Vec<RevlogEntry>) -> Result<()> {
        for entry in entries {
            self.storage.add_revlog_entry(&entry, false)?;
        }
        Ok(())
    }

    fn merge_cards(&self, entries: Vec<CardEntry>, pending_usn: Usn) -> Result<AppliedChunk> {
        let mut applied = AppliedChunk::default();
        for entry in entries {
            let existing_card = self.storage.get_card(entry.id)?;
            let proceed = existing_card.as_ref().map_or(true, |existing_card| {
                !existing_card.usn.is_pending_sync(pending_usn) || existing_card.mtime < entry.mtime
            });
            if !proceed {
                continue;
            }
            let card: Card = entry.into();
            applied.card_identity_changed |= existing_card.map_or(true, |existing_card| {
                existing_card.note_id != card.note_id
                    || existing_card.original_or_current_deck_id()
                        != card.original_or_current_deck_id()
            });
            applied.card_ids.push(card.id);
            self.storage.add_or_update_card(&card)?;
        }
        Ok(applied)
    }

    fn merge_notes(&mut self, entries: Vec<NoteEntry>, pending_usn: Usn) -> Result<Vec<NoteId>> {
        let mut applied_note_ids = vec![];
        for entry in entries {
            let note_id = entry.id;
            if self.add_or_update_note_if_newer(entry, pending_usn)? {
                applied_note_ids.push(note_id);
            }
        }
        Ok(applied_note_ids)
    }

    fn add_or_update_note_if_newer(&mut self, entry: NoteEntry, pending_usn: Usn) -> Result<bool> {
        let proceed = if let Some(existing_note) = self.storage.get_note(entry.id)? {
            !existing_note.usn.is_pending_sync(pending_usn) || existing_note.mtime < entry.mtime
        } else {
            true
        };
        if proceed {
            let mut note: Note = entry.into();
            let nt = self
                .get_notetype(note.notetype_id)?
                .or_invalid("note missing notetype")?;
            note.prepare_for_update(&nt, false)?;
            self.storage.add_or_update_note(&note)?;
        }
        Ok(proceed)
    }

    // Local->remote chunks
    //----------------------------------------------------------------

    pub(in crate::sync) fn get_chunkable_ids(&self, pending_usn: Usn) -> Result<ChunkableIds> {
        Ok(ChunkableIds {
            revlog: self.storage.objects_pending_sync("revlog", pending_usn)?,
            cards: self.storage.objects_pending_sync("cards", pending_usn)?,
            notes: self.storage.objects_pending_sync("notes", pending_usn)?,
        })
    }

    /// Fetch a chunk of ids from `ids`, returning the referenced objects.
    pub(in crate::sync) fn get_chunk(
        &self,
        ids: &mut ChunkableIds,
        server_usn_if_client: Option<Usn>,
    ) -> Result<Chunk> {
        // get a bunch of IDs
        let mut limit = CHUNK_SIZE as i32;
        let mut revlog_ids = vec![];
        let mut card_ids = vec![];
        let mut note_ids = vec![];
        let mut chunk = Chunk::default();
        while limit > 0 {
            let last_limit = limit;
            if let Some(id) = ids.revlog.pop() {
                revlog_ids.push(id);
                limit -= 1;
            }
            if let Some(id) = ids.notes.pop() {
                note_ids.push(id);
                limit -= 1;
            }
            if let Some(id) = ids.cards.pop() {
                card_ids.push(id);
                limit -= 1;
            }
            if limit == last_limit {
                // all empty
                break;
            }
        }
        if limit > 0 {
            chunk.done = true;
        }

        // remove pending status
        if !self.server {
            self.storage
                .maybe_update_object_usns("revlog", &revlog_ids, server_usn_if_client)?;
            self.storage
                .maybe_update_object_usns("cards", &card_ids, server_usn_if_client)?;
            self.storage
                .maybe_update_object_usns("notes", &note_ids, server_usn_if_client)?;
        }

        // the fetch associated objects, and return
        chunk.revlog = revlog_ids
            .into_iter()
            .map(|id| {
                self.storage.get_revlog_entry(id).map(|e| {
                    let mut e = e.unwrap();
                    e.usn = server_usn_if_client.unwrap_or(e.usn);
                    e
                })
            })
            .collect::<Result<_>>()?;
        chunk.cards = card_ids
            .into_iter()
            .map(|id| {
                self.storage.get_card(id).map(|e| {
                    let mut e: CardEntry = e.unwrap().into();
                    e.usn = server_usn_if_client.unwrap_or(e.usn);
                    e
                })
            })
            .collect::<Result<_>>()?;
        chunk.notes = note_ids
            .into_iter()
            .map(|id| {
                self.storage.get_note(id).map(|e| {
                    let mut e: NoteEntry = e.unwrap().into();
                    e.usn = server_usn_if_client.unwrap_or(e.usn);
                    e
                })
            })
            .collect::<Result<_>>()?;

        Ok(chunk)
    }
}

impl From<CardEntry> for Card {
    fn from(e: CardEntry) -> Self {
        let data = CardData::from_str(&e.data);
        Card {
            id: e.id,
            note_id: e.nid,
            deck_id: e.did,
            template_idx: e.ord,
            mtime: e.mtime,
            usn: e.usn,
            ctype: e.ctype,
            queue: e.queue,
            due: e.due,
            interval: e.ivl,
            ease_factor: e.factor,
            reps: e.reps,
            lapses: e.lapses,
            remaining_steps: e.left,
            original_due: e.odue,
            original_deck_id: e.odid,
            flags: e.flags,
            original_position: data.original_position,
            memory_state: data.memory_state(),
            desired_retention: data.fsrs_desired_retention,
            decay: data.decay,
            last_review_time: data.last_review_time,
            custom_data: data.custom_data,
        }
    }
}

impl From<Card> for CardEntry {
    fn from(e: Card) -> Self {
        CardEntry {
            id: e.id,
            nid: e.note_id,
            did: e.deck_id,
            ord: e.template_idx,
            mtime: e.mtime,
            usn: e.usn,
            ctype: e.ctype,
            queue: e.queue,
            due: e.due,
            ivl: e.interval,
            factor: e.ease_factor,
            reps: e.reps,
            lapses: e.lapses,
            left: e.remaining_steps,
            odue: e.original_due,
            odid: e.original_deck_id,
            flags: e.flags,
            data: card_data_string(&e),
        }
    }
}

impl From<NoteEntry> for Note {
    fn from(e: NoteEntry) -> Self {
        let fields = e.fields.split('\x1f').map(ToString::to_string).collect();
        Note::new_from_storage(
            e.id,
            e.guid,
            e.ntid,
            e.mtime,
            e.usn,
            split_tags(&e.tags).map(ToString::to_string).collect(),
            fields,
            None,
            None,
        )
    }
}

impl From<Note> for NoteEntry {
    fn from(e: Note) -> Self {
        NoteEntry {
            id: e.id,
            fields: e.fields().iter().join("\x1f"),
            guid: e.guid,
            ntid: e.notetype_id,
            mtime: e.mtime,
            usn: e.usn,
            tags: join_tags(&e.tags),
            sfld: String::new(),
            csum: String::new(),
            flags: 0,
            data: String::new(),
        }
    }
}

pub fn server_chunk(col: &mut Collection, state: &mut ServerSyncState) -> Result<Chunk> {
    if state.server_chunk_ids.is_none() {
        state.server_chunk_ids = Some(col.get_chunkable_ids(state.client_usn)?);
    }
    col.get_chunk(state.server_chunk_ids.as_mut().unwrap(), None)
}

pub fn server_apply_chunk(
    req: ApplyChunkRequest,
    col: &mut Collection,
    state: &mut ServerSyncState,
) -> Result<()> {
    col.apply_chunk(req.chunk, state.client_usn).map(|_| ())
}

impl Usn {
    pub(crate) fn is_pending_sync(self, pending_usn: Usn) -> bool {
        if pending_usn.0 == -1 {
            self.0 == -1
        } else {
            self.0 >= pending_usn.0
        }
    }
}

pub const CHUNK_SIZE: usize = 250;

#[derive(Serialize, Deserialize, Debug)]
pub struct ApplyChunkRequest {
    pub chunk: Chunk,
}

#[cfg(test)]
mod test {
    use fsrs::FSRS;
    use serde_json::json;

    use super::*;
    use crate::card::FsrsMemoryState;
    use crate::deckconfig::FsrsVersion;
    use crate::revlog::RevlogReviewKind;
    use crate::scheduler::fsrs::params::tests::revlog;
    use crate::scheduler::fsrs::preset::tagged_test_overlay;
    use crate::scheduler::fsrs::preset::FsrsPresetId;
    use crate::scheduler::fsrs::preset::FSRS_PRESET_OVERLAY_CONFIG_KEY;
    use crate::tests::NoteAdder;

    #[test]
    fn fsrs6_sync_preserves_the_winning_state_and_schedule_without_repair_uploads() -> Result<()> {
        for locally_newer in [false, true] {
            let mut col = Collection::new();
            col.set_config_bool(BoolKey::Fsrs, true, false)?;
            col.update_default_deck_config(|config| config.fsrs_version = FsrsVersion::Six as i32);
            NoteAdder::basic(&mut col).add(&mut col);
            let mut local = col.get_first_card();
            local.ctype = CardType::Review;
            local.queue = CardQueue::Review;
            local.due = 123;
            local.interval = 30;
            local.usn = Usn(-1);
            local.mtime = TimestampSecs(if locally_newer { 30 } else { 10 });
            local.memory_state = Some(FsrsMemoryState {
                stability: 20.0,
                stability_internal: 20.0,
                difficulty: 6.0,
                stability_fast: None,
            });
            col.storage.update_card(&local)?;

            let mut remote_card = local.clone();
            remote_card.mtime = TimestampSecs(20);
            remote_card.usn = Usn(7);
            remote_card.due = 140;
            remote_card.interval = 47;
            remote_card.memory_state = Some(FsrsMemoryState {
                stability: 35.0,
                stability_internal: 35.0,
                difficulty: 5.0,
                stability_fast: None,
            });
            let mut remote: CardEntry = remote_card.clone().into();
            remote.data = json!({"s":35.0,"d":5.0}).to_string();
            let expected = if locally_newer {
                local.clone()
            } else {
                remote_card
            };
            col.apply_chunk(
                Chunk {
                    cards: vec![remote],
                    done: true,
                    ..Default::default()
                },
                Usn(-1),
            )?;

            assert_eq!(col.repair_foreign_fsrs_memory_states()?, 0);
            assert_eq!(col.storage.get_card(local.id)?.unwrap(), expected);
            let pending = col
                .storage
                .objects_pending_sync::<CardId>("cards", Usn(-1))?;
            assert_eq!(
                pending,
                if locally_newer {
                    vec![local.id]
                } else {
                    vec![]
                }
            );
        }
        Ok(())
    }

    #[test]
    fn stripped_fsrs_state_is_recovered_after_later_revlog_chunks_without_uploads() -> Result<()> {
        // Each client receives the same server-shaped data, repeatedly.
        for mut col in [Collection::new(), Collection::new()] {
            col.set_config_bool(BoolKey::Fsrs, true, false)?;
            NoteAdder::basic(&mut col).add(&mut col);
            let mut card = col.get_first_card();
            card.usn = Usn(7);
            card.mtime = TimestampSecs(1_700_000_000);
            card.ctype = CardType::Review;
            card.queue = CardQueue::Review;
            card.due = 123;
            card.interval = 30;
            card.reps = 3;
            card.lapses = 1;
            col.storage.update_card(&card)?;
            let mut entries = vec![
                revlog(RevlogReviewKind::Learning, 0),
                revlog(RevlogReviewKind::Review, 0),
                revlog(RevlogReviewKind::Relearning, 0),
            ];
            for (entry, offset) in
                entries
                    .iter_mut()
                    .zip([0, 10 * 86_400_000, 10 * 86_400_000 + 300_000])
            {
                entry.id = RevlogId(1_700_000_000_000 + offset);
                entry.cid = card.id;
                entry.usn = Usn(7);
                col.storage.add_revlog_entry(entry, false)?;
            }
            let expected: FsrsMemoryState =
                col.compute_memory_state(card.id)?.state.unwrap().into();
            col.storage.db.execute("delete from revlog", [])?;
            let fsrs = FSRS::new(&col.fsrs_preset_for_card(&card)?.params)?;

            for _ in 0..2 {
                let mut incoming: CardEntry = card.clone().into();
                incoming.data =
                    json!({"s": expected.stability, "d": expected.difficulty}).to_string();
                col.apply_chunk(
                    Chunk {
                        cards: vec![incoming],
                        ..Default::default()
                    },
                    Usn(-1),
                )?;
                let incomplete = col
                    .storage
                    .get_card(card.id)?
                    .unwrap()
                    .memory_state
                    .unwrap();
                assert_eq!(incomplete.stability_internal, incomplete.stability);
                assert_eq!(incomplete.stability_fast, None);

                col.apply_chunk(
                    Chunk {
                        revlog: entries.clone(),
                        done: true,
                        ..Default::default()
                    },
                    Usn(-1),
                )?;
                let candidates = col.storage.card_ids_with_foreign_fsrs_state()?;
                assert_eq!(col.repair_foreign_fsrs_memory_states_inner(candidates)?, 1);

                let repaired = col.storage.get_card(card.id)?.unwrap();
                let state = repaired.memory_state.unwrap();
                assert!((state.stability_internal - expected.stability_internal).abs() < 1e-4);
                assert!(
                    (state.stability_fast.unwrap() - expected.stability_fast.unwrap()).abs() < 1e-4
                );
                assert!((state.difficulty - expected.difficulty).abs() < 1e-3);
                for elapsed in [0.0, 0.5, 10.0, 30.0] {
                    assert!(
                        (fsrs.current_retrievability(state.into(), elapsed)
                            - fsrs.current_retrievability(expected.into(), elapsed))
                        .abs()
                            < 1e-4
                    );
                }
                assert!(
                    (fsrs.interval_at_retrievability(state.into(), 0.85)
                        - fsrs.interval_at_retrievability(expected.into(), 0.85))
                    .abs()
                        < 0.01
                );
                assert_eq!(repaired.mtime, card.mtime);
                assert_eq!(repaired.usn, Usn(7));
                assert_eq!(
                    (
                        repaired.due,
                        repaired.interval,
                        repaired.reps,
                        repaired.lapses
                    ),
                    (123, 30, 3, 1)
                );
                assert!(col
                    .storage
                    .objects_pending_sync::<CardId>("cards", Usn(-1))?
                    .is_empty());
                assert_eq!(col.repair_foreign_fsrs_memory_states()?, 0);
            }

            // A genuine later card change must still be uploaded.
            col.get_and_update_card(card.id, |card| {
                card.reps += 1;
                Ok(())
            })?;
            assert_eq!(
                col.storage
                    .objects_pending_sync::<CardId>("cards", Usn(-1))?,
                vec![card.id]
            );
        }
        Ok(())
    }

    #[test]
    fn applied_remote_notes_refresh_preset_overlay_matches() -> Result<()> {
        let mut col = Collection::new();
        let note = NoteAdder::basic(&mut col).add(&mut col);
        let card = col.get_first_card();
        col.set_config(
            FSRS_PRESET_OVERLAY_CONFIG_KEY,
            &tagged_test_overlay("medical"),
        )?;
        assert_ne!(
            col.fsrs_preset_for_card(&card)?.id,
            FsrsPresetId::Addon("addon:test:tagged".into())
        );

        let mut remote_note = col.storage.get_note(note.id)?.unwrap();
        remote_note.tags.push("medical".into());
        remote_note.set_modified_with_mtime(Usn(5), TimestampSecs(remote_note.mtime.0 + 1));
        let applied = col.apply_chunk(
            Chunk {
                notes: vec![remote_note.into()],
                ..Default::default()
            },
            Usn(-1),
        )?;

        assert_eq!(applied.note_ids, vec![note.id]);
        assert_eq!(applied.card_ids, vec![card.id]);
        assert!(!applied.card_identity_changed);
        assert_eq!(
            col.fsrs_preset_for_card(&card)?.id,
            FsrsPresetId::Addon("addon:test:tagged".into())
        );
        Ok(())
    }

    #[test]
    fn applied_remote_cards_report_identity_changes() -> Result<()> {
        let mut col = Collection::new();
        NoteAdder::basic(&mut col).add(&mut col);
        let card = col.get_first_card();
        let remote_entry = |col: &Collection, update: fn(&mut Card)| {
            let mut remote = col.storage.get_card(card.id).unwrap().unwrap();
            update(&mut remote);
            remote.usn = Usn(5);
            remote.mtime = TimestampSecs(remote.mtime.0 + 1);
            Chunk {
                cards: vec![remote.into()],
                ..Default::default()
            }
        };

        let rescheduled = remote_entry(&col, |card| card.interval = 10);
        let applied = col.apply_chunk(rescheduled, Usn(-1))?;
        assert_eq!(applied.card_ids, vec![card.id]);
        assert!(!applied.card_identity_changed);

        let filtered = remote_entry(&col, |card| {
            card.original_deck_id = card.deck_id;
            card.deck_id = DeckId(50);
        });
        assert!(!col.apply_chunk(filtered, Usn(-1))?.card_identity_changed);

        let moved = remote_entry(&col, |card| {
            card.deck_id = DeckId(60);
            card.original_deck_id = DeckId(0);
        });
        assert!(col.apply_chunk(moved, Usn(-1))?.card_identity_changed);
        Ok(())
    }
}
