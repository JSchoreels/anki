// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use anki_proto::scheduler::rwkv_historical_review_inputs_response::Review;
use anki_proto::scheduler::RwkvHistoricalReviewIdentity;
use anki_proto::scheduler::RwkvHistoricalReviewInputsRequest;
use anki_proto::scheduler::RwkvHistoricalReviewInputsResponse;
use anki_proto::scheduler::RwkvHistoricalReviewMetadata;

use super::*;

impl Collection {
    pub(crate) fn rwkv_historical_review_inputs(
        &mut self,
        input: RwkvHistoricalReviewInputsRequest,
    ) -> Result<RwkvHistoricalReviewInputsResponse> {
        if let Some(age) = input.recovery_checkpoint_max_age_millis {
            require!(age >= 0, "negative RWKV recovery checkpoint age");
        }
        let mut output = RwkvHistoricalReviewInputsResponse::default();
        let mut metadata = RwkvHistoricalReviewMetadata::default();
        let fingerprint = self.visit_rwkv_historical_reviews(
            input.history.unwrap_or_default(),
            RwkvHistoricalReplayOptions {
                preserved_learning_start_cutoffs: input.preserved_learning_start_cutoffs,
                first_review_uses_creation: input.first_review_uses_creation,
                recovery_checkpoint_max_age_millis: input.recovery_checkpoint_max_age_millis,
                card_id: None,
            },
            |review, hash, is_checkpoint| {
                let row = review.row;
                output.reviews.push(historical_review(&review));
                metadata
                    .previous_review_id_by_card
                    .insert(row.card_id, row.review_id);
                metadata
                    .previous_interval_days_by_card
                    .insert(row.card_id, row.interval_days);
                *metadata
                    .review_count_by_card
                    .entry(row.card_id)
                    .or_default() += 1;
                if is_checkpoint {
                    // Capture the maps after this review, before later reviews
                    // advance them. The traversal supplies this prefix's hash.
                    let mut checkpoint = metadata.clone();
                    checkpoint.identity = Some(RwkvHistoricalReviewIdentity {
                        last_review_id: row.review_id,
                        review_count: output.reviews.len() as u64,
                        history_hash: rwkv_history_hash_hex(hash),
                    });
                    output.recovery_checkpoint = Some(checkpoint);
                }
            },
        )?;
        metadata.identity = Some(RwkvHistoricalReviewIdentity {
            last_review_id: fingerprint.last_review_id,
            review_count: fingerprint.review_count,
            history_hash: fingerprint.history_hash,
        });
        output.metadata = Some(metadata);
        output.active_ignored_review_ids = fingerprint.active_ignored_review_ids;
        Ok(output)
    }
}

pub(super) fn historical_review(review: &RwkvHistoricalFingerprintReview) -> Review {
    let row = review.row;
    Review {
        review_id: row.review_id,
        card_id: row.card_id,
        note_id: row.note_id,
        deck_id: row.deck_id,
        preset_id: review.stable_preset_id,
        ease: row.ease,
        duration_millis: row.duration_millis,
        review_kind: row.review_kind,
        interval_days: row.interval_days,
        ease_factor: row.ease_factor,
        // The desktop's legacy card_type field holds RwkvReviewState:
        // zero marks learning start; other states are revlog kind + 1.
        card_type: if row.is_learning_start {
            0
        } else {
            row.review_kind + 1
        },
        day_offset: review.day_offset,
        elapsed_days: review.elapsed_days,
        elapsed_seconds: review.elapsed_seconds,
    }
}

impl Collection {
    /// The newest of `card_id`'s reviews as the history replay sees it, and
    /// how the history identity `identity` reads once it is appended. None
    /// when appending isn't the same as a full replay: the review isn't newer
    /// than `identity`, or it restarted the card's learning history, which
    /// drops the card's earlier reviews from the history.
    pub(super) fn rwkv_historical_review_appended(
        &mut self,
        card_id: CardId,
        identity: &RwkvHistoricalReviewIdentity,
        history: RwkvHistoricalReviewFingerprintRequest,
    ) -> Result<Option<(Review, RwkvHistoricalReviewIdentity)>> {
        let mut newest = None;
        let _ = self.visit_rwkv_historical_reviews(
            history,
            RwkvHistoricalReplayOptions {
                card_id: Some(card_id),
                ..Default::default()
            },
            |review, _, _| newest = Some(review),
        )?;
        let Some(review) = newest.filter(|r| r.row.review_id > identity.last_review_id) else {
            return Ok(None);
        };
        if review.row.is_learning_start {
            let earlier: i64 = self.storage.db.query_row(
                "select count() from revlog where cid = ? and id < ?
                   and ease between 1 and 4 and type in (0, 1, 2, 3, 4, 5)
                   and not (type = 3 and factor = 0)",
                [card_id.0, review.row.review_id],
                |row| row.get(0),
            )?;
            if earlier > 0 {
                return Ok(None);
            }
        }
        let Ok(previous) =
            <[u8; 32]>::try_from(hex::decode(&identity.history_hash).unwrap_or_default())
        else {
            return Ok(None);
        };
        let identity = RwkvHistoricalReviewIdentity {
            last_review_id: review.row.review_id,
            review_count: identity.review_count + 1,
            history_hash: rwkv_history_hash_hex(rwkv_history_hash_after_review(previous, &review)),
        };
        Ok(Some((historical_review(&review), identity)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::notes::NoteId;
    use crate::revlog::RevlogEntry;
    use crate::revlog::RevlogReviewKind;

    #[test]
    fn historical_inputs_use_forget_boundary_before_relearning() -> Result<()> {
        let mut col = Collection::new();
        let mut card = Card::new(NoteId(10), 0, DeckId(1), 0);
        col.add_card(&mut card)?;
        let start = 1_700_000_000_000;
        let add_review = |col: &mut Collection, offset, kind, button, factor| {
            col.storage.add_revlog_entry(
                &RevlogEntry {
                    id: RevlogId(start + offset),
                    cid: card.id,
                    usn: Usn(0),
                    button_chosen: button,
                    interval: 1,
                    ease_factor: factor,
                    taken_millis: 1_000,
                    review_kind: kind,
                    ..Default::default()
                },
                false,
            )
        };
        add_review(&mut col, 0, RevlogReviewKind::Learning, 3, 2_500)?;
        add_review(&mut col, 1, RevlogReviewKind::Review, 3, 2_500)?;
        assert_eq!(
            col.rwkv_historical_review_inputs(Default::default())?
                .reviews
                .len(),
            2
        );

        // Forget writes a non-answer manual row with zero factor. It must
        // exclude the old sequence before the first new learning answer.
        add_review(&mut col, 2, RevlogReviewKind::Manual, 0, 0)?;
        assert!(col
            .rwkv_historical_review_inputs(Default::default())?
            .reviews
            .is_empty());
        assert_eq!(
            col.rwkv_historical_review_fingerprint(Default::default())?
                .review_count,
            0
        );
        let legacy_request = RwkvHistoricalReviewInputsRequest {
            preserved_learning_start_cutoffs: HashMap::from([(card.id.0, start + 1)]),
            ..Default::default()
        };
        assert!(col
            .rwkv_historical_review_inputs(legacy_request.clone())?
            .reviews
            .is_empty());

        add_review(&mut col, 3, RevlogReviewKind::Learning, 3, 2_500)?;
        // Grade Now answers may have zero factor; Set Due Date writes a
        // non-answer manual row with a nonzero factor. Neither is Forget.
        add_review(&mut col, 4, RevlogReviewKind::Manual, 3, 0)?;
        add_review(&mut col, 5, RevlogReviewKind::Manual, 0, 2_500)?;
        let history = col.rwkv_historical_review_inputs(Default::default())?;
        assert_eq!(
            history
                .reviews
                .iter()
                .map(|review| (review.review_id, review.card_type))
                .collect::<Vec<_>>(),
            vec![(start + 3, 0), (start + 4, 5)]
        );
        assert_eq!(
            col.rwkv_historical_review_inputs(legacy_request)?.reviews,
            history.reviews
        );
        assert_eq!(
            col.rwkv_historical_review_fingerprint(Default::default())?
                .review_count,
            2
        );
        Ok(())
    }

    #[test]
    fn historical_inputs_checkpoint_preserves_prefix_maps_and_identity() -> Result<()> {
        let mut col = Collection::new();
        let mut first = Card::new(NoteId(10), 0, DeckId(1), 0);
        let mut second = Card::new(NoteId(20), 0, DeckId(1), 0);
        col.add_card(&mut first)?;
        col.add_card(&mut second)?;
        let day = 86_400_000;
        let start = 1_700_000_000_000;
        let add_review = |col: &mut Collection, card: CardId, offset, interval, kind| {
            col.storage.add_revlog_entry(
                &RevlogEntry {
                    id: RevlogId(start + offset * day),
                    cid: card,
                    usn: Usn(0),
                    button_chosen: 3,
                    interval,
                    ease_factor: 2_500,
                    taken_millis: 1_000,
                    review_kind: kind,
                    ..Default::default()
                },
                false,
            )
        };
        add_review(&mut col, first.id, 0, 1, RevlogReviewKind::Learning)?;
        add_review(&mut col, second.id, 1, 2, RevlogReviewKind::Learning)?;
        add_review(&mut col, first.id, 2, 4, RevlogReviewKind::Review)?;
        let prefix = col.rwkv_historical_review_inputs(Default::default())?;
        add_review(&mut col, second.id, 9, 8, RevlogReviewKind::Review)?;
        add_review(&mut col, first.id, 10, 16, RevlogReviewKind::Review)?;

        let history = col.rwkv_historical_review_inputs(RwkvHistoricalReviewInputsRequest {
            recovery_checkpoint_max_age_millis: Some(8 * day),
            ..Default::default()
        })?;

        assert_eq!(history.reviews.len(), 5);
        let checkpoint = history.recovery_checkpoint.unwrap();
        // The review exactly eight days before the last belongs to the prefix.
        assert_eq!(checkpoint.identity.as_ref().unwrap().review_count, 3);
        assert_eq!(checkpoint, prefix.metadata.unwrap());
        assert_eq!(checkpoint.review_count_by_card[&first.id.0], 2);
        assert_eq!(checkpoint.review_count_by_card[&second.id.0], 1);
        assert_eq!(checkpoint.previous_interval_days_by_card[&first.id.0], 4);
        assert_eq!(checkpoint.previous_interval_days_by_card[&second.id.0], 2);
        assert_ne!(Some(checkpoint), history.metadata);
        Ok(())
    }
}
