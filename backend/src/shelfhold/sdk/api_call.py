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

import re
from collections.abc import Iterator
from typing import Annotated, Any, Final, Literal

from pydantic import ConfigDict, Field, model_validator

from shelfhold.sdk.fsops import ContentRef, FsOpPlanError, PlannedContent, check_content_ref
from shelfhold.sdk.models import StrictSdkModel

_CONTENT_TYPE: Final = re.compile(r"[A-Za-z0-9][\w.+-]{0,126}/[A-Za-z0-9][\w.+-]{0,126}")
_FIELD_NAME: Final = re.compile(r"[\w.\-\[\]]{1,128}")
_DOT_SEGMENT: Final = re.compile(r"(\.|%2e){1,2}", re.IGNORECASE)
_ENCODED_SEPARATOR: Final = re.compile(r"%(2f|5c)", re.IGNORECASE)
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
    """Described, not executed: the core runs it with its own secrets through its Fetcher."""

    method: str
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


def _check_path(path: str) -> None:
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise ApiCallError("path is relative to the configured host: one leading slash, no host")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path):
        raise ApiCallError("control character in path")
    if any(ch in path for ch in ("\\", "?", "#")) or _ENCODED_SEPARATOR.search(path):
        raise ApiCallError("path carries no query, fragment, backslash or encoded separator")
    if any(_DOT_SEGMENT.fullmatch(part) for part in path.split("/")):
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


def check_api_call(call: ApiCall, planned: PlannedContent) -> None:
    """Raise `ApiCallError` unless `call` is one a destination may ask for. The host calls it on
    its own copy of the call, right before it runs it, with what it planned for this binding."""
    if type(call) is not ApiCall:
        raise ApiCallError("a call is an exact ApiCall, never a subclass")
    _check_path(call.path)
    body = call.body
    if not isinstance(body, ContentBody):
        return
    if call.method.upper() in ("GET", "HEAD"):
        raise ApiCallError("a content body does not go with GET or HEAD")
    _check_encoding(body.encoding)
    try:
        check_content_ref(body.content_ref, planned)
    except FsOpPlanError as error:
        raise ApiCallError(str(error)) from None


__all__ = [
    "ApiCall",
    "ApiCallError",
    "ContentBody",
    "JsonBody",
    "MultipartEncoding",
    "RawEncoding",
    "check_api_call",
]
