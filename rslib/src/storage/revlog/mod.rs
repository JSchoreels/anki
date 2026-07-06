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
use super::sqlite::RETRIEVABILITY_CACHE_DB_SCHEMA;
use super::SqliteStorage;
use crate::config::ConfigEntry;
use crate::error::Result;
use crate::prelude::*;
use crate::revlog::RevlogEntry;
use crate::revlog::RevlogReviewKind;

pub(crate) const FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE: &str =
    "search_stats_fsrs_review_retrievability";
pub(crate) const RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE: &str =
    "search_stats_rwkv_review_retrievability";
const REVIEW_RETRIEVABILITY_CACHE_CLEANUP_FULL_SYNC_MARKER: &str =
    "reviewRetrievabilityCacheCleanupFullSync";
const REVIEW_RETRIEVABILITY_CACHE_WRITE_SAVEPOINT: &str = "review_retrievability_cache_write";
const FSRS_REVIEW_RETRIEVABILITY_SAMPLE_ROLES: &str =
    "'final_fit', 'validation_fold', 'post_optimization'";
const RWKV_REVIEW_RETRIEVABILITY_SAMPLE_ROLES: &str =
    "'final_fit', 'test_fold', 'validation_fold', 'post_optimization'";

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
    fn qualified_retrievability_cache_table(table: &str) -> String {
        format!("{RETRIEVABILITY_CACHE_DB_SCHEMA}.{table}")
    }

    fn table_columns(&self, schema: &str, table: &str) -> Result<Vec<String>> {
        self.db
            .prepare(&format!("PRAGMA {schema}.table_info({table})"))?
            .query_map([], |row| row.get::<_, String>(1))?
            .collect::<std::result::Result<Vec<_>, _>>()
            .map_err(Into::into)
    }

    fn main_table_exists(&self, table: &str) -> Result<bool> {
        self.db
            .prepare("SELECT null FROM main.sqlite_master WHERE type = 'table' AND name = ?")?
            .exists([table])
            .map_err(Into::into)
    }

    fn migrate_legacy_retrievability_cache_table(
        &self,
        table: &str,
        valid_sample_roles: &str,
    ) -> Result<bool> {
        if !self.main_table_exists(table)? {
            return Ok(false);
        }

        let columns = self.table_columns("main", table)?;
        let has_required_columns = ["revlog_id", "prediction", "source", "updated_at"]
            .iter()
            .all(|required| columns.iter().any(|column| column == required));
        if has_required_columns {
            let has_sample_role = columns.iter().any(|column| column == "sample_role");
            let has_fold_index = columns.iter().any(|column| column == "fold_index");
            let sample_role = if has_sample_role {
                "sample_role"
            } else {
                "'final_fit'"
            };
            let fold_index = if has_fold_index { "fold_index" } else { "-1" };
            let sample_role_filter = if has_sample_role {
                format!("AND sample_role IN ({valid_sample_roles})")
            } else {
                String::new()
            };
            let target = Self::qualified_retrievability_cache_table(table);
            self.db.execute_batch(&format!(
                "
                INSERT OR REPLACE INTO {target}
                    (revlog_id, prediction, source, updated_at, sample_role, fold_index)
                SELECT revlog_id, prediction, source, updated_at, {sample_role}, {fold_index}
                FROM main.{table}
                WHERE revlog_id > 0
                  AND prediction BETWEEN 0 AND 1
                  AND source IS NOT NULL
                  AND updated_at IS NOT NULL
                  {sample_role_filter};
                "
            ))?;
        }

        self.db
            .execute_batch(&format!("DROP TABLE IF EXISTS main.{table};"))?;
        Ok(true)
    }

    pub(crate) fn migrate_review_retrievability_cache_to_sidecar(&self) -> Result<usize> {
        self.ensure_fsrs_review_retrievability_cache_schema()?;
        let mut dropped = 0;
        if self.migrate_legacy_retrievability_cache_table(
            FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE,
            FSRS_REVIEW_RETRIEVABILITY_SAMPLE_ROLES,
        )? {
            dropped += 1;
        }
        self.ensure_rwkv_review_retrievability_cache_schema()?;
        if self.migrate_legacy_retrievability_cache_table(
            RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE,
            RWKV_REVIEW_RETRIEVABILITY_SAMPLE_ROLES,
        )? {
            dropped += 1;
        }
        Ok(dropped)
    }

    pub(crate) fn review_retrievability_cache_cleanup_full_sync_marked(&self) -> Result<bool> {
        Ok(self
            .get_config_value::<bool>(REVIEW_RETRIEVABILITY_CACHE_CLEANUP_FULL_SYNC_MARKER)?
            .unwrap_or(false))
    }

    pub(crate) fn mark_review_retrievability_cache_cleanup_full_sync(&self) -> Result<()> {
        self.set_config_entry(&ConfigEntry::boxed(
            REVIEW_RETRIEVABILITY_CACHE_CLEANUP_FULL_SYNC_MARKER,
            serde_json::to_vec(&true)?,
            Usn(0),
            TimestampSecs::now(),
        ))
    }

    fn with_retrievability_cache_write_batch(
        &self,
        op: impl FnOnce() -> Result<usize>,
    ) -> Result<usize> {
        let started_savepoint = self.db.is_autocommit();
        if started_savepoint {
            self.db.execute_batch(&format!(
                "SAVEPOINT {REVIEW_RETRIEVABILITY_CACHE_WRITE_SAVEPOINT};"
            ))?;
        }

        let result = op();
        if !started_savepoint {
            return result;
        }

        match result {
            Ok(stored) => {
                self.db.execute_batch(&format!(
                    "RELEASE {REVIEW_RETRIEVABILITY_CACHE_WRITE_SAVEPOINT};"
                ))?;
                Ok(stored)
            }
            Err(err) => {
                if let Err(rollback_err) = self.db.execute_batch(&format!(
                    "ROLLBACK TO {REVIEW_RETRIEVABILITY_CACHE_WRITE_SAVEPOINT};
                     RELEASE {REVIEW_RETRIEVABILITY_CACHE_WRITE_SAVEPOINT};"
                )) {
                    tracing::warn!(
                        ?rollback_err,
                        "failed to roll back retrievability cache write batch"
                    );
                }
                Err(err)
            }
        }
    }

    fn ensure_fsrs_review_retrievability_cache_schema(&self) -> Result<()> {
        let table_info = self.table_columns(
            RETRIEVABILITY_CACHE_DB_SCHEMA,
            FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE,
        )?;
        let has_sample_role = table_info.iter().any(|col| col == "sample_role");
        let has_fold_index = table_info.iter().any(|col| col == "fold_index");
        if !table_info.is_empty() && (!has_sample_role || !has_fold_index) {
            let table =
                Self::qualified_retrievability_cache_table(FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE);
            self.db
                .execute_batch(&format!("DROP TABLE IF EXISTS {table};"))?;
        }

        let table =
            Self::qualified_retrievability_cache_table(FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE);
        self.db.execute_batch(&format!(
            "
            CREATE TABLE IF NOT EXISTS {table} (
                revlog_id INTEGER NOT NULL,
                prediction REAL NOT NULL CHECK(prediction >= 0 AND prediction <= 1),
                source TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                sample_role TEXT NOT NULL DEFAULT 'final_fit'
                    CHECK(sample_role IN ('final_fit', 'validation_fold', 'post_optimization')),
                fold_index INTEGER NOT NULL DEFAULT -1,
                PRIMARY KEY (revlog_id, sample_role, fold_index, source)
            );
            CREATE INDEX IF NOT EXISTS {RETRIEVABILITY_CACHE_DB_SCHEMA}.ix_fsrs_review_retrievability_role_revlog
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

        self.with_retrievability_cache_write_batch(|| {
            let updated_at = TimestampMillis::now().0;
            let mut stored = 0;
            let table =
                Self::qualified_retrievability_cache_table(FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE);
            let mut stmt = self.db.prepare_cached(&format!(
                "
                INSERT INTO {table}
                    (revlog_id, prediction, source, updated_at, sample_role, fold_index)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(revlog_id, sample_role, fold_index, source) DO UPDATE SET
                    prediction = excluded.prediction,
                    updated_at = excluded.updated_at
                WHERE {table}.prediction IS NOT excluded.prediction
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
        })
    }

    fn ensure_rwkv_review_retrievability_cache_schema(&self) -> Result<()> {
        let table_info = self.table_columns(
            RETRIEVABILITY_CACHE_DB_SCHEMA,
            RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE,
        )?;
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
            let table =
                Self::qualified_retrievability_cache_table(RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE);
            self.db
                .execute_batch(&format!("DROP TABLE IF EXISTS {table};"))?;
        }

        let table =
            Self::qualified_retrievability_cache_table(RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE);
        self.db.execute_batch(&format!(
            "
            CREATE TABLE IF NOT EXISTS {table} (
                revlog_id INTEGER NOT NULL,
                prediction REAL NOT NULL CHECK(prediction >= 0 AND prediction <= 1),
                source TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                sample_role TEXT NOT NULL DEFAULT 'final_fit'
                    CHECK(sample_role IN ('final_fit', 'test_fold', 'validation_fold', 'post_optimization')),
                fold_index INTEGER NOT NULL DEFAULT -1,
                PRIMARY KEY (revlog_id, sample_role, fold_index, source)
            );
            CREATE INDEX IF NOT EXISTS {RETRIEVABILITY_CACHE_DB_SCHEMA}.ix_rwkv_review_retrievability_role_revlog
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

        self.with_retrievability_cache_write_batch(|| {
            let updated_at = TimestampMillis::now().0;
            let mut stored = 0;
            let table =
                Self::qualified_retrievability_cache_table(RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE);
            let mut stmt = self.db.prepare_cached(&format!(
                "
                INSERT INTO {table}
                    (revlog_id, prediction, source, updated_at, sample_role, fold_index)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(revlog_id, sample_role, fold_index, source) DO UPDATE SET
                    prediction = excluded.prediction,
                    updated_at = excluded.updated_at
                WHERE {table}.prediction IS NOT excluded.prediction
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
        })
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
            "select id, (select revlog.id / 1000 from revlog \
             where cid = cards.id and ease between 1 and 4 \
             and (type != 3 or factor != 0) \
             order by revlog.id desc limit 1) from cards where id in {ids}"
        );
        let mut review_times = HashMap::new();
        let mut stmt = self.db.prepare(&sql)?;
        let mut rows = stmt.query([])?;
        while let Some(row) = rows.next()? {
            let cid: CardId = row.get(0)?;
            let last_review_time: Option<TimestampSecs> = row.get(1)?;
            if let Some(time) = last_review_time {
                review_times.insert(cid, time);
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

#[cfg(test)]
mod tests {
    use std::path::Path;
    use std::path::PathBuf;
    use std::time::Instant;

    use rusqlite::params;
    use tempfile::tempdir;
    use tempfile::TempDir;

    use super::*;
    use crate::collection::Collection;
    use crate::collection::CollectionBuilder;

    fn temp_collection(name: &str) -> Result<(Collection, TempDir, PathBuf)> {
        let tempdir = tempdir()?;
        let col_path = tempdir.path().join(format!("{name}.anki2"));
        let mut builder = CollectionBuilder::new(&col_path);
        builder.with_desktop_media_paths();
        let col = builder.build()?;
        Ok((col, tempdir, col_path))
    }

    fn fsrs_cache_rows(count: usize, offset: i64) -> Vec<FsrsReviewRetrievabilityCacheRow> {
        (0..count)
            .map(|index| FsrsReviewRetrievabilityCacheRow {
                revlog_id: RevlogId(offset + index as i64 + 1),
                prediction: (index % 100) as f32 / 100.0,
                sample_role: FsrsReviewRetrievabilitySampleRole::FinalFit,
                fold_index: -1,
            })
            .collect()
    }

    fn count_fsrs_cache_rows(storage: &SqliteStorage) -> Result<usize> {
        storage
            .db
            .query_row(
                &format!("SELECT count() FROM {FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE}"),
                [],
                |row| row.get::<_, usize>(0),
            )
            .map_err(Into::into)
    }

    fn clear_fsrs_cache_rows(storage: &SqliteStorage) -> Result<()> {
        storage.db.execute_batch(&format!(
            "DELETE FROM {FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE};"
        ))?;
        Ok(())
    }

    fn checkpoint_truncate(storage: &SqliteStorage) -> Result<()> {
        storage
            .db
            .execute_batch("PRAGMA wal_checkpoint(TRUNCATE);")?;
        Ok(())
    }

    fn wal_path(path: &Path) -> PathBuf {
        let mut wal = path.as_os_str().to_os_string();
        wal.push("-wal");
        wal.into()
    }

    fn file_size(path: &Path) -> u64 {
        std::fs::metadata(path)
            .map(|metadata| metadata.len())
            .unwrap_or(0)
    }

    fn sidecar_table_exists(storage: &SqliteStorage, table: &str) -> Result<bool> {
        storage
            .db
            .prepare(&format!(
                "SELECT null FROM {RETRIEVABILITY_CACHE_DB_SCHEMA}.sqlite_master \
                 WHERE type = 'table' AND name = ?"
            ))?
            .exists([table])
            .map_err(Into::into)
    }

    #[test]
    fn retrievability_cache_tables_are_external_to_collection_db() -> Result<()> {
        let (col, _tempdir, col_path) = temp_collection("external-retrievability-cache")?;
        let rows = fsrs_cache_rows(1, 0);

        col.storage
            .set_fsrs_review_retrievability_predictions(&rows, "test")?;
        col.storage
            .set_rwkv_review_retrievability_prediction(RevlogId(1), 0.42, "rwkv_review")?;

        assert!(!col
            .storage
            .main_table_exists(FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE)?);
        assert!(!col
            .storage
            .main_table_exists(RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE)?);
        assert!(sidecar_table_exists(
            &col.storage,
            FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE
        )?);
        assert!(sidecar_table_exists(
            &col.storage,
            RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE
        )?);
        assert!(super::super::sqlite::retrievability_cache_path(&col_path).exists());
        assert_eq!(count_fsrs_cache_rows(&col.storage)?, 1);

        let rwkv_count: usize = col.storage.db.query_row(
            &format!("SELECT count() FROM {RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE}"),
            [],
            |row| row.get(0),
        )?;
        assert_eq!(rwkv_count, 1);
        Ok(())
    }

    #[test]
    fn legacy_main_retrievability_cache_tables_migrate_to_sidecar() -> Result<()> {
        let (col, _tempdir, col_path) = temp_collection("migrate-retrievability-cache")?;
        col.storage.db.execute_batch(&format!(
            "
            CREATE TABLE main.{FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE} (
                revlog_id INTEGER NOT NULL,
                prediction REAL NOT NULL,
                source TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                sample_role TEXT NOT NULL DEFAULT 'final_fit',
                fold_index INTEGER NOT NULL DEFAULT -1,
                PRIMARY KEY (revlog_id, sample_role, fold_index, source)
            );
            INSERT INTO main.{FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE}
                (revlog_id, prediction, source, updated_at, sample_role, fold_index)
            VALUES (1, 0.25, 'legacy_fsrs', 123, 'validation_fold', 2);
            CREATE TABLE main.{RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE} (
                revlog_id INTEGER NOT NULL,
                prediction REAL NOT NULL,
                source TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                sample_role TEXT NOT NULL DEFAULT 'final_fit',
                fold_index INTEGER NOT NULL DEFAULT -1,
                PRIMARY KEY (revlog_id, sample_role, fold_index, source)
            );
            INSERT INTO main.{RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE}
                (revlog_id, prediction, source, updated_at, sample_role, fold_index)
            VALUES (2, 0.75, 'legacy_rwkv', 456, 'test_fold', 0);
            "
        ))?;
        col.storage
            .set_schema_modified_time(TimestampMillis(1_000))?;
        col.storage.set_last_sync(TimestampMillis(1_000))?;
        col.close(None)?;

        let mut builder = CollectionBuilder::new(&col_path);
        builder.with_desktop_media_paths();
        let col = builder.build()?;

        assert!(!col
            .storage
            .main_table_exists(FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE)?);
        assert!(!col
            .storage
            .main_table_exists(RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE)?);
        assert!(col
            .storage
            .get_collection_timestamps()?
            .schema_changed_since_sync());
        assert!(col
            .storage
            .review_retrievability_cache_cleanup_full_sync_marked()?);

        let fsrs: (f64, String, i64) = col.storage.db.query_row(
            &format!(
                "SELECT prediction, sample_role, fold_index \
                 FROM {FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE} WHERE revlog_id = 1"
            ),
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )?;
        assert_eq!(fsrs, (0.25, "validation_fold".to_string(), 2));

        let rwkv: (f64, String, i64) = col.storage.db.query_row(
            &format!(
                "SELECT prediction, sample_role, fold_index \
                 FROM {RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE} WHERE revlog_id = 2"
            ),
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )?;
        assert_eq!(rwkv, (0.75, "test_fold".to_string(), 0));
        Ok(())
    }

    fn legacy_autocommit_fsrs_insert(
        storage: &SqliteStorage,
        rows: &[FsrsReviewRetrievabilityCacheRow],
        source: &str,
    ) -> Result<usize> {
        storage.ensure_fsrs_review_retrievability_cache_schema()?;
        let updated_at = TimestampMillis::now().0;
        let mut stored = 0;
        let mut stmt = storage.db.prepare_cached(&format!(
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

    #[test]
    fn fsrs_retrievability_cache_batch_works_inside_transaction() -> Result<()> {
        let (col, _tempdir, _col_path) = temp_collection("fsrs-cache-transaction")?;
        let rows = fsrs_cache_rows(3, 0);

        col.storage.begin_trx()?;
        let stored = col
            .storage
            .set_fsrs_review_retrievability_predictions(&rows, "test")?;
        col.storage.commit_trx()?;

        assert_eq!(stored, 3);
        assert_eq!(count_fsrs_cache_rows(&col.storage)?, 3);
        Ok(())
    }

    #[test]
    fn fsrs_retrievability_cache_skips_unchanged_rebuild_rows() -> Result<()> {
        let (col, _tempdir, _col_path) = temp_collection("fsrs-cache-skip-unchanged")?;
        let mut rows = fsrs_cache_rows(1, 0);

        col.storage
            .set_fsrs_review_retrievability_predictions(&rows, "test")?;
        col.storage.db.execute(
            &format!(
                "
                UPDATE {FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE}
                SET updated_at = 123
                WHERE revlog_id = ? AND sample_role = ? AND fold_index = ? AND source = ?
                "
            ),
            params![
                rows[0].revlog_id,
                rows[0].sample_role.as_str(),
                rows[0].fold_index,
                "test",
            ],
        )?;

        assert_eq!(
            col.storage
                .set_fsrs_review_retrievability_predictions(&rows, "test")?,
            1
        );
        let unchanged_updated_at: i64 = col.storage.db.query_row(
            &format!(
                "SELECT updated_at FROM {FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE} WHERE revlog_id = ?"
            ),
            [rows[0].revlog_id],
            |row| row.get(0),
        )?;
        assert_eq!(unchanged_updated_at, 123);

        rows[0].prediction = 0.99;
        col.storage
            .set_fsrs_review_retrievability_predictions(&rows, "test")?;
        let (changed_prediction, changed_updated_at): (f64, i64) = col.storage.db.query_row(
            &format!(
                "SELECT prediction, updated_at FROM {FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE} WHERE revlog_id = ?"
            ),
            [rows[0].revlog_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )?;
        assert!((changed_prediction - 0.99).abs() < 1e-6);
        assert_ne!(changed_updated_at, 123);
        Ok(())
    }

    #[test]
    fn rwkv_retrievability_cache_batch_works_inside_transaction() -> Result<()> {
        let (col, _tempdir, _col_path) = temp_collection("rwkv-cache-transaction")?;
        let rows = [
            RwkvReviewRetrievabilityCacheRow {
                revlog_id: RevlogId(1),
                prediction: 0.25,
                sample_role: RwkvReviewRetrievabilitySampleRole::FinalFit,
                fold_index: -1,
            },
            RwkvReviewRetrievabilityCacheRow {
                revlog_id: RevlogId(2),
                prediction: 0.75,
                sample_role: RwkvReviewRetrievabilitySampleRole::PostOptimization,
                fold_index: -1,
            },
        ];

        col.storage.begin_trx()?;
        let stored = col
            .storage
            .set_rwkv_review_retrievability_predictions(&rows, "test")?;
        col.storage.commit_trx()?;

        let count: usize = col.storage.db.query_row(
            &format!("SELECT count() FROM {RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE}"),
            [],
            |row| row.get(0),
        )?;
        assert_eq!(stored, 2);
        assert_eq!(count, 2);
        Ok(())
    }

    #[test]
    fn rwkv_retrievability_cache_skips_unchanged_rebuild_rows() -> Result<()> {
        let (col, _tempdir, _col_path) = temp_collection("rwkv-cache-skip-unchanged")?;
        let mut rows = [RwkvReviewRetrievabilityCacheRow {
            revlog_id: RevlogId(1),
            prediction: 0.25,
            sample_role: RwkvReviewRetrievabilitySampleRole::FinalFit,
            fold_index: -1,
        }];

        col.storage
            .set_rwkv_review_retrievability_predictions(&rows, "test")?;
        col.storage.db.execute(
            &format!(
                "
                UPDATE {RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE}
                SET updated_at = 123
                WHERE revlog_id = ? AND sample_role = ? AND fold_index = ? AND source = ?
                "
            ),
            params![
                rows[0].revlog_id,
                rows[0].sample_role.as_str(),
                rows[0].fold_index,
                "test",
            ],
        )?;

        assert_eq!(
            col.storage
                .set_rwkv_review_retrievability_predictions(&rows, "test")?,
            1
        );
        let unchanged_updated_at: i64 = col.storage.db.query_row(
            &format!(
                "SELECT updated_at FROM {RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE} WHERE revlog_id = ?"
            ),
            [rows[0].revlog_id],
            |row| row.get(0),
        )?;
        assert_eq!(unchanged_updated_at, 123);

        rows[0].prediction = 0.75;
        col.storage
            .set_rwkv_review_retrievability_predictions(&rows, "test")?;
        let (changed_prediction, changed_updated_at): (f64, i64) = col.storage.db.query_row(
            &format!(
                "SELECT prediction, updated_at FROM {RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE} WHERE revlog_id = ?"
            ),
            [rows[0].revlog_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )?;
        assert!((changed_prediction - 0.75).abs() < 1e-6);
        assert_ne!(changed_updated_at, 123);
        Ok(())
    }

    #[test]
    #[ignore]
    fn retrievability_cache_insert_benchmark() -> Result<()> {
        let row_count = std::env::var("ANKI_RETRIEVABILITY_CACHE_BENCH_ROWS")
            .ok()
            .and_then(|value| value.parse::<usize>().ok())
            .unwrap_or(10_000);
        let repeated_runs = std::env::var("ANKI_RETRIEVABILITY_CACHE_BENCH_REPEATS")
            .ok()
            .and_then(|value| value.parse::<usize>().ok())
            .unwrap_or(3);
        let (col, _tempdir, col_path) =
            if let Ok(source) = std::env::var("ANKI_RETRIEVABILITY_CACHE_BENCH_COLLECTION") {
                let tempdir = tempdir()?;
                let col_path = tempdir.path().join("bench.anki2");
                std::fs::copy(source, &col_path)?;
                let mut builder = CollectionBuilder::new(&col_path);
                builder.with_desktop_media_paths();
                (builder.build()?, tempdir, col_path)
            } else {
                temp_collection("retrievability-cache-bench")?
            };
        let wal_path = wal_path(&col_path);
        let rows = fsrs_cache_rows(row_count, 1_000_000_000);

        col.storage
            .set_fsrs_review_retrievability_predictions(&rows[0..1], "setup")?;
        clear_fsrs_cache_rows(&col.storage)?;
        checkpoint_truncate(&col.storage)?;

        let legacy_started = Instant::now();
        let legacy_stored = legacy_autocommit_fsrs_insert(&col.storage, &rows, "legacy")?;
        let legacy_elapsed = legacy_started.elapsed();
        let legacy_wal_bytes = file_size(&wal_path);
        clear_fsrs_cache_rows(&col.storage)?;
        checkpoint_truncate(&col.storage)?;

        let batched_started = Instant::now();
        let batched_stored = col
            .storage
            .set_fsrs_review_retrievability_predictions(&rows, "batched")?;
        let batched_elapsed = batched_started.elapsed();
        let batched_wal_bytes = file_size(&wal_path);

        let mut repeated_stored = 0;
        let mut repeated_total_ms = 0.0;
        let mut repeated_wal_bytes = 0;
        for _ in 0..repeated_runs {
            checkpoint_truncate(&col.storage)?;
            let repeated_started = Instant::now();
            repeated_stored += col
                .storage
                .set_fsrs_review_retrievability_predictions(&rows, "batched")?;
            repeated_total_ms += repeated_started.elapsed().as_secs_f64() * 1000.0;
            repeated_wal_bytes += file_size(&wal_path);
        }

        println!(
            "retrievability_cache_insert_benchmark rows={row_count} repeated_runs={repeated_runs} \
             legacy_stored={legacy_stored} legacy_ms={:.3} legacy_wal_bytes={} \
             batched_stored={batched_stored} batched_ms={:.3} batched_wal_bytes={} \
             repeated_stored={repeated_stored} repeated_total_ms={repeated_total_ms:.3} \
             repeated_wal_bytes={repeated_wal_bytes}",
            legacy_elapsed.as_secs_f64() * 1000.0,
            legacy_wal_bytes,
            batched_elapsed.as_secs_f64() * 1000.0,
            batched_wal_bytes,
        );

        assert_eq!(legacy_stored, row_count);
        assert_eq!(batched_stored, row_count);
        assert_eq!(repeated_stored, row_count * repeated_runs);
        Ok(())
    }

    #[test]
    #[ignore]
    fn times_of_last_review_benchmark() -> Result<()> {
        let card_count: usize = std::env::var("ANKI_TIMES_BENCH_CARDS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(2048);
        let reviews_per_card: usize = std::env::var("ANKI_TIMES_BENCH_REVIEWS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(20);
        let repeats: usize = std::env::var("ANKI_TIMES_BENCH_REPEATS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(5);

        let (col, _tempdir, _col_path) = if let Ok(source) =
            std::env::var("ANKI_TIMES_BENCH_COLLECTION")
        {
            let tempdir = tempdir()?;
            let col_path = tempdir.path().join("bench.anki2");
            std::fs::copy(&source, &col_path)?;
            let mut builder = CollectionBuilder::new(&col_path);
            builder.with_desktop_media_paths();
            (builder.build()?, tempdir, col_path)
        } else {
            let (col, tempdir, col_path) = temp_collection("times-bench")?;
            let base_ts: i64 = 1_700_000_000_000;
            col.storage.db.execute_batch("BEGIN")?;
            for card_idx in 0..card_count {
                let card_id = (card_idx + 1) as i64;
                col.storage.db.execute(
                    "INSERT INTO cards (id, nid, did, ord, mod, usn, type, queue, due, ivl, \
                         factor, reps, lapses, left, odue, odid, flags, data) \
                         VALUES (?1, 1, 1, 0, 0, 0, 2, 2, 0, 30, 2500, ?2, 0, 0, 0, 0, 0, '')",
                    params![card_id, reviews_per_card],
                )?;
                for rev_idx in 0..reviews_per_card {
                    let revlog_id = base_ts + (card_idx * reviews_per_card + rev_idx) as i64;
                    col.storage.db.execute(
                            "INSERT INTO revlog (id, cid, usn, ease, ivl, lastIvl, factor, time, type) \
                             VALUES (?1, ?2, 0, 2, 30, 1, 2500, 10000, 1)",
                            params![revlog_id, card_id],
                        )?;
                }
            }
            col.storage.db.execute_batch("COMMIT")?;
            (col, tempdir, col_path)
        };

        let card_ids: Vec<CardId> = if std::env::var("ANKI_TIMES_BENCH_COLLECTION").is_ok() {
            let mut stmt = col
                .storage
                .db
                .prepare("SELECT id FROM cards WHERE queue = 2 LIMIT ?1")?;
            let ids: Vec<CardId> = stmt
                .query_map([card_count as i64], |row| row.get(0))?
                .collect::<std::result::Result<Vec<_>, _>>()?;
            ids
        } else {
            (1..=card_count as i64).map(CardId).collect()
        };

        println!(
            "times_of_last_review_benchmark: cards={} reviews_per_card={} repeats={}",
            card_ids.len(),
            reviews_per_card,
            repeats,
        );

        // Benchmark: new GROUP BY approach (current)
        let mut group_by_total_ms = 0.0;
        let mut group_by_result = HashMap::new();
        for _ in 0..repeats {
            let start = Instant::now();
            group_by_result = col.storage.times_of_last_review(&card_ids)?;
            group_by_total_ms += start.elapsed().as_secs_f64() * 1000.0;
        }

        // Benchmark: old correlated subquery approach
        let mut correlated_total_ms = 0.0;
        let mut correlated_result = HashMap::new();
        for _ in 0..repeats {
            let start = Instant::now();
            correlated_result = times_of_last_review_correlated(&col.storage, &card_ids)?;
            correlated_total_ms += start.elapsed().as_secs_f64() * 1000.0;
        }

        println!(
            "  GROUP BY:   total={:.1}ms avg={:.2}ms results={}",
            group_by_total_ms,
            group_by_total_ms / repeats as f64,
            group_by_result.len(),
        );
        println!(
            "  CORRELATED: total={:.1}ms avg={:.2}ms results={}",
            correlated_total_ms,
            correlated_total_ms / repeats as f64,
            correlated_result.len(),
        );
        println!(
            "  Speedup: {:.1}x",
            correlated_total_ms / group_by_total_ms.max(0.001),
        );

        assert_eq!(group_by_result.len(), correlated_result.len());
        for (card_id, group_by_time) in &group_by_result {
            assert_eq!(
                correlated_result.get(card_id),
                Some(group_by_time),
                "mismatch for card_id={card_id:?}",
            );
        }
        Ok(())
    }

    fn times_of_last_review_correlated(
        storage: &SqliteStorage,
        card_ids: &[CardId],
    ) -> Result<HashMap<CardId, TimestampSecs>> {
        if card_ids.is_empty() {
            return Ok(HashMap::new());
        }
        let mut ids = String::new();
        ids_to_string(&mut ids, card_ids);
        let sql = format!(
            "select id, (select revlog.id / 1000 from revlog \
             where cid = cards.id and ease between 1 and 4 \
             and (type != 3 or factor != 0) \
             order by revlog.id desc limit 1) from cards where id in {ids}"
        );
        let mut review_times = HashMap::new();
        let mut stmt = storage.db.prepare(&sql)?;
        let mut rows = stmt.query([])?;
        while let Some(row) = rows.next()? {
            let cid: CardId = row.get(0)?;
            let last_review_time: Option<TimestampSecs> = row.get(1)?;
            if let Some(time) = last_review_time {
                review_times.insert(cid, time);
            }
        }
        Ok(review_times)
    }
}
