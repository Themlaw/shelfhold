"""The shards of the image E2E: each runs on its own runner from the same image artifact and must
stay under 10 minutes (`.github/workflows/image.yml`, job `e2e`, `matrix.shard`).

A test that is in no shard fails the collection of a sharded run, so a new journey cannot be left
out of the CI by forgetting to list it here."""

SHARDS: dict[str, tuple[str, ...]] = {
    "smoke-install": (
        "test_stack_is_ready_after_up_wait",
        "test_container_runs_as_uid_1000_with_read_only_root",
        "test_volumes_are_distinct_and_writable",
        "test_no_outbound_request_on_fresh_install",
        "test_egress_watch_detects_tcp_http_and_dns",
        "test_root_serves_the_spa_index",
        "test_m1a_headless_config_apply_declarative_idempotent",
        "test_m1a_headless_install_to_first_chapter_in_kavita",
    ),
    "redeploy": ("test_redeploy_recreates_container_without_loss",),
    "redeploy-export": ("test_redeploy_during_running_export_finishes_without_open_intent",),
    "detach-exits": ("test_detach_restores_suwayomi_updates_and_leaves_files",),
    "exits-live-roots": ("test_exits_work_after_target_add_on_the_running_server",),
    "detach-exclusion": (
        "test_detach_removes_the_exclusion_shelfhold_placed_and_keeps_the_administrator_s",
    ),
    "name-collision": ("test_two_series_with_one_title_wait_for_a_name_then_export",),
    "name-foreign": ("test_name_taken_by_a_series_of_another_source_in_kavita_waits_for_a_name",),
}
