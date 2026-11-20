# SPDX-License-Identifier: MIT
"""A call a destination describes and the host executes (SDK 1.4: `ApiCall.body`).

Described, not executed: the core runs it with its own secrets, through its Fetcher and the SSRF
guard. An `ApiCall` carries a path relative to the host the configuration fixed, never a host, and
no credential: `auth` only names which one the core injects. A body is one of:

- `json_body` (SDK 1.3, still accepted) or `JsonBody`: JSON the destination builds;
- `ContentBody`: bytes the host owns, sent raw or as multipart. The reference follows the rules of
  `FsOp.write` (`check_content_ref`): an `export_item` planned for this call and this binding, a
  watermarked file never towards an `externe` destination, the provenance mirror never.

No value of a call (query, body, multipart field) appears in a `repr`, in a validation error or in
a refusal of the host: they may hold a secret, so they are never echoed.
"""

import json
import re
import typing
import unicodedata
from collections.abc import Iterator
from typing import Annotated, Any, Final, Literal

from pydantic import ConfigDict, Field, model_validator

from shelfhold_sdk.fsops import ContentRef, FsOpPlanError, PlannedContent, check_content_ref
from shelfhold_sdk.models import StrictSdkModel

_CONTENT_TYPE: Final = re.compile(
    r"[A-Za-z0-9][\w.+-]{0,126}/[A-Za-z0-9][\w.+-]{0,126}", re.ASCII
)  # a header is ASCII: `\w` must not match Unicode letters
_FIELD_NAME: Final = re.compile(r"[\w.\-\[\]]{1,128}", re.ASCII)
_PERCENT: Final = re.compile(r"%([0-9A-Fa-f]{2})")
_BAD_PERCENT: Final = re.compile(r"%(?![0-9A-Fa-f]{2})")
MAX_DECODE_PASSES: Final = 4
Method = Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]
MAX_MULTIPART_FIELDS: Final = 64


class ApiCallError(FsOpPlanError):
    """A call the host refuses to execute. The message names the rule, never a value."""


class _Quiet(StrictSdkModel):
    model_config = ConfigDict(hide_input_in_errors=True)


class JsonBody(_Quiet):
    model_config = ConfigDict(frozen=True)

    kind: Literal["json"] = "json"
    value: Any = None

    def __repr_args__(self) -> Iterator[tuple[str | None, Any]]:
        yield "kind", self.kind


class RawEncoding(_Quiet):
    """The bytes are the whole body."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["raw"] = "raw"
    content_type: str | None = None


class MultipartEncoding(_Quiet):
    """The bytes are one part, named `file_field`, next to plain text `fields`."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["multipart"] = "multipart"
    file_field: str
    fields: dict[str, str] = Field(default_factory=dict)

    def __repr_args__(self) -> Iterator[tuple[str | None, Any]]:
        yield "file_field", self.file_field
        yield "fields", sorted(self.fields)


Encoding = Annotated[RawEncoding | MultipartEncoding, Field(discriminator="kind")]


class ContentBody(_Quiet):
    """A body made of bytes the host owns (SDK 1.4); the plan never carries them."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["content"] = "content"
    content_ref: ContentRef
    encoding: Encoding = Field(default_factory=RawEncoding)


class ApiCall(_Quiet):
    """Described, not executed: the core runs it with its own secrets through its Fetcher.

    Frozen and revalidated (SDK 1.4): the host never executes the object a plugin handed over but
    the copy `parse_api_call` returns, as `parse_destination_plan` does for a plan."""

    model_config = ConfigDict(frozen=True, revalidate_instances="always")

    method: Method
    path: str  # relative: the host is fixed by the configuration
    query: dict[str, Any] = Field(default_factory=dict)
    json_body: Any = None  # SDK 1.3 shape, still accepted
    body: Annotated[JsonBody | ContentBody, Field(discriminator="kind")] | None = None  # 1.4
    auth: Literal["target_admin", "target_user", "none"]
    expect: list[int] = Field(default_factory=lambda: [200])

    @model_validator(mode="after")
    def _one_body(self) -> "ApiCall":
        if self.json_body is not None and self.body is not None:
            raise ValueError("an ApiCall has json_body or body, not both")
        return self

    def __repr_args__(self) -> Iterator[tuple[str | None, Any]]:
        yield "method", self.method
        yield "path", re.split(r"[?#]", self.path, maxsplit=1)[0]
        yield "auth", self.auth
        yield "query", sorted(self.query)
        yield "body", self.body if self.body is not None else self.json_body is not None


def _decoded(path: str) -> str:
    """The path as the last server in a chain would read it: percent-decoded as long as a `%`
    remains (a proxy may decode twice). An escape that is not two hex digits, or that stands for a
    byte beyond ASCII (an overlong form of `.` or `/`), is refused."""
    current = path
    for _ in range(MAX_DECODE_PASSES):
        if "%" not in current:
            return current
        if _BAD_PERCENT.search(current):
            raise ApiCallError("path has a malformed percent escape")
        if any(int(m[1], 16) > 0x7F for m in _PERCENT.finditer(current)):
            raise ApiCallError("path has a percent escape beyond ASCII")
        current = _PERCENT.sub(lambda m: chr(int(m[1], 16)), current)
    raise ApiCallError("path is percent-encoded too many times")


def _check_path(path: str) -> None:
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise ApiCallError("path is relative to the configured host: one leading slash, no host")
    decoded = _decoded(path)
    if unicodedata.normalize("NFKC", decoded) != decoded:
        raise ApiCallError("path is not in NFKC form: full-width or compatibility characters")
    if any(unicodedata.category(ch) in ("Cc", "Cf", "Cs") for ch in decoded):
        raise ApiCallError("control character in path")  # C0, DEL, C1, zero-width, bidi
    if any(ch in decoded for ch in ("\\", "?", "#", ";")):
        raise ApiCallError("path carries no query, fragment, backslash or `;` parameter")
    if decoded.count("/") != path.count("/") or decoded.startswith("//"):
        raise ApiCallError("path carries no encoded separator")
    if any(part in (".", "..") for part in decoded.split("/")):
        raise ApiCallError("path has a `.` or `..` segment")


def _check_encoding(encoding: RawEncoding | MultipartEncoding) -> None:
    if isinstance(encoding, RawEncoding):
        if encoding.content_type is not None and not _CONTENT_TYPE.fullmatch(encoding.content_type):
            raise ApiCallError("content_type is not a plain media type")
        return
    if not _FIELD_NAME.fullmatch(encoding.file_field):
        raise ApiCallError("multipart file_field is not a plain field name")
    if len(encoding.fields) > MAX_MULTIPART_FIELDS:
        raise ApiCallError("too many multipart fields")
    if not all(_FIELD_NAME.fullmatch(name) for name in encoding.fields):
        raise ApiCallError("a multipart field name is not a plain name")


def _exact(call: object) -> ApiCall:
    """The call, if every part of it is exactly an SDK type (never a subclass, a stand-in or an
    object built around the validation); `ApiCallError` otherwise."""
    if type(call) is not ApiCall:
        raise ApiCallError("a call is an exact ApiCall, never a subclass")
    body = call.body
    if body is not None and type(body) not in (JsonBody, ContentBody):
        raise ApiCallError("a body is an exact JsonBody or ContentBody")
    encodings = (RawEncoding, MultipartEncoding)
    if isinstance(body, ContentBody) and type(body.encoding) not in encodings:
        raise ApiCallError("an encoding is an exact RawEncoding or MultipartEncoding")
    if call.method not in typing.get_args(Method):
        raise ApiCallError("method is not one of the closed list")
    if call.auth not in ("target_admin", "target_user", "none"):
        raise ApiCallError("auth is not one of the closed list")
    if call.json_body is not None and body is not None:
        raise ApiCallError("an ApiCall has json_body or body, not both")
    return call


def check_api_call(call: ApiCall, planned: PlannedContent, *, can_purge: bool = False) -> None:
    """Raise `ApiCallError` unless `call` is one a destination may ask for. The host calls it on
    its own copy of the call (`parse_api_call`), right before it runs it, with what it planned for
    this binding. `planned` is required: its `egress` must be the one the instance attested, not
    the default the plugin declared. A `DELETE` is accepted only for a destination that declares
    `can_purge` (pass `capabilities.can_purge`); the host's own purge is another path."""
    _exact(call)
    _check_path(call.path)
    if call.method == "DELETE" and not can_purge:
        raise ApiCallError("DELETE needs a destination that declares can_purge")
    body = call.body
    if not isinstance(body, ContentBody):
        return
    if call.method in ("GET", "HEAD"):
        raise ApiCallError("a content body does not go with GET or HEAD")
    _check_encoding(body.encoding)
    try:
        check_content_ref(body.content_ref, planned)
    except FsOpPlanError as error:
        raise ApiCallError(str(error)) from None


def parse_api_call(raw: str | bytes | object) -> ApiCall:
    """The call a destination returned, as JSON text or as an object. What comes back is the
    host's own copy: an object is checked for exact types, then passed through JSON."""
    if isinstance(raw, str | bytes | bytearray):
        return ApiCall.model_validate_json(raw)
    if isinstance(raw, ApiCall):
        try:
            return ApiCall.model_validate_json(_exact(raw).model_dump_json())
        except (ValueError, TypeError) as error:
            if isinstance(error, ApiCallError):
                raise
            raise ApiCallError("the call cannot be read back as JSON") from None
    return ApiCall.model_validate_json(json.dumps(raw))


__all__ = [
    "ApiCall",
    "ApiCallError",
    "ContentBody",
    "JsonBody",
    "MultipartEncoding",
    "RawEncoding",
    "check_api_call",
    "parse_api_call",
]
