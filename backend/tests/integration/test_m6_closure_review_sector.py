"""Contre-épreuves (relecture adverse M6, secteur C). Chaque test ÉCHOUE sur le code actuel."""

from __future__ import annotations

import contextlib
import os
import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from shelfhold_sdk.testing.fakes import TRACKER_KEY

from shelfhold.config import Settings
from shelfhold.infrastructure.volumes import open_data_folder, touch_data_file
from tests.tracker_env import Fixture, tracker_env, trackers_path


@pytest.fixture
def fx(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[Fixture]:
    with tracker_env(monkeypatch, settings) as env:
        yield env


def _token() -> str:
    return secrets.token_urlsafe(32)


@pytest.mark.req("EF-INT-010", "EF-USR-024", "ENF-SEC-005")
def test_allowance_given_to_an_adult_survives_a_demotion_to_child(fx: Fixture) -> None:
    """L'autorisation `tracker.allow_child` est acceptée pour un profil qui n'est pas enfant, et
    elle survit au changement de rôle (qui efface pourtant le lien et les politiques de source) :
    le profil rétrogradé en enfant lie un tracker sans nouvelle parole de l'admin."""
    teen = fx.add("teen", "member")
    allowed = fx.send("PUT", trackers_path(teen, TRACKER_KEY, "/allowance"), {"allowed": True})
    # Décision de la relecture de clôture (S4) : l'autorisation n'est donnée qu'à un enfant. Le
    # PoC attendait 200 pour un adulte ; c'est ce que la décision rejette (refus 422, rien d'écrit).
    assert allowed.status_code == 422
    assert fx.allowances() == []
    fx.auth().profile_set(teen, role="child", max_age_rating=12)
    kid = fx.client_of("teen")
    done = fx.link("teen", _token(), client=kid)
    # attendu : 403 tracker_child_refused (l'enfant n'a reçu aucune autorisation en tant qu'enfant)
    assert done.status_code == 403, f"enfant rétrogradé lié sans autorisation: {done.status_code}"


@pytest.mark.req("EF-INT-010", "EF-USR-024", "ENF-SEC-005")
def test_allowance_survives_child_member_child_cycle(fx: Fixture) -> None:
    kid_id = fx.add("kid", "child", max_age_rating=10)
    path = trackers_path(kid_id, TRACKER_KEY, "/allowance")
    assert fx.send("PUT", path, {"allowed": True}).status_code == 200
    fx.auth().profile_set(kid_id, role="member")
    fx.auth().profile_set(kid_id, role="child", max_age_rating=10)
    assert fx.allowances() == [], "l'autorisation de l'ancien rôle enfant survit au cycle"


@pytest.mark.req("EF-BIB-018")
def test_rendition_touch_follows_a_symlink(tmp_path: Path) -> None:
    """`DataFolder.touch` (appelé par `RenditionService._touch` après la lecture O_NOFOLLOW) se
    dit « never a link » mais `os.utime(name, dir_fd=...)` suit les liens : un lien posé à la
    place du fichier entre la lecture et le touch change la date d'un fichier hors de /data."""
    data = tmp_path / "data"
    shard = data / "cache" / "renditions" / "ab"
    shard.mkdir(parents=True)
    outside = tmp_path / "library" / "original.epub"
    outside.parent.mkdir()
    outside.write_bytes(b"original")
    os.utime(outside, (1_000_000_000, 1_000_000_000))
    name = "ab" + "0" * 62 + ".kepub"
    (shard / name).symlink_to(outside)
    with (
        open_data_folder(data, "cache/renditions/ab") as folder,
        contextlib.suppress(OSError, ValueError),  # un refus est le comportement attendu
    ):
        touch_data_file(folder, name, at=1_700_000_000)
    assert outside.stat().st_mtime == 1_000_000_000, "le touch a suivi le lien hors de /data"
