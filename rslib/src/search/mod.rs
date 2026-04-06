// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

mod builder;
mod parser;
mod service;
mod sqlwriter;
pub(crate) mod writer;

use std::borrow::Cow;
use std::cmp::Ordering;

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
    }
}

pub struct NoteTableGuard<'a> {
    pub col: &'a mut Collection,
    pub notes: usize,
}

impl Drop for NoteTableGuard<'_> {
    fn drop(&mut self) {
        if let Err(err) = self.col.storage.clear_searched_notes_table() {
            println!("{err:?}");
        }
    }
}

impl Collection {
    fn with_exact_retrievability_table<R>(
        &mut self,
        enabled: bool,
        op: impl FnOnce(&mut Self) -> Result<R>,
    ) -> Result<R> {
        if !enabled {
            return op(self);
        }
        self.setup_exact_retrievability_table()?;
        let result = op(self);
        let cleanup = self.clear_exact_retrievability_table();
        match (result, cleanup) {
            (Ok(value), Ok(())) => Ok(value),
            (Err(err), Ok(())) => Err(err),
            (Ok(_), Err(err)) => Err(err),
            (Err(err), Err(_cleanup_err)) => Err(err),
        }
    }

    fn setup_exact_retrievability_table(&mut self) -> Result<()> {
        self.storage.db.execute_batch(&format!(
            "drop table if exists {EXACT_RETRIEVABILITY_TABLE};\
             create temporary table {EXACT_RETRIEVABILITY_TABLE}(cid integer primary key, r real, s90 real)"
        ))?;
        let ids: Vec<i64> = {
            let mut stmt = self.storage.db.prepare("select id from cards")?;
            let rows = stmt.query_map([], |row| row.get(0))?;
            rows.collect::<std::result::Result<_, _>>()?
        };
        let timing = self.timing_today()?;
        let mut rows_to_insert = Vec::new();
        for cid in ids {
            let card_id = CardId(cid);
            let card = self.storage.get_card(card_id)?.or_not_found(card_id)?;
            if let Some((r, s90)) = self.exact_fsrs_metrics_for_card(&card, timing)? {
                rows_to_insert.push((cid, r, s90));
            }
        }
        let mut insert = self.storage.db.prepare_cached(&format!(
            "insert into {EXACT_RETRIEVABILITY_TABLE}(cid, r, s90) values (?, ?, ?)"
        ))?;
        for (cid, r, s90) in rows_to_insert {
            insert.execute(rusqlite::params![cid, r, s90])?;
        }
        Ok(())
    }

    fn clear_exact_retrievability_table(&self) -> Result<()> {
        self.storage.db.execute(
            &format!("drop table if exists {EXACT_RETRIEVABILITY_TABLE}"),
            [],
        )?;
        Ok(())
    }

    pub fn search_cards<N>(&mut self, search: N, mode: SortMode) -> Result<Vec<CardId>>
    where
        N: TryIntoSearch,
    {
        if let Some((metric, reverse)) = exact_fsrs_sort_mode(ReturnItemType::Cards, &mode) {
            let top_node = search.try_into_search()?;
            let mut ids = self.search_card_ids_for_node(&top_node, mode.required_table())?;
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
    ) -> Result<Vec<CardId>> {
        let use_exact_fsrs_metrics = has_exact_fsrs_metrics_property(top_node);
        self.with_exact_retrievability_table(use_exact_fsrs_metrics, |col| {
            let writer = SqlWriter::new(col, ReturnItemType::Cards);
            let (sql, args) = writer.build_query(top_node, required_table)?;
            let mut stmt = col.storage.db.prepare(&sql)?;
            let ids: Vec<_> = stmt
                .query_map(params_from_iter(args.iter()), |row| row.get(0))?
                .collect::<std::result::Result<_, _>>()?;
            Ok(ids)
        })
    }

    fn elapsed_seconds_since_last_review_for_card(
        &self,
        card: &Card,
        timing: SchedTimingToday,
    ) -> u32 {
        if let Some(last_review_time) = card.last_review_time {
            timing.now.elapsed_secs_since(last_review_time) as u32
        } else {
            let due = card.original_or_current_due() as i64;
            if due > 365_000 {
                let last_review_time = due.saturating_sub(card.interval as i64);
                timing.now.0.saturating_sub(last_review_time) as u32
            } else {
                let review_day = due.saturating_sub(card.interval as i64);
                timing.days_elapsed.saturating_sub(review_day as u32) * 86_400
            }
        }
    }

    fn exact_fsrs_metrics_for_card(
        &mut self,
        card: &Card,
        timing: SchedTimingToday,
    ) -> Result<Option<(f32, f32)>> {
        let Some(state) = card.memory_state else {
            return Ok(None);
        };
        let elapsed_days =
            self.elapsed_seconds_since_last_review_for_card(card, timing) as f32 / 86_400.0;
        let r =
            self.fsrs_current_retrievability_for_card(card.id, state.stability, elapsed_days)?;
        let s90 = self.fsrs_interval_at_retrievability_for_card(card.id, state.stability, 0.9)?;
        Ok(Some((r, s90)))
    }

    fn exact_fsrs_metric_for_card(
        &mut self,
        card: &Card,
        timing: SchedTimingToday,
        metric: ExactFsrsSortMetric,
    ) -> Result<Option<f32>> {
        Ok(self
            .exact_fsrs_metrics_for_card(card, timing)?
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
        let timing = self.timing_today()?;
        let mut with_metric = Vec::with_capacity(ids.len());
        for &cid in ids.iter() {
            let card = self.storage.get_card(cid)?.or_not_found(cid)?;
            with_metric.push((cid, self.exact_fsrs_metric_for_card(&card, timing, metric)?));
        }
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
        self.with_exact_retrievability_table(use_exact_fsrs_metrics, |col| {
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
        if let Some((metric, reverse)) = exact_fsrs_sort_mode(ReturnItemType::Cards, &mode) {
            let top_node = search.try_into_search()?;
            let mut ids = self.search_card_ids_for_node(&top_node, mode.required_table())?;
            self.sort_card_ids_by_exact_fsrs_metric(&mut ids, metric, reverse)?;
            self.storage
                .setup_searched_cards_table_to_preserve_order()?;
            self.storage.set_search_table_to_card_ids(&ids)?;
            return Ok(CardTableGuard {
                cards: ids.len(),
                col: self,
                cleanup_exact_retrievability: false,
            });
        }
        let top_node = search.try_into_search()?;
        let want_order = mode != SortMode::NoOrder;
        let use_exact_fsrs_metrics = has_exact_fsrs_metrics_property(&top_node);
        if use_exact_fsrs_metrics {
            self.setup_exact_retrievability_table()?;
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
            }),
            Err(err) => {
                if use_exact_fsrs_metrics {
                    let _ = self.clear_exact_retrievability_table();
                }
                Err(err)
            }
        }
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
        let writer = SqlWriter::new(self, ReturnItemType::Notes);
        let mode = SortMode::NoOrder;

        let (sql, args) = writer.build_query(&top_node, mode.required_table())?;

        self.storage.setup_searched_notes_table()?;
        let sql = format!("insert into search_nids {sql}");

        let notes = self
            .storage
            .db
            .prepare(&sql)?
            .execute(params_from_iter(args))?;

        Ok(NoteTableGuard { notes, col: self })
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
            kind: PropertyKind::Retrievability(_) | PropertyKind::Stability(_),
            ..
        }) => true,
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
    use anki_proto::deck_config::deck_configs_for_update::current_deck::Limits;
    use anki_proto::deck_config::UpdateDeckConfigsMode;
    use anki_proto::search::browser_columns::Sorting;
    use strum::IntoEnumIterator;

    use super::*;
    use crate::card::CardQueue;
    use crate::card::CardType;
    use crate::card::FsrsMemoryState;
    use crate::config::BoolKey;
    use crate::deckconfig::FsrsVersion;
    use crate::deckconfig::UpdateDeckConfigsRequest;

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
            fsrs_reschedule: false,
            fsrs_health_check: true,
        };
        input.configs[0].inner.fsrs_version = FsrsVersion::Seven as i32;
        input.configs[0].inner.fsrs_params_7 = params;
        col.update_deck_configs(input)?;
        Ok(())
    }

    fn set_selected_fsrs7_params(col: &mut Collection, params: Vec<f32>) -> Result<()> {
        set_selected_fsrs7_params_for_deck(col, DeckId(1), params)
    }

    #[test]
    fn browser_retrievability_sort_uses_exact_model_r() -> Result<()> {
        let mut col = Collection::new();
        set_selected_fsrs7_params(
            &mut col,
            vec![
                0.4843, 3.0562, 10.9946, 32.7202, 5.6296, 0.5900, 3.1230, 2.4679, 0.2733, 1.4895,
                0.4868, 0.0010, 0.8082, 0.1723, 0.6389, 1.5767, 0.8918, 0.3341, 3.5942, 0.3455,
                0.0022, 0.2834, 2.6418, 0.5604, 1.3042, 2.5054, 0.9376, 0.0611, 0.0830, 0.6339,
                0.9846, 0.2485, 0.6014, 0.0545, 0.2885,
            ],
        )?;
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
        let params_a = vec![
            0.4843, 3.0562, 10.9946, 32.7202, 5.6296, 0.5900, 3.1230, 2.4679, 0.2733, 1.4895,
            0.4868, 0.0010, 0.8082, 0.1723, 0.6389, 1.5767, 0.8918, 0.3341, 3.5942, 0.3455, 0.0022,
            0.2834, 2.6418, 0.5604, 1.3042, 2.5054, 0.9376, 0.0611, 0.0830, 0.6339, 0.9846, 0.2485,
            0.6014, 0.0545, 0.2885,
        ];
        let params_b = vec![
            0.4843, 3.0562, 10.9946, 32.7202, 5.6296, 0.5900, 3.1230, 2.4679, 0.2733, 1.4895,
            0.4868, 0.0010, 0.8082, 0.1723, 0.6389, 1.5767, 0.8918, 0.3341, 3.5942, 0.3455, 0.0022,
            0.2834, 2.6418, 0.5604, 1.3042, 2.5054, 0.9376, 0.3000, 0.3000, 0.6000, 0.9500, 0.3500,
            0.9000, 0.1500, 0.9000,
        ];
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
            fsrs_reschedule: false,
            fsrs_health_check: true,
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
        for card in [&mut card1, &mut card2] {
            card.ctype = CardType::Review;
            card.queue = CardQueue::Review;
            card.interval = 20;
            card.due = 0;
            card.memory_state = Some(FsrsMemoryState {
                stability: 30.0,
                difficulty: 5.0,
            });
            card.last_review_time = Some(timing.now.adding_secs(-20 * 86_400));
        }
        col.storage.update_card(&card1)?;
        col.storage.update_card(&card2)?;

        let s90_1 = col.fsrs_interval_at_retrievability_for_card(card1.id, 30.0, 0.9)?;
        let s90_2 = col.fsrs_interval_at_retrievability_for_card(card2.id, 30.0, 0.9)?;
        require!(
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
    fn retrievability_property_filter_uses_exact_model_r() -> Result<()> {
        let mut col = Collection::new();
        set_selected_fsrs7_params(
            &mut col,
            vec![
                0.4843, 3.0562, 10.9946, 32.7202, 5.6296, 0.5900, 3.1230, 2.4679, 0.2733, 1.4895,
                0.4868, 0.0010, 0.8082, 0.1723, 0.6389, 1.5767, 0.8918, 0.3341, 3.5942, 0.3455,
                0.0022, 0.2834, 2.6418, 0.5604, 1.3042, 2.5054, 0.9376, 0.0611, 0.0830, 0.6339,
                0.9846, 0.2485, 0.6014, 0.0545, 0.2885,
            ],
        )?;
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
    fn stability_property_filter_uses_s90_for_fsrs7() -> Result<()> {
        let mut col = Collection::new();
        set_selected_fsrs7_params(
            &mut col,
            vec![
                0.4843, 3.0562, 10.9946, 32.7202, 5.6296, 0.5900, 3.1230, 2.4679, 0.2733, 1.4895,
                0.4868, 0.0010, 0.8082, 0.1723, 0.6389, 1.5767, 0.8918, 0.3341, 3.5942, 0.3455,
                0.0022, 0.2834, 2.6418, 0.5604, 1.3042, 2.5054, 0.9376, 0.0611, 0.0830, 0.6339,
                0.9846, 0.2485, 0.6014, 0.0545, 0.2885,
            ],
        )?;
        col.set_config_bool(BoolKey::Fsrs, true, true)?;

        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note = nt.new_note();
        col.add_note(&mut note, DeckId(1))?;
        let card_id = col.search_cards("", SortMode::NoOrder)?[0];

        let mut card = col.storage.get_card(card_id)?.unwrap();
        card.ctype = CardType::Review;
        card.queue = CardQueue::Review;
        card.memory_state = Some(FsrsMemoryState {
            stability: 30.0,
            difficulty: 5.0,
        });
        col.storage.update_card(&card)?;

        let raw_s = 30.0;
        let s90 = col.fsrs_interval_at_retrievability_for_card(card.id, raw_s, 0.9)?;
        require!(
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
}
