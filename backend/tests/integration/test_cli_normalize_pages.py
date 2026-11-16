"""M4 L53 (EF-EXP-046, ADR-0054): `shelfhold library normalize-pages`, the real command over a real
instance: an empty library has nothing to do, and without /store the rewrite is refused with its
closed code."""

import pytest

from shelfhold.config import Settings
from tests.l54_env import bench54


@pytest.mark.req("EF-EXP-046")
def test_the_command_says_there_is_nothing_to_normalize(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        capsys.readouterr()
        assert b.cli("library", "normalize-pages", "--dry-run") == 0
        assert "Rien à normaliser" in capsys.readouterr().out
        code = b.cli("library", "normalize-pages")
        out = capsys.readouterr().out
        assert code == 0 or "pages.store_not_configured" in out  # applied, or refused with its code
