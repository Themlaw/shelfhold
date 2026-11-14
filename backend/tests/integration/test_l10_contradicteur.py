"""M3 L10, contradicteur de la #226: attacks on the read-only audit of the children's Kavita
accounts. Each test is a proof of concept that is red on the PR as reviewed: an "ok" that the
audit should never give (EF-EXP-036, EF-USR-017). See `docs/decisions/M3.md`, section
"L10 : contradicteur de la #226".
"""

import contextlib
from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.export.domain.kavita_access import Account
from shelfhold.export.domain.kavita_child_accounts import ChildAccount as DomainChild
from shelfhold.export.domain.kavita_child_accounts import LibraryClass, assess_child
from shelfhold.export.targets.kavita.accounts_audit import (
    ChildAccount,
    ChildAccountsAuditor,
    health_entry,
)
from shelfhold.identity.public import local_auth
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.ops.wiring import open_ops
from shelfhold.runtime_jobs import access_audit
from tests.auth_env import PASSWORD
from tests.export_env import export_env
from tests.fakes.kavita import member
from tests.kavita_env import run
from tests.kavita_export_env import LIBRARY_ID, REMOTE_ROOT, Wired, wire
from tests.l41_env import instance

UNLISTED_LIBRARY = 99  # a library the account reaches, but that the library listing leaves out


def _child(max_age: int = 10, kavita: str | None = "kid") -> ChildAccount:
    return ChildAccount(profile_id=new_ulid(), max_age=max_age, kavita_username=kavita)


def _audit(w: Wired, *children: ChildAccount) -> Any:
    auditor = ChildAccountsAuditor(w.env.engine, w.env.clock, w.events)
    return run(auditor.audit(w.client, w.env.target_id, list(children)))


def _sound(libraries: tuple[int, ...] = (LIBRARY_ID,)) -> dict[str, Any]:
    return member("kid", libraries=libraries, age_rating=5, include_unknowns=False)


@pytest.mark.req("EF-EXP-036")
def test_poc_l10_shared_library_over_a_family_shelf_is_not_family() -> None:
    """B1: a Kavita library whose folders hold the family shelf AND another folder is classed
    "family" (ceiling of the shelf) by `_classes`: `library_is_dedicated` is never applied. The
    child reads the other folder too, and the audit says "ok"."""
    with export_env(max_age_rating=10) as env:
        w = wire(env)
        library = next(lib for lib in w.fake.libraries if int(lib["id"]) == LIBRARY_ID)
        library["folders"] = [REMOTE_ROOT, "/adult-only"]  # the shelf, and something else
        w.fake.users.append(_sound())
        report = _audit(w, _child(10))
        assert report.children[0].status != "ok"


@pytest.mark.req("EF-EXP-036")
def test_poc_l10_reached_library_missing_from_the_listing_is_not_ok() -> None:
    """B2: `assess_child` only looks at the libraries the listing returned. An id the account
    reaches but that the listing leaves out (partial answer, item dropped by `list_libraries`)
    is never looked at: the child is "ok"."""
    with export_env(max_age_rating=10) as env:
        w = wire(env)
        w.fake.users.append(_sound((LIBRARY_ID, UNLISTED_LIBRARY)))
        report = _audit(w, _child(10))
        assert report.children[0].status != "ok"


@pytest.mark.req("EF-EXP-036")
def test_poc_l10_malformed_listing_after_an_ok_never_leaves_ok() -> None:
    """M1: an error after the two GETs (`_classes`: a library without `id`) escapes the `try`.
    Nothing is recorded, the previous "ok" stays on the Health page and on the admin route."""
    with export_env(max_age_rating=10) as env:
        w = wire(env)
        w.fake.users.append(_sound())
        child = _child(10)
        assert _audit(w, child).children[0].status == "ok"
        w.fake.libraries.append({"name": "no id", "folders": ["/elsewhere"]})
        with contextlib.suppress(Exception):  # the escape itself is part of the finding
            _audit(w, child)
        assert health_entry(env.engine, env.target_id)["state"] != "ok"


@pytest.mark.req("EF-EXP-036", "EF-USR-017")
def test_poc_l10_daily_job_on_a_target_without_key_keeps_a_stale_ok(
    settings: Settings, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1: `connected_clients` skips a target whose key cannot be unsealed, and the daily job
    records nothing for it: the "ok" of last week stays "ok" forever (the route records a
    failure in the same case, the daily job does not)."""
    with instance(settings, tmp_path) as inst:
        inst.configure()
        auth = local_auth(inst.engine, settings)
        access_audit(inst.comp)()  # no child mapped yet: a clean "ok"
        (target,) = inst.comp.kavita_targets()
        assert health_entry(inst.engine, target)["state"] == "ok"
        kid = auth.profile_add("kid", PASSWORD, "child", max_age_rating=6)
        auth.profile_set_kavita_username(kid, "grown.up")
        inst.kavita.users.append(member("grown.up", admin=True))  # reaches everything

        def no_key(_target_id: str) -> Any:
            raise KeyError("sealed key")

        monkeypatch.setattr(inst.comp, "kavita_client", no_key)
        access_audit(inst.comp)()  # the next day: the key cannot be unsealed
        assert health_entry(inst.engine, target)["state"] == "unverified"


@pytest.mark.req("EF-EXP-036", "EF-USR-017")
def test_poc_l10_mapping_made_after_the_audit_is_not_reported_ok(
    settings: Settings, tmp_path: Any
) -> None:
    """M2: the kept answer is read back as it is, whatever changed since. A child mapped (or
    whose ceiling was lowered) after the last audit does not appear, and the state stays "ok"
    until the next day: the admin who just mapped an adult account is told "ok"."""
    with instance(settings, tmp_path) as inst:
        inst.configure()
        auth = local_auth(inst.engine, settings)
        ops = open_ops(settings, engine=inst.engine, composition=inst.comp)
        (target,) = inst.comp.kavita_targets()
        ops.kavita_accounts.run()  # no child mapped yet
        before = health_entry(inst.engine, target)
        assert before["state"] == "ok" and before["children"] == []
        kid = auth.profile_add("kid", PASSWORD, "child", max_age_rating=6)
        auth.profile_set_kavita_username(kid, "grown.up")
        inst.kavita.users.append(member("grown.up", admin=True))  # reaches everything
        (entry,) = ops.kavita_accounts.entries()  # what GET /api/v1/audit/kavita-accounts shows
        assert entry["state"] != "ok"


@pytest.mark.req("EF-EXP-036")
def test_poc_l10_name_matching_is_looser_than_kavita_and_takes_the_first() -> None:
    """m1: `assess_child` matches with `casefold` and takes the first account. `casefold` folds
    "ß" into "ss", which Kavita's upper-case normalisation does not: two distinct Kavita accounts
    answer the same declared name, and the sound one listed first hides the admin one the child
    actually signs in with. An exact match must win, an ambiguity must be an alert."""
    sound = Account("STRASSE", False, 5, False, frozenset({LIBRARY_ID}))
    admin = Account("straße", True, -1, True, frozenset())
    child = DomainChild(profile_id="p", max_age=10, kavita_username="straße")
    report = assess_child(child, [sound, admin], [LibraryClass(LIBRARY_ID, 10)])
    assert report.status != "ok"
