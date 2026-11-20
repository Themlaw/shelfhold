"""ExportTarget (SDK 2.0, M6 L02): the members that only described Kavita are removed, as the
1.x series announced (ADR-0024, amendement SDK 2.0)."""

from shelfhold_sdk.export_target import ExportTarget

KAVITA_ONLY = (
    "parse_name_call",
    "ensure_library_call",
    "resolve_remote_calls",
    "event_subscription",
    "progress_calls",
)
KEPT = (
    "config_schema",
    "default_template",
    "render_sidecars",
    "inventory_calls",
    "scan_calls",
    "interpret",
    "supported_versions",
)


def test_export_target_deprecated_members_are_removed_at_sdk_2() -> None:
    for name in KAVITA_ONLY:
        assert not hasattr(ExportTarget, name), (
            name
        )  # a 1.x destination loads through the api 1 shim


def test_export_target_members_of_every_adapter_are_not_deprecated() -> None:
    for name in KEPT:
        assert not hasattr(getattr(ExportTarget, name), "__deprecated__"), name
