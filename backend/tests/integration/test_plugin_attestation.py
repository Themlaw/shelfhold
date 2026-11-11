"""M2 L07 (EF-EXP-065 (5), GAP-11-09): a trust value declared by a plugin that is not first-party
is a proposed default until the admin attests it, per instance and per plugin version.

`content_trust: publisher` skips the neutralizer only for an admitted first-party source or on the
admin's attestation; the attestation is audited, bound to the installed version of the plugin, and
lapses as soon as that version changes. `access_partitioning` and `egress` follow the same rule.
"""

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.headless.actor import Actor
from shelfhold.identity.public import RESERVED_ACTIONS, Effect
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema import acquisition
from shelfhold.library.adapters.epub_derivative import trust_of
from shelfhold.plugin_host.attestation import (
    AttestationError,
    AttestationNotNeeded,
    AttestationStore,
    InstalledVersion,
    retain,
)
from shelfhold.plugin_host.registry import (
    PluginCandidate,
    PluginOrigin,
    PluginRegistry,
    PluginStatus,
)
from shelfhold.sdk.models import API_VERSION
from shelfhold.sdk.source import SourceManifest
from shelfhold.sdk.testing.fakes import FakeRangedConnectedService
from tests.auth_env import auth_env
from tests.integration.test_untrusted_epub_pipeline import (
    CLEAN,
    TRAPPED,
    has_active_content,
    ingest,
    set_binding_trust,
)
from tests.library_env import LibEnv, lib_env
from tests.plugin_guard_env import build_wheel, make_cli

pytestmark = pytest.mark.req("EF-EXP-065")

KEY = "acme"
DIGEST = "sha256:" + "c" * 64
ADMIN = Actor("session:admin-test")


def _raw(key: str = KEY, **extra: Any) -> dict[str, Any]:
    raw = FakeRangedConnectedService().manifest.model_dump(mode="json")
    return {**raw, "key": key, "api": API_VERSION, **extra}


def _candidate(origin: PluginOrigin, **extra: Any) -> PluginCandidate:
    raw = _raw(**extra)
    dist = None if origin is PluginOrigin.FIRST_PARTY else KEY
    return PluginCandidate(KEY, origin, lambda: SimpleNamespace(manifest=raw), dist=dist)


def _loaded(origin: PluginOrigin, **extra: Any) -> Any:
    registry = PluginRegistry(granted_distributions=[KEY])
    (record,) = registry.load_all([_candidate(origin, **extra)])
    assert record.status is PluginStatus.LOADED, record.reason
    return registry, record


def _third_party_binding(env: LibEnv, version: str = "1.0.0") -> AttestationStore:
    """The Variant of the env came by a binding of the third-party plugin `acme`."""
    with env.engine.begin() as connection:
        connection.execute(sa.update(acquisition.source_item).values(plugin_id=KEY))
    attestations = AttestationStore(env.engine, clock=SystemClock())
    attestations.sync_installed({KEY: InstalledVersion(KEY, KEY, version, DIGEST)})
    return attestations


def _audit(engine: sa.Engine) -> list[tuple[str, str | None, str | None]]:
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT action, target, actor_id FROM audit "
                "WHERE action LIKE 'plugin.attest%' ORDER BY at, id"
            )
        )
        return [(r[0], r[1], r[2]) for r in rows]


@pytest.fixture
def engine(settings: Settings) -> Iterator[sa.Engine]:
    migrate(settings)
    engine = create_db_engine(settings.database_path)
    yield engine
    engine.dispose()


def test_non_first_party_publisher_needs_attestation_from_m2() -> None:
    """Given a third-party plugin loaded in process that declares `publisher`, when it produces an
    EPUB, then the neutralizer applies as long as the admin has not attested (GAP-11-09)."""
    registry, record = _loaded(PluginOrigin.THIRD_PARTY, content_trust="publisher")
    assert isinstance(record.manifest, SourceManifest)
    assert record.manifest.content_trust == "untrusted"  # what the host reads: the safe default
    assert record.proposed == {"content_trust": "publisher"}  # what the plugin asked for
    with lib_env() as env:
        attestations = _third_party_binding(env)
        set_binding_trust(env, "publisher")  # even a binding row that says so is not enough
        with env.engine.connect() as connection:
            assert trust_of(connection, env.chain["variant"]) == "untrusted"
        _result, library = ingest(env, TRAPPED)
        assert not has_active_content(library.read_bytes())  # neutralized
        assert env.files_under(env.store)  # the original kept in /store, the derivative exported

        attestations.attest(KEY, "content_trust", "publisher", actor=ADMIN)
        with env.engine.connect() as connection:
            assert trust_of(connection, env.chain["variant"]) == "publisher"
        registry.apply_attestations(attestations.valid())
        trusted = registry.get(KEY)
        assert trusted is not None and isinstance(trusted.manifest, SourceManifest)
        assert trusted.manifest.content_trust == "publisher"


def test_third_party_publisher_trust_requires_admin_attestation() -> None:
    """The M6 wording, applied from M2 to every plugin that is not first-party: no attestation, no
    trust; the attestation is audited with its actor and the version it was given for."""
    with lib_env() as env:
        attestations = _third_party_binding(env, version="2.1.0")
        set_binding_trust(env, "publisher")
        _result, library = ingest(env, TRAPPED)
        assert not has_active_content(library.read_bytes())
        given = attestations.attest(KEY, "content_trust", "publisher", actor=ADMIN)
        assert (given.plugin_version, given.attested_by_actor) == ("2.1.0", ADMIN.actor_id)
        assert _audit(env.engine) == [
            ("plugin.attested", f"{KEY}@2.1.0:content_trust=publisher", ADMIN.actor_id)
        ]


def test_attestation_lapses_when_the_plugin_version_changes() -> None:
    with lib_env() as env:
        attestations = _third_party_binding(env, version="1.0.0")
        set_binding_trust(env, "publisher")
        attestations.attest(KEY, "content_trust", "publisher", actor=ADMIN)
        # the plugin is upgraded: whatever the new code does was never vouched for
        revoked = attestations.sync_installed({KEY: InstalledVersion(KEY, KEY, "1.0.1", DIGEST)})
        assert [(a.plugin_key, a.field) for a in revoked] == [(KEY, "content_trust")]
        with env.engine.connect() as connection:
            assert trust_of(connection, env.chain["variant"]) == "untrusted"
        assert attestations.valid() == {}
        assert _audit(env.engine)[-1] == (
            "plugin.attestation_revoked",
            f"{KEY}@1.0.0:content_trust=publisher",
            None,
        )
        _result, library = ingest(env, TRAPPED)
        assert not has_active_content(library.read_bytes())


def test_attestation_checked_against_the_installed_version_even_before_a_sync() -> None:
    """A row left for another version never counts, whoever forgot to revoke it."""
    with lib_env() as env:
        attestations = _third_party_binding(env, version="1.0.0")
        set_binding_trust(env, "publisher")
        attestations.attest(KEY, "content_trust", "publisher", actor=ADMIN)
        with env.engine.begin() as connection:  # the install moved on behind the store's back
            connection.execute(sa.update(acquisition.plugin_install).values(version="9.9.9"))
            assert trust_of(connection, env.chain["variant"]) == "untrusted"
        assert attestations.valid() == {}


def test_first_party_declared_values_need_no_attestation() -> None:
    _registry, record = _loaded(PluginOrigin.FIRST_PARTY, content_trust="publisher")
    assert record.manifest.content_trust == "publisher" and record.proposed == {}
    with lib_env() as env:  # the binding of a first-party source: as before M2
        set_binding_trust(env, "publisher")
        _result, library = ingest(env, CLEAN)
        assert library.read_bytes() == CLEAN
        attestations = AttestationStore(env.engine, clock=SystemClock())
        with pytest.raises(AttestationNotNeeded):  # nothing to attest for the host's own code
            attestations.attest("suwayomi", "content_trust", "publisher", actor=ADMIN)


def test_only_a_trust_value_can_be_attested(engine: sa.Engine) -> None:
    attestations = AttestationStore(engine, clock=SystemClock())
    attestations.sync_installed({KEY: InstalledVersion(KEY, KEY, "1.0.0", DIGEST)})
    for field, value in (
        ("content_trust", "untrusted"),  # the default needs nobody's word
        ("content_trust", "trusted"),  # not a value of the contract
        ("rights_basis", "licence"),  # not a trust value
        ("access_partitioning", "none"),
    ):
        with pytest.raises(AttestationError):
            attestations.attest(KEY, field, value, actor=ADMIN)
    with pytest.raises(AttestationError):  # an unknown plugin has no version to bind to
        attestations.attest("nobody", "content_trust", "publisher", actor=ADMIN)
    assert _audit(engine) == []


def test_access_partitioning_and_egress_are_proposed_defaults_too() -> None:
    declared = {"access_partitioning": "user", "egress": "foyer", "content_trust": "publisher"}
    kept = retain(PluginOrigin.THIRD_PARTY, declared, {})
    assert kept.values == {
        "access_partitioning": "none", "egress": "externe", "content_trust": "untrusted"
    }  # fmt: skip
    assert kept.proposed == declared
    partial = retain(PluginOrigin.THIRD_PARTY, declared, {"access_partitioning": "user"})
    assert partial.values["access_partitioning"] == "user"
    assert partial.values["egress"] == "externe"
    # an attestation of another value than the plugin's never raises it further
    other = retain(PluginOrigin.THIRD_PARTY, declared, {"access_partitioning": "library"})
    assert other.values["access_partitioning"] == "none"
    first = retain(PluginOrigin.FIRST_PARTY, declared, {})
    assert first.values == declared and first.proposed == {}


def test_plugin_attest_is_a_reserved_action_refused_to_keys_fresh_only_hardened() -> None:
    """D15: reserved by property (trust boundary; parental control for `access_partitioning`),
    refused to API keys, audited, freshness only in the hardened profile."""
    attest = RESERVED_ACTIONS["plugin.attest"]
    assert Effect.ATTESTS_TRUST in attest.effects
    assert not attest.needs_fresh_auth(restricted_profile_exists=True, hardened=False)
    assert attest.needs_fresh_auth(restricted_profile_exists=False, hardened=True)
    partitioning = RESERVED_ACTIONS["plugin.attest_partitioning"]
    assert {Effect.ATTESTS_TRUST, Effect.PARENTAL_CONTROL} <= partitioning.effects


def test_http_attestation_is_audited_and_refused_without_a_session(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    migrate(settings)
    engine = create_db_engine(settings.database_path)
    try:
        AttestationStore(engine, clock=SystemClock()).sync_installed(
            {KEY: InstalledVersion(KEY, KEY, "3.0.0", DIGEST)}
        )
    finally:
        engine.dispose()
    with auth_env(monkeypatch, settings) as api:
        anonymous = api.client.post(
            f"/api/v1/plugins/{KEY}/attestations",
            json={"field": "content_trust", "value": "publisher"},
        )
        assert anonymous.status_code in {401, 403}
        assert api.login().status_code == 200
        api.clock.advance(timedelta(minutes=30))  # not fresh: still allowed outside hardened
        done = api.post(
            f"/api/v1/plugins/{KEY}/attestations",
            json={"field": "content_trust", "value": "publisher"},
        )
        assert done.status_code == 200, done.text
        assert done.json()["plugin_version"] == "3.0.0"
        refused = api.post(
            f"/api/v1/plugins/{KEY}/attestations", json={"field": "egress", "value": "externe"}
        )
        assert refused.status_code == 422 and refused.json()["code"] == "plugin.attestation_invalid"
        listed = api.client.get(f"/api/v1/plugins/{KEY}/attestations")
        assert listed.status_code == 200
        assert [a["field"] for a in listed.json()["items"]] == ["content_trust"]
    engine = create_db_engine(settings.database_path)
    try:
        actions = [a for a, _t, _actor in _audit(engine)]
    finally:
        engine.dispose()
    # the reserved-action gate logs `plugin.attest` per call; one attestation was recorded
    assert actions.count("plugin.attested") == 1 and "plugin.attest" in actions


def test_cli_plugin_attest_is_bound_to_the_installed_wheel_and_lapses_on_removal(
    tmp_path: Path,
) -> None:
    """`plugin attest` reads the installed version without importing the plugin, writes the
    audit line, and a removal takes the word back (a reinstall is code nobody vouched for)."""
    cli = make_cli(tmp_path)
    wheel = build_wheel(
        tmp_path, "acme-plug", {"acme_plug/__init__.py": "plugin = None\n"},
        entry=(KEY, "acme_plug:plugin"), version="1.4",
    )  # fmt: skip
    assert cli.run("plugin", "install", str(wheel.path), "--sha256", wheel.sha256).returncode == 0
    refused = cli.run("plugin", "attest", "nobody", "content_trust", "publisher")
    assert refused.returncode != 0
    done = cli.run("plugin", "attest", KEY, "content_trust", "publisher")
    assert done.returncode == 0, done.stderr
    assert "1.4" in done.stdout
    assert cli.run("plugin", "remove", "acme-plug").returncode == 0
    trail = [(a, t) for a, t in cli.audit_actions() if a.startswith("plugin.attest")]
    assert trail == [
        ("plugin.attested", f"{KEY}@1.4:content_trust=publisher"),
        ("plugin.attestation_revoked", f"{KEY}@1.4:content_trust=publisher"),
    ]
