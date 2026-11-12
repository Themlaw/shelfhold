"""The module trust card (M3 L18, EF-UX-011): three lines, Officiel / Communauté / Inconnu, the one
that applies marked. A plugin is official only when it ships with the host; community only on the
admin's attestation of the installed version; anything else is unknown."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from shelfhold.plugin_host.trust_card import TrustLevel, trust_card

from shelfhold.config import Settings
from shelfhold.headless.actor import Actor
from shelfhold.i18n import t
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import migrate
from shelfhold.plugin_host.attestation import (
    FIRST_PARTY_PLUGIN_KEYS,
    AttestationStore,
    InstalledVersion,
)
from shelfhold.plugin_host.registry import PluginOrigin, PluginRecord, PluginStatus
from shelfhold.sdk.models import API_VERSION
from shelfhold.sdk.testing.fakes import FakeRangedConnectedService

pytestmark = pytest.mark.req("EF-UX-011")

KEY = "acme"
DIGEST = "sha256:" + "d" * 64
ADMIN = Actor("session:admin-test")


@pytest.fixture
def engine(settings: Settings) -> Iterator[sa.Engine]:
    migrate(settings)
    engine = create_db_engine(settings.database_path)
    yield engine
    engine.dispose()


def record(key: str, origin: PluginOrigin) -> PluginRecord:
    manifest = FakeRangedConnectedService().manifest.model_copy(
        update={"key": key, "api": API_VERSION}
    )
    return PluginRecord(
        key, origin, PluginStatus.LOADED, manifest=manifest, instance=SimpleNamespace()
    )


def install(engine: sa.Engine, version: str) -> AttestationStore:
    store = AttestationStore(engine, clock=SystemClock())
    store.sync_installed({KEY: InstalledVersion(KEY, KEY, version, DIGEST)})
    return store


def test_module_trust_card_three_lines(engine: sa.Engine) -> None:
    with engine.connect() as connection:
        card = trust_card(record("gutenberg", PluginOrigin.FIRST_PARTY), connection)
    assert [line.level for line in card.lines] == [
        TrustLevel.OFFICIAL, TrustLevel.COMMUNITY, TrustLevel.UNKNOWN,
    ]  # fmt: skip
    assert len(card.lines) == 3  # always three, whatever the module
    assert [line.label for line in card.lines] == [
        t("trust_card.official"), t("trust_card.community"), t("trust_card.unknown"),
    ]  # fmt: skip
    assert [line.current for line in card.lines] == [True, False, False]
    assert card.level is TrustLevel.OFFICIAL


def test_card_as_dict_is_what_the_api_shows(engine: sa.Engine) -> None:
    with engine.connect() as connection:
        shown = trust_card(record("gutenberg", PluginOrigin.FIRST_PARTY), connection).as_dict()
    assert shown["level"] == "official" and len(shown["lines"]) == 3
    assert shown["lines"][0] == {
        "level": "official",
        "label": t("trust_card.official"),
        "current": True,
    }


def test_third_party_module_is_unknown_until_attested(engine: sa.Engine) -> None:
    store = install(engine, "1.0.0")
    third = record(KEY, PluginOrigin.THIRD_PARTY)
    with engine.connect() as connection:
        card = trust_card(third, connection)
    assert card.level is TrustLevel.UNKNOWN and [x.current for x in card.lines] == [
        False,
        False,
        True,
    ]
    store.attest(KEY, "content_trust", "publisher", actor=ADMIN)
    with engine.connect() as connection:
        card = trust_card(third, connection)
    assert card.level is TrustLevel.COMMUNITY and [x.current for x in card.lines] == [
        False,
        True,
        False,
    ]


def test_attestation_lapses_with_the_installed_version(engine: sa.Engine) -> None:
    store = install(engine, "1.0.0")
    store.attest(KEY, "content_trust", "publisher", actor=ADMIN)
    install(engine, "2.0.0")  # new code, nobody vouched for it
    with engine.connect() as connection:
        assert (
            trust_card(record(KEY, PluginOrigin.THIRD_PARTY), connection).level
            is TrustLevel.UNKNOWN
        )


def test_a_third_party_cannot_borrow_an_official_key(engine: sa.Engine) -> None:
    """The attack: a plugin from outside that carries the key of one of the host's own."""
    stolen = sorted(FIRST_PARTY_PLUGIN_KEYS)[0]
    with engine.connect() as connection:
        card = trust_card(record(stolen, PluginOrigin.THIRD_PARTY), connection)
    assert card.level is TrustLevel.UNKNOWN


def test_a_module_the_host_does_not_know_is_unknown(engine: sa.Engine) -> None:
    with engine.connect() as connection:
        assert (
            trust_card(record("nobody", PluginOrigin.THIRD_PARTY), connection).level
            is TrustLevel.UNKNOWN
        )
