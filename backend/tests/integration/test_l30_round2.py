"""M3 L30, second round of the counter-review (EF-EXP-031): the answers to N1 and N2, and the
minors. The red proofs of the reviewer stay in `test_l30_counter_review_2.py`."""

import shutil
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.detach import DetachRefusedError, full_detach_phrase
from shelfhold.export.targets.kavita.dto import HIDDEN_DIR_PATTERNS
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from tests.detach_env import ADMIN, DetachEnv, detach_env
from tests.fakes.kavita import API_KEY, OTHER_KEY
from tests.rename_env import export_chapters

pytestmark = pytest.mark.req("EF-EXP-031")

ROOT_REMOTE = "/manga"


@pytest.fixture
def d() -> Iterator[DetachEnv]:
    with detach_env() as made:
        export_chapters(made.env, "Dr Stone", ["1", "2"])
        made.revoker.fake = made.wired.fake
        for name in ("recycle", "received"):
            shutil.rmtree(made.root / ".shelfhold" / name, ignore_errors=True)
        shutil.rmtree(made.env.lib.manga / ".shelfhold", ignore_errors=True)
        yield made


def _detach(d: DetachEnv, **more: Any) -> Any:
    target = d.env.target_id
    return d.detacher.apply(
        target, principal=ADMIN, typed=full_detach_phrase(target), client=d.wired.client,
        revoker=d.revoker, credential="the account password", **more,
    )  # fmt: skip


def _share_the_root(d: DetachEnv) -> None:
    with d.env.engine.begin() as conn:
        conn.execute(
            sa.insert(export.export_target).values(
                id=new_ulid(), kind="folder", name="Another", path_map=[
                    {"local": str(d.env.lib.library), "remote": "/lib", "mode": "rw"}
                ],
            )
        )  # fmt: skip


def _hidden_kept(d: DetachEnv) -> bool:
    return any(p in HIDDEN_DIR_PATTERNS for p in d.kavita_patterns())


def _kill_the_key(d: DetachEnv) -> None:
    d.wired.fake.auth_keys = [k for k in d.wired.fake.auth_keys if k["key"] != API_KEY]


# -- N1: the report says why the pattern stays, and only where it must --------------------------


def test_the_report_says_why_the_pattern_stays(d: DetachEnv) -> None:
    d.wired.fake.libraries[0]["folders"] = [ROOT_REMOTE]
    _share_the_root(d)
    report = _detach(d)
    assert _hidden_kept(d)
    assert ("hidden_pattern_kept", 1) in {(x.code, x.count) for x in report.leftovers}
    assert report.settings[0].hidden_pattern_kept


def test_a_library_away_from_the_kept_root_still_loses_the_pattern(d: DetachEnv) -> None:
    d.wired.fake.libraries[0]["folders"] = ["/elsewhere"]
    _share_the_root(d)
    report = _detach(d)
    assert not _hidden_kept(d)
    assert "hidden_pattern_kept" not in {x.code for x in report.leftovers}


def test_nothing_kept_nothing_pinned(d: DetachEnv) -> None:
    d.wired.fake.libraries[0]["folders"] = [ROOT_REMOTE]
    report = _detach(d)
    assert not _hidden_kept(d)
    assert "hidden_pattern_kept" not in {x.code for x in report.leftovers}


# -- N2: `key_gone` -------------------------------------------------------------------------------


def test_key_gone_gives_the_settings_back_through_the_account_and_says_so(d: DetachEnv) -> None:
    _kill_the_key(d)
    report = _detach(
        d, key_gone=True, account=d.account_session(stored_fingerprint=d.instance_fingerprint())
    )
    assert report.complete and report.key_gone and not report.key_revoked
    assert "key_gone" in {x.code for x in report.leftovers}
    assert not _hidden_kept(d)  # the settings went back
    assert d.kavita_patterns() == ["*.tmp"]
    assert [k["key"] for k in d.wired.fake.auth_keys] == [OTHER_KEY]  # no other key touched


def test_key_gone_never_leaves_a_live_key(d: DetachEnv) -> None:
    """The flag says the key is gone; the account says otherwise: it is revoked as usual."""
    report = _detach(d, key_gone=True, account=d.account_session())
    assert report.complete and report.key_revoked and not report.key_gone
    assert API_KEY not in [k["key"] for k in d.wired.fake.auth_keys]


def test_key_gone_needs_the_accounts_password(d: DetachEnv) -> None:
    before = d.intent_ids()
    target = d.env.target_id
    with pytest.raises(DetachRefusedError) as refused:
        d.detacher.apply(
            target, principal=ADMIN, typed=full_detach_phrase(target), client=d.wired.client,
            revoker=d.revoker, credential=None, key_gone=True,
        )  # fmt: skip
    assert refused.value.reason == "credential_required"
    assert d.intent_ids() == before and (d.root / ".shelfhold").exists()


def test_without_the_flag_a_dead_key_is_said_and_the_flag_is_proposed(d: DetachEnv) -> None:
    _kill_the_key(d)
    report = _detach(d)
    assert not report.complete and not report.key_gone
    assert "settings_not_restored" in {x.code for x in report.leftovers}


# -- minor: an identifier given to another key is not deleted on the rerun -----------------------


def test_a_reused_identifier_is_left_alone_on_the_rerun(d: DetachEnv) -> None:
    d.revoker.raises = RuntimeError("Kavita went away")  # the stop: the intention is `applying`
    first = _detach(d)
    assert not first.complete
    d.revoker.raises = None
    # Kavita gave the freed identifier to a key created in between
    d.wired.fake.auth_keys = [{"id": 1, "name": "created since", "key": OTHER_KEY}]
    second = _detach(d)
    assert second.complete and second.key_revoked
    assert "key_id_reused" in {x.code for x in second.leftovers}
    assert [k["key"] for k in d.wired.fake.auth_keys] == [OTHER_KEY]  # still there


# -- F1: the folder a stop leaves is still under a `.shelfhold` segment ---------------------------


def test_a_foreign_erasing_folder_is_left_alone_and_said(d: DetachEnv) -> None:
    mine = d.root / ".shelfhold-erasing"
    mine.mkdir()
    (mine / "notes.txt").write_bytes(b"the admin's own")
    report = _detach(d)
    assert not report.complete
    assert "erasing_name_taken" in {x.code for x in report.leftovers}
    assert (mine / "notes.txt").read_bytes() == b"the admin's own"  # untouched
    assert (d.root / ".shelfhold").exists()  # nothing was erased: the intention is given up
    assert d.intents("pending", "applying") == []


# -- f2: `--key-gone` writes nothing to another Kavita ---------------------------------------------


def test_key_gone_refuses_a_kavita_that_is_not_the_linked_one(d: DetachEnv) -> None:
    linked = d.instance_fingerprint()
    _kill_the_key(d)
    d.wired.fake.reinstall("synthetic-install-0002")  # a fresh install: library ids start again
    before = list(d.wired.fake.libraries)
    report = _detach(d, key_gone=True, account=d.account_session(stored_fingerprint=linked))
    assert not report.complete
    assert "instance_changed" in {x.code for x in report.leftovers}
    assert d.wired.fake.libraries == before
    assert d.wired.fake.requests_to("/api/Library/update") == []


# -- f3: the intention holds the identifier and nothing of the key -----------------------------


def test_the_revocation_intention_holds_no_trace_of_the_key_value(d: DetachEnv) -> None:
    d.revoker.raises = RuntimeError("Kavita went away")
    _detach(d)
    rows = [str(i.expected_file_id) for i in d.intents() if "revoke_key" in str(i.expected_file_id)]
    assert rows and all(API_KEY not in r and "digest" not in r for r in rows)
