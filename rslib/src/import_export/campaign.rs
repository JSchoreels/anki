// Copyright: Anki Software, LLC and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::collections::HashMap;

use anki_proto::import_export::ExportCampaignPackageRequest;
use serde_json::Map;
use serde_json::Value;

use super::gather::ExchangeData;
use crate::notes::field_checksum;
use crate::prelude::*;
use crate::search::SearchNode;
use crate::search::SortMode;

fn sharing_key(id: Option<i64>, name: &str) -> String {
    match id {
        Some(id) => format!("id:{id}:{name}"),
        None => format!("name:{name}"),
    }
}

/// Replace user-written deck text. Each name component becomes `Deck <id>` of
/// the deck at that level, so the hierarchy survives without the names.
fn anonymize_decks(decks: &mut [Deck]) -> Result<()> {
    let ids: HashMap<String, DeckId> = decks
        .iter()
        .map(|d| (d.name.as_native_str().to_owned(), d.id))
        .collect();
    for deck in decks {
        let mut prefix = String::new();
        let mut components = Vec::new();
        for (level, component) in deck.name.components().enumerate() {
            if level > 0 {
                prefix.push('\x1f');
            }
            prefix.push_str(component);
            components.push(match ids.get(&prefix) {
                Some(id) => format!("Deck {}", id.0),
                None => format!("Deck {}-{level}", deck.id.0),
            });
        }
        deck.name = NativeDeckName::from_native_str(components.join("\x1f"));
        let respect_subdeck_minimums = deck.rwkv_respect_subdeck_minimums();
        deck.common.other.clear();
        deck.set_rwkv_respect_subdeck_minimums(respect_subdeck_minimums)?;
        match &mut deck.kind {
            DeckKind::Normal(normal) => normal.description.clear(),
            DeckKind::Filtered(filtered) => {
                for term in &mut filtered.search_terms {
                    term.search.clear();
                }
            }
        }
    }
    Ok(())
}

/// Keep only the scheduling flags read straight from a preset's `other` JSON.
/// The fork fields also stored there are restored into `inner` on load and
/// written back on save, so they need no copy here.
fn preset_scheduling_other(other: &[u8]) -> Vec<u8> {
    let Ok(Value::Object(source)) = serde_json::from_slice(other) else {
        return vec![];
    };
    let mut kept: Map<String, Value> = [
        "fsrs7IncludeSameDayOptimize",
        "fsrs7IncludeSameDayEvaluate",
        "fsrs7EnableSchedulingPenalties",
    ]
    .into_iter()
    .filter_map(|key| {
        Some((
            key.into(),
            source.get(key).filter(|v| v.is_boolean())?.clone(),
        ))
    })
    .collect();
    let options: Map<String, Value> = ["intraday_learning_mix", "intraday_learning_before_reviews"]
        .into_iter()
        .filter_map(|key| {
            let value = source.get("jschoreels.deckoptions")?.get(key)?;
            (value.is_number() || value.is_boolean()).then(|| (key.into(), value.clone()))
        })
        .collect();
    if !options.is_empty() {
        kept.insert("jschoreels.deckoptions".into(), options.into());
    }
    if kept.is_empty() {
        vec![]
    } else {
        serde_json::to_vec(&kept).unwrap_or_default()
    }
}

impl Collection {
    /// Build a fresh database so excluded text cannot survive in sort caches,
    /// unused SQLite pages, or unrelated profile/add-on configuration.
    pub(super) fn write_campaign_package(
        &mut self,
        request: ExportCampaignPackageRequest,
    ) -> Result<()> {
        let directory = tempfile::tempdir()?;
        let mut snapshot = Self::new_minimal(directory.path().join("collection.anki2"))?;
        // The snapshot is discarded if the export fails, so it needs no
        // crash safety. Skipping it roughly halves the time the export holds
        // the user's collection.
        snapshot
            .storage
            .db
            .pragma_update(None, "journal_mode", "memory")?;
        snapshot
            .storage
            .db
            .pragma_update(None, "synchronous", "off")?;
        let choices: HashMap<_, _> = request
            .selections
            .into_iter()
            .map(|s| (NotetypeId(s.notetype_id), s))
            .collect();
        let mut fields_by_type = HashMap::new();
        let mut notetypes = Vec::new();
        for original in self.get_all_notetypes()? {
            let mut nt = (*original).clone();
            let choice = choices.get(&nt.id);
            let fields: Vec<bool> = nt
                .fields
                .iter()
                .map(|f| {
                    choice.is_some_and(|s| s.fields.contains(&sharing_key(f.config.id, &f.name)))
                })
                .collect();
            for (index, (field, shared)) in nt.fields.iter_mut().zip(&fields).enumerate() {
                field.config.description.clear();
                if !shared {
                    field.config.other.clear();
                }
                if choice.is_none() {
                    field.name = format!("Field {}", index + 1);
                }
            }
            fields_by_type.insert(nt.id, (original, fields));
            let mut has_template = false;
            for (index, template) in nt.templates.iter_mut().enumerate() {
                if choice.is_some_and(|s| {
                    s.templates
                        .contains(&sharing_key(template.config.id, &template.name))
                }) {
                    has_template = true;
                } else {
                    template.config = Default::default();
                }
                if choice.is_none() {
                    template.name = format!("Card {}", index + 1);
                }
            }
            if !has_template {
                nt.config.css.clear();
                nt.config.latex_pre.clear();
                nt.config.latex_post.clear();
            }
            nt.config.other.clear();
            if choice.is_none() {
                nt.name = format!("Note type {}", nt.id.0);
            }
            notetypes.push(nt);
        }
        let mut decks = self.storage.get_all_decks()?;
        anonymize_decks(&mut decks)?;
        let mut deck_configs = self.storage.all_deck_config()?;
        for config in &mut deck_configs {
            config.name = format!("Preset {}", config.id.0);
            // Optimizer searches usually name decks or tags.
            config.inner.param_search.clear();
            config.inner.other = preset_scheduling_other(&config.inner.other);
        }
        let data = ExchangeData {
            notetypes,
            decks,
            deck_configs,
            revlog: self.storage.get_all_revlog_entries_in_card_order()?,
            ..Default::default()
        };
        snapshot.insert_data(&data)?;
        drop(data);
        snapshot.transact_no_undo(|copy| {
            let guard = self.search_notes_into_table(SearchNode::WholeCollection)?;
            guard.col.storage.for_each_note_in_search(|mut note| {
                let (nt, selected) = fields_by_type
                    .get(&note.notetype_id)
                    .or_not_found(note.notetype_id)?;
                // Stored values were validated when written, so skip the costly
                // prepare_for_update(). fields_mut() resets the cached sort
                // field and checksum: keep them, and replace only the cleared
                // ones with those of an empty field.
                let sort_idx = nt.config.sort_field_idx as usize;
                let mut sort_field = note.sort_field.take();
                let mut checksum = note.checksum;
                for (index, value) in note.fields_mut().iter_mut().enumerate() {
                    if selected.get(index) != Some(&true) && !value.is_empty() {
                        value.clear();
                        if index == 0 {
                            checksum = Some(field_checksum(""));
                        }
                        if index == sort_idx {
                            sort_field = Some(String::new());
                        }
                    }
                }
                note.sort_field = sort_field;
                note.checksum = checksum;
                note.tags.clear();
                copy.storage.add_or_update_note(&note)
            })?;
            drop(guard);
            let guard =
                self.search_cards_into_table(SearchNode::WholeCollection, SortMode::NoOrder)?;
            guard.col.storage.for_each_card_in_search(|mut card| {
                guard.col.storage.populate_rwkv_history_reset(&mut card)?;
                card.custom_data.clear();
                copy.storage.add_or_update_card(&card)
            })
        })?;
        snapshot.set_creation_stamp(self.storage.creation_stamp()?)?;
        snapshot.set_creation_utc_offset(self.get_creation_utc_offset())?;
        snapshot.set_config("donationCampaignCollectionId", &request.collection_id)?;
        snapshot.export_colpkg(request.out_path, false, false)
    }
}

#[cfg(test)]
mod tests {
    use std::fs::File;

    use anki_proto::import_export::CampaignNotetypeSelection;
    use zip::ZipArchive;

    use super::*;
    use crate::collection::CollectionBuilder;
    use crate::revlog::RevlogEntry;

    #[test]
    fn campaign_writes_only_selected_content_and_preserves_review_history() -> Result<()> {
        let mut source = Collection::new();
        let mut nt = source.basic_notetype();
        nt.templates[0].config.q_format = "private-layout-8672 {{Front}}".into();
        nt.templates[0].config.a_format_browser = "private-browser-8672".into();
        nt.config.css = "/* private-style-8672 */".into();
        // The kept field is the sort field, the cleared one feeds the checksum.
        nt.config.sort_field_idx = 1;
        source.update_notetype(&mut nt, false)?;
        let note = NoteAdder::new(&nt)
            .fields(&["private-sort-field-8672", "shared definition"])
            .add(&mut source);
        let card = source.storage.all_cards_of_note(note.id)?.remove(0);
        let review = RevlogEntry {
            id: RevlogId(123456),
            cid: card.id,
            button_chosen: 3,
            ..Default::default()
        };
        source.storage.add_revlog_entry(&review, false)?;
        let dir = tempfile::tempdir()?;
        let package = dir.path().join("campaign.colpkg");
        let field = &nt.fields[1];
        source.write_campaign_package(ExportCampaignPackageRequest {
            out_path: package.to_str().unwrap().into(),
            collection_id: "test-donor".into(),
            selections: vec![CampaignNotetypeSelection {
                notetype_id: nt.id.0,
                fields: vec![sharing_key(field.config.id, &field.name)],
                templates: vec![],
            }],
        })?;
        let mut archive = ZipArchive::new(File::open(package)?)?;
        let bytes = zstd::decode_all(archive.by_name("collection.anki21b")?)?;
        for marker in [
            "private-sort-field-8672",
            "private-layout-8672",
            "private-browser-8672",
            "private-style-8672",
        ] {
            assert!(
                !bytes.windows(marker.len()).any(|w| w == marker.as_bytes()),
                "{marker}"
            );
        }
        let db = dir.path().join("copy.anki2");
        std::fs::write(&db, bytes)?;
        let copy = CollectionBuilder::new(db).build()?;
        assert_eq!(
            copy.storage.get_note(note.id)?.unwrap().fields(),
            &["", "shared definition"]
        );
        let copied_note = copy.storage.get_note(note.id)?.unwrap();
        assert_eq!(copied_note.sort_field.as_deref(), Some("shared definition"));
        assert_eq!(copied_note.checksum, Some(field_checksum("")));
        assert_eq!(copy.storage.all_cards_of_note(note.id)?, [card]);
        assert_eq!(
            copy.storage.get_all_revlog_entries_in_card_order()?,
            [review]
        );
        assert_eq!(source.storage.get_note(note.id)?.unwrap(), note);
        assert_eq!(source.get_notetype(nt.id)?.unwrap().templates, nt.templates);
        Ok(())
    }

    #[test]
    fn campaign_strips_user_text_but_keeps_deck_structure_and_presets() -> Result<()> {
        let mut source = Collection::new();
        let mut nt = source.basic_notetype();
        nt.fields[1].config.description = "private-description-8672".into();
        source.update_notetype(&mut nt, false)?;
        let mut excluded = source.basic_notetype();
        excluded.id = NotetypeId(0);
        excluded.name = "private-notetype-8672".into();
        excluded.fields[0].name = "private-fieldname-8672".into();
        excluded.templates[0].name = "private-template-8672".into();
        excluded.templates[0].config.q_format = "{{private-fieldname-8672}}".into();
        source.add_notetype(&mut excluded, false)?;

        let mut preset = DeckConfig {
            name: "private-preset-8672".into(),
            ..Default::default()
        };
        preset.inner.desired_retention = 0.83;
        preset.inner.new_per_day = 7;
        preset.inner.param_search = "private-paramsearch-8672".into();
        preset.inner.other = serde_json::to_vec(&serde_json::json!({
            "jschoreels.deckoptions": {"intraday_learning_mix": 1},
            "fsrsEvaluationSearch": "private-evalsearch-8672",
        }))?;
        source.add_or_update_deck_config(&mut preset)?;
        let preset = source.storage.get_deck_config(preset.id)?.unwrap();
        let mut child =
            source.get_or_create_normal_deck("private-parent-8672::private-child-8672")?;
        child.normal_mut()?.config_id = preset.id.0;
        child.normal_mut()?.description = "private-deckdesc-8672".into();
        child.set_rwkv_respect_subdeck_minimums(true)?;
        source.update_deck(&mut child)?;
        let parent = source.get_or_create_normal_deck("private-parent-8672")?;
        let mut filtered = Deck::new_filtered();
        filtered.name = NativeDeckName::from_human_name("private-filtered-8672");
        filtered.filtered_mut()?.search_terms[0].search = "private-search-8672".into();
        source.add_deck(&mut filtered)?;

        let mut note = NoteAdder::new(&nt)
            .fields(&["front", "back"])
            .deck(child.id)
            .add(&mut source);
        note.tags = vec!["private-tag-8672".into()];
        source.update_note(&mut note)?;
        let mut card = source.storage.all_cards_of_note(note.id)?.remove(0);
        card.custom_data = r#"{"x":"private-custom-8672"}"#.into();
        source.storage.update_card(&card)?;

        let dir = tempfile::tempdir()?;
        let package = dir.path().join("campaign.colpkg");
        source.write_campaign_package(ExportCampaignPackageRequest {
            out_path: package.to_str().unwrap().into(),
            collection_id: "test-donor".into(),
            selections: vec![CampaignNotetypeSelection {
                notetype_id: nt.id.0,
                fields: nt
                    .fields
                    .iter()
                    .map(|f| sharing_key(f.config.id, &f.name))
                    .collect(),
                templates: vec![],
            }],
        })?;
        let mut archive = ZipArchive::new(File::open(package)?)?;
        let bytes = zstd::decode_all(archive.by_name("collection.anki21b")?)?;
        for marker in [
            "private-description-8672",
            "private-notetype-8672",
            "private-fieldname-8672",
            "private-template-8672",
            "private-preset-8672",
            "private-paramsearch-8672",
            "private-evalsearch-8672",
            "private-parent-8672",
            "private-child-8672",
            "private-deckdesc-8672",
            "private-filtered-8672",
            "private-search-8672",
            "private-tag-8672",
            "private-custom-8672",
        ] {
            assert!(
                !bytes.windows(marker.len()).any(|w| w == marker.as_bytes()),
                "{marker}"
            );
        }
        let db = dir.path().join("copy.anki2");
        std::fs::write(&db, bytes)?;
        let mut copy = CollectionBuilder::new(db).build()?;

        let copied_child = copy.storage.get_deck(child.id)?.unwrap();
        assert_eq!(
            copied_child.human_name(),
            format!("Deck {}::Deck {}", parent.id.0, child.id.0)
        );
        assert_eq!(copied_child.config_id(), Some(preset.id));
        assert!(copied_child.rwkv_respect_subdeck_minimums());
        assert_eq!(
            copy.storage.get_deck(DeckId(1))?.unwrap().human_name(),
            "Deck 1"
        );
        let copied_filtered = copy.storage.get_deck(filtered.id)?.unwrap();
        let terms = &copied_filtered.filtered()?.search_terms;
        assert_eq!(terms.len(), 2);
        assert_eq!(terms[0].limit, 100);
        assert!(terms.iter().all(|t| t.search.is_empty()));

        let copied_preset = copy.storage.get_deck_config(preset.id)?.unwrap();
        assert_eq!(copied_preset.name, format!("Preset {}", preset.id.0));
        assert_eq!(
            copied_preset.intraday_learning_mix(),
            preset.intraday_learning_mix()
        );
        assert_ne!(
            preset.intraday_learning_mix(),
            crate::deckconfig::ReviewMix::MixWithReviews
        );
        assert_eq!(
            copied_preset.inner,
            crate::deckconfig::DeckConfigInner {
                param_search: String::new(),
                other: copied_preset.inner.other.clone(),
                ..preset.inner.clone()
            }
        );

        let copied_excluded = copy.get_notetype(excluded.id)?.unwrap();
        assert_eq!(copied_excluded.name, format!("Note type {}", excluded.id.0));
        assert_eq!(copied_excluded.fields[0].name, "Field 1");
        assert_eq!(copied_excluded.templates[0].name, "Card 1");
        let copied_nt = copy.get_notetype(nt.id)?.unwrap();
        assert_eq!(copied_nt.name, nt.name);
        assert_eq!(copied_nt.fields[1].name, nt.fields[1].name);
        let copied_note = copy.storage.get_note(note.id)?.unwrap();
        assert!(copied_note.tags.is_empty());
        assert_eq!(copied_note.fields(), note.fields());
        let stored = source.storage.get_note(note.id)?.unwrap();
        assert_eq!(copied_note.sort_field, stored.sort_field);
        assert_eq!(copied_note.checksum, stored.checksum);
        card.custom_data.clear();
        assert_eq!(copy.storage.all_cards_of_note(note.id)?, [card]);
        assert_eq!(source.storage.get_note(note.id)?.unwrap(), note);
        Ok(())
    }
}
