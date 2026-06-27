// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

pub mod backup;
mod service;
pub(crate) mod timestamps;
mod transact;
pub(crate) mod undo;

use std::collections::HashMap;
use std::fmt::Debug;
use std::fmt::Formatter;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::Mutex;

use anki_i18n::I18n;
use anki_io::create_dir_all;

use crate::browser_table;
use crate::card::CardId;
use crate::decks::Deck;
use crate::decks::DeckId;
use crate::error::Result;
use crate::notetype::Notetype;
use crate::notetype::NotetypeId;
use crate::progress::ProgressState;
use crate::scheduler::fsrs::preset::FsrsPresetOverlayCache;
use crate::scheduler::queue::CardQueues;
use crate::scheduler::SchedulerInfo;
use crate::storage::SchemaVersion;
use crate::storage::SqliteStorage;
use crate::timestamp::TimestampMillis;
use crate::types::Usn;
use crate::undo::UndoManager;

#[derive(Default)]
pub struct CollectionBuilder {
    collection_path: Option<PathBuf>,
    media_folder: Option<PathBuf>,
    media_db: Option<PathBuf>,
    server: Option<bool>,
    tr: Option<I18n>,
    check_integrity: bool,
    progress_handler: Option<Arc<Mutex<ProgressState>>>,
}

impl CollectionBuilder {
    /// Create a new builder with the provided collection path.
    /// If an in-memory database is desired, used ::default() instead.
    pub fn new(col_path: impl Into<PathBuf>) -> Self {
        let mut builder = Self::default();
        builder.set_collection_path(col_path);
        builder
    }

    pub fn build(&mut self) -> Result<Collection> {
        let col_path = self
            .collection_path
            .clone()
            .unwrap_or_else(|| PathBuf::from(":memory:"));
        let tr = self.tr.clone().unwrap_or_else(I18n::template_only);
        let server = self.server.unwrap_or_default();
        let media_folder = self.media_folder.clone().unwrap_or_default();
        let media_db = self.media_db.clone().unwrap_or_default();
        let storage = SqliteStorage::open_or_create(&col_path, &tr, server, self.check_integrity)?;
        let col = Collection {
            storage,
            col_path,
            media_folder,
            media_db,
            tr,
            server,
            state: CollectionState {
                progress: self.progress_handler.clone().unwrap_or_default(),
                ..Default::default()
            },
        };

        Ok(col)
    }

    pub fn set_collection_path<P: Into<PathBuf>>(&mut self, collection: P) -> &mut Self {
        self.collection_path = Some(collection.into());
        self
    }

    pub fn set_media_paths<P: Into<PathBuf>>(&mut self, media_folder: P, media_db: P) -> &mut Self {
        self.media_folder = Some(media_folder.into());
        self.media_db = Some(media_db.into());
        self
    }

    /// For a `foo.anki2` file, use `foo.media` and `foo.mdb`. Mobile clients
    /// use different paths, so the backend must continue to use
    /// [set_media_paths].
    pub fn with_desktop_media_paths(&mut self) -> &mut Self {
        let col_path = self.collection_path.as_ref().unwrap();
        let media_folder = col_path.with_extension("media");
        create_dir_all(&media_folder).expect("creating media folder");
        let media_db = col_path.with_extension("mdb");
        self.set_media_paths(media_folder, media_db)
    }

    pub fn set_server(&mut self, server: bool) -> &mut Self {
        self.server = Some(server);
        self
    }

    pub fn set_tr(&mut self, tr: I18n) -> &mut Self {
        self.tr = Some(tr);
        self
    }

    pub fn set_check_integrity(&mut self, check_integrity: bool) -> &mut Self {
        self.check_integrity = check_integrity;
        self
    }

    /// If provided, progress info will be written to the provided mutex, and
    /// can be tracked on a separate thread.
    pub fn set_shared_progress_state(&mut self, state: Arc<Mutex<ProgressState>>) -> &mut Self {
        self.progress_handler = Some(state);
        self
    }
}

#[derive(Debug, Default)]
pub struct CollectionState {
    pub(crate) undo: UndoManager,
    pub(crate) notetype_cache: HashMap<NotetypeId, Arc<Notetype>>,
    pub(crate) deck_cache: HashMap<DeckId, Arc<Deck>>,
    pub(crate) scheduler_info: Option<SchedulerInfo>,
    pub(crate) card_queues: Option<CardQueues>,
    pub(crate) rwkv_review_queue_scores: Option<RwkvReviewQueueScores>,
    pub(crate) rwkv_stats_graph_scores: Option<RwkvStatsGraphScores>,
    pub(crate) rwkv_card_info_scores: Option<RwkvCardInfoScores>,
    pub(crate) fsrs_preset_overlay_cache: Option<FsrsPresetOverlayCache>,
    pub(crate) active_browser_columns: Option<Arc<Vec<browser_table::Column>>>,
    /// True if legacy Python code has executed SQL that has modified the
    /// database, requiring modification time to be bumped.
    pub(crate) modified_by_dbproxy: bool,
    /// The modification time at the last backup, so we don't create multiple
    /// identical backups.
    pub(crate) last_backup_modified: Option<TimestampMillis>,
    pub(crate) progress: Arc<Mutex<ProgressState>>,
}

pub struct Collection {
    pub storage: SqliteStorage,
    pub(crate) col_path: PathBuf,
    pub(crate) media_folder: PathBuf,
    pub(crate) media_db: PathBuf,
    pub(crate) tr: I18n,
    pub(crate) server: bool,
    pub(crate) state: CollectionState,
}

#[derive(Debug, Clone)]
pub(crate) struct RwkvReviewQueueScores {
    pub(crate) deck_id: DeckId,
    pub(crate) days_elapsed: u32,
    pub(crate) scores: HashMap<CardId, f32>,
}

#[derive(Debug, Clone)]
pub(crate) struct RwkvStatsGraphScores {
    pub(crate) search: String,
    pub(crate) days_elapsed: u32,
    pub(crate) scores: HashMap<CardId, f32>,
}

#[derive(Debug, Clone)]
pub(crate) struct RwkvCardInfoScores {
    pub(crate) days_elapsed: u32,
    pub(crate) scores: HashMap<CardId, f32>,
}

impl Debug for Collection {
    fn fmt(&self, f: &mut Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Collection")
            .field("col_path", &self.col_path)
            .finish()
    }
}

impl Collection {
    pub fn as_builder(&self) -> CollectionBuilder {
        let mut builder = CollectionBuilder::new(&self.col_path);
        builder
            .set_media_paths(self.media_folder.clone(), self.media_db.clone())
            .set_server(self.server)
            .set_tr(self.tr.clone())
            .set_shared_progress_state(self.state.progress.clone());
        builder
    }

    // A count of all changed rows since the collection was opened, which can be
    // used to detect if the collection was modified or not.
    pub fn changes_since_open(&self) -> Result<u64> {
        self.storage
            .db
            .query_row("select total_changes()", [], |row| row.get(0))
            .map_err(Into::into)
    }

    pub fn close(self, desired_version: Option<SchemaVersion>) -> Result<()> {
        self.storage.close(desired_version)
    }

    pub(crate) fn usn(&self) -> Result<Usn> {
        // if we cache this in the future, must make sure to invalidate cache when usn
        // bumped in sync.finish()
        self.storage.usn(self.server)
    }

    /// Prepare for upload. Caller should not create transaction.
    pub(crate) fn before_upload(&mut self) -> Result<()> {
        self.transact_no_undo(|col| {
            col.storage.clear_all_graves()?;
            col.storage.clear_pending_note_usns()?;
            col.storage.clear_pending_card_usns()?;
            col.storage.clear_pending_revlog_usns()?;
            col.storage.clear_tag_usns()?;
            col.storage.clear_deck_conf_usns()?;
            col.storage.clear_deck_usns()?;
            col.storage.clear_notetype_usns()?;
            col.storage.increment_usn()?;
            col.set_schema_modified()?;
            col.storage
                .set_last_sync(col.storage.get_collection_timestamps()?.schema_change)
        })?;
        self.storage.optimize()
    }

    pub(crate) fn clear_caches(&mut self) {
        self.state.deck_cache.clear();
        self.state.notetype_cache.clear();
        self.state.fsrs_preset_overlay_cache = None;
    }

    pub fn tr(&self) -> &I18n {
        &self.tr
    }

    pub(crate) fn set_rwkv_review_queue_scores(
        &mut self,
        deck_id: DeckId,
        scores: HashMap<CardId, f32>,
    ) -> Result<()> {
        if scores.is_empty() {
            self.state.rwkv_review_queue_scores = None;
            self.state.card_queues = None;
            return Ok(());
        }

        let days_elapsed = self.timing_today()?.days_elapsed;
        self.state.card_queues = None;

        self.state.rwkv_review_queue_scores = Some(RwkvReviewQueueScores {
            deck_id,
            days_elapsed,
            scores,
        });
        Ok(())
    }

    pub(crate) fn rwkv_review_queue_scores(
        &self,
        deck_id: DeckId,
        days_elapsed: u32,
    ) -> Option<&HashMap<CardId, f32>> {
        self.state
            .rwkv_review_queue_scores
            .as_ref()
            .filter(|scores| scores.deck_id == deck_id && scores.days_elapsed == days_elapsed)
            .map(|scores| &scores.scores)
    }

    pub(crate) fn rwkv_review_queue_scores_for_day(
        &self,
        days_elapsed: u32,
    ) -> Option<&HashMap<CardId, f32>> {
        self.state
            .rwkv_review_queue_scores
            .as_ref()
            .filter(|scores| scores.days_elapsed == days_elapsed)
            .map(|scores| &scores.scores)
    }

    pub(crate) fn set_rwkv_stats_graph_scores(
        &mut self,
        search: String,
        scores: HashMap<CardId, f32>,
    ) -> Result<()> {
        let days_elapsed = self.timing_today()?.days_elapsed;
        self.state.rwkv_stats_graph_scores = Some(RwkvStatsGraphScores {
            search,
            days_elapsed,
            scores,
        });
        Ok(())
    }

    pub(crate) fn rwkv_stats_graph_scores(
        &self,
        search: &str,
        days_elapsed: u32,
    ) -> Option<&HashMap<CardId, f32>> {
        self.state
            .rwkv_stats_graph_scores
            .as_ref()
            .filter(|scores| scores.search == search && scores.days_elapsed == days_elapsed)
            .map(|scores| &scores.scores)
    }

    pub(crate) fn rwkv_stats_graph_scores_for_day(
        &self,
        days_elapsed: u32,
    ) -> Option<&HashMap<CardId, f32>> {
        self.state
            .rwkv_stats_graph_scores
            .as_ref()
            .filter(|scores| scores.days_elapsed == days_elapsed)
            .map(|scores| &scores.scores)
    }

    pub(crate) fn set_rwkv_card_info_score(
        &mut self,
        card_id: CardId,
        retrievability: Option<f32>,
    ) -> Result<()> {
        let days_elapsed = self.timing_today()?.days_elapsed;
        if self
            .state
            .rwkv_card_info_scores
            .as_ref()
            .map_or(true, |scores| scores.days_elapsed != days_elapsed)
        {
            self.state.rwkv_card_info_scores = Some(RwkvCardInfoScores {
                days_elapsed,
                scores: HashMap::new(),
            });
        }

        let scores = self.state.rwkv_card_info_scores.as_mut().unwrap();
        match retrievability {
            Some(retrievability) => {
                scores.scores.insert(card_id, retrievability);
            }
            None => {
                scores.scores.remove(&card_id);
            }
        }

        if scores.scores.is_empty() {
            self.state.rwkv_card_info_scores = None;
        }
        Ok(())
    }

    pub(crate) fn rwkv_card_info_scores_for_day(
        &self,
        days_elapsed: u32,
    ) -> Option<&HashMap<CardId, f32>> {
        self.state
            .rwkv_card_info_scores
            .as_ref()
            .filter(|scores| scores.days_elapsed == days_elapsed)
            .map(|scores| &scores.scores)
    }
}
