"""The fault suite runs: a crash between write and rename never leaves a partial target."""

from pathlib import Path

import pytest


@pytest.mark.req("ENF-OPS-011")
def test_failed_rename_leaves_the_previous_content_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    target.write_text("old", encoding="utf-8")
    staging = tmp_path / "state.json.tmp"
    staging.write_text("new", encoding="utf-8")

    def crash(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated crash before the rename")

    monkeypatch.setattr(Path, "replace", crash)
    with pytest.raises(OSError, match="simulated crash"):
        staging.replace(target)
    assert target.read_text(encoding="utf-8") == "old"
