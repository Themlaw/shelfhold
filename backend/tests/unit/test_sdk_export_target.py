"""ExportTarget (SDK 1.1): the members that only describe Kavita are deprecated, not removed."""

from shelfhold.sdk.export_target import DEPRECATED_SINCE, ExportTarget

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


def test_export_target_deprecated_members_still_resolve() -> None:
    for name in KAVITA_ONLY:
        member = getattr(ExportTarget, name)  # a destination written for SDK 1.0 still resolves
        assert callable(member)
        message = member.__deprecated__
        assert DEPRECATED_SINCE in message and "M4" in message, name


def test_export_target_members_of_every_adapter_are_not_deprecated() -> None:
    for name in KEPT:
        assert not hasattr(getattr(ExportTarget, name), "__deprecated__"), name
