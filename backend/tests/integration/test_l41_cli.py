"""L41: the operating commands of the library, driven through the real parser with a channel that
records the call and answers what the server would (the server side is tested in test_l41_jobs)."""

from typing import Any

import pytest

from shelfhold.interfaces.cli import _adopt_ops as adopt_ops
from shelfhold.interfaces.cli import detach, probe, purge
from shelfhold.interfaces.cli.registry import build_parser

pytestmark = pytest.mark.req("EF-INT-023", "EF-EXP-051", "EF-EXP-056", "EF-BIB-052")


class Channel:
    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, method: str, **params: Any) -> Any:
        self.calls.append((method, params))
        return self.answers[method]


def _run(handler: Any, argv: list[str], channel: Channel) -> int:
    args = build_parser().parse_args(argv)
    return int(getattr(handler, "__wrapped__", handler)(args, channel))


PROPOSAL = {
    "export_series_id": "S1", "series": "Dr Stone", "state": "proposed", "command": "do it",
    "candidates": [{"source_key": "a", "remote_id": "1", "score": 0.97}],
}  # fmt: skip


def test_pair_lists_then_confirms(capsys: pytest.CaptureFixture[str]) -> None:
    channel = Channel(
        {
            "ops.pair_propose": {"proposals": [PROPOSAL]},
            "ops.pair_confirm": {
                "state": "paired",
                "export_series_id": "S1",
                "follow_from": "51",
                "command": None,
            },
        }
    )
    assert _run(adopt_ops.run_pair, ["adopt", "pair"], channel) == 0
    assert "Dr Stone" in capsys.readouterr().out
    assert _run(adopt_ops.run_pair, ["adopt", "pair", "--series", "S1"], channel) == 2
    argv = ["adopt", "pair", "--series", "S1", "--source-key", "a", "--remote-id", "1"]
    assert _run(adopt_ops.run_pair, argv, channel) == 0
    assert channel.calls[-1][0] == "ops.pair_confirm"
    channel.answers["ops.pair_confirm"] = {"state": "coverage_unknown", "command": "x"}
    assert _run(adopt_ops.run_pair, argv, channel) == 3


def test_pair_auto_report_then_batch(capsys: pytest.CaptureFixture[str]) -> None:
    report = {"fingerprint": "f", "ready": [PROPOSAL], "held": []}
    channel = Channel({"ops.pair_auto": report})
    assert _run(adopt_ops.run_pair_auto, ["adopt", "--pair-auto"], channel) == 0
    assert "f" in capsys.readouterr().out
    channel.answers["ops.pair_auto"] = {
        "paired": [{"export_series_id": "S1", "follow_from": None}],
        "refused": [{"export_series_id": "S2", "reason": "ambiguous"}],
    }
    argv = ["adopt", "--pair-auto", "--confirm", "f"]
    assert _run(adopt_ops.run_pair_auto, argv, channel) == 3
    assert channel.calls[-1][1]["fingerprint"] == "f"


def test_set_follow_from_and_the_migration_confirmation(capsys: pytest.CaptureFixture[str]) -> None:
    channel = Channel({
        "ops.adopt_set_follow_from": {}, "ops.adopt_confirm_migration": {},
        "ops.migration_report": {"fingerprint": "f", "detected": True, "steps": ["adopted"],
                                 "series": [{"export_series_id": "S", "series_dir": "d",
                                             "signals": ["x"]}]},
    })  # fmt: skip
    argv = ["adopt", "set-follow-from", "Dr Stone", "51"]
    assert _run(adopt_ops.run_set_follow_from, argv, channel) == 0
    assert channel.calls[-1][1]["chapter"] == "51"
    assert _run(adopt_ops.run_confirm_migration, ["adopt", "--confirm-migration"], channel) == 2
    argv = ["adopt", "--confirm-migration", "--confirm", "f"]
    assert _run(adopt_ops.run_confirm_migration, argv, channel) == 0
    channel.answers["ops.migration_report"] = {"fingerprint": "f", "detected": False}
    assert _run(adopt_ops.run_confirm_migration, ["adopt", "--confirm-migration"], channel) == 0


def test_repatriation_needs_the_preview_first(capsys: pytest.CaptureFixture[str]) -> None:
    plan = {"fingerprint": "f", "series": 1, "chapters": 2, "files": 3, "bytes_to_copy": 4,
            "free_bytes": 5, "problems": [{"path": "p", "status": "missing"}],
            "ready": False}  # fmt: skip
    applied = {"copied": 3, "already_present": 0,
               "check": {"missing": [], "different": ["d"], "safe_to_unmount": False}}  # fmt: skip
    channel = Channel({"ops.repatriate_plan": plan, "ops.repatriate_apply": applied})
    assert _run(adopt_ops.run_repatriate, ["adopt", "--repatriate"], channel) == 2
    argv = ["adopt", "--repatriate", "--dry-run"]
    assert _run(adopt_ops.run_repatriate, argv, channel) == 0
    argv = ["adopt", "--repatriate", "--confirm", "f"]
    assert _run(adopt_ops.run_repatriate, argv, channel) == 3
    applied["check"]["different"] = []  # type: ignore[index]
    applied["check"]["safe_to_unmount"] = True  # type: ignore[index]
    assert _run(adopt_ops.run_repatriate, argv, channel) == 0


def test_detach_purge_and_probe(capsys: pytest.CaptureFixture[str]) -> None:
    channel = Channel({
        "ops.detach_restore_upstream": {"complete": False, "managed": {"d": ["f"]},
            "series": [{"binding_id": "b", "restored_from": "snap", "ok": False, "error": "e"}]},
        "ops.purge": {"units": ["u"], "erased": 1, "unreachable_roots": ["r"], "pending_roots": [],
                      "alerts": ["a"], "out_of_reach": ["backups"], "complete": False},
        "ops.probe": {"ok": False, "folder": "/x", "failed_step": "write", "cause": "EACCES",
                      "yaml_hint": "group_add: [1000]"},
    })  # fmt: skip
    assert _run(adopt_ops.run_detach, ["detach", "--restore-upstream"], channel) == 3
    assert detach.run(build_parser().parse_args(["detach"]), None) == 2
    assert _run(purge._purge, ["purge", "--now", "--unit", "U", "--confirm", "U"], channel) == 3
    assert purge.run(build_parser().parse_args(["purge", "--unit", "U"]), None) == 2
    assert purge.run(build_parser().parse_args(["purge", "--now", "--unit", "U"]), None) == 2
    assert _run(probe.run, ["probe", "/x"], channel) == 1
    channel.answers["ops.probe"] = {"ok": True, "folder": "/x"}
    assert _run(probe.run, ["probe", "/x"], channel) == 0
    assert "/x" in capsys.readouterr().out
