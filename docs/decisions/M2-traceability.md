# Traceability M2: closure checks (L31/L33)

(`docs/traceability/` is git-ignored: the generated matrix is a CI artifact. This is the committed record.)

Date: 2026-11-12. Tool: `python3 tools/traceability.py --milestone M2` (the full matrix is a CI
artifact, not committed, as for M0 and M1b).

## Tool result

| | Count |
|---|---|
| Requirements of the milestone seen by the tool | 58 (the 57 of PLAN-M2 section 1, plus ENF-GOV-004 moved from M1b) |
| Covered | 57 |
| Not covered | 1: **ENF-GOV-004** |
| Orphan tests, unknown IDs | 0, 0 |

ENF-GOV-004 (second image registry, repository mirror, non-blocking update checker) was moved
from M1b to M2 by the owner (2026-11-11) and is declared in `[[deferred]]` of
`tools/traceability-exceptions.toml` ("second registry delivered with an M2 lot"). No M2 lot of
PLAN-M2 carries it and it is not on the cut list, so it is **neither done nor cut**: it needs the
owner's decision (token of the second registry, or a cut to M3). Nothing was invented for it.

## Manual check of PLAN-M2 section 1 (behavior tests, not schema tests)

The six own requirements and the EF-SRC-037 aspect that only schema tests of M1a covered:

| Requirement | Lot | Behavior test |
|---|---|---|
| EF-BIB-003 | L12 | `test_renumbering_adds_the_new_value_and_supersedes_the_old_one`, `test_auto_attach_displayed_and_undoable` |
| EF-BIB-040 | L26 | `test_the_novel_is_cut_in_volumes_the_size_of_the_key_cap_and_the_open_tail_waits` |
| EF-BIB-043 | L17 | `test_pre_m2_revisions_never_bulk_rewritten`, `test_metadata_locked_by_a_third_party_is_never_rewritten` |
| EF-BIB-056 | L28 | `test_cli_follow_gap_ack`, `test_gap_searched_on_other_bindings` |
| EF-SRC-039 | L28 | `test_language_filter_applied_before_request`, `test_filtered_unit_costs_no_request_in_any_cycle` |
| ENF-LEG-007 | L06 | `test_conditional_get_304_reuses_cache`, `test_default_schedule_offset_differs_between_instances` |
| EF-SRC-037 (aspect) | L27 | `test_unit_never_requested_from_two_bindings_same_cycle`, `test_fallback_hands_over_to_a_binding_the_cycle_may_ask` |

The other aspects (volets) named by PLAN-M2 section 1:

| Requirement | Lot | Behavior test |
|---|---|---|
| EF-BIB-001 | L09 | `test_omnibus_packaging_separate_edition_two_kavita_series` |
| EF-BIB-006, EF-BIB-045 | L03 | `test_untrusted_original_never_served_to_pull_destination`, `test_an_untrusted_epub_whose_original_was_not_kept_stays` |
| EF-BIB-008 | L12 | `test_field_provenance_and_lock`, `test_lock_refuses_a_hostile_field_name_and_writes_nothing` |
| EF-BIB-009 | L17 | `test_removed_upstream_keeps_local_copy`, `test_removed_upstream_with_another_live_variant_keeps_the_unit_available` |
| EF-BIB-024 | L17 | `test_suspect_revision_quarantined`, `test_progressive_truncation_is_judged_against_the_stored_version` |
| EF-BIB-041 | L20, L30 | `test_watermarked_never_in_diagnostic_bundle` |
| EF-BIB-048 | L08 | `test_original_opf_kept_in_meta_for_extraction` |
| EF-EXP-015 (cuttable, kept) | L18 | `test_import_mode_copy_default_hardlink_option_same_fs`, `test_irreplaceable_unit_never_hardlinked`, `test_never_symlink_the_source_is_never_followed` |
| EF-EXP-016 | L16 | `test_upgrade_interrupted_resumes_from_intent`, `test_same_path_upgrade_moves_old_to_recycle` |
| EF-EXP-033, EF-EXP-040 | L08, L09 | `test_first_epub_export_waits_for_age_push`, `test_epub_series_age_pushed_locked_visible_to_restricted_child` |
| EF-EXP-056 | L04 | `test_upstream_snapshot_payload_schema_has_no_secret` |
| EF-INT-006 | L27, L28 | `test_source_fallback.py` (`source.fallback`), `test_numbering_gaps.py` and `test_cli_follow_gap_ack.py` (`series.gap`) |
| EF-SRC-023 | L04, L22 | `test_opds_kavita_api_key_in_the_path_search_and_acquire`, `test_opds_credentials_never_leak_into_journal_reference_cache_or_database` |
| ENF-LEG-005 | L13 | `test_openlibrary_isbn_proposes_authors_and_cover_with_provenance`, `test_a_locked_field_is_never_overwritten_by_openlibrary` |
| ENF-I18N-003 | L15, L08 | `test_epub_language_golden_zh_hant_es_419`, `test_no_translation_call_in_exporters` |

The other volets (EF-SRC-032, EF-SRC-034, EF-BIB-044, ...) are covered by tests added by their
M2 lots (for example `test_lncrawl_epub_per_range_single_export_item`,
`test_adoption_reads_the_range_of_each_volume_from_its_comicinfo`); the tool lists them all.

Test names announced by PLAN-M2 that no longer exist: `test_revision_pending_waits_for_m2`
(replaced by behavior tests, as L17 planned), `test_never_symlink` (split in three in
`test_hardlink_option.py`), and the ten tests of L32 (end-to-end journeys and the real Kavita
nightly case: L32 is not merged yet).

## Reachability allowlist

No entry is left `until = "M2"` (`test_no_allowlist_entry_until_past_milestone`, no longer
exempting L10/L29 entries). `Scheduler.add_default` was deleted (the periodic jobs register
their seeded spread through `add(..., jitter_s=)`, which the ENF-LEG-007 test now uses);
`probe_image` and `diagnose_worker` are written down for M3 (`m2-l31-deferred-m3-decode-probe`).
