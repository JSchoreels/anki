// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::collections::HashMap;
use std::collections::HashSet;
use std::iter::Peekable;
use std::ops::AddAssign;

pub use anki_proto::decks::set_deck_collapsed_request::Scope as DeckCollapseScope;
use anki_proto::decks::DeckTreeNode;
use serde_tuple::Serialize_tuple;
use unicase::UniCase;

use super::limits::remaining_limits_map;
use super::limits::RemainingLimits;
use super::DueCounts;
use crate::ops::OpOutput;
use crate::prelude::*;
use crate::undo::Op;

fn deck_names_to_tree(names: impl Iterator<Item = (DeckId, String)>) -> DeckTreeNode {
    let mut top = DeckTreeNode::default();
    let mut it = names.peekable();

    add_child_nodes(&mut it, &mut top);

    top
}

fn add_child_nodes(
    names: &mut Peekable<impl Iterator<Item = (DeckId, String)>>,
    parent: &mut DeckTreeNode,
) {
    while let Some((id, name)) = names.peek() {
        let split_name: Vec<_> = name.split("::").collect();
        // protobuf refuses to decode messages with 100+ levels of nesting, and
        // broken collections with such nesting have been found in the wild
        let capped_len = split_name.len().min(99) as u32;
        match capped_len {
            l if l <= parent.level => {
                // next item is at a higher level
                return;
            }
            l if l == parent.level + 1 => {
                // next item is an immediate descendent of parent
                parent.children.push(DeckTreeNode {
                    deck_id: id.0,
                    name: (*split_name.last().unwrap()).into(),
                    children: vec![],
                    level: parent.level + 1,
                    ..Default::default()
                });
                names.next();
            }
            _ => {
                // next item is at a lower level
                if let Some(last_child) = parent.children.last_mut() {
                    add_child_nodes(names, last_child)
                } else {
                    // immediate parent is missing, skip the deck until a DB check is run
                    names.next();
                }
            }
        }
    }
}

fn add_collapsed_and_filtered(
    node: &mut DeckTreeNode,
    decks: &HashMap<DeckId, Deck>,
    browser: bool,
) {
    if let Some(deck) = decks.get(&DeckId(node.deck_id)) {
        node.collapsed = if browser {
            deck.common.browser_collapsed
        } else {
            deck.common.study_collapsed
        };
        node.filtered = deck.is_filtered();
    }
    for child in &mut node.children {
        add_collapsed_and_filtered(child, decks, browser);
    }
}

fn add_counts(node: &mut DeckTreeNode, counts: &HashMap<DeckId, DueCounts>) {
    if let Some(counts) = counts.get(&DeckId(node.deck_id)) {
        node.new_count = counts.new;
        node.review_count = counts.review;
        node.learn_count = counts.learning;
        node.intraday_learning = counts.intraday_learning;
        node.interday_learning_uncapped = counts.interday_learning;
        node.new_uncapped = counts.new;
        node.review_uncapped = counts.review;
        node.total_in_deck = counts.total_cards;
    }
    for child in &mut node.children {
        add_counts(child, counts);
    }
}

/// A temporary container used during count summation and limit application.
#[derive(Default, Clone)]
struct NodeCountsV3 {
    new: u32,
    review: u32,
    intraday_learning: u32,
    interday_learning: u32,
    total: u32,
}

impl NodeCountsV3 {
    fn capped(&self, remaining: &RemainingLimits) -> Self {
        let mut capped = self.clone();
        // apply review limit to interday learning
        capped.interday_learning = capped.interday_learning.min(remaining.review);
        let mut remaining_reviews = remaining.review.saturating_sub(capped.interday_learning);
        // any remaining review limit is applied to reviews
        capped.review = capped.review.min(remaining_reviews);
        capped.new = capped.new.min(remaining.new);
        if remaining.cap_new_to_review {
            remaining_reviews = remaining_reviews.saturating_sub(capped.review);
            capped.new = capped.new.min(remaining_reviews);
        }
        capped
    }
}

impl AddAssign for NodeCountsV3 {
    fn add_assign(&mut self, rhs: Self) {
        self.new += rhs.new;
        self.review += rhs.review;
        self.intraday_learning += rhs.intraday_learning;
        self.interday_learning += rhs.interday_learning;
        self.total += rhs.total;
    }
}

/// Adjust new, review and learning counts based on the daily limits.
/// As part of this process, the separate interday and intraday learning
/// counts are combined after the limits have been applied.
fn sum_counts_and_apply_limits_v3(
    node: &mut DeckTreeNode,
    limits: &HashMap<DeckId, RemainingLimits>,
    mut parent_limits: Option<RemainingLimits>,
) -> NodeCountsV3 {
    let mut remaining = limits
        .get(&DeckId(node.deck_id))
        .copied()
        .unwrap_or_default();
    if let Some(parent_remaining) = parent_limits {
        remaining.cap_to(parent_remaining);
        parent_limits.replace(remaining);
    }

    // initialize with this node's values
    let mut this_node_uncapped = NodeCountsV3 {
        new: node.new_count,
        review: node.review_count,
        intraday_learning: node.intraday_learning,
        interday_learning: node.interday_learning_uncapped,
        total: node.total_in_deck,
    };
    let mut total_including_children = node.total_in_deck;

    // add capped child counts / uncapped total
    for child in &mut node.children {
        this_node_uncapped += sum_counts_and_apply_limits_v3(child, limits, parent_limits);
        total_including_children += child.total_including_children;
    }

    let this_node_capped = this_node_uncapped.capped(&remaining);

    node.new_count = this_node_capped.new;
    node.review_count = this_node_capped.review;
    node.learn_count = this_node_capped.intraday_learning + this_node_capped.interday_learning;
    node.total_including_children = total_including_children;

    this_node_capped
}

fn hide_default_deck(node: &mut DeckTreeNode) {
    for (idx, child) in node.children.iter().enumerate() {
        // we can hide the default if it has no children
        if child.deck_id == 1 && child.children.is_empty() {
            if child.level == 1 && node.children.len() == 1 {
                // can't remove if there are no other decks
            } else {
                // safe to remove
                _ = node.children.remove(idx);
            }
            return;
        }
    }
}

/// Locate provided deck in tree, and return it.
pub fn get_deck_in_tree(tree: DeckTreeNode, deck_id: DeckId) -> Option<DeckTreeNode> {
    if tree.deck_id == deck_id.0 {
        return Some(tree);
    }
    for child in tree.children {
        if let Some(node) = get_deck_in_tree(child, deck_id) {
            return Some(node);
        }
    }

    None
}

pub(crate) fn sum_deck_tree_node<T: AddAssign>(
    node: &DeckTreeNode,
    map: fn(&DeckTreeNode) -> T,
) -> T {
    let mut output = map(node);
    for child in &node.children {
        output += sum_deck_tree_node(child, map)
    }
    output
}

#[derive(Serialize_tuple)]
pub(crate) struct LegacyDueCounts {
    name: String,
    deck_id: i64,
    review: u32,
    learn: u32,
    new: u32,
    children: Vec<LegacyDueCounts>,
}

impl From<DeckTreeNode> for LegacyDueCounts {
    fn from(n: DeckTreeNode) -> Self {
        LegacyDueCounts {
            name: n.name,
            deck_id: n.deck_id,
            review: n.review_count,
            learn: n.learn_count,
            new: n.new_count,
            children: n.children.into_iter().map(From::from).collect(),
        }
    }
}

impl Collection {
    /// Get the deck tree.
    /// - If `timestamp` is provided, due counts for the provided timestamp will
    ///   be populated.
    /// - Buried cards from previous days will be unburied if necessary. Because
    ///   this does not happen for future stamps, future due numbers may not be
    ///   accurate.
    pub fn deck_tree(&mut self, timestamp: Option<TimestampSecs>) -> Result<DeckTreeNode> {
        let names = self.storage.get_all_deck_names()?;
        let mut tree = deck_names_to_tree(names.into_iter());

        let decks_map = self.storage.get_decks_map()?;

        add_collapsed_and_filtered(&mut tree, &decks_map, timestamp.is_none());
        if self.default_deck_is_empty()? {
            hide_default_deck(&mut tree);
        }

        if let Some(timestamp) = timestamp {
            // cards buried on previous days need to be unburied for the current
            // day's counts to be accurate
            let timing_today = self.timing_today()?;
            self.unbury_if_day_rolled_over(timing_today)?;

            let timing_at_stamp = self.timing_for_timestamp(timestamp)?;
            let days_elapsed = timing_at_stamp.days_elapsed;
            let learn_cutoff = (timestamp.0 as u32) + self.learn_ahead_secs();
            let new_cards_ignore_review_limit =
                self.get_config_bool(BoolKey::NewCardsIgnoreReviewLimit);
            let parent_limits = self
                .get_config_bool(BoolKey::ApplyAllParentLimits)
                .then(Default::default);
            let dconf = self.storage.get_deck_config_map()?;
            let mut counts = self.due_counts(days_elapsed, learn_cutoff)?;
            self.apply_rwkv_review_queue_counts(&mut counts, &decks_map, &dconf, timing_at_stamp)?;
            add_counts(&mut tree, &counts);
            let limits = remaining_limits_map(
                decks_map.values(),
                &dconf,
                days_elapsed,
                new_cards_ignore_review_limit,
            );
            sum_counts_and_apply_limits_v3(&mut tree, &limits, parent_limits);
        }

        Ok(tree)
    }

    pub fn current_deck_tree(&mut self) -> Result<Option<DeckTreeNode>> {
        let target = self.get_current_deck_id();
        let tree = self.deck_tree(Some(TimestampSecs::now()))?;
        Ok(get_deck_in_tree(tree, target))
    }

    pub fn set_deck_collapsed(
        &mut self,
        did: DeckId,
        collapsed: bool,
        scope: DeckCollapseScope,
    ) -> Result<OpOutput<()>> {
        self.transact(Op::SkipUndo, |col| {
            if let Some(mut deck) = col.storage.get_deck(did)? {
                let original = deck.clone();
                let c = &mut deck.common;
                match scope {
                    DeckCollapseScope::Reviewer => c.study_collapsed = collapsed,
                    DeckCollapseScope::Browser => c.browser_collapsed = collapsed,
                };
                col.update_deck_inner(&mut deck, original, col.usn()?)?;
            }
            Ok(())
        })
    }
}

impl Collection {
    pub(crate) fn legacy_deck_tree(&mut self) -> Result<LegacyDueCounts> {
        let tree = self.deck_tree(Some(TimestampSecs::now()))?;
        Ok(LegacyDueCounts::from(tree))
    }

    pub(crate) fn add_missing_deck_names(&mut self, names: &[(DeckId, String)]) -> Result<usize> {
        let mut parents = HashSet::new();
        let mut missing = 0;
        for (_id, name) in names {
            parents.insert(UniCase::new(name.as_str()));
            if let Some((immediate_parent, _)) = name.rsplit_once("::") {
                let immediate_parent_uni = UniCase::new(immediate_parent);
                if !parents.contains(&immediate_parent_uni) {
                    self.get_or_create_normal_deck(immediate_parent)?;
                    parents.insert(immediate_parent_uni);
                    missing += 1;
                }
            }
        }
        Ok(missing)
    }
}

#[cfg(test)]
mod test {
    use std::collections::HashMap;

    use super::*;
    use crate::card::CardQueue;
    use crate::card::CardType;
    use crate::deckconfig::DeckConfigId;
    use crate::deckconfig::ReviewCardOrder;
    use crate::error::Result;

    #[test]
    fn wellformed() -> Result<()> {
        let mut col = Collection::new();

        col.get_or_create_normal_deck("1")?;
        col.get_or_create_normal_deck("2")?;
        col.get_or_create_normal_deck("2::a")?;
        col.get_or_create_normal_deck("2::b")?;
        col.get_or_create_normal_deck("2::c")?;
        col.get_or_create_normal_deck("2::c::A")?;
        col.get_or_create_normal_deck("3")?;

        let tree = col.deck_tree(None)?;

        assert_eq!(tree.children.len(), 3);

        assert_eq!(tree.children[1].name, "2");
        assert_eq!(tree.children[1].children[0].name, "a");
        assert_eq!(tree.children[1].children[2].name, "c");
        assert_eq!(tree.children[1].children[2].children[0].name, "A");

        Ok(())
    }

    #[test]
    fn malformed() -> Result<()> {
        let mut col = Collection::new();

        col.get_or_create_normal_deck("1")?;
        col.get_or_create_normal_deck("2::3::4")?;

        // remove the top parent and middle parent
        col.storage.remove_deck(col.get_deck_id("2")?.unwrap())?;
        col.storage.remove_deck(col.get_deck_id("2::3")?.unwrap())?;

        let tree = col.deck_tree(None)?;
        assert_eq!(tree.children.len(), 1);

        Ok(())
    }

    #[test]
    fn counts() -> Result<()> {
        let mut col = Collection::new();

        let mut parent_deck = col.get_or_create_normal_deck("Default")?;
        let mut child_deck = col.get_or_create_normal_deck("Default::one")?;

        // add some new cards
        let nt = col.get_notetype_by_name("Cloze")?.unwrap();
        let mut note = nt.new_note();
        note.set_field(0, "{{c1::}} {{c2::}} {{c3::}} {{c4::}}")?;
        col.add_note(&mut note, child_deck.id)?;

        let tree = col.deck_tree(Some(TimestampSecs::now()))?;
        assert_eq!(tree.children[0].new_count, 4);
        assert_eq!(tree.children[0].children[0].new_count, 4);

        // simulate answering a card
        child_deck.common.new_studied = 1;
        col.add_or_update_deck(&mut child_deck)?;
        parent_deck.common.new_studied = 1;
        col.add_or_update_deck(&mut parent_deck)?;

        // with the default limit of 20, there should still be 4 due
        let tree = col.deck_tree(Some(TimestampSecs::now()))?;
        assert_eq!(tree.children[0].new_count, 4);
        assert_eq!(tree.children[0].children[0].new_count, 4);

        // set the limit to 4, which should mean 3 are left
        let mut conf = col.get_deck_config(DeckConfigId(1), false)?.unwrap();
        conf.inner.new_per_day = 4;
        col.add_or_update_deck_config(&mut conf)?;

        let tree = col.deck_tree(Some(TimestampSecs::now()))?;
        assert_eq!(tree.children[0].new_count, 3);
        assert_eq!(tree.children[0].children[0].new_count, 3);

        Ok(())
    }

    #[test]
    fn nested_counts_v3() -> Result<()> {
        fn create_deck_with_new_limit(col: &mut Collection, name: &str, new_limit: u32) -> Deck {
            let mut deck = col.get_or_create_normal_deck(name).unwrap();
            let mut conf = DeckConfig::default();
            conf.inner.new_per_day = new_limit;
            col.add_or_update_deck_config(&mut conf).unwrap();
            deck.normal_mut().unwrap().config_id = conf.id.0;
            col.add_or_update_deck(&mut deck).unwrap();
            deck
        }

        let mut col = Collection::new();

        let parent_deck = create_deck_with_new_limit(&mut col, "Default", 8);
        let child_deck = create_deck_with_new_limit(&mut col, "Default::child", 4);
        let grandchild_1 = create_deck_with_new_limit(&mut col, "Default::child::grandchild_1", 2);
        let grandchild_2 = create_deck_with_new_limit(&mut col, "Default::child::grandchild_2", 1);

        // add 2 new cards to each deck
        let nt = col.get_notetype_by_name("Cloze")?.unwrap();
        let mut note = nt.new_note();
        note.set_field(0, "{{c1::}} {{c2::}}")?;
        col.add_note(&mut note, parent_deck.id)?;
        note.id.0 = 0;
        col.add_note(&mut note, child_deck.id)?;
        note.id.0 = 0;
        col.add_note(&mut note, grandchild_1.id)?;
        note.id.0 = 0;
        col.add_note(&mut note, grandchild_2.id)?;

        let parent = &col.deck_tree(Some(TimestampSecs::now()))?.children[0];
        // grandchildren: own cards, limited by own new limits
        assert_eq!(parent.children[0].children[0].new_count, 2);
        assert_eq!(parent.children[0].children[1].new_count, 1);
        // child: cards from self and children, limited by own new limit
        assert_eq!(parent.children[0].new_count, 4);
        // parent: cards from self and all subdecks, all limits in the hierarchy are
        // respected
        assert_eq!(parent.new_count, 6);
        assert_eq!(parent.total_including_children, 8);
        assert_eq!(parent.total_in_deck, 2);

        Ok(())
    }

    fn add_review_card(
        col: &mut Collection,
        deck_id: DeckId,
        due: i32,
        desired_retention: f32,
        last_review_time: Option<TimestampSecs>,
    ) -> Result<CardId> {
        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note = nt.new_note();
        note.set_field(0, "foo")?;
        col.add_note(&mut note, deck_id)?;
        let mut card = col.storage.get_card_by_ordinal(note.id, 0)?.unwrap();
        card.ctype = CardType::Review;
        card.queue = CardQueue::Review;
        card.due = due;
        card.interval = 1;
        card.desired_retention = Some(desired_retention);
        card.last_review_time = last_review_time;
        col.storage.update_card(&card)?;
        Ok(card.id)
    }

    fn enable_rwkv_review_counts(
        col: &mut Collection,
        deck: &mut Deck,
        allow_same_day_review: bool,
    ) -> Result<()> {
        let mut conf = DeckConfig::default();
        conf.inner.review_order = ReviewCardOrder::Day as i32;
        conf.inner.rwkv_review_enabled = true;
        conf.inner.rwkv_review_instant_order_enabled = true;
        conf.inner.rwkv_review_allow_same_day_review = allow_same_day_review;
        col.add_or_update_deck_config(&mut conf)?;
        deck.normal_mut().unwrap().config_id = conf.id.0;
        col.add_or_update_deck(deck)?;
        Ok(())
    }

    #[test]
    fn rwkv_deck_tree_counts_exclude_ineligible_scored_reviews() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        enable_rwkv_review_counts(&mut col, &mut deck, false)?;
        let timing = col.timing_today()?;

        let high_r_due =
            add_review_card(&mut col, deck.id, timing.days_elapsed as i32, 0.75, None)?;
        add_review_card(&mut col, deck.id, timing.days_elapsed as i32, 0.75, None)?;
        col.set_rwkv_review_queue_scores(deck.id, HashMap::from([(high_r_due, 0.80)]))?;

        let tree = col.deck_tree(Some(timing.now))?;
        assert_eq!(tree.children[0].review_count, 1);
        assert_eq!(tree.children[0].review_uncapped, 1);
        Ok(())
    }

    #[test]
    fn rwkv_deck_tree_counts_include_future_eligible_scored_reviews() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        enable_rwkv_review_counts(&mut col, &mut deck, false)?;
        let timing = col.timing_today()?;

        let future_low_r = add_review_card(
            &mut col,
            deck.id,
            timing.days_elapsed as i32 + 7,
            0.75,
            None,
        )?;
        col.set_rwkv_review_queue_scores(deck.id, HashMap::from([(future_low_r, 0.20)]))?;

        let tree = col.deck_tree(Some(timing.now))?;
        assert_eq!(tree.children[0].review_count, 1);
        assert_eq!(tree.children[0].review_uncapped, 1);
        Ok(())
    }

    #[test]
    fn rwkv_deck_tree_counts_follow_same_day_toggle() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        let timing = col.timing_today()?;

        let reviewed_today = add_review_card(
            &mut col,
            deck.id,
            timing.days_elapsed as i32,
            0.75,
            Some(timing.now),
        )?;
        enable_rwkv_review_counts(&mut col, &mut deck, false)?;
        col.set_rwkv_review_queue_scores(deck.id, HashMap::from([(reviewed_today, 0.20)]))?;

        let tree = col.deck_tree(Some(timing.now))?;
        assert_eq!(tree.children[0].review_count, 0);
        assert_eq!(tree.children[0].review_uncapped, 0);

        enable_rwkv_review_counts(&mut col, &mut deck, true)?;
        let tree = col.deck_tree(Some(timing.now))?;
        assert_eq!(tree.children[0].review_count, 1);
        assert_eq!(tree.children[0].review_uncapped, 1);
        Ok(())
    }

    #[test]
    fn rwkv_deck_tree_counts_accumulate_disjoint_deck_browser_scopes() -> Result<()> {
        let mut col = Collection::new();
        let mut first_deck = col.get_or_create_normal_deck("First")?;
        let mut second_deck = col.get_or_create_normal_deck("Second")?;
        enable_rwkv_review_counts(&mut col, &mut first_deck, false)?;
        enable_rwkv_review_counts(&mut col, &mut second_deck, false)?;
        let timing = col.timing_today()?;

        let first_high_r = add_review_card(
            &mut col,
            first_deck.id,
            timing.days_elapsed as i32,
            0.75,
            None,
        )?;
        add_review_card(
            &mut col,
            first_deck.id,
            timing.days_elapsed as i32,
            0.75,
            None,
        )?;
        let second_high_r = add_review_card(
            &mut col,
            second_deck.id,
            timing.days_elapsed as i32,
            0.75,
            None,
        )?;
        add_review_card(
            &mut col,
            second_deck.id,
            timing.days_elapsed as i32,
            0.75,
            None,
        )?;

        col.set_rwkv_deck_count_scores(first_deck.id, HashMap::from([(first_high_r, 0.80)]))?;
        col.set_rwkv_deck_count_scores(second_deck.id, HashMap::from([(second_high_r, 0.80)]))?;

        let tree = col.deck_tree(Some(timing.now))?;
        let first = get_deck_in_tree(tree.clone(), first_deck.id).unwrap();
        let second = get_deck_in_tree(tree, second_deck.id).unwrap();
        assert_eq!(first.review_count, 1);
        assert_eq!(second.review_count, 1);

        col.clear_rwkv_deck_count_scores();
        let tree = col.deck_tree(Some(timing.now))?;
        let first = get_deck_in_tree(tree.clone(), first_deck.id).unwrap();
        let second = get_deck_in_tree(tree, second_deck.id).unwrap();
        assert_eq!(first.review_count, 2);
        assert_eq!(second.review_count, 2);
        Ok(())
    }
}
