// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::cmp::Ordering;
use std::hash::Hasher;

use fnv::FnvHasher;

use super::NewCard;
use super::NewCardGatherPriority;
use super::NewCardSortOrder;
use super::QueueBuilder;

impl QueueBuilder {
    pub(super) fn sort_new(&mut self) {
        if matches!(
            self.context.sort_options.new_gather_priority,
            NewCardGatherPriority::AscendingRetrievability
                | NewCardGatherPriority::DescendingRetrievability
                | NewCardGatherPriority::NoteFieldAscending
                | NewCardGatherPriority::NoteFieldDescending
        ) {
            return;
        }

        let order = self.context.sort_options.new_order;
        if order == NewCardSortOrder::NoSort {
            return;
        }
        let salt = i64::from(self.context.timing.days_elapsed);
        if self.new.iter().any(|card| card.suspended) && self.new.iter().any(|card| !card.suspended)
        {
            // Preserve the group chosen for each daily slot, even when the
            // display order shuffles cards or groups them by template.
            let (mut suspended, mut new): (Vec<_>, Vec<_>) =
                self.new.iter().copied().partition(|card| card.suspended);
            sort_new_group(&mut suspended, order, salt);
            sort_new_group(&mut new, order, salt);
            let (mut suspended, mut new) = (suspended.into_iter(), new.into_iter());
            for slot in &mut self.new {
                *slot = if slot.suspended {
                    suspended.next()
                } else {
                    new.next()
                }
                .unwrap();
            }
        } else {
            sort_new_group(&mut self.new, order, salt);
        }
    }
}

fn sort_new_group(cards: &mut [NewCard], order: NewCardSortOrder, salt: i64) {
    match order {
        // preserve gather order
        NewCardSortOrder::NoSort => (),
        NewCardSortOrder::Template => {
            // stable sort to preserve gather order
            cards.sort_by_key(|a| a.template_index)
        }
        NewCardSortOrder::TemplateThenRandom => {
            cards
                .iter_mut()
                .for_each(|card| card.hash_id_with_salt(salt));
            cards.sort_unstable_by(cmp_template_then_hash);
        }
        NewCardSortOrder::RandomNoteThenTemplate => {
            cards
                .iter_mut()
                .for_each(|card| card.hash_note_id_with_salt(salt));
            cards.sort_unstable_by(cmp_hash_then_template);
        }
        NewCardSortOrder::RandomCard => {
            cards
                .iter_mut()
                .for_each(|card| card.hash_id_with_salt(salt));
            cards.sort_unstable_by(cmp_hash)
        }
    }
}

fn cmp_hash(a: &NewCard, b: &NewCard) -> Ordering {
    a.hash.cmp(&b.hash)
}

fn cmp_template_then_hash(a: &NewCard, b: &NewCard) -> Ordering {
    (a.template_index, a.hash).cmp(&(b.template_index, b.hash))
}

fn cmp_hash_then_template(a: &NewCard, b: &NewCard) -> Ordering {
    (a.hash, a.template_index).cmp(&(b.hash, b.template_index))
}

// We sort based on a hash so that if the queue is rebuilt, remaining
// cards come back in the same approximate order (mixing + due learning cards
// may still result in a different card)

impl NewCard {
    fn hash_id_with_salt(&mut self, salt: i64) {
        let mut hasher = FnvHasher::default();
        hasher.write_i64(self.id.0);
        hasher.write_i64(salt);
        self.hash = hasher.finish();
    }

    fn hash_note_id_with_salt(&mut self, salt: i64) {
        let mut hasher = FnvHasher::default();
        hasher.write_i64(self.note_id.0);
        hasher.write_i64(salt);
        self.hash = hasher.finish();
    }
}
