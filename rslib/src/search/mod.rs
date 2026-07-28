// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

mod builder;
mod parser;
mod service;
mod sqlwriter;
pub(crate) mod writer;

use std::borrow::Cow;
use std::cmp::Ordering;
use std::time::Instant;

pub use builder::JoinSearches;
pub use builder::Negated;
pub use builder::SearchBuilder;
pub use parser::parse as parse_search;
pub use parser::FieldSearchMode;
pub use parser::Node;
pub use parser::PropertyKind;
pub use parser::RatingKind;
pub use parser::SearchNode;
pub use parser::StateKind;
pub use parser::TemplateKind;
use rusqlite::params_from_iter;
use rusqlite::types::FromSql;
use sqlwriter::RequiredTable;
use sqlwriter::SqlWriter;
pub use writer::replace_search_node;

use crate::browser_table::Column;
use crate::card::Card;
use crate::card::CardType;
use crate::prelude::*;
use crate::scheduler::fsrs::memory_state::fsrs_current_retrievability_for_params;
use crate::scheduler::rwkv::rwkv_review_candidate_metadata;
use crate::scheduler::rwkv::rwkv_review_score_eligibility;
use crate::scheduler::rwkv::RwkvReviewScoreEligibility;
use crate::scheduler::timing::SchedTimingToday;

#[derive(Debug, PartialEq, Eq, Clone, Copy)]
pub enum ReturnItemType {
    Cards,
    Notes,
}

#[derive(Debug, PartialEq, Eq, Clone)]
pub enum SortMode {
    NoOrder,
    Builtin { column: Column, reverse: bool },
    Custom(String),
}

const EXACT_RETRIEVABILITY_TABLE: &str = "search_exact_retrievability";
pub(super) const RWKV_DUE_TABLE: &str = "search_rwkv_due";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ExactFsrsSortMetric {
    Retrievability,
    StabilityS90,
}

pub trait AsReturnItemType {
    fn as_return_item_type() -> ReturnItemType;
}

impl AsReturnItemType for CardId {
    fn as_return_item_type() -> ReturnItemType {
        ReturnItemType::Cards
    }
}

impl AsReturnItemType for NoteId {
    fn as_return_item_type() -> ReturnItemType {
        ReturnItemType::Notes
    }
}

impl ReturnItemType {
    fn required_table(&self) -> RequiredTable {
        match self {
            ReturnItemType::Cards => RequiredTable::Cards,
            ReturnItemType::Notes => RequiredTable::Notes,
        }
    }
}

impl SortMode {
    fn required_table(&self) -> RequiredTable {
        match self {
            SortMode::NoOrder => RequiredTable::CardsOrNotes,
            SortMode::Builtin { column, .. } => column.required_table(),
            SortMode::Custom(ref text) => {
                if text.contains("n.") {
                    if text.contains("c.") {
                        RequiredTable::CardsAndNotes
                    } else {
                        RequiredTable::Notes
                    }
                } else {
                    RequiredTable::Cards
                }
            }
        }
    }
}

impl Column {
    fn required_table(self) -> RequiredTable {
        match self {
            Column::Cards
            | Column::NoteCreation
            | Column::NoteMod
            | Column::Notetype
            | Column::SortField
            | Column::Tags => RequiredTable::Notes,
            _ => RequiredTable::CardsOrNotes,
        }
    }
}

pub trait TryIntoSearch {
    fn try_into_search(self) -> Result<Node, AnkiError>;
}

impl TryIntoSearch for &str {
    fn try_into_search(self) -> Result<Node, AnkiError> {
        parser::parse(self).map(Node::Group)
    }
}

impl TryIntoSearch for &String {
    fn try_into_search(self) -> Result<Node, AnkiError> {
        parser::parse(self).map(Node::Group)
    }
}

impl<T> TryIntoSearch for T
where
    T: Into<Node>,
{
    fn try_into_search(self) -> Result<Node, AnkiError> {
        Ok(self.into())
    }
}

pub struct CardTableGuard<'a> {
    pub col: &'a mut Collection,
    pub cards: usize,
    cleanup_exact_retrievability: bool,
    cleanup_rwkv_due: bool,
}

impl Drop for CardTableGuard<'_> {
    fn drop(&mut self) {
        if let Err(err) = self.col.storage.clear_searched_cards_table() {
            println!("{err:?}");
        }
        if self.cleanup_exact_retrievability {
            if let Err(err) = self.col.clear_exact_retrievability_table() {
                println!("{err:?}");
            }
        }
        if self.cleanup_rwkv_due {
            if let Err(err) = self.col.clear_rwkv_due_table() {
                println!("{err:?}");
            }
        }
    }
}

pub struct NoteTableGuard<'a> {
    pub col: &'a mut Collection,
    pub notes: usize,
    cleanup_rwkv_due: bool,
}

impl Drop for NoteTableGuard<'_> {
    fn drop(&mut self) {
        if let Err(err) = self.col.storage.clear_searched_notes_table() {
            println!("{err:?}");
        }
        if self.cleanup_rwkv_due {
            if let Err(err) = self.col.clear_rwkv_due_table() {
                println!("{err:?}");
            }
        }
    }
}

impl Collection {
    fn with_search_auxiliary_tables<R>(
        &mut self,
        exact_retrievability: bool,
        rwkv_due: bool,
        stats_search: Option<&str>,
        op: impl FnOnce(&mut Self) -> Result<R>,
    ) -> Result<R> {
        if exact_retrievability {
            self.setup_exact_retrievability_table(stats_search)?;
        }
        if rwkv_due {
            if let Err(err) = self.setup_rwkv_due_table(stats_search) {
                if exact_retrievability {
                    let _ = self.clear_exact_retrievability_table();
                }
                return Err(err);
            }
        }

        let result = op(self);
        let rwkv_due_cleanup = rwkv_due.then(|| self.clear_rwkv_due_table());
        let exact_cleanup = exact_retrievability.then(|| self.clear_exact_retrievability_table());
        match result {
            Err(err) => Err(err),
            Ok(value) => {
                if let Some(cleanup) = rwkv_due_cleanup {
                    cleanup?;
                }
                if let Some(cleanup) = exact_cleanup {
                    cleanup?;
                }
                Ok(value)
            }
        }
    }

    fn setup_exact_retrievability_table(&mut self, stats_search: Option<&str>) -> Result<()> {
        let start = Instant::now();
        self.storage.db.execute_batch(&format!(
            "drop table if exists {EXACT_RETRIEVABILITY_TABLE};\
             create temporary table {EXACT_RETRIEVABILITY_TABLE}(\
                cid integer primary key, r real, fsrs_r real, rwkv_r real, s90 real)"
        ))?;
        let ids_start = Instant::now();
        let ids: Vec<i64> = {
            let mut stmt = self.storage.db.prepare("select id from cards")?;
            let rows = stmt.query_map([], |row| row.get(0))?;
            rows.collect::<std::result::Result<_, _>>()?
        };
        let ids_elapsed_ms = ids_start.elapsed().as_secs_f64() * 1000.0;
        let timing = self.timing_today()?;
        let rwkv_stats_scores =
            self.rwkv_stats_graph_scores_for_search(timing.days_elapsed, stats_search);
        let rwkv_card_info_scores = self.rwkv_card_info_scores_for_day(timing.days_elapsed);
        let rwkv_review_queue_scores = self.rwkv_review_queue_scores_for_day(timing.days_elapsed);
        let rwkv_active_scores = stats_search
            .is_none()
            .then(|| self.rwkv_retrievability_scores_for_day(timing.days_elapsed, None))
            .flatten();
        let rwkv_retrievability_scores = if stats_search.is_some() {
            rwkv_stats_scores.as_ref()
        } else {
            rwkv_active_scores.as_ref()
        };
        let rwkv_stats_scores_count = rwkv_stats_scores
            .as_ref()
            .map(|scores| scores.len())
            .unwrap_or(0);
        let rwkv_card_info_scores_count = rwkv_card_info_scores
            .as_ref()
            .map(|scores| scores.len())
            .unwrap_or(0);
        let rwkv_review_queue_scores_count = rwkv_review_queue_scores
            .as_ref()
            .map(|(_, scores)| scores.len())
            .unwrap_or(0);
        let load_start = Instant::now();
        let cards = ids
            .into_iter()
            .map(|cid| {
                let card_id = CardId(cid);
                self.storage.get_card(card_id)?.or_not_found(card_id)
            })
            .collect::<Result<Vec<_>>>()?;
        let load_elapsed_ms = load_start.elapsed().as_secs_f64() * 1000.0;
        let card_count = cards.len();
        let preset_start = Instant::now();
        let presets_by_card = self.fsrs_presets_for_cards(&cards)?;
        let preset_elapsed_ms = preset_start.elapsed().as_secs_f64() * 1000.0;
        let metric_start = Instant::now();
        let mut rows_to_insert = Vec::new();
        let mut rwkv_rows = 0;
        for card in cards {
            let rwkv_r = rwkv_retrievability_scores
                .and_then(|scores| scores.get(&card.id))
                .copied();
            let preset = presets_by_card
                .get(&card.id)
                .or_invalid("missing FSRS preset for card")?;
            if let Some((fsrs_r, s90)) =
                self.exact_fsrs_metrics_for_card_with_params(&card, timing, &preset.params)?
            {
                if rwkv_r.is_some() {
                    rwkv_rows += 1;
                }
                rows_to_insert.push((
                    card.id.0,
                    rwkv_r.unwrap_or(fsrs_r),
                    Some(fsrs_r),
                    rwkv_r,
                    Some(s90),
                ));
            } else if let Some(rwkv_r) = rwkv_r {
                rwkv_rows += 1;
                rows_to_insert.push((card.id.0, rwkv_r, None, Some(rwkv_r), None));
            }
        }
        let metric_elapsed_ms = metric_start.elapsed().as_secs_f64() * 1000.0;
        let insert_start = Instant::now();
        let mut insert = self.storage.db.prepare_cached(&format!(
            "insert into {EXACT_RETRIEVABILITY_TABLE}(cid, r, fsrs_r, rwkv_r, s90) \
             values (?, ?, ?, ?, ?)"
        ))?;
        for (cid, r, fsrs_r, rwkv_r, s90) in rows_to_insert {
            insert.execute(rusqlite::params![cid, r, fsrs_r, rwkv_r, s90])?;
        }
        tracing::debug!(
            cards = card_count,
            ids_elapsed_ms,
            load_elapsed_ms,
            preset_elapsed_ms,
            metric_elapsed_ms,
            rwkv_stats_scores = rwkv_stats_scores_count,
            rwkv_card_info_scores = rwkv_card_info_scores_count,
            rwkv_review_queue_scores = rwkv_review_queue_scores_count,
            rwkv_rows,
            insert_elapsed_ms = insert_start.elapsed().as_secs_f64() * 1000.0,
            elapsed_ms = start.elapsed().as_secs_f64() * 1000.0,
            "built exact retrievability search table"
        );
        Ok(())
    }

    fn clear_exact_retrievability_table(&self) -> Result<()> {
        self.storage.db.execute(
            &format!("drop table if exists {EXACT_RETRIEVABILITY_TABLE}"),
            [],
        )?;
        Ok(())
    }

    fn setup_rwkv_due_table(&mut self, stats_search: Option<&str>) -> Result<()> {
        let start = Instant::now();
        self.storage.db.execute_batch(&format!(
            "drop table if exists {RWKV_DUE_TABLE};\
             create temporary table {RWKV_DUE_TABLE}(\
                cid integer not null, kind integer not null, primary key(cid, kind)) without rowid"
        ))?;

        let timing = self.timing_today()?;
        let Some(scores) =
            self.rwkv_stats_graph_score_entries_for_search(timing.days_elapsed, stats_search)
        else {
            return Ok(());
        };
        let card_ids: Vec<_> = scores.keys().copied().collect();
        let metadata = rwkv_review_candidate_metadata(self, &card_ids, timing)?;
        let decks = self.storage.get_decks_map()?;
        let configs = self.storage.get_deck_config_map()?;
        let mut rows = Vec::new();

        for (card_id, score) in scores {
            let Some(metadata) = metadata.get(&card_id) else {
                continue;
            };
            let Some(config) = decks
                .get(&metadata.source_deck_id)
                .and_then(|deck| deck.config_id())
                .and_then(|config_id| configs.get(&config_id))
            else {
                continue;
            };

            if config.inner.rwkv_review_instant_order_enabled
                && matches!(
                    rwkv_review_score_eligibility(
                        score.retrievability,
                        metadata,
                        config.inner.rwkv_review_allow_same_day_review,
                        config.inner.rwkv_review_min_intervening_reviews,
                        config.inner.rwkv_review_min_elapsed_secs,
                        score.intervening_reviews,
                        score.target_retention,
                    ),
                    RwkvReviewScoreEligibility::Eligible
                )
            {
                rows.push((card_id, 0));
            }
            if config.inner.rwkv_review_enabled && score.curve_due {
                rows.push((card_id, 1));
            }
        }

        let mut insert = self.storage.db.prepare_cached(&format!(
            "insert into {RWKV_DUE_TABLE}(cid, kind) values (?, ?)"
        ))?;
        for (card_id, kind) in &rows {
            insert.execute(rusqlite::params![card_id.0, kind])?;
        }
        tracing::debug!(
            scores = card_ids.len(),
            due_rows = rows.len(),
            elapsed_ms = start.elapsed().as_secs_f64() * 1000.0,
            "built RWKV due search table"
        );
        Ok(())
    }

    fn clear_rwkv_due_table(&self) -> Result<()> {
        self.storage
            .db
            .execute(&format!("drop table if exists {RWKV_DUE_TABLE}"), [])?;
        Ok(())
    }

    pub fn search_cards<N>(&mut self, search: N, mode: SortMode) -> Result<Vec<CardId>>
    where
        N: TryIntoSearch,
    {
        if let Some((metric, reverse)) = exact_fsrs_sort_mode(ReturnItemType::Cards, &mode) {
            let top_node = search.try_into_search()?;
            let mut ids = self.search_card_ids_for_node(&top_node, mode.required_table(), None)?;
            self.sort_card_ids_by_exact_fsrs_metric(&mut ids, metric, reverse)?;
            return Ok(ids);
        }
        self.search(search, mode)
    }

    pub fn search_notes<N>(&mut self, search: N, mode: SortMode) -> Result<Vec<NoteId>>
    where
        N: TryIntoSearch,
    {
        self.search(search, mode)
    }

    pub fn search_notes_unordered<N>(&mut self, search: N) -> Result<Vec<NoteId>>
    where
        N: TryIntoSearch,
    {
        self.search(search, SortMode::NoOrder)
    }
}

impl Collection {
    fn search_card_ids_for_node(
        &mut self,
        top_node: &Node,
        required_table: RequiredTable,
        stats_search: Option<&str>,
    ) -> Result<Vec<CardId>> {
        let use_exact_fsrs_metrics = has_exact_fsrs_metrics_property(top_node);
        let use_rwkv_due = has_rwkv_due_state(top_node);
        self.with_search_auxiliary_tables(
            use_exact_fsrs_metrics,
            use_rwkv_due,
            stats_search,
            |col| {
                let writer = SqlWriter::new(col, ReturnItemType::Cards);
                let (sql, args) = writer.build_query(top_node, required_table)?;
                let mut stmt = col.storage.db.prepare(&sql)?;
                let ids: Vec<_> = stmt
                    .query_map(params_from_iter(args.iter()), |row| row.get(0))?
                    .collect::<std::result::Result<_, _>>()?;
                Ok(ids)
            },
        )
    }

    fn elapsed_seconds_since_last_review_for_card(
        &self,
        card: &Card,
        timing: SchedTimingToday,
    ) -> u32 {
        if let Some(last_review_time) = card.last_review_time {
            timing.now.elapsed_secs_since_clamped(last_review_time)
        } else {
            let due = card.original_or_current_due() as i64;
            if due > 365_000 {
                let last_review_time = TimestampSecs(due.saturating_sub(card.interval as i64));
                timing.now.elapsed_secs_since_clamped(last_review_time)
            } else {
                let review_day = due.saturating_sub(card.interval as i64);
                timing.days_elapsed.saturating_sub(review_day as u32) * 86_400
            }
        }
    }

    fn exact_fsrs_metrics_for_card_with_params(
        &self,
        card: &Card,
        timing: SchedTimingToday,
        params: &[f32],
    ) -> Result<Option<(f32, f32)>> {
        let Some(state) = card.memory_state else {
            return Ok(None);
        };
        let elapsed_days =
            self.elapsed_seconds_since_last_review_for_card(card, timing) as f32 / 86_400.0;
        let r =
            fsrs_current_retrievability_for_params(params, state.stability_internal, elapsed_days)?;
        Ok(Some((r, state.stability)))
    }

    fn exact_fsrs_metric_for_card_with_params(
        &self,
        card: &Card,
        timing: SchedTimingToday,
        params: &[f32],
        metric: ExactFsrsSortMetric,
    ) -> Result<Option<f32>> {
        Ok(self
            .exact_fsrs_metrics_for_card_with_params(card, timing, params)?
            .map(|(r, s90)| match metric {
                ExactFsrsSortMetric::Retrievability => r,
                ExactFsrsSortMetric::StabilityS90 => s90,
            }))
    }

    fn sort_card_ids_by_exact_fsrs_metric(
        &mut self,
        ids: &mut [CardId],
        metric: ExactFsrsSortMetric,
        reverse: bool,
    ) -> Result<()> {
        let start = Instant::now();
        let timing = self.timing_today()?;
        let load_start = Instant::now();
        let cards = self.all_cards_for_ids(ids, false)?;
        let load_elapsed_ms = load_start.elapsed().as_secs_f64() * 1000.0;
        let card_count = cards.len();
        let preset_start = Instant::now();
        let presets_by_card = self.fsrs_presets_for_cards(&cards)?;
        let preset_elapsed_ms = preset_start.elapsed().as_secs_f64() * 1000.0;
        let metric_start = Instant::now();
        let mut with_metric = Vec::with_capacity(ids.len());
        for card in cards {
            let preset = presets_by_card
                .get(&card.id)
                .or_invalid("missing FSRS preset for card")?;
            with_metric.push((
                card.id,
                self.exact_fsrs_metric_for_card_with_params(&card, timing, &preset.params, metric)?,
            ));
        }
        let metric_elapsed_ms = metric_start.elapsed().as_secs_f64() * 1000.0;
        let sort_start = Instant::now();
        with_metric.sort_by(|(cid_a, metric_a), (cid_b, metric_b)| {
            let ord = match (metric_a, metric_b) {
                (Some(a), Some(b)) => a.total_cmp(b),
                (None, Some(_)) => Ordering::Less,
                (Some(_), None) => Ordering::Greater,
                (None, None) => Ordering::Equal,
            };
            let ord = if reverse { ord.reverse() } else { ord };
            ord.then_with(|| cid_a.cmp(cid_b))
        });
        for (target, (cid, _)) in ids.iter_mut().zip(with_metric.into_iter()) {
            *target = cid;
        }
        tracing::debug!(
            ?metric,
            reverse,
            cards = card_count,
            load_elapsed_ms,
            preset_elapsed_ms,
            metric_elapsed_ms,
            sort_elapsed_ms = sort_start.elapsed().as_secs_f64() * 1000.0,
            elapsed_ms = start.elapsed().as_secs_f64() * 1000.0,
            "sorted cards by exact FSRS metric"
        );
        Ok(())
    }

    fn search<T, N>(&mut self, search: N, mode: SortMode) -> Result<Vec<T>>
    where
        N: TryIntoSearch,
        T: FromSql + AsReturnItemType,
    {
        let item_type = T::as_return_item_type();
        let top_node = search.try_into_search()?;
        let use_exact_fsrs_metrics = has_exact_fsrs_metrics_property(&top_node);
        let use_rwkv_due = has_rwkv_due_state(&top_node);
        self.with_search_auxiliary_tables(use_exact_fsrs_metrics, use_rwkv_due, None, |col| {
            let writer = SqlWriter::new(col, item_type);
            let (mut sql, args) = writer.build_query(&top_node, mode.required_table())?;
            col.add_order(&mut sql, item_type, mode)?;

            let mut stmt = col.storage.db.prepare(&sql)?;
            let ids: Vec<_> = stmt
                .query_map(params_from_iter(args.iter()), |row| row.get(0))?
                .collect::<std::result::Result<_, _>>()?;

            Ok(ids)
        })
    }

    fn add_order(
        &mut self,
        sql: &mut String,
        item_type: ReturnItemType,
        mode: SortMode,
    ) -> Result<()> {
        match mode {
            SortMode::NoOrder => (),
            SortMode::Builtin { column, reverse } => {
                prepare_sort(self, column, item_type)?;
                sql.push_str(" order by ");
                write_order(sql, item_type, column, reverse, self.timing_today()?)?;
            }
            SortMode::Custom(order_clause) => {
                sql.push_str(" order by ");
                sql.push_str(&order_clause);
            }
        }
        Ok(())
    }

    /// Place the matched card ids into a temporary 'search_cids' table
    /// instead of returning them. Returns a guard with a collection reference
    /// and the number of added cards. When the guard is dropped, the temporary
    /// table is cleaned up.
    pub(crate) fn search_cards_into_table(
        &mut self,
        search: impl TryIntoSearch,
        mode: SortMode,
    ) -> Result<CardTableGuard<'_>> {
        self.search_cards_into_table_with_stats_search(search, mode, None)
    }

    pub(crate) fn search_cards_into_table_with_stats_search(
        &mut self,
        search: impl TryIntoSearch,
        mode: SortMode,
        stats_search: Option<&str>,
    ) -> Result<CardTableGuard<'_>> {
        if let Some((metric, reverse)) = exact_fsrs_sort_mode(ReturnItemType::Cards, &mode) {
            let top_node = search.try_into_search()?;
            let mut ids =
                self.search_card_ids_for_node(&top_node, mode.required_table(), stats_search)?;
            self.sort_card_ids_by_exact_fsrs_metric(&mut ids, metric, reverse)?;
            self.storage
                .setup_searched_cards_table_to_preserve_order()?;
            self.storage.set_search_table_to_card_ids(&ids)?;
            return Ok(CardTableGuard {
                cards: ids.len(),
                col: self,
                cleanup_exact_retrievability: false,
                cleanup_rwkv_due: false,
            });
        }
        let top_node = search.try_into_search()?;
        let want_order = mode != SortMode::NoOrder;
        let use_exact_fsrs_metrics = has_exact_fsrs_metrics_property(&top_node);
        let use_rwkv_due = has_rwkv_due_state(&top_node);
        if use_exact_fsrs_metrics {
            self.setup_exact_retrievability_table(stats_search)?;
        }
        if use_rwkv_due {
            if let Err(err) = self.setup_rwkv_due_table(stats_search) {
                if use_exact_fsrs_metrics {
                    let _ = self.clear_exact_retrievability_table();
                }
                return Err(err);
            }
        }

        let result = (|| {
            let writer = SqlWriter::new(self, ReturnItemType::Cards);
            let (mut sql, args) = writer.build_query(&top_node, mode.required_table())?;
            self.add_order(&mut sql, ReturnItemType::Cards, mode)?;

            if want_order {
                self.storage
                    .setup_searched_cards_table_to_preserve_order()?;
            } else {
                self.storage.setup_searched_cards_table()?;
            }
            let sql = format!("insert into search_cids {sql}");

            let cards = self
                .storage
                .db
                .prepare(&sql)?
                .execute(params_from_iter(args))?;

            Ok(cards)
        })();

        match result {
            Ok(cards) => Ok(CardTableGuard {
                cards,
                col: self,
                cleanup_exact_retrievability: use_exact_fsrs_metrics,
                cleanup_rwkv_due: use_rwkv_due,
            }),
            Err(err) => {
                if use_exact_fsrs_metrics {
                    let _ = self.clear_exact_retrievability_table();
                }
                if use_rwkv_due {
                    let _ = self.clear_rwkv_due_table();
                }
                Err(err)
            }
        }
    }

    pub(crate) fn search_cards_in_fsrs_preset_search_table(
        &mut self,
        search: impl TryIntoSearch,
        use_first_grade_table: bool,
    ) -> Result<Vec<CardId>> {
        let top_node = search.try_into_search()?;
        let use_rwkv_due = has_rwkv_due_state(&top_node);
        self.with_search_auxiliary_tables(false, use_rwkv_due, None, |col| {
            let mut writer = SqlWriter::new(col, ReturnItemType::Cards)
                .with_card_id_filter_table("fsrs_preset_search_cids");
            if use_first_grade_table {
                writer = writer.with_first_grade_table("fsrs_preset_first_grades");
            }
            let (sql, args) = writer.build_query(&top_node, RequiredTable::Cards)?;
            let mut stmt = col.storage.db.prepare(&sql)?;
            let ids = stmt
                .query_map(params_from_iter(args.iter()), |row| row.get(0))?
                .collect::<std::result::Result<_, _>>()
                .map_err(AnkiError::from)?;
            Ok(ids)
        })
    }

    pub(crate) fn all_cards_for_search(&mut self, search: impl TryIntoSearch) -> Result<Vec<Card>> {
        let guard = self.search_cards_into_table(search, SortMode::NoOrder)?;
        guard.col.storage.all_searched_cards()
    }

    pub(crate) fn all_cards_for_search_in_order(
        &mut self,
        search: impl TryIntoSearch,
        mode: SortMode,
    ) -> Result<Vec<Card>> {
        let guard = self.search_cards_into_table(search, mode)?;
        guard.col.storage.all_searched_cards_in_search_order()
    }

    pub(crate) fn all_cards_for_ids(
        &self,
        cards: &[CardId],
        preserve_order: bool,
    ) -> Result<Vec<Card>> {
        self.storage.with_searched_cards_table(preserve_order, || {
            self.storage.set_search_table_to_card_ids(cards)?;
            if preserve_order {
                self.storage.all_searched_cards_in_search_order()
            } else {
                self.storage.all_searched_cards()
            }
        })
    }

    pub(crate) fn for_each_card_in_search(
        &mut self,
        search: impl TryIntoSearch,
        mut func: impl FnMut(&Collection, Card) -> Result<()>,
    ) -> Result<()> {
        let guard = self.search_cards_into_table(search, SortMode::NoOrder)?;
        guard
            .col
            .storage
            .for_each_card_in_search(|card| func(guard.col, card))
    }

    /// Place the matched card ids into a temporary 'search_nids' table
    /// instead of returning them. Returns a guard with a collection reference
    /// and the number of added notes. When the guard is dropped, the temporary
    /// table is cleaned up.
    pub(crate) fn search_notes_into_table(
        &mut self,
        search: impl TryIntoSearch,
    ) -> Result<NoteTableGuard<'_>> {
        let top_node = search.try_into_search()?;
        let use_rwkv_due = has_rwkv_due_state(&top_node);
        if use_rwkv_due {
            self.setup_rwkv_due_table(None)?;
        }
        let writer = SqlWriter::new(self, ReturnItemType::Notes);
        let mode = SortMode::NoOrder;

        let (sql, args) = match writer.build_query(&top_node, mode.required_table()) {
            Ok(query) => query,
            Err(err) => {
                if use_rwkv_due {
                    let _ = self.clear_rwkv_due_table();
                }
                return Err(err);
            }
        };

        if let Err(err) = self.storage.setup_searched_notes_table() {
            if use_rwkv_due {
                let _ = self.clear_rwkv_due_table();
            }
            return Err(err);
        }
        let sql = format!("insert into search_nids {sql}");

        let notes_result: Result<usize> = (|| {
            let mut stmt = self.storage.db.prepare(&sql)?;
            stmt.execute(params_from_iter(args)).map_err(Into::into)
        })();
        let notes = match notes_result {
            Ok(notes) => notes,
            Err(err) => {
                let _ = self.storage.clear_searched_notes_table();
                if use_rwkv_due {
                    let _ = self.clear_rwkv_due_table();
                }
                return Err(err);
            }
        };

        Ok(NoteTableGuard {
            notes,
            col: self,
            cleanup_rwkv_due: use_rwkv_due,
        })
    }

    /// Place the ids of cards with notes in 'search_nids' into 'search_cids'.
    /// Returns number of added cards.
    pub(crate) fn search_cards_of_notes_into_table(&mut self) -> Result<CardTableGuard<'_>> {
        self.storage.setup_searched_cards_table()?;
        let cards = self.storage.search_cards_of_notes_into_table()?;
        Ok(CardTableGuard {
            cards,
            col: self,
            cleanup_exact_retrievability: false,
            cleanup_rwkv_due: false,
        })
    }
}

fn exact_fsrs_sort_mode(
    item_type: ReturnItemType,
    mode: &SortMode,
) -> Option<(ExactFsrsSortMetric, bool)> {
    match (item_type, mode) {
        (
            ReturnItemType::Cards,
            SortMode::Builtin {
                column: Column::Retrievability,
                reverse,
            },
        ) => Some((ExactFsrsSortMetric::Retrievability, *reverse)),
        (
            ReturnItemType::Cards,
            SortMode::Builtin {
                column: Column::Stability,
                reverse,
            },
        ) => Some((ExactFsrsSortMetric::StabilityS90, *reverse)),
        _ => None,
    }
}

fn has_exact_fsrs_metrics_property(node: &Node) -> bool {
    match node {
        Node::Not(inner) => has_exact_fsrs_metrics_property(inner),
        Node::Group(nodes) => nodes.iter().any(has_exact_fsrs_metrics_property),
        Node::Search(SearchNode::Property {
            kind:
                PropertyKind::Retrievability(_)
                | PropertyKind::FsrsRetrievability(_)
                | PropertyKind::RwkvRetrievability(_)
                | PropertyKind::Stability(_),
            ..
        }) => true,
        _ => false,
    }
}

fn has_rwkv_due_state(node: &Node) -> bool {
    match node {
        Node::Not(inner) => has_rwkv_due_state(inner),
        Node::Group(nodes) => nodes.iter().any(has_rwkv_due_state),
        Node::Search(SearchNode::State(StateKind::RwkvDue | StateKind::RwkvCurveDue)) => true,
        _ => false,
    }
}

/// Add the order clause to the sql.
fn write_order(
    sql: &mut String,
    item_type: ReturnItemType,
    column: Column,
    reverse: bool,
    timing: SchedTimingToday,
) -> Result<()> {
    let order = match item_type {
        ReturnItemType::Cards => card_order_from_sort_column(column, timing),
        ReturnItemType::Notes => note_order_from_sort_column(column),
    };
    require!(!order.is_empty(), "Can't sort {item_type:?} by {column:?}.");
    if reverse {
        sql.push_str(
            &order
                .to_ascii_lowercase()
                .replace(" desc", "")
                .replace(" asc", " desc"),
        )
    } else {
        sql.push_str(&order);
    }
    Ok(())
}

fn card_order_from_sort_column(column: Column, timing: SchedTimingToday) -> Cow<'static, str> {
    match column {
        Column::CardMod => "c.mod asc".into(),
        Column::Cards => concat!(
            "coalesce((select pos from sort_order where ntid = n.mid and ord = c.ord),",
            // need to fall back on ord 0 for cloze cards
            "(select pos from sort_order where ntid = n.mid and ord = 0)) asc, ord asc"
        )
        .into(),
        Column::Deck => "(select pos from sort_order where did = c.did) asc".into(),
        Column::Due => format!("(case when c.due > 1000000000 or c.type = {} then due else (due - {}) * 86400 + {} end) asc", CardType::New as i8, timing.days_elapsed, TimestampSecs::now().0).into(),
        Column::Ease => format!("c.type = {} asc, c.factor asc", CardType::New as i8).into(),
        Column::Interval => "c.ivl asc".into(),
        Column::Lapses => "c.lapses asc".into(),
        Column::NoteCreation => "n.id asc, c.ord asc".into(),
        Column::NoteMod => "n.mod asc, c.ord asc".into(),
        Column::Notetype => "(select pos from sort_order where ntid = n.mid) asc".into(),
        Column::OriginalPosition => "(select pos from sort_order where nid = c.nid) asc".into(),
        Column::Reps => "c.reps asc".into(),
        Column::SortField => "n.sfld collate nocase asc, c.ord asc".into(),
        Column::Tags => "n.tags asc".into(),
        Column::Answer | Column::Custom | Column::Question => "".into(),
        Column::Stability => "extract_fsrs_variable(c.data, 's') asc".into(),
        Column::Difficulty => "extract_fsrs_variable(c.data, 'd') asc".into(),
        Column::Retrievability => format!(
            "extract_fsrs_retrievability(c.data, case when c.odue !=0 then c.odue else c.due end, c.ivl, {}, {}, {}) asc",
            timing.days_elapsed,
            timing.next_day_at.0,
            timing.now.0,
        )
        .into(),
    }
}

fn note_order_from_sort_column(column: Column) -> Cow<'static, str> {
    match column {
        Column::CardMod
        | Column::Cards
        | Column::Deck
        | Column::Due
        | Column::Ease
        | Column::Interval
        | Column::Lapses
        | Column::OriginalPosition
        | Column::Reps => "(select pos from sort_order where nid = n.id) asc".into(),
        Column::NoteCreation => "n.id asc".into(),
        Column::NoteMod => "n.mod asc".into(),
        Column::Notetype => "(select pos from sort_order where ntid = n.mid) asc".into(),
        Column::SortField => "n.sfld collate nocase asc".into(),
        Column::Tags => "n.tags asc".into(),
        Column::Answer
        | Column::Custom
        | Column::Question
        | Column::Stability
        | Column::Difficulty
        | Column::Retrievability => "".into(),
    }
}

fn prepare_sort(col: &mut Collection, column: Column, item_type: ReturnItemType) -> Result<()> {
    let temp_string;
    let sql = match item_type {
        ReturnItemType::Cards => match column {
            Column::Cards => include_str!("template_order.sql"),
            Column::Deck => include_str!("deck_order.sql"),
            Column::Notetype => include_str!("notetype_order.sql"),
            Column::OriginalPosition => include_str!("note_original_position_order.sql"),
            _ => return Ok(()),
        },
        ReturnItemType::Notes => match column {
            Column::Cards => include_str!("note_cards_order.sql"),
            Column::CardMod => include_str!("card_mod_order.sql"),
            Column::Deck => include_str!("note_decks_order.sql"),
            Column::Due => {
                temp_string = format!("{} ORDER BY MIN({});", include_str!("note_due_order.sql"), format_args!("CASE WHEN due > 1000000000 OR type = {ctype} THEN due ELSE (due - {today}) * 86400 + {current_timestamp} END", ctype = CardType::New as i8, today = col.timing_today()?.days_elapsed, current_timestamp = TimestampSecs::now().0));
                &temp_string
            }
            Column::Ease => include_str!("note_ease_order.sql"),
            Column::Interval => include_str!("note_interval_order.sql"),
            Column::Lapses => include_str!("note_lapses_order.sql"),
            Column::OriginalPosition => include_str!("note_original_position_order.sql"),
            Column::Reps => include_str!("note_reps_order.sql"),
            Column::Notetype => include_str!("notetype_order.sql"),
            _ => return Ok(()),
        },
    };

    col.storage.db.execute_batch(sql)?;

    Ok(())
}

#[cfg(test)]
mod test {
    use std::collections::HashMap;

    use anki_proto::deck_config::deck_configs_for_update::current_deck::Limits;
    use anki_proto::deck_config::UpdateDeckConfigsMode;
    use anki_proto::search::browser_columns::Sorting;
    use strum::IntoEnumIterator;

    use super::*;
    use crate::card::CardQueue;
    use crate::card::CardType;
    use crate::card::FsrsMemoryState;
    use crate::collection::RwkvStatsGraphScoreEntry;
    use crate::config::BoolKey;
    use crate::deckconfig::FsrsVersion;
    use crate::deckconfig::UpdateDeckConfigsRequest;
    use crate::revlog::RevlogEntry;
    use crate::revlog::RevlogReviewKind;
    use crate::scheduler::fsrs::preset::AddonFsrsPreset;
    use crate::scheduler::fsrs::preset::AddonFsrsVersion;
    use crate::scheduler::fsrs::preset::FsrsPresetOverlay;
    use crate::scheduler::fsrs::preset::FsrsPresetRule;
    use crate::scheduler::fsrs::preset::FSRS_PRESET_OVERLAY_CONFIG_KEY;

    impl SchedTimingToday {
        pub(crate) fn zero() -> Self {
            SchedTimingToday {
                now: TimestampSecs(0),
                days_elapsed: 0,
                next_day_at: TimestampSecs(0),
            }
        }
    }

    #[test]
    fn column_default_sort_order_should_match_order_by_clause() {
        let timing = SchedTimingToday::zero();
        for column in Column::iter() {
            assert_eq!(
                card_order_from_sort_column(column, timing).is_empty(),
                matches!(column.default_cards_order(), Sorting::None)
            );
            assert_eq!(
                note_order_from_sort_column(column).is_empty(),
                matches!(column.default_notes_order(), Sorting::None)
            );
        }
    }

    #[test]
    fn exact_retrievability_clamps_future_last_review_time() -> Result<()> {
        let mut col = Collection::new();
        let timing = col.timing_today()?;
        let mut card = Card::new(NoteId(1), 0, DeckId(1), 0);
        card.last_review_time = Some(timing.now.adding_secs(60));

        assert_eq!(
            col.elapsed_seconds_since_last_review_for_card(&card, timing),
            0
        );

        card.last_review_time = None;
        card.due = timing.now.adding_secs(60).0 as i32;
        card.interval = 0;
        assert_eq!(
            col.elapsed_seconds_since_last_review_for_card(&card, timing),
            0
        );

        card.due = timing.now.adding_secs(-60).0 as i32;
        card.interval = 30;
        assert_eq!(
            col.elapsed_seconds_since_last_review_for_card(&card, timing),
            90
        );
        Ok(())
    }

    #[test]
    fn numeric_field_search_uses_named_field_not_sort_field() -> Result<()> {
        let mut col = Collection::new();
        let mut nt = col.get_notetype_by_name("Basic")?.unwrap().as_ref().clone();
        nt.add_field("Frequency");
        col.update_notetype(&mut nt, false)?;

        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        assert_ne!(nt.config.sort_field_idx, 2);

        let mut add_note = |front: &str, frequency: &str| -> Result<NoteId> {
            let mut note = nt.new_note();
            note.set_field(0, front)?;
            note.set_field(2, frequency)?;
            col.add_note(&mut note, DeckId(1))?;
            Ok(note.id)
        };

        let lower_bound = add_note("lower bound", "500")?;
        let in_range = add_note("in range", "550")?;
        let upper_bound = add_note("upper bound", "600")?;
        let too_high = add_note("too high", "1500")?;
        let not_numeric = add_note("not numeric", "abc")?;

        let mut ids = col.search_notes("Frequency>500 Frequency<600", SortMode::NoOrder)?;
        ids.sort();
        assert_eq!(ids, vec![in_range]);

        let ids = col.search_notes("Frequency<600", SortMode::NoOrder)?;
        assert!(ids.contains(&lower_bound));
        assert!(!ids.contains(&upper_bound));
        assert!(!ids.contains(&too_high));
        assert!(!ids.contains(&not_numeric));

        let mut ids = col.search_notes("Frequency:[500,600]", SortMode::NoOrder)?;
        ids.sort();
        assert_eq!(ids, vec![lower_bound, in_range, upper_bound]);

        let mut ids = col.search_notes("Frequency:[500,600[", SortMode::NoOrder)?;
        ids.sort();
        assert_eq!(ids, vec![lower_bound, in_range]);

        let mut ids = col.search_notes("Frequency:]500,600]", SortMode::NoOrder)?;
        ids.sort();
        assert_eq!(ids, vec![in_range, upper_bound]);

        let ids = col.search_notes("Frequency:]500,600[", SortMode::NoOrder)?;
        assert_eq!(ids, vec![in_range]);

        Ok(())
    }

    fn set_selected_fsrs7_params_for_deck(
        col: &mut Collection,
        deck_id: DeckId,
        params: Vec<f32>,
    ) -> Result<()> {
        let output = col.get_deck_configs_for_update(deck_id)?;
        let mut input = UpdateDeckConfigsRequest {
            target_deck_id: deck_id,
            configs: output
                .all_config
                .into_iter()
                .map(|c| c.config.unwrap().into())
                .collect(),
            removed_config_ids: vec![],
            mode: UpdateDeckConfigsMode::Normal,
            card_state_customizer: String::new(),
            limits: Limits::default(),
            new_cards_ignore_review_limit: false,
            apply_all_parent_limits: false,
            fsrs: true,
            load_balancer_enabled: false,
            fsrs_short_term_with_steps_enabled: false,
            fsrs_learning_queues_disabled: false,
            fsrs_reschedule: false,
            fsrs_health_check: true,
            review_fuzz_config: Default::default(),
        };
        input.configs[0].inner.fsrs_version = FsrsVersion::Seven as i32;
        input.configs[0].inner.fsrs_params_7 = params;
        col.update_deck_configs(input)?;
        Ok(())
    }

    fn set_selected_fsrs7_params(col: &mut Collection, params: Vec<f32>) -> Result<()> {
        set_selected_fsrs7_params_for_deck(col, DeckId(1), params)
    }

    fn fsrs7_sort_params_a() -> Vec<f32> {
        vec![
            0.4843, 3.0562, 10.9946, 32.7202, 5.6296, 0.5900, 3.1230, 2.4679, 0.2733, 1.4895,
            0.4868, 0.0010, 0.8082, 0.1723, 0.6389, 1.5767, 0.8918, 0.3341, 3.5942, 0.3455, 0.0022,
            0.2834, 2.6418, 0.5604, 1.3042, 2.5054, 0.9376, 0.0611, 0.0830, 0.6339, 0.9846, 0.2485,
            0.6014, 0.0545,
        ]
    }

    fn fsrs7_sort_params_b() -> Vec<f32> {
        vec![
            0.4843, 3.0562, 10.9946, 32.7202, 5.6296, 0.5900, 3.1230, 2.4679, 0.2733, 1.4895,
            0.4868, 0.0010, 0.8082, 0.1723, 0.6389, 1.5767, 0.8918, 0.3341, 3.5942, 0.3455, 0.0022,
            0.2834, 2.6418, 0.5604, 1.3042, 2.5054, 0.9376, 0.3000, 0.3000, 0.6000, 0.9500, 0.3500,
            0.9000, 0.1500,
        ]
    }

    #[test]
    fn browser_retrievability_sort_uses_exact_model_r() -> Result<()> {
        let mut col = Collection::new();
        set_selected_fsrs7_params(&mut col, fsrs7_sort_params_a())?;
        col.set_config_bool(BoolKey::Fsrs, true, true)?;

        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note1 = nt.new_note();
        let mut note2 = nt.new_note();
        col.add_note(&mut note1, DeckId(1))?;
        col.add_note(&mut note2, DeckId(1))?;
        let mut ids = col.search_cards("", SortMode::NoOrder)?;
        ids.sort();

        let timing = col.timing_today()?;
        let mut card1 = col.storage.get_card(ids[0])?.unwrap();
        let mut card2 = col.storage.get_card(ids[1])?.unwrap();
        for card in [&mut card1, &mut card2] {
            card.ctype = CardType::Review;
            card.queue = CardQueue::Review;
            card.interval = 20;
            card.due = 0;
            card.memory_state = Some(FsrsMemoryState {
                stability: 30.0,
                stability_internal: 30.0,
                stability_fast: None,
                difficulty: 5.0,
            });
            card.last_review_time = Some(timing.now.adding_secs(-20 * 86_400));
        }
        // stale per-card decay values must not affect exact sorting
        card1.decay = Some(2.0);
        card2.decay = Some(0.1);
        col.storage.update_card(&card1)?;
        col.storage.update_card(&card2)?;

        let sorted = col.search_cards(
            "",
            SortMode::Builtin {
                column: Column::Retrievability,
                reverse: false,
            },
        )?;
        assert_eq!(sorted, vec![card1.id, card2.id]);

        let sorted_cards = col.all_cards_for_search_in_order(
            "",
            SortMode::Builtin {
                column: Column::Retrievability,
                reverse: false,
            },
        )?;
        assert_eq!(
            sorted_cards.into_iter().map(|c| c.id).collect::<Vec<_>>(),
            vec![card1.id, card2.id]
        );
        Ok(())
    }

    #[test]
    fn browser_stability_sort_uses_exact_model_s90() -> Result<()> {
        let mut col = Collection::new();
        let params_a = fsrs7_sort_params_a();
        let params_b = fsrs7_sort_params_b();
        set_selected_fsrs7_params_for_deck(&mut col, DeckId(1), params_a)?;
        let second_deck = col.get_or_create_normal_deck("second")?;
        let output = col.get_deck_configs_for_update(second_deck.id)?;
        let mut input = UpdateDeckConfigsRequest {
            target_deck_id: second_deck.id,
            configs: output
                .all_config
                .into_iter()
                .map(|c| c.config.unwrap().into())
                .collect(),
            removed_config_ids: vec![],
            mode: UpdateDeckConfigsMode::Normal,
            card_state_customizer: String::new(),
            limits: Limits::default(),
            new_cards_ignore_review_limit: false,
            apply_all_parent_limits: false,
            fsrs: true,
            load_balancer_enabled: false,
            fsrs_short_term_with_steps_enabled: false,
            fsrs_learning_queues_disabled: false,
            fsrs_reschedule: false,
            fsrs_health_check: true,
            review_fuzz_config: Default::default(),
        };
        let mut new_config = input.configs[0].clone();
        new_config.id = DeckConfigId(0);
        new_config.inner.fsrs_version = FsrsVersion::Seven as i32;
        new_config.inner.fsrs_params_7 = params_b;
        input.configs.push(new_config);
        col.update_deck_configs(input)?;
        col.set_config_bool(BoolKey::Fsrs, true, true)?;

        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note1 = nt.new_note();
        let mut note2 = nt.new_note();
        col.add_note(&mut note1, DeckId(1))?;
        col.add_note(&mut note2, second_deck.id)?;
        let mut ids = col.search_cards("", SortMode::NoOrder)?;
        ids.sort();
        let card1_id = ids[0];
        let card2_id = ids[1];

        let timing = col.timing_today()?;
        let mut card1 = col.storage.get_card(card1_id)?.unwrap();
        let mut card2 = col.storage.get_card(card2_id)?.unwrap();
        let s90_1 = col.fsrs_interval_at_retrievability_for_card(card1.id, 30.0, 0.9)?;
        let s90_2 = col.fsrs_interval_at_retrievability_for_card(card2.id, 30.0, 0.9)?;
        for card in [&mut card1, &mut card2] {
            card.ctype = CardType::Review;
            card.queue = CardQueue::Review;
            card.interval = 20;
            card.due = 0;
            card.memory_state = Some(FsrsMemoryState {
                stability: 30.0,
                stability_internal: 30.0,
                stability_fast: None,
                difficulty: 5.0,
            });
            card.last_review_time = Some(timing.now.adding_secs(-20 * 86_400));
        }
        card1.memory_state.as_mut().unwrap().stability = s90_1;
        card2.memory_state.as_mut().unwrap().stability = s90_2;
        col.storage.update_card(&card1)?;
        col.storage.update_card(&card2)?;

        assert!(
            (s90_1 - s90_2).abs() > 0.001,
            "test requires different s90 values across deck presets"
        );
        let expected = if s90_1 <= s90_2 {
            vec![card1.id, card2.id]
        } else {
            vec![card2.id, card1.id]
        };

        let sorted = col.search_cards(
            "",
            SortMode::Builtin {
                column: Column::Stability,
                reverse: false,
            },
        )?;
        assert_eq!(sorted, expected);

        let sorted_cards = col.all_cards_for_search_in_order(
            "",
            SortMode::Builtin {
                column: Column::Stability,
                reverse: false,
            },
        )?;
        assert_eq!(
            sorted_cards.into_iter().map(|c| c.id).collect::<Vec<_>>(),
            expected
        );
        Ok(())
    }

    #[test]
    fn browser_stability_sort_uses_addon_preset_overlay() -> Result<()> {
        let mut col = Collection::new();
        set_selected_fsrs7_params(&mut col, fsrs7_sort_params_a())?;
        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        col.set_config(
            FSRS_PRESET_OVERLAY_CONFIG_KEY,
            &FsrsPresetOverlay {
                presets: vec![AddonFsrsPreset {
                    id: "addon:test:overlay".into(),
                    name: "Overlay".into(),
                    fsrs_version: AddonFsrsVersion::Seven,
                    params: fsrs7_sort_params_b(),
                    desired_retention: 0.9,
                    historical_retention: 0.9,
                    ..Default::default()
                }],
                rules: vec![FsrsPresetRule {
                    search: "overlay".into(),
                    preset_id: "addon:test:overlay".into(),
                }],
                simulator_rules: Vec::new(),
            },
        )?;

        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut default_note = nt.new_note();
        let mut overlay_note = nt.new_note();
        default_note.set_field(0, "default")?;
        overlay_note.set_field(0, "overlay")?;
        col.add_note(&mut default_note, DeckId(1))?;
        col.add_note(&mut overlay_note, DeckId(1))?;

        let default_card_id = col.search_cards("default", SortMode::NoOrder)?[0];
        let overlay_card_id = col.search_cards("overlay", SortMode::NoOrder)?[0];
        let timing = col.timing_today()?;
        let s90_default =
            col.fsrs_interval_at_retrievability_for_card(default_card_id, 30.0, 0.9)?;
        let s90_overlay =
            col.fsrs_interval_at_retrievability_for_card(overlay_card_id, 30.0, 0.9)?;

        for (card_id, s90) in [
            (default_card_id, s90_default),
            (overlay_card_id, s90_overlay),
        ] {
            let mut card = col.storage.get_card(card_id)?.unwrap();
            card.ctype = CardType::Review;
            card.queue = CardQueue::Review;
            card.interval = 20;
            card.due = 0;
            card.memory_state = Some(FsrsMemoryState {
                stability: s90,
                stability_internal: 30.0,
                stability_fast: None,
                difficulty: 5.0,
            });
            card.last_review_time = Some(timing.now.adding_secs(-20 * 86_400));
            col.storage.update_card(&card)?;
        }

        assert!(
            (s90_default - s90_overlay).abs() > 0.001,
            "test requires the overlay preset to change S90"
        );
        let expected = if s90_default <= s90_overlay {
            vec![default_card_id, overlay_card_id]
        } else {
            vec![overlay_card_id, default_card_id]
        };

        let sorted = col.search_cards(
            "",
            SortMode::Builtin {
                column: Column::Stability,
                reverse: false,
            },
        )?;
        assert_eq!(sorted, expected);

        Ok(())
    }

    #[test]
    fn retrievability_property_filter_uses_exact_model_r() -> Result<()> {
        let mut col = Collection::new();
        set_selected_fsrs7_params(&mut col, fsrs7_sort_params_a())?;
        col.set_config_bool(BoolKey::Fsrs, true, true)?;

        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note1 = nt.new_note();
        let mut note2 = nt.new_note();
        col.add_note(&mut note1, DeckId(1))?;
        col.add_note(&mut note2, DeckId(1))?;
        let mut ids = col.search_cards("", SortMode::NoOrder)?;
        ids.sort();

        let timing = col.timing_today()?;
        let mut card1 = col.storage.get_card(ids[0])?.unwrap();
        let mut card2 = col.storage.get_card(ids[1])?.unwrap();
        for card in [&mut card1, &mut card2] {
            card.ctype = CardType::Review;
            card.queue = CardQueue::Review;
            card.interval = 20;
            card.due = 0;
            card.memory_state = Some(FsrsMemoryState {
                stability: 30.0,
                stability_internal: 30.0,
                stability_fast: None,
                difficulty: 5.0,
            });
            card.last_review_time = Some(timing.now.adding_secs(-20 * 86_400));
        }
        // stale per-card decay values should not affect prop:r filtering
        card1.decay = Some(2.0);
        card2.decay = Some(0.1);
        col.storage.update_card(&card1)?;
        col.storage.update_card(&card2)?;

        let exact_r = col.fsrs_current_retrievability_for_card(card1.id, 30.0, 20.0)?;
        let query = format!("prop:r>{:.6}", exact_r - 0.0005);
        let filtered = col.search_cards(&query, SortMode::NoOrder)?;
        assert_eq!(filtered.len(), 2);

        let filtered_cards = col.all_cards_for_search(&query)?;
        assert_eq!(filtered_cards.len(), 2);
        Ok(())
    }

    #[test]
    fn retrievability_property_filter_prefers_available_rwkv_r() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;

        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note1 = nt.new_note();
        let mut note2 = nt.new_note();
        col.add_note(&mut note1, DeckId(1))?;
        col.add_note(&mut note2, DeckId(1))?;
        let mut ids = col.search_cards("", SortMode::NoOrder)?;
        ids.sort();

        let timing = col.timing_today()?;
        let mut card1 = col.storage.get_card(ids[0])?.unwrap();
        let mut card2 = col.storage.get_card(ids[1])?.unwrap();
        for card in [&mut card1, &mut card2] {
            card.ctype = CardType::Review;
            card.queue = CardQueue::Review;
            card.interval = 1;
            card.due = 0;
            card.memory_state = Some(FsrsMemoryState {
                stability: 1.0,
                stability_internal: 1.0,
                stability_fast: None,
                difficulty: 5.0,
            });
            card.last_review_time = Some(timing.now.adding_secs(-100 * 86_400));
        }
        col.storage.update_card(&card1)?;
        col.storage.update_card(&card2)?;

        let fsrs_r = col.fsrs_current_retrievability_for_card(card1.id, 1.0, 100.0)?;
        assert!(
            fsrs_r < 0.9,
            "test requires FSRS retrievability below threshold, got {fsrs_r}"
        );
        col.set_rwkv_stats_graph_scores("deck:current".into(), HashMap::from([(card1.id, 0.95)]))?;

        let filtered = col.search_cards("prop:r>0.9", SortMode::NoOrder)?;
        assert_eq!(filtered, vec![card1.id]);

        let fallback_filtered = col.search_cards("prop:r<0.9", SortMode::NoOrder)?;
        assert_eq!(fallback_filtered, vec![card2.id]);

        assert_eq!(
            col.search_cards("prop:fsrs:r<0.9", SortMode::NoOrder)?,
            vec![card1.id, card2.id]
        );
        assert_eq!(
            col.search_cards("prop:rwkv:r>0.9", SortMode::NoOrder)?,
            vec![card1.id]
        );
        assert_eq!(
            col.search_cards("prop:rwkv:r<0.9", SortMode::NoOrder)?,
            Vec::<CardId>::new()
        );

        let filtered_cards = col.all_cards_for_search("prop:r>0.9")?;
        assert_eq!(
            filtered_cards
                .into_iter()
                .map(|card| card.id)
                .collect::<Vec<_>>(),
            vec![card1.id]
        );

        col.set_rwkv_stats_graph_scores("deck:other".into(), HashMap::new())?;
        assert_eq!(
            col.search_cards("prop:r>0.9", SortMode::NoOrder)?,
            Vec::<CardId>::new()
        );
        Ok(())
    }

    #[test]
    fn retrievability_property_filter_uses_card_info_rwkv_r() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;

        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note1 = nt.new_note();
        let mut note2 = nt.new_note();
        col.add_note(&mut note1, DeckId(1))?;
        col.add_note(&mut note2, DeckId(1))?;
        let mut ids = col.search_cards("", SortMode::NoOrder)?;
        ids.sort();

        let timing = col.timing_today()?;
        let mut card1 = col.storage.get_card(ids[0])?.unwrap();
        let mut card2 = col.storage.get_card(ids[1])?.unwrap();
        for card in [&mut card1, &mut card2] {
            card.ctype = CardType::Review;
            card.queue = CardQueue::Review;
            card.interval = 1;
            card.due = 0;
            card.memory_state = Some(FsrsMemoryState {
                stability: 1.0,
                stability_internal: 1.0,
                stability_fast: None,
                difficulty: 5.0,
            });
            card.last_review_time = Some(timing.now.adding_secs(-100 * 86_400));
        }
        col.storage.update_card(&card1)?;
        col.storage.update_card(&card2)?;

        let fsrs_r = col.fsrs_current_retrievability_for_card(card1.id, 1.0, 100.0)?;
        assert!(
            fsrs_r < 0.9,
            "test requires FSRS retrievability below threshold, got {fsrs_r}"
        );

        col.set_rwkv_card_info_score(card1.id, Some(0.95))?;
        assert_eq!(
            col.search_cards("prop:r>0.9", SortMode::NoOrder)?,
            vec![card1.id]
        );

        col.set_rwkv_card_info_score(card1.id, None)?;
        assert_eq!(
            col.search_cards("prop:r>0.9", SortMode::NoOrder)?,
            Vec::<CardId>::new()
        );
        Ok(())
    }

    #[test]
    fn retrievability_property_filter_uses_deck_count_rwkv_r() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;

        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note = nt.new_note();
        col.add_note(&mut note, DeckId(1))?;
        let card_id = col.search_cards("", SortMode::NoOrder)?[0];

        let timing = col.timing_today()?;
        let mut card = col.storage.get_card(card_id)?.unwrap();
        card.ctype = CardType::Review;
        card.queue = CardQueue::Review;
        card.interval = 30;
        card.due = 0;
        card.memory_state = Some(FsrsMemoryState {
            stability: 30.0,
            stability_internal: 30.0,
            stability_fast: None,
            difficulty: 5.0,
        });
        card.last_review_time = Some(timing.now.adding_secs(-86_400));
        col.storage.update_card(&card)?;

        let fsrs_r = col.fsrs_current_retrievability_for_card(card.id, 30.0, 1.0)?;
        assert!(
            fsrs_r > 0.9,
            "test requires FSRS retrievability above threshold, got {fsrs_r}"
        );
        col.set_rwkv_deck_count_scores(DeckId(1), HashMap::from([(card.id, 0.88)]))?;

        assert_eq!(
            col.search_cards("prop:r<0.9", SortMode::NoOrder)?,
            vec![card.id]
        );
        assert_eq!(
            col.search_cards("prop:rwkv:r<0.9", SortMode::NoOrder)?,
            vec![card.id]
        );
        assert_eq!(
            col.search_cards("prop:fsrs:r<0.9", SortMode::NoOrder)?,
            Vec::<CardId>::new()
        );
        Ok(())
    }

    #[test]
    fn retrievability_property_filter_prefers_card_info_rwkv_r() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;

        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note1 = nt.new_note();
        let mut note2 = nt.new_note();
        col.add_note(&mut note1, DeckId(1))?;
        col.add_note(&mut note2, DeckId(1))?;
        let mut ids = col.search_cards("", SortMode::NoOrder)?;
        ids.sort();

        let timing = col.timing_today()?;
        let mut card1 = col.storage.get_card(ids[0])?.unwrap();
        let mut card2 = col.storage.get_card(ids[1])?.unwrap();
        for card in [&mut card1, &mut card2] {
            card.ctype = CardType::Review;
            card.queue = CardQueue::Review;
            card.interval = 1;
            card.due = 0;
            card.memory_state = Some(FsrsMemoryState {
                stability: 1.0,
                stability_internal: 1.0,
                stability_fast: None,
                difficulty: 5.0,
            });
            card.last_review_time = Some(timing.now.adding_secs(-100 * 86_400));
        }
        col.storage.update_card(&card1)?;
        col.storage.update_card(&card2)?;

        let fsrs_r = col.fsrs_current_retrievability_for_card(card1.id, 1.0, 100.0)?;
        assert!(
            fsrs_r < 0.6,
            "test requires FSRS retrievability below threshold, got {fsrs_r}"
        );

        col.set_rwkv_stats_graph_scores(
            "deck:current".into(),
            HashMap::from([(card1.id, 0.55), (card2.id, 0.56)]),
        )?;
        col.set_rwkv_review_queue_scores(DeckId(1), HashMap::from([(card1.id, 0.57)]))?;
        col.set_rwkv_card_info_score(card1.id, Some(0.83))?;

        assert_eq!(
            col.search_cards("prop:r>=0.55 prop:r<0.6", SortMode::NoOrder)?,
            vec![card2.id]
        );
        assert_eq!(
            col.search_cards("prop:r>0.8", SortMode::NoOrder)?,
            vec![card1.id]
        );
        Ok(())
    }

    #[test]
    fn rwkv_due_states_use_explicit_model_definitions() -> Result<()> {
        let mut col = Collection::new();
        let mut config = col.get_deck_config(DeckConfigId(1), false)?.unwrap();
        config.inner.rwkv_review_instant_order_enabled = true;
        config.inner.rwkv_review_enabled = true;
        col.add_or_update_deck_config(&mut config)?;

        let timing = col.timing_today()?;
        let mut instant_due = Card::new(NoteId(10), 0, DeckId(1), timing.days_elapsed as i32 + 10);
        instant_due.ctype = CardType::Review;
        instant_due.queue = CardQueue::Review;
        instant_due.interval = 30;
        instant_due.desired_retention = Some(0.8);
        instant_due.last_review_time = Some(timing.now.adding_secs(-20 * 86_400));
        col.add_card(&mut instant_due)?;

        let mut dynamic_dr_not_due =
            Card::new(NoteId(12), 0, DeckId(1), timing.days_elapsed as i32 + 10);
        dynamic_dr_not_due.ctype = CardType::Review;
        dynamic_dr_not_due.queue = CardQueue::Review;
        dynamic_dr_not_due.interval = 30;
        dynamic_dr_not_due.desired_retention = Some(0.9);
        dynamic_dr_not_due.last_review_time = Some(timing.now.adding_secs(-20 * 86_400));
        col.add_card(&mut dynamic_dr_not_due)?;

        let mut curve_due = Card::new(NoteId(11), 0, DeckId(1), timing.days_elapsed as i32);
        curve_due.ctype = CardType::Review;
        curve_due.queue = CardQueue::Review;
        curve_due.interval = 30;
        curve_due.desired_retention = Some(0.8);
        curve_due.last_review_time = Some(timing.now.adding_secs(-20 * 86_400));
        col.add_card(&mut curve_due)?;

        col.set_rwkv_stats_graph_score_entries(
            "filtered deck".into(),
            HashMap::from([
                (
                    instant_due.id,
                    RwkvStatsGraphScoreEntry {
                        retrievability: 0.7,
                        intervening_reviews: None,
                        target_retention: Some(0.8),
                        curve_due: false,
                    },
                ),
                (
                    dynamic_dr_not_due.id,
                    RwkvStatsGraphScoreEntry {
                        retrievability: 0.6,
                        intervening_reviews: None,
                        target_retention: Some(0.4),
                        curve_due: false,
                    },
                ),
                (
                    curve_due.id,
                    RwkvStatsGraphScoreEntry {
                        retrievability: 0.9,
                        intervening_reviews: None,
                        target_retention: Some(0.8),
                        curve_due: true,
                    },
                ),
            ]),
        )?;

        assert_eq!(
            col.search_cards("is:rwkv:due", SortMode::NoOrder)?,
            vec![instant_due.id]
        );
        assert_eq!(
            col.search_cards("is:rwkv-curve:due", SortMode::NoOrder)?,
            vec![curve_due.id]
        );
        assert_eq!(
            col.search_cards("is:due", SortMode::NoOrder)?,
            vec![curve_due.id]
        );

        config.inner.rwkv_review_instant_order_enabled = false;
        config.inner.rwkv_review_enabled = false;
        col.add_or_update_deck_config(&mut config)?;
        assert!(col
            .search_cards("is:rwkv:due", SortMode::NoOrder)?
            .is_empty());
        assert!(col
            .search_cards("is:rwkv-curve:due", SortMode::NoOrder)?
            .is_empty());
        Ok(())
    }

    #[test]
    fn stats_scoped_retrievability_filter_survives_exact_sort() -> Result<()> {
        let mut col = Collection::new();

        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note1 = nt.new_note();
        let mut note2 = nt.new_note();
        col.add_note(&mut note1, DeckId(1))?;
        col.add_note(&mut note2, DeckId(1))?;
        let mut ids = col.search_cards("", SortMode::NoOrder)?;
        ids.sort();

        let query = "prop:rwkv:r>0.9";
        col.set_rwkv_stats_graph_scores(
            query.into(),
            HashMap::from([(ids[0], 0.95), (ids[1], 0.10)]),
        )?;
        col.set_rwkv_card_info_score(ids[1], Some(0.99))?;

        let guard = col.search_cards_into_table_with_stats_search(
            query,
            SortMode::Builtin {
                column: Column::Retrievability,
                reverse: false,
            },
            Some(query),
        )?;
        let found = guard.col.storage.all_searched_cards_in_search_order()?;

        assert_eq!(
            found.into_iter().map(|card| card.id).collect::<Vec<_>>(),
            vec![ids[0]]
        );
        Ok(())
    }

    #[test]
    fn stability_property_filter_uses_s90_for_fsrs7() -> Result<()> {
        let mut col = Collection::new();
        set_selected_fsrs7_params(&mut col, fsrs7_sort_params_a())?;
        col.set_config_bool(BoolKey::Fsrs, true, true)?;

        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note = nt.new_note();
        col.add_note(&mut note, DeckId(1))?;
        let card_id = col.search_cards("", SortMode::NoOrder)?[0];

        let mut card = col.storage.get_card(card_id)?.unwrap();
        card.ctype = CardType::Review;
        card.queue = CardQueue::Review;
        let raw_s = 30.0;
        let s90 = col.fsrs_interval_at_retrievability_for_card(card.id, raw_s, 0.9)?;
        card.memory_state = Some(FsrsMemoryState {
            stability: s90,
            stability_internal: raw_s,
            stability_fast: None,
            difficulty: 5.0,
        });
        col.storage.update_card(&card)?;

        assert!(
            (s90 - raw_s).abs() > 0.001,
            "test requires fsrs7 s90 to differ from raw stability"
        );
        let threshold = (raw_s + s90) / 2.0;
        let query = format!("prop:s>{threshold:.6}");
        let filtered = col.search_cards(&query, SortMode::NoOrder)?;
        let expected_match = s90 > threshold;
        assert_eq!(filtered.contains(&card.id), expected_match);
        Ok(())
    }

    #[test]
    fn first_grade_search_matches_first_answer_button() -> Result<()> {
        let mut col = Collection::new();
        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut notes = (0..4).map(|_| nt.new_note()).collect::<Vec<_>>();
        for note in &mut notes {
            col.add_note(note, DeckId(1))?;
        }
        let mut ids = col.search_cards("", SortMode::NoOrder)?;
        ids.sort();

        add_revlog(&mut col, ids[0], 1_000, 1)?;
        add_revlog(&mut col, ids[0], 2_000, 4)?;
        add_revlog(&mut col, ids[1], 500, 0)?;
        add_revlog(&mut col, ids[1], 1_500, 2)?;
        add_revlog(&mut col, ids[2], 3_000, 3)?;

        assert_eq!(
            col.search_cards("firstgrade:1", SortMode::NoOrder)?,
            vec![ids[0]]
        );
        assert_eq!(
            col.search_cards("firstgrade:2", SortMode::NoOrder)?,
            vec![ids[1]]
        );
        assert_eq!(
            col.search_cards("firstgrade:3", SortMode::NoOrder)?,
            vec![ids[2]]
        );
        assert_eq!(
            col.search_cards("firstgrade:4", SortMode::NoOrder)?,
            Vec::<CardId>::new()
        );
        Ok(())
    }

    fn add_revlog(col: &mut Collection, cid: CardId, id: i64, button: u8) -> Result<()> {
        col.storage.add_revlog_entry(
            &RevlogEntry {
                id: RevlogId(id),
                cid,
                usn: Usn(0),
                button_chosen: button,
                interval: 1,
                last_interval: 0,
                ease_factor: 2500,
                taken_millis: 0,
                review_kind: if button == 0 {
                    RevlogReviewKind::Manual
                } else {
                    RevlogReviewKind::Learning
                },
            },
            false,
        )?;
        Ok(())
    }
}
