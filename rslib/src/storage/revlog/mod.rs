// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::collections::HashMap;
use std::convert::TryFrom;

use rusqlite::params;
use rusqlite::types::FromSql;
use rusqlite::types::FromSqlError;
use rusqlite::types::ValueRef;
use rusqlite::OptionalExtension;
use rusqlite::Row;

use super::ids_to_string;
use super::SqliteStorage;
use crate::error::Result;
use crate::prelude::*;
use crate::revlog::RevlogEntry;
use crate::revlog::RevlogReviewKind;

pub(crate) const FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE: &str =
    "search_stats_fsrs_review_retrievability";
pub(crate) const RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE: &str =
    "search_stats_rwkv_review_retrievability";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum FsrsReviewRetrievabilitySampleRole {
    FinalFit,
    ValidationFold,
    PostOptimization,
}

impl FsrsReviewRetrievabilitySampleRole {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::FinalFit => "final_fit",
            Self::ValidationFold => "validation_fold",
            Self::PostOptimization => "post_optimization",
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub(crate) struct FsrsReviewRetrievabilityCacheRow {
    pub revlog_id: RevlogId,
    pub prediction: f32,
    pub sample_role: FsrsReviewRetrievabilitySampleRole,
    pub fold_index: i32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum RwkvReviewRetrievabilitySampleRole {
    FinalFit,
    TestFold,
    PostOptimization,
}

impl RwkvReviewRetrievabilitySampleRole {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::FinalFit => "final_fit",
            Self::TestFold => "test_fold",
            Self::PostOptimization => "post_optimization",
        }
    }

    pub(crate) fn from_str(value: &str) -> Option<Self> {
        match value {
            "" | "final_fit" => Some(Self::FinalFit),
            "test_fold" | "validation_fold" => Some(Self::TestFold),
            "post_optimization" => Some(Self::PostOptimization),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub(crate) struct RwkvReviewRetrievabilityCacheRow {
    pub revlog_id: RevlogId,
    pub prediction: f32,
    pub sample_role: RwkvReviewRetrievabilitySampleRole,
    pub fold_index: i32,
}

pub(crate) struct StudiedToday {
    pub cards: u32,
    pub seconds: f64,
}

impl FromSql for RevlogReviewKind {
    fn column_result(value: ValueRef<'_>) -> std::result::Result<Self, FromSqlError> {
        if let ValueRef::Integer(i) = value {
            Ok(Self::try_from(i as u8).map_err(|_| FromSqlError::InvalidType)?)
        } else {
            Err(FromSqlError::InvalidType)
        }
    }
}

fn row_to_revlog_entry(row: &Row) -> Result<RevlogEntry> {
    Ok(RevlogEntry {
        id: row.get(0)?,
        cid: row.get(1)?,
        usn: row.get(2)?,
        button_chosen: row.get(3)?,
        interval: row.get(4)?,
        last_interval: row.get(5)?,
        ease_factor: row.get(6)?,
        taken_millis: row.get(7).unwrap_or_default(),
        review_kind: row.get(8).unwrap_or_default(),
    })
}

impl SqliteStorage {
    fn ensure_fsrs_review_retrievability_cache_schema(&self) -> Result<()> {
        let table_info = self
            .db
            .prepare(&format!(
                "PRAGMA table_info({FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE})"
            ))?
            .query_map([], |row| row.get::<_, String>(1))?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        let has_sample_role = table_info.iter().any(|col| col == "sample_role");
        let has_fold_index = table_info.iter().any(|col| col == "fold_index");
        if !table_info.is_empty() && (!has_sample_role || !has_fold_index) {
            self.db.execute_batch(&format!(
                "DROP TABLE IF EXISTS {FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE};"
            ))?;
        }

        self.db.execute_batch(&format!(
            "
            CREATE TABLE IF NOT EXISTS {FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE} (
                revlog_id INTEGER NOT NULL,
                prediction REAL NOT NULL CHECK(prediction >= 0 AND prediction <= 1),
                source TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                sample_role TEXT NOT NULL DEFAULT 'final_fit'
                    CHECK(sample_role IN ('final_fit', 'validation_fold', 'post_optimization')),
                fold_index INTEGER NOT NULL DEFAULT -1,
                PRIMARY KEY (revlog_id, sample_role, fold_index, source)
            );
            CREATE INDEX IF NOT EXISTS ix_fsrs_review_retrievability_role_revlog
                ON {FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE} (sample_role, revlog_id);
            "
        ))?;
        Ok(())
    }

    pub(crate) fn set_fsrs_review_retrievability_predictions(
        &self,
        rows: &[FsrsReviewRetrievabilityCacheRow],
        source: &str,
    ) -> Result<usize> {
        if rows.is_empty() {
            return Ok(0);
        }

        self.ensure_fsrs_review_retrievability_cache_schema()?;

        let updated_at = TimestampMillis::now().0;
        let mut stored = 0;
        let mut stmt = self.db.prepare_cached(&format!(
            "
            INSERT OR REPLACE INTO {FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE}
                (revlog_id, prediction, source, updated_at, sample_role, fold_index)
            VALUES (?, ?, ?, ?, ?, ?)
            "
        ))?;

        for row in rows {
            if row.revlog_id.0 > 0
                && row.prediction.is_finite()
                && (0.0..=1.0).contains(&row.prediction)
            {
                stmt.execute(params![
                    row.revlog_id,
                    row.prediction,
                    source,
                    updated_at,
                    row.sample_role.as_str(),
                    row.fold_index
                ])?;
                stored += 1;
            }
        }

        Ok(stored)
    }

    fn ensure_rwkv_review_retrievability_cache_schema(&self) -> Result<()> {
        let table_info = self
            .db
            .prepare(&format!(
                "PRAGMA table_info({RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE})"
            ))?
            .query_map([], |row| row.get::<_, String>(1))?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        let required_columns = ["revlog_id", "prediction", "source", "updated_at"];
        let has_sample_role = table_info.iter().any(|col| col == "sample_role");
        let has_fold_index = table_info.iter().any(|col| col == "fold_index");
        if !table_info.is_empty()
            && (!required_columns
                .iter()
                .all(|required| table_info.iter().any(|column| column == required))
                || !has_sample_role
                || !has_fold_index)
        {
            self.db.execute_batch(&format!(
                "DROP TABLE IF EXISTS {RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE};"
            ))?;
        }

        self.db.execute_batch(&format!(
            "
            CREATE TABLE IF NOT EXISTS {RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE} (
                revlog_id INTEGER NOT NULL,
                prediction REAL NOT NULL CHECK(prediction >= 0 AND prediction <= 1),
                source TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                sample_role TEXT NOT NULL DEFAULT 'final_fit'
                    CHECK(sample_role IN ('final_fit', 'test_fold', 'validation_fold', 'post_optimization')),
                fold_index INTEGER NOT NULL DEFAULT -1,
                PRIMARY KEY (revlog_id, sample_role, fold_index, source)
            );
            CREATE INDEX IF NOT EXISTS ix_rwkv_review_retrievability_role_revlog
                ON {RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE} (sample_role, revlog_id);
            "
        ))?;
        Ok(())
    }

    pub(crate) fn set_rwkv_review_retrievability_predictions(
        &self,
        rows: &[RwkvReviewRetrievabilityCacheRow],
        source: &str,
    ) -> Result<usize> {
        if rows.is_empty() {
            return Ok(0);
        }

        self.ensure_rwkv_review_retrievability_cache_schema()?;

        let updated_at = TimestampMillis::now().0;
        let mut stored = 0;
        let mut stmt = self.db.prepare_cached(&format!(
            "
            INSERT OR REPLACE INTO {RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE}
                (revlog_id, prediction, source, updated_at, sample_role, fold_index)
            VALUES (?, ?, ?, ?, ?, ?)
            "
        ))?;
        for row in rows {
            if row.revlog_id.0 > 0
                && row.prediction.is_finite()
                && (0.0..=1.0).contains(&row.prediction)
            {
                stmt.execute(params![
                    row.revlog_id,
                    row.prediction,
                    source,
                    updated_at,
                    row.sample_role.as_str(),
                    row.fold_index,
                ])?;
                stored += 1;
            }
        }
        Ok(stored)
    }

    pub(crate) fn set_rwkv_review_retrievability_prediction(
        &self,
        revlog_id: RevlogId,
        prediction: f32,
        source: &str,
    ) -> Result<()> {
        self.set_rwkv_review_retrievability_predictions(
            &[RwkvReviewRetrievabilityCacheRow {
                revlog_id,
                prediction,
                sample_role: RwkvReviewRetrievabilitySampleRole::PostOptimization,
                fold_index: -1,
            }],
            source,
        )
        .map(|_| ())
    }

    pub(crate) fn fix_revlog_properties(&self) -> Result<usize> {
        self.db
            .prepare(include_str!("fix_props.sql"))?
            .execute([])
            .map_err(Into::into)
    }

    pub(crate) fn clear_pending_revlog_usns(&self) -> Result<()> {
        self.db
            .prepare("update revlog set usn = 0 where usn = -1")?
            .execute([])?;
        Ok(())
    }

    /// Adds the entry, if its id is unique. If it is not, and `uniquify` is
    /// true, adds it with a new id. Returns the added id.
    /// (I.e., the option is safe to unwrap, if `uniquify` is true.)
    pub(crate) fn add_revlog_entry(
        &self,
        entry: &RevlogEntry,
        uniquify: bool,
    ) -> Result<Option<RevlogId>> {
        let added = self
            .db
            .prepare_cached(include_str!("add.sql"))?
            .execute(params![
                uniquify,
                entry.id,
                entry.cid,
                entry.usn,
                entry.button_chosen,
                entry.interval,
                entry.last_interval,
                entry.ease_factor,
                entry.taken_millis,
                entry.review_kind as u8
            ])?;
        Ok((added > 0).then(|| RevlogId(self.db.last_insert_rowid())))
    }

    pub(crate) fn get_revlog_entry(&self, id: RevlogId) -> Result<Option<RevlogEntry>> {
        self.db
            .prepare_cached(concat!(include_str!("get.sql"), " where id=?"))?
            .query_and_then([id], row_to_revlog_entry)?
            .next()
            .transpose()
    }

    /// Determine the the last review time based on the revlog.
    pub(crate) fn time_of_last_review(&self, card_id: CardId) -> Result<Option<TimestampSecs>> {
        self.db
            .prepare_cached(include_str!("time_of_last_review.sql"))?
            .query_row([card_id], |row| row.get(0))
            .optional()
            .map_err(Into::into)
    }

    pub(crate) fn times_of_last_review(
        &self,
        card_ids: &[CardId],
    ) -> Result<HashMap<CardId, TimestampSecs>> {
        if card_ids.is_empty() {
            return Ok(HashMap::new());
        }

        let mut ids = String::new();
        ids_to_string(&mut ids, card_ids);
        let sql = format!(
            "select id, \
             (select revlog.id / 1000 \
                from revlog \
               where cid = cards.id \
                 and ease between 1 and 4 \
                 and (type != 3 or factor != 0) \
               order by revlog.id desc \
               limit 1) \
             from cards \
             where id in {ids}"
        );
        let mut review_times = HashMap::new();
        let mut stmt = self.db.prepare(&sql)?;
        let mut rows = stmt.query([])?;
        while let Some(row) = rows.next()? {
            if let Some(last_review_time) = row.get::<_, Option<TimestampSecs>>(1)? {
                review_times.insert(row.get(0)?, last_review_time);
            }
        }

        Ok(review_times)
    }

    /// Only intended to be used by the undo code, as Anki can not sync revlog
    /// deletions.
    pub(crate) fn remove_revlog_entry(&self, id: RevlogId) -> Result<()> {
        self.db
            .prepare_cached("delete from revlog where id = ?")?
            .execute([id])?;
        Ok(())
    }

    pub(crate) fn get_revlog_entries_for_card(&self, cid: CardId) -> Result<Vec<RevlogEntry>> {
        self.db
            .prepare_cached(concat!(include_str!("get.sql"), " where cid=?"))?
            .query_and_then([cid], row_to_revlog_entry)?
            .collect()
    }

    pub(crate) fn get_revlog_entries_for_searched_cards_after_stamp(
        &self,
        after: TimestampSecs,
    ) -> Result<Vec<RevlogEntry>> {
        self.db
            .prepare_cached(concat!(
                include_str!("get.sql"),
                " where cid in (select cid from search_cids) and id >= ?"
            ))?
            .query_and_then([after.0 * 1000], row_to_revlog_entry)?
            .collect()
    }

    pub(crate) fn get_revlog_entries_for_searched_cards(&self) -> Result<Vec<RevlogEntry>> {
        self.db
            .prepare_cached(concat!(
                include_str!("get.sql"),
                " where cid in (select cid from search_cids)"
            ))?
            .query_and_then([], row_to_revlog_entry)?
            .collect()
    }

    pub(crate) fn get_revlog_entries_for_searched_cards_in_card_order(
        &self,
    ) -> Result<Vec<RevlogEntry>> {
        self.db
            .prepare_cached(concat!(
                include_str!("get.sql"),
                " where cid in (select cid from search_cids) order by cid, id"
            ))?
            .query_and_then([], row_to_revlog_entry)?
            .collect()
    }

    pub(crate) fn get_revlog_entries_for_export_dataset(&self) -> Result<Vec<RevlogEntry>> {
        self.db
            .prepare_cached(concat!(
                include_str!("get.sql"),
                " where (ease between 1 and 4) or (ease = 0 and factor = 0)",
                " order by cid, id"
            ))?
            .query_and_then([], row_to_revlog_entry)?
            .collect()
    }

    pub(crate) fn get_all_revlog_entries_in_card_order(&self) -> Result<Vec<RevlogEntry>> {
        self.db
            .prepare_cached(concat!(include_str!("get.sql"), " order by cid, id"))?
            .query_and_then([], row_to_revlog_entry)?
            .collect()
    }

    pub(crate) fn get_all_revlog_entries(&self, after: TimestampSecs) -> Result<Vec<RevlogEntry>> {
        self.db
            .prepare_cached(concat!(include_str!("get.sql"), " where id >= ?"))?
            .query_and_then([after.0 * 1000], row_to_revlog_entry)?
            .collect()
    }

    pub(crate) fn studied_today(&self, day_cutoff: TimestampSecs) -> Result<StudiedToday> {
        let start = day_cutoff.adding_secs(-86_400).as_millis();
        self.db
            .prepare_cached(include_str!("studied_today.sql"))?
            .query_map(
                [
                    start.0,
                    RevlogReviewKind::Manual as i64,
                    RevlogReviewKind::Rescheduled as i64,
                ],
                |row| {
                    Ok(StudiedToday {
                        cards: row.get(0)?,
                        seconds: row.get(1)?,
                    })
                },
            )?
            .next()
            .unwrap()
            .map_err(Into::into)
    }

    pub(crate) fn studied_today_by_deck(
        &self,
        day_cutoff: TimestampSecs,
    ) -> Result<Vec<(DeckId, usize)>> {
        let start = day_cutoff.adding_secs(-86_400).as_millis();
        self.db
            .prepare_cached(include_str!("studied_today_by_deck.sql"))?
            .query_and_then([start.0], |row| -> Result<_> {
                Ok((DeckId(row.get(0)?), row.get(1)?))
            })?
            .collect()
    }
    pub(crate) fn upgrade_revlog_to_v2(&self) -> Result<()> {
        self.db
            .execute_batch(include_str!("v2_upgrade.sql"))
            .map_err(Into::into)
    }
}
