# SPDX-License-Identifier: MIT
"""The file operation plan a destination describes and the host executes (SDK 1.3, frozen).

A destination never touches a file. It returns a plan: JSON, every path relative to the binding
root, no content (a `ContentRef` names bytes the host owns). The host parses the plan with
`parse_destination_plan`, and calls `check_destination_plan` again right before it executes it,
because a plan can be built or altered around the validation.

Three things are refused whatever the plugin says:

- a path that is not rooted: absolute, drive letter, `..`, `.` or empty segment, control character;
- a reserved path: the host area `.shelfhold` and everything the host keeps next to it
  (`.shelfhold-mount-id`, `.shelfhold-probe-*`), under any disguise of case, Unicode form,
  separator, trailing dot or space, at any depth, and any short 8.3 name (`SHELFH~1`);
- `unlink`: a destination can recycle, never delete for good. The operation stays in `FsOp` because
  the host builds its own (`purge_unlink`), and it is not a member of a destination plan.

Overwriting (frozen with SDK 1.3): `rename` never overwrites (the host renames with
RENAME_NOREPLACE and refuses when the target exists), and `write` refuses a path that exists and
that the plugin did not write itself. A plan that names one target twice, whatever the case, is
refused statically: two writes, a write and a rename onto it, or two renames onto it.

The host executes the copy `parse_destination_plan` returns, never the object the plugin handed
over: the plan goes through JSON first, so a subclass or a property that answers differently the
second time cannot survive, and the models are frozen. The executor of M4 must call
`check_destination_plan` on that copy just before each run (an end-of-lot criterion of M4).
"""

import json
import re
import unicodedata
from datetime import datetime
from typing import Final, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from shelfhold.sdk.models import StrictSdkModel

HOST_PREFIX: Final = ".shelfhold"
MAX_PLAN_OPS: Final = 10_000
MAX_PATH_BYTES: Final = 4096
_DRIVE: Final = re.compile(r"[A-Za-z]:")
_SHORT_NAME: Final = re.compile(r"~[0-9]")


class FsOpPlanError(ValueError):
    """A plan, or one operation of a plan, that the host refuses to execute."""


class ContentRef(StrictSdkModel):
    """Reference to bytes the host owns; a plan never carries raw content."""

    model_config = ConfigDict(frozen=True, revalidate_instances="always")

    kind: Literal["export_item", "sidecar"]
    ref: str


class FsOp(StrictSdkModel):
    """One file operation (passe 8, H3). The set of operations and the fields are frozen."""

    model_config = ConfigDict(frozen=True, revalidate_instances="always")

    op: Literal["mkdir", "write", "write_inline", "rename", "recycle", "touch_mtime", "unlink"]
    rel_path: str  # relative to the binding root; the host refuses any escape
    dst_rel_path: str | None = None
    content_ref: ContentRef | None = None
    mtime: datetime | None = None


def _fold(component: str) -> str:
    """What a file system that is lax about case, Unicode form and trailing dots makes of it."""
    text = unicodedata.normalize("NFKC", component)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")  # zero-width, bidi
    return text.casefold().rstrip(". ")


def _reserved_component(part: str) -> bool:
    folded = _fold(part)
    # `SHELFH~1` is the short name a Windows share gives `.shelfhold`
    return folded.startswith(HOST_PREFIX) or _SHORT_NAME.search(folded) is not None


def is_reserved_path(path: str) -> bool:
    """True when any component of `path` is, or begins like, the host area, or looks like a short
    8.3 name (`~` then a digit). Lenient on purpose: it also answers for a path that is not
    rooted, and never resolves `..` away."""
    return any(_reserved_component(part) for part in path.replace("\\", "/").split("/"))


def _unrooted(path: str) -> str | None:
    if not isinstance(path, str) or not path:
        return "empty path"
    if len(path.encode("utf-8", "surrogatepass")) > MAX_PATH_BYTES:
        return "path too long"
    if any(unicodedata.category(ch) == "Cc" for ch in path):
        return "control character in path"
    if "\\" in path or path.startswith(("/", "~")) or _DRIVE.match(path):
        return "path is not relative to the binding root"
    if any(part in ("", ".", "..") for part in path.split("/")):
        return "path has an empty, `.` or `..` segment"
    return None


def _check_path(path: str | None, what: str) -> None:
    if path is None:
        return
    problem = _unrooted(path)
    if problem is not None:
        raise FsOpPlanError(f"{what}: {problem}")
    if is_reserved_path(path):
        raise FsOpPlanError(f"{what}: the host area is reserved")


def _check_shape(op: FsOp) -> None:
    kind = op.op
    needs_dst = kind == "rename"
    needs_content = kind in ("write", "write_inline")
    if needs_dst != (op.dst_rel_path is not None):
        raise FsOpPlanError(f"{kind}: dst_rel_path is for rename and only rename")
    if needs_content != (op.content_ref is not None):
        raise FsOpPlanError(f"{kind}: content_ref is for write and write_inline and only them")
    if kind == "touch_mtime" and op.mtime is None:
        raise FsOpPlanError("touch_mtime: mtime is required")
    if op.mtime is not None and kind not in ("touch_mtime", "write", "write_inline"):
        raise FsOpPlanError(f"{kind}: mtime is not allowed")


def check_destination_operation(op: FsOp) -> None:
    """The host calls this on each operation just before it runs it."""
    if type(op) is not FsOp or (
        op.content_ref is not None and type(op.content_ref) is not ContentRef
    ):
        raise FsOpPlanError("an operation is an exact FsOp, never a subclass")
    if op.op == "unlink":
        raise FsOpPlanError("unlink belongs to the host: a destination plan cannot carry it")
    _check_path(op.rel_path, "rel_path")
    _check_path(op.dst_rel_path, "dst_rel_path")
    _check_shape(op)


def _target(op: FsOp) -> str | None:
    """The path an operation creates, folded as a lax file system would; None if it creates none."""
    if op.op == "rename":
        path = op.dst_rel_path
    elif op.op in ("write", "write_inline"):
        path = op.rel_path
    else:
        return None
    return None if path is None else "/".join(_fold(part) for part in path.split("/"))


def check_destination_plan(plan: "FsOpPlan") -> None:
    """Raise `FsOpPlanError` unless every operation of `plan` is one a destination may ask for.
    It re-reads the fields as they are now, so it also catches a plan built with `model_construct`
    or altered after validation. Run it on the host's copy, right before executing."""
    if type(plan) is not FsOpPlan:
        raise FsOpPlanError("a plan is an exact FsOpPlan, never a subclass")
    if len(plan.ops) > MAX_PLAN_OPS:
        raise FsOpPlanError("plan has too many operations")
    created: set[str] = set()
    for op in plan.ops:
        check_destination_operation(op)
        target = _target(op)
        if target is None:
            continue
        if target in created:
            raise FsOpPlanError("the plan would overwrite a path it already wrote or moved onto")
        created.add(target)


class FsOpPlan(StrictSdkModel):
    """A destination's plan: JSON in, JSON out, validated on construction, frozen."""

    model_config = ConfigDict(frozen=True, revalidate_instances="always")

    ops: tuple[FsOp, ...] = Field(max_length=MAX_PLAN_OPS)

    @field_validator("ops", mode="before")
    @classmethod
    def _exact_operations(cls, value: object) -> object:
        if isinstance(value, list | tuple) and any(
            isinstance(op, FsOp) and type(op) is not FsOp for op in value
        ):
            raise FsOpPlanError("an operation is an exact FsOp, never a subclass")
        return value

    @model_validator(mode="after")
    def _destination_rules(self) -> "FsOpPlan":
        check_destination_plan(self)
        return self


def parse_destination_plan(raw: str | bytes | object) -> FsOpPlan:
    """The plan a destination returned, as JSON text or as an object. What comes back is the
    host's own copy: an object is checked for exact types, then passed through JSON."""
    if isinstance(raw, str | bytes | bytearray):
        return FsOpPlan.model_validate_json(raw)
    if isinstance(raw, FsOpPlan):
        check_destination_plan(raw)  # exact types and rules, on what the plugin handed over
        return FsOpPlan.model_validate_json(raw.model_dump_json())
    return FsOpPlan.model_validate_json(json.dumps(raw))


__all__ = [
    "ContentRef",
    "FsOp",
    "FsOpPlan",
    "FsOpPlanError",
    "check_destination_operation",
    "check_destination_plan",
    "is_reserved_path",
    "parse_destination_plan",
]
