// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::collections::HashMap;

use rusqlite::Row;

use super::row_to_new_card;
use super::NewCardSorting;
use crate::deckconfig::SuspendedCardOrder;
use crate::prelude::*;
use crate::scheduler::queue::NewCard;
use crate::scheduler::timing::SchedTimingToday;
use crate::storage::comma_separated_ids;
use crate::storage::SqliteStorage;

/// Suspended candidates keep their queue until answered. A sibling buried by
/// an answer today must therefore be excluded without changing its queue.
pub(crate) fn learnable_suspended_filter(
    scope: impl Iterator<Item = DeckId>,
    decks: &HashMap<DeckId, Deck>,
    configs: &HashMap<DeckConfigId, DeckConfig>,
    timing: SchedTimingToday,
) -> Option<String> {
    let config = |id: &DeckId| decks.get(id)?.config_id().and_then(|id| configs.get(&id));
    let mut included: Vec<_> = scope
        .filter(|id| config(id).is_some_and(|c| c.inner.include_suspended_cards_in_learnable_pool))
        .collect();
    if included.is_empty() {
        return None;
    }
    included.sort_unstable();
    let mut filter = format!("queue = -1 and did in ({})", comma_separated_ids(&included));
    let mut burying: Vec<_> = decks
        .keys()
        .copied()
        .filter(|id| config(id).is_some_and(|c| c.inner.bury_new))
        .collect();
    if !burying.is_empty() {
        burying.sort_unstable();
        filter.push_str(&format!(
            " and id not in (
                select sibling.id from revlog r
                join cards answered on answered.id = r.cid
                join cards sibling on sibling.nid = answered.nid and sibling.id != answered.id
                where r.id >= {} and r.id < {}
                  and r.ease between 1 and 4 and (r.type != 3 or r.factor != 0)
                  and (case when answered.odid = 0 then answered.did else answered.odid end) in ({})
                  and sibling.queue = -1)",
            timing.next_day_at.adding_secs(-86_400).as_millis().0,
            timing.next_day_at.as_millis().0,
            comma_separated_ids(&burying),
        ));
    }
    Some(filter)
}

impl SqliteStorage {
    /// Stream the two groups independently so daily limits and sibling
    /// burying can be applied before selecting the next group's card.
    pub(crate) fn with_new_card_candidates(
        &self,
        order: NewCardSorting,
        by_deck: bool,
        suspended_filter: Option<&str>,
        visit: impl FnOnce(
            &mut dyn Iterator<Item = Result<NewCard>>,
            &mut dyn Iterator<Item = Result<NewCard>>,
        ) -> Result<()>,
    ) -> Result<()> {
        let query = |filter: &str, suspended| {
            let deck_order = if by_deck {
                "(select name from decks where id = did) collate unicase, "
            } else {
                ""
            };
            format!(
                "{} and {filter} order by {deck_order}{}",
                include_str!("active_new_cards.sql"),
                order.sql(suspended)
            )
        };
        let mut new_stmt = self.db.prepare_cached(&query("queue = 0", false))?;
        let mut new_cards =
            new_stmt.query_and_then([], |row: &Row| Ok(row_to_new_card(row, false)?))?;
        if let Some(filter) = suspended_filter {
            let mut suspended_stmt = self.db.prepare_cached(&query(filter, true))?;
            let mut suspended =
                suspended_stmt.query_and_then([], |row| Ok(row_to_new_card(row, true)?))?;
            visit(&mut new_cards, &mut suspended)
        } else {
            visit(&mut new_cards, &mut std::iter::empty())
        }
    }

    pub(crate) fn for_each_new_card_by_field(
        &self,
        field: &str,
        descending: bool,
        group_order: SuspendedCardOrder,
        suspended_filter: Option<&str>,
        mut visit: impl FnMut(NewCard) -> Result<bool>,
    ) -> Result<()> {
        let candidates = match suspended_filter {
            Some(filter) => format!("queue = 0 or ({filter})"),
            None => "queue = 0".into(),
        };
        let group_order = match group_order {
            SuspendedCardOrder::Mixed => "",
            SuspendedCardOrder::SuspendedBeforeNew => "suspended desc,",
            SuspendedCardOrder::NewBeforeSuspended => "suspended asc,",
        };
        let direction = if descending { "desc" } else { "asc" };
        let sql = include_str!("new_cards_by_field.sql")
            .replace("CANDIDATE_FILTER", &candidates)
            .replace("GROUP_ORDER", group_order)
            .replace("FIELD_DIRECTION", direction);
        let mut stmt = self.db.prepare_cached(&sql)?;
        let mut rows = stmt.query([field])?;
        while let Some(row) = rows.next()? {
            if !visit(row_to_new_card(row, row.get(6)?)?)? {
                break;
            }
        }
        Ok(())
    }

    pub(crate) fn learnable_suspended_counts(&self, filter: &str) -> Result<Vec<(DeckId, u32)>> {
        self.db
            .prepare_cached(&format!(
                "select did, count(*) from cards where {filter} group by did"
            ))?
            .query_and_then([], |row| Ok((row.get(0)?, row.get(1)?)))?
            .collect()
    }

    pub(crate) fn have_learnable_suspended_cards(&self, filter: &str) -> Result<bool> {
        Ok(self.db.query_row(
            &format!("select exists(select 1 from cards where {filter})"),
            [],
            |row| row.get(0),
        )?)
    }
}
