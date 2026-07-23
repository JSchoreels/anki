// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

pub mod backup;
mod service;
pub(crate) mod timestamps;
mod transact;
pub(crate) mod undo;

use std::collections::HashMap;
use std::collections::VecDeque;
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
use crate::require;
use crate::scheduler::fsrs::preset::FsrsPresetOverlayCache;
use crate::scheduler::queue::CardQueues;
use crate::scheduler::queue::DeferredRwkvReview;
use crate::scheduler::rwkv::rwkv_review_candidate_metadata;
use crate::scheduler::rwkv::rwkv_review_score_eligibility;
use crate::scheduler::rwkv::RwkvReviewScoreEligibility;
use crate::scheduler::timing::SchedTimingToday;
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
    pub(crate) rwkv_retrievability_scores: Option<RwkvRetrievabilityScores>,
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
pub(crate) struct RwkvRetrievabilityScores {
    pub(crate) days_elapsed: u32,
    scores: HashMap<CardId, RwkvRetrievabilityScore>,
    review_queue_scores: Option<RwkvReviewQueueScores>,
    stats_graph_scores: VecDeque<RwkvStatsGraphScores>,
    deck_count_scores: HashMap<DeckId, HashMap<CardId, RwkvReviewQueueScoreEntry>>,
}

#[derive(Debug, Clone, Default)]
struct RwkvRetrievabilityScore {
    card_info: Option<f32>,
}

#[derive(Debug, Clone)]
struct RwkvStatsGraphScores {
    search: String,
    scores: HashMap<CardId, f32>,
}

// Different stats filters can be prepared concurrently. Keep a small set of
// recent snapshots so one request cannot invalidate another before it renders.
const MAX_RWKV_STATS_GRAPH_SEARCHES: usize = 8;

#[derive(Debug, Clone)]
struct RwkvReviewQueueScores {
    deck_id: DeckId,
    scores: Arc<HashMap<CardId, RwkvReviewQueueScoreEntry>>,
}

#[derive(Clone, Copy)]
enum RwkvRetrievabilityScoreSource<'a> {
    DeckCounts(&'a HashMap<DeckId, HashMap<CardId, RwkvReviewQueueScoreEntry>>),
    Stats(&'a HashMap<CardId, f32>),
    ReviewQueue(&'a HashMap<CardId, RwkvReviewQueueScoreEntry>),
    CardInfo(&'a HashMap<CardId, RwkvRetrievabilityScore>),
}

#[derive(Debug, Clone, Copy)]
pub(crate) struct RwkvReviewQueueScoreEntry {
    pub(crate) retrievability: f32,
    pub(crate) intervening_reviews: Option<u32>,
    pub(crate) target_retention: Option<f32>,
}

impl RwkvReviewQueueScoreEntry {
    #[cfg(test)]
    pub(crate) fn new(retrievability: f32) -> Self {
        Self {
            retrievability,
            intervening_reviews: None,
            target_retention: None,
        }
    }
}

impl RwkvRetrievabilityScores {
    fn active_score_for_card(&self, card_id: CardId, stats_search: Option<&str>) -> Option<f32> {
        self.active_score_sources(stats_search)
            .rev()
            .find_map(|source| source.score_for_card(card_id))
    }

    fn active_scores(&self, stats_search: Option<&str>) -> Option<HashMap<CardId, f32>> {
        let mut scores = HashMap::new();
        for source in self.active_score_sources(stats_search) {
            source.extend_scores(&mut scores);
        }
        (!scores.is_empty()).then_some(scores)
    }

    /// Score sources in ascending priority. Later sources replace earlier ones.
    fn active_score_sources(
        &self,
        stats_search: Option<&str>,
    ) -> impl DoubleEndedIterator<Item = RwkvRetrievabilityScoreSource<'_>> {
        [
            Some(RwkvRetrievabilityScoreSource::DeckCounts(
                &self.deck_count_scores,
            )),
            self.stats_graph_score_map(stats_search)
                .map(RwkvRetrievabilityScoreSource::Stats),
            self.review_queue_scores
                .as_ref()
                .map(|queue| RwkvRetrievabilityScoreSource::ReviewQueue(&queue.scores)),
            Some(RwkvRetrievabilityScoreSource::CardInfo(&self.scores)),
        ]
        .into_iter()
        .flatten()
    }

    fn stats_graph_scores(&self, stats_search: Option<&str>) -> Option<HashMap<CardId, f32>> {
        self.stats_graph_score_map(stats_search).cloned()
    }

    fn stats_graph_score_map(&self, stats_search: Option<&str>) -> Option<&HashMap<CardId, f32>> {
        match stats_search {
            Some(search) => self
                .stats_graph_scores
                .iter()
                .rev()
                .find(|entry| entry.search == search),
            None => self.stats_graph_scores.back(),
        }
        .map(|entry| &entry.scores)
    }

    fn review_queue_scores(
        &self,
        deck_id: DeckId,
    ) -> Option<Arc<HashMap<CardId, RwkvReviewQueueScoreEntry>>> {
        self.review_queue_scores
            .as_ref()
            .filter(|queue| queue.deck_id == deck_id)
            .map(|queue| Arc::clone(&queue.scores))
    }

    fn review_queue_scores_for_any_deck(
        &self,
    ) -> Option<(DeckId, Arc<HashMap<CardId, RwkvReviewQueueScoreEntry>>)> {
        self.review_queue_scores
            .as_ref()
            .map(|queue| (queue.deck_id, Arc::clone(&queue.scores)))
    }

    fn card_info_scores(&self) -> Option<HashMap<CardId, f32>> {
        let scores: HashMap<_, _> = self
            .scores
            .iter()
            .filter_map(|(&card_id, score)| {
                score
                    .card_info
                    .map(|retrievability| (card_id, retrievability))
            })
            .collect();

        (!scores.is_empty()).then_some(scores)
    }

    fn set_stats_graph_scores(&mut self, search: String, scores: HashMap<CardId, f32>) {
        self.stats_graph_scores
            .retain(|entry| entry.search != search);
        self.stats_graph_scores
            .push_back(RwkvStatsGraphScores { search, scores });
        while self.stats_graph_scores.len() > MAX_RWKV_STATS_GRAPH_SEARCHES {
            self.stats_graph_scores.pop_front();
        }
    }

    fn set_review_queue_scores(
        &mut self,
        deck_id: DeckId,
        scores: HashMap<CardId, RwkvReviewQueueScoreEntry>,
    ) {
        self.review_queue_scores = (!scores.is_empty()).then_some(RwkvReviewQueueScores {
            deck_id,
            scores: Arc::new(scores),
        });
    }

    fn patch_review_queue_score(
        &mut self,
        deck_id: DeckId,
        card_id: CardId,
        entry: Option<RwkvReviewQueueScoreEntry>,
    ) {
        let Some(queue) = self
            .review_queue_scores
            .as_mut()
            .filter(|queue| queue.deck_id == deck_id)
        else {
            return;
        };
        let scores = Arc::make_mut(&mut queue.scores);

        match entry {
            Some(entry) => {
                scores.insert(card_id, entry);
            }
            None => {
                scores.remove(&card_id);
            }
        }
        if scores.is_empty() {
            self.review_queue_scores = None;
        }
    }

    fn set_deck_count_scores(
        &mut self,
        deck_id: DeckId,
        scores: HashMap<CardId, RwkvReviewQueueScoreEntry>,
    ) {
        if scores.is_empty() {
            self.deck_count_scores.remove(&deck_id);
        } else {
            self.deck_count_scores.insert(deck_id, scores);
        }
    }

    fn clear_deck_count_scores(&mut self) {
        self.deck_count_scores.clear();
    }

    fn update_review_queue_intervening_reviews(
        &mut self,
        deck_id: DeckId,
        intervening_reviews_by_card_id: HashMap<CardId, u32>,
    ) {
        let Some(queue) = self
            .review_queue_scores
            .as_mut()
            .filter(|queue| queue.deck_id == deck_id)
        else {
            return;
        };
        let scores = Arc::make_mut(&mut queue.scores);
        for (card_id, intervening_reviews) in intervening_reviews_by_card_id {
            if let Some(score) = scores.get_mut(&card_id) {
                score.intervening_reviews = Some(intervening_reviews);
            }
        }
    }

    fn set_card_info_score(&mut self, card_id: CardId, retrievability: Option<f32>) {
        match retrievability {
            Some(retrievability) => {
                self.scores.entry(card_id).or_default().card_info = Some(retrievability);
            }
            None => {
                if let Some(score) = self.scores.get_mut(&card_id) {
                    score.card_info = None;
                }
            }
        }
        self.prune_empty_scores();
    }

    fn is_empty(&self) -> bool {
        self.scores.is_empty()
            && self.review_queue_scores.is_none()
            && self.stats_graph_scores.is_empty()
            && self.deck_count_scores.is_empty()
    }

    fn prune_empty_scores(&mut self) {
        self.scores.retain(|_, score| !score.is_empty());
    }
}

impl RwkvRetrievabilityScoreSource<'_> {
    fn score_for_card(self, card_id: CardId) -> Option<f32> {
        match self {
            Self::DeckCounts(scopes) => scopes
                .values()
                .find_map(|scores| scores.get(&card_id).map(|entry| entry.retrievability)),
            Self::Stats(scores) => scores.get(&card_id).copied(),
            Self::ReviewQueue(scores) => scores.get(&card_id).map(|entry| entry.retrievability),
            Self::CardInfo(scores) => scores
                .get(&card_id)
                .and_then(RwkvRetrievabilityScore::active_score),
        }
    }

    fn extend_scores(self, active_scores: &mut HashMap<CardId, f32>) {
        match self {
            Self::DeckCounts(scopes) => {
                active_scores.extend(scopes.values().flat_map(|scores| {
                    scores
                        .iter()
                        .map(|(&card_id, entry)| (card_id, entry.retrievability))
                }));
            }
            Self::Stats(scores) => {
                active_scores.extend(scores.iter().map(|(&card_id, &score)| (card_id, score)));
            }
            Self::ReviewQueue(scores) => {
                active_scores.extend(
                    scores
                        .iter()
                        .map(|(&card_id, entry)| (card_id, entry.retrievability)),
                );
            }
            Self::CardInfo(scores) => {
                active_scores.extend(scores.iter().filter_map(|(&card_id, score)| {
                    score
                        .active_score()
                        .map(|retrievability| (card_id, retrievability))
                }));
            }
        }
    }
}

impl RwkvRetrievabilityScore {
    fn active_score(&self) -> Option<f32> {
        self.card_info
    }

    fn is_empty(&self) -> bool {
        self.card_info.is_none()
    }
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

    fn rwkv_retrievability_scores_mut(
        &mut self,
        days_elapsed: u32,
    ) -> &mut RwkvRetrievabilityScores {
        if self
            .state
            .rwkv_retrievability_scores
            .as_ref()
            .map_or(true, |scores| scores.days_elapsed != days_elapsed)
        {
            self.state.rwkv_retrievability_scores = Some(RwkvRetrievabilityScores {
                days_elapsed,
                scores: HashMap::new(),
                review_queue_scores: None,
                stats_graph_scores: VecDeque::new(),
                deck_count_scores: HashMap::new(),
            });
        }

        self.state.rwkv_retrievability_scores.as_mut().unwrap()
    }

    fn clear_empty_rwkv_retrievability_scores(&mut self) {
        if self
            .state
            .rwkv_retrievability_scores
            .as_ref()
            .is_some_and(|scores| scores.is_empty())
        {
            self.state.rwkv_retrievability_scores = None;
        }
    }

    #[cfg(test)]
    pub(crate) fn set_rwkv_review_queue_scores(
        &mut self,
        deck_id: DeckId,
        scores: HashMap<CardId, f32>,
    ) -> Result<()> {
        let scores = scores
            .into_iter()
            .map(|(card_id, retrievability)| {
                (card_id, RwkvReviewQueueScoreEntry::new(retrievability))
            })
            .collect();
        self.set_rwkv_review_queue_score_entries(deck_id, scores)
    }

    pub(crate) fn set_rwkv_review_queue_score_entries(
        &mut self,
        deck_id: DeckId,
        scores: HashMap<CardId, RwkvReviewQueueScoreEntry>,
    ) -> Result<()> {
        let days_elapsed = self.timing_today()?.days_elapsed;
        self.state.card_queues = None;
        self.clear_rwkv_deck_count_scores();
        self.rwkv_retrievability_scores_mut(days_elapsed)
            .set_review_queue_scores(deck_id, scores);
        self.clear_empty_rwkv_retrievability_scores();
        Ok(())
    }

    pub(crate) fn patch_answered_card_rwkv_review_queue_score_entry(
        &mut self,
        deck_id: DeckId,
        card_id: CardId,
        entry: Option<RwkvReviewQueueScoreEntry>,
    ) -> Result<()> {
        require!(
            self.state
                .card_queues
                .as_ref()
                .map_or(true, |queues| !queues.main_contains(card_id)),
            "cannot patch RWKV queue score before the answered card is removed"
        );
        let timing = self.timing_today()?;
        let eligibility = if self.state.card_queues.is_some() {
            entry
                .map(|entry| {
                    self.rwkv_answered_card_score_eligibility(deck_id, card_id, entry, timing)
                })
                .transpose()?
                .flatten()
        } else {
            None
        };
        if let Some(scores) = self
            .state
            .rwkv_retrievability_scores
            .as_mut()
            .filter(|scores| scores.days_elapsed == timing.days_elapsed)
        {
            scores.patch_review_queue_score(deck_id, card_id, entry);
        }
        match eligibility {
            Some(RwkvReviewScoreEligibility::Eligible) => {
                self.state.card_queues = None;
            }
            Some(eligibility @ RwkvReviewScoreEligibility::Deferred { .. }) => {
                if let Some(queues) = self.state.card_queues.as_mut() {
                    queues.defer_rwkv_review(
                        card_id,
                        DeferredRwkvReview::from_eligibility(eligibility, timing.now).unwrap(),
                    );
                }
            }
            Some(RwkvReviewScoreEligibility::Blocked) | None => {
                if let Some(queues) = self.state.card_queues.as_mut() {
                    queues.remove_deferred_rwkv_review(card_id);
                }
            }
        }
        self.clear_rwkv_deck_count_scores();
        self.clear_empty_rwkv_retrievability_scores();
        Ok(())
    }

    fn rwkv_answered_card_score_eligibility(
        &mut self,
        deck_id: DeckId,
        card_id: CardId,
        entry: RwkvReviewQueueScoreEntry,
        timing: SchedTimingToday,
    ) -> Result<Option<RwkvReviewScoreEligibility>> {
        let Some(deck) = self.storage.get_deck(deck_id)? else {
            return Ok(None);
        };
        let Some(config_id) = deck.config_id() else {
            return Ok(None);
        };
        let Some(config) = self.storage.get_deck_config(config_id)? else {
            return Ok(None);
        };
        let Some(metadata) =
            rwkv_review_candidate_metadata(self, &[card_id], timing)?.remove(&card_id)
        else {
            return Ok(None);
        };
        Ok(Some(rwkv_review_score_eligibility(
            entry.retrievability,
            &metadata,
            config.inner.rwkv_review_allow_same_day_review,
            config.inner.rwkv_review_min_intervening_reviews,
            config.inner.rwkv_review_min_elapsed_secs,
            entry.intervening_reviews,
            entry.target_retention,
        )))
    }

    pub(crate) fn update_rwkv_review_queue_intervening_reviews(
        &mut self,
        deck_id: DeckId,
        intervening_reviews_by_card_id: HashMap<CardId, u32>,
    ) -> Result<()> {
        let days_elapsed = self.timing_today()?.days_elapsed;
        let queue_rebuild_required = self.state.card_queues.as_mut().is_some_and(|queues| {
            queues.update_deferred_rwkv_intervening_reviews(&intervening_reviews_by_card_id)
        });
        if let Some(scores) = self
            .state
            .rwkv_retrievability_scores
            .as_mut()
            .filter(|scores| scores.days_elapsed == days_elapsed)
        {
            scores.update_review_queue_intervening_reviews(deck_id, intervening_reviews_by_card_id);
        }
        if queue_rebuild_required {
            self.state.card_queues = None;
        }
        Ok(())
    }

    pub(crate) fn rwkv_review_queue_scores(
        &self,
        deck_id: DeckId,
        days_elapsed: u32,
    ) -> Option<Arc<HashMap<CardId, RwkvReviewQueueScoreEntry>>> {
        self.state
            .rwkv_retrievability_scores
            .as_ref()
            .filter(|scores| scores.days_elapsed == days_elapsed)
            .and_then(|scores| scores.review_queue_scores(deck_id))
    }

    pub(crate) fn rwkv_review_queue_scores_for_day(
        &self,
        days_elapsed: u32,
    ) -> Option<(DeckId, Arc<HashMap<CardId, RwkvReviewQueueScoreEntry>>)> {
        self.state
            .rwkv_retrievability_scores
            .as_ref()
            .filter(|scores| scores.days_elapsed == days_elapsed)
            .and_then(RwkvRetrievabilityScores::review_queue_scores_for_any_deck)
    }

    pub(crate) fn set_rwkv_deck_count_score_entries(
        &mut self,
        deck_id: DeckId,
        scores: HashMap<CardId, RwkvReviewQueueScoreEntry>,
    ) -> Result<()> {
        let days_elapsed = self.timing_today()?.days_elapsed;
        self.rwkv_retrievability_scores_mut(days_elapsed)
            .set_deck_count_scores(deck_id, scores);
        self.clear_empty_rwkv_retrievability_scores();
        Ok(())
    }

    #[cfg(test)]
    pub(crate) fn set_rwkv_deck_count_scores(
        &mut self,
        deck_id: DeckId,
        scores: HashMap<CardId, f32>,
    ) -> Result<()> {
        let scores = scores
            .into_iter()
            .map(|(card_id, retrievability)| {
                (card_id, RwkvReviewQueueScoreEntry::new(retrievability))
            })
            .collect();
        self.set_rwkv_deck_count_score_entries(deck_id, scores)
    }

    pub(crate) fn clear_rwkv_deck_count_scores(&mut self) {
        if let Some(scores) = self.state.rwkv_retrievability_scores.as_mut() {
            scores.clear_deck_count_scores();
        }
        self.clear_empty_rwkv_retrievability_scores();
    }

    pub(crate) fn take_rwkv_deck_count_scores_for_day(
        &mut self,
        days_elapsed: u32,
    ) -> HashMap<DeckId, HashMap<CardId, RwkvReviewQueueScoreEntry>> {
        self.state
            .rwkv_retrievability_scores
            .as_mut()
            .filter(|scores| scores.days_elapsed == days_elapsed)
            .map(|scores| std::mem::take(&mut scores.deck_count_scores))
            .unwrap_or_default()
    }

    pub(crate) fn restore_rwkv_deck_count_scores(
        &mut self,
        days_elapsed: u32,
        deck_count_scores: HashMap<DeckId, HashMap<CardId, RwkvReviewQueueScoreEntry>>,
    ) {
        if deck_count_scores.is_empty() {
            return;
        }
        self.rwkv_retrievability_scores_mut(days_elapsed)
            .deck_count_scores = deck_count_scores;
    }

    pub(crate) fn set_rwkv_stats_graph_scores(
        &mut self,
        search: String,
        scores: HashMap<CardId, f32>,
    ) -> Result<()> {
        let days_elapsed = self.timing_today()?.days_elapsed;
        self.rwkv_retrievability_scores_mut(days_elapsed)
            .set_stats_graph_scores(search, scores);
        self.clear_empty_rwkv_retrievability_scores();
        Ok(())
    }

    pub(crate) fn rwkv_stats_graph_scores_for_day(
        &self,
        days_elapsed: u32,
    ) -> Option<HashMap<CardId, f32>> {
        self.rwkv_stats_graph_scores_for_search(days_elapsed, None)
    }

    pub(crate) fn rwkv_stats_graph_scores_for_search(
        &self,
        days_elapsed: u32,
        search: Option<&str>,
    ) -> Option<HashMap<CardId, f32>> {
        self.state
            .rwkv_retrievability_scores
            .as_ref()
            .filter(|scores| scores.days_elapsed == days_elapsed)
            .and_then(|scores| scores.stats_graph_scores(search))
    }

    pub(crate) fn set_rwkv_card_info_score(
        &mut self,
        card_id: CardId,
        retrievability: Option<f32>,
    ) -> Result<()> {
        let days_elapsed = self.timing_today()?.days_elapsed;
        self.rwkv_retrievability_scores_mut(days_elapsed)
            .set_card_info_score(card_id, retrievability);
        self.clear_empty_rwkv_retrievability_scores();
        Ok(())
    }

    pub(crate) fn rwkv_card_info_scores_for_day(
        &self,
        days_elapsed: u32,
    ) -> Option<HashMap<CardId, f32>> {
        self.state
            .rwkv_retrievability_scores
            .as_ref()
            .filter(|scores| scores.days_elapsed == days_elapsed)
            .and_then(RwkvRetrievabilityScores::card_info_scores)
    }

    pub(crate) fn rwkv_retrievability_scores_for_day(
        &self,
        days_elapsed: u32,
        stats_search: Option<&str>,
    ) -> Option<HashMap<CardId, f32>> {
        self.state
            .rwkv_retrievability_scores
            .as_ref()
            .filter(|scores| scores.days_elapsed == days_elapsed)
            .and_then(|scores| scores.active_scores(stats_search))
    }

    pub(crate) fn rwkv_retrievability_score_for_day(
        &self,
        card_id: CardId,
        days_elapsed: u32,
    ) -> Option<f32> {
        self.state
            .rwkv_retrievability_scores
            .as_ref()
            .filter(|scores| scores.days_elapsed == days_elapsed)
            .and_then(|scores| scores.active_score_for_card(card_id, None))
    }
}

#[cfg(test)]
mod test {
    use super::*;

    #[test]
    fn rwkv_score_resolver_shares_source_priority_between_lookup_forms() {
        let card_info_card = CardId(1);
        let review_queue_card = CardId(2);
        let stats_card = CardId(3);
        let deck_count_card = CardId(4);
        let missing_card = CardId(5);
        let all_cards = [
            card_info_card,
            review_queue_card,
            stats_card,
            deck_count_card,
        ];

        let deck_count_scores = all_cards
            .into_iter()
            .map(|card_id| (card_id, RwkvReviewQueueScoreEntry::new(0.1)))
            .collect();
        let stats_scores = [card_info_card, review_queue_card, stats_card]
            .into_iter()
            .map(|card_id| (card_id, 0.2))
            .collect();
        let review_queue_scores = [card_info_card, review_queue_card]
            .into_iter()
            .map(|card_id| (card_id, RwkvReviewQueueScoreEntry::new(0.3)))
            .collect();
        let card_info_scores = HashMap::from([(
            card_info_card,
            RwkvRetrievabilityScore {
                card_info: Some(0.4),
            },
        )]);
        let scores = RwkvRetrievabilityScores {
            days_elapsed: 0,
            scores: card_info_scores,
            review_queue_scores: Some(RwkvReviewQueueScores {
                deck_id: DeckId(1),
                scores: Arc::new(review_queue_scores),
            }),
            stats_graph_scores: VecDeque::from([RwkvStatsGraphScores {
                search: "deck:current".into(),
                scores: stats_scores,
            }]),
            deck_count_scores: HashMap::from([(DeckId(1), deck_count_scores)]),
        };
        let expected = HashMap::from([
            (card_info_card, 0.4),
            (review_queue_card, 0.3),
            (stats_card, 0.2),
            (deck_count_card, 0.1),
        ]);

        assert_eq!(scores.active_scores(None), Some(expected.clone()));
        for (card_id, expected_score) in expected {
            assert_eq!(
                scores.active_score_for_card(card_id, None),
                Some(expected_score)
            );
        }
        assert_eq!(scores.active_score_for_card(missing_card, None), None);
    }
}
