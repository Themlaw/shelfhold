"""The v1 document of a signed plugin index (EF-SRC-017, D14): parsed strictly, nothing guessed.

`version` is the monotone number the trusted comment repeats; `expires` bounds how long the
index may be believed (and never by more than 90 days); each entry carries the sha256 of its
wheel, so a download is checked against what was signed, not against what was served; `revoked`
names plugin keys and wheel fingerprints; `next_pubkey` exists only during a rotation and
designates the key the pin moves to once the same document is signed by both keys.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import urlsplit

from shelfhold.plugin_host.index.minisign import PinnedKey, PluginIndexError, parse_public_key
from shelfhold.plugin_host.rpc.runner_wire import KEY

__all__ = [
    "MAX_AHEAD",
    "IndexDocument",
    "IndexEntry",
    "parse_document",
    "trusted_comment_of",
]

FORMAT = 1
MAX_AHEAD = timedelta(days=90)  # an index is never believed for more than three months
MAX_VERSION = 2**63 - 1  # what the database can keep: a bigger number is not a version
_DOCUMENT_FIELDS = frozenset({"format", "version", "expires", "plugins", "revoked", "next_pubkey"})
_ENTRY_FIELDS = frozenset(
    {
        "key",
        "version",
        "api",
        "rpc_protocol",
        "wheel_url",
        "sha256",
        "min_host",
        "deprecated",
        "replaced_by",
    }
)
_REVOKED_FIELDS = frozenset({"plugin_keys", "wheel_sha256"})
# a host version is dotted numbers, nothing else compares (M6 L09b, EF-SRC-030)
VERSION: Final = re.compile(r"\d+(?:\.\d+)*")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """The `object_pairs_hook` of the parse: a key that appears twice is a refusal, not a guess.

    JSON allows it, the last value winning; an index that says two things about one name is
    not an index this host believes.
    """
    seen: dict[str, Any] = {}
    for name, value in pairs:
        if name in seen:
            raise ValueError(f"the member {name!r} appears twice")
        seen[name] = value
    return seen


@dataclass(frozen=True)
class IndexEntry:
    """One plugin as the index vouches for it; `sha256` is what a download is checked against."""

    key: str
    version: str
    api: int
    rpc_protocol: int
    wheel_url: str
    sha256: str
    min_host: str
    deprecated: bool
    replaced_by: str | None


@dataclass(frozen=True)
class IndexDocument:
    format: int
    version: int
    expires: datetime
    plugins: tuple[IndexEntry, ...] = ()
    revoked_plugin_keys: frozenset[str] = field(default_factory=frozenset)
    revoked_wheel_sha256: frozenset[str] = field(default_factory=frozenset)
    next_pubkey: PinnedKey | None = None


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise PluginIndexError("document_invalid", f"{what} is missing or not text")
    return value


def _sha256(value: Any, what: str) -> str:
    text = _text(value, what)
    if not text.startswith("sha256:") or len(text) != 71:
        raise PluginIndexError("document_invalid", f"{what} is not a sha256 fingerprint")
    if any(c not in "0123456789abcdef" for c in text[7:]):
        raise PluginIndexError("document_invalid", f"{what} is not a sha256 fingerprint")
    return text


def _positive_int(value: Any, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise PluginIndexError("document_invalid", f"{what} is not a positive integer")
    return value


def _web_url(value: Any, what: str) -> str:
    text = _text(value, what)
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username:
        raise PluginIndexError("document_invalid", f"{what} is not a plain web address")
    return text


def _entry(raw: Any) -> IndexEntry:
    if not isinstance(raw, dict):
        raise PluginIndexError("document_invalid", "a plugin entry is not an object")
    if not set(raw) <= _ENTRY_FIELDS:
        raise PluginIndexError("document_invalid", "a plugin entry has unknown members")
    key = _text(raw.get("key"), "a plugin key")
    if not KEY.fullmatch(key):
        raise PluginIndexError("document_invalid", f"the plugin key {key!r} is not a plugin key")
    replaced_by = raw.get("replaced_by")
    if replaced_by is not None:
        replaced_by = _text(replaced_by, "the replacing plugin key")
        # a plugin that replaces itself says nothing, and a key spelled like no key names
        # nothing: both are refused here, a revoked one below, once the document is whole
        if replaced_by == key:
            raise PluginIndexError(
                "document_invalid", "a plugin's replaced_by names the entry itself"
            )
        if not KEY.fullmatch(replaced_by):
            raise PluginIndexError(
                "document_invalid", f"the replacing key {replaced_by!r} is not a plugin key"
            )
    min_host = _text(raw.get("min_host"), "the minimum host version")
    if not VERSION.fullmatch(min_host):
        # "latest" or "9.x" compares below every host and would never refuse anything
        raise PluginIndexError(
            "document_invalid", f"the minimum host version {min_host!r} is not a dotted version"
        )
    deprecated = raw.get("deprecated")
    if not isinstance(deprecated, bool):
        raise PluginIndexError("document_invalid", "deprecated is not a boolean")
    return IndexEntry(
        key=key,
        version=_text(raw.get("version"), "a plugin version"),
        api=_positive_int(raw.get("api"), "the api version"),
        rpc_protocol=_positive_int(raw.get("rpc_protocol"), "the rpc protocol"),
        wheel_url=_web_url(raw.get("wheel_url"), "the wheel address"),
        sha256=_sha256(raw.get("sha256"), "the wheel sha256"),
        min_host=min_host,
        deprecated=deprecated,
        replaced_by=replaced_by,
    )


def parse_document(data: bytes) -> IndexDocument:
    """Parse and check a v1 index document; anything unexpected is a refusal, never a guess."""
    try:
        raw = json.loads(data, object_pairs_hook=_object)
    except (ValueError, UnicodeDecodeError):
        raise PluginIndexError("document_invalid", "the index is not a JSON document") from None
    if not isinstance(raw, dict):
        raise PluginIndexError("document_invalid", "the index is not a JSON object")
    if not set(raw) <= _DOCUMENT_FIELDS:
        raise PluginIndexError("document_invalid", "the index has unknown members")
    fmt = raw.get("format")
    if not isinstance(fmt, int) or isinstance(fmt, bool) or fmt != FORMAT:
        raise PluginIndexError("document_invalid", "the index format is not 1")
    version = raw.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise PluginIndexError("document_invalid", "the index version is not a positive integer")
    if version > MAX_VERSION:
        raise PluginIndexError("document_invalid", "the index version is out of range")
    expires = _expires(raw.get("expires"))
    plugins = raw.get("plugins")
    if not isinstance(plugins, list):
        raise PluginIndexError("document_invalid", "the plugins are not a list")
    revoked = raw.get("revoked")
    if not isinstance(revoked, dict):
        raise PluginIndexError("document_invalid", "the revocations are not an object")
    if not set(revoked) <= _REVOKED_FIELDS:
        raise PluginIndexError("document_invalid", "the revocations have unknown members")
    keys, wheels = revoked.get("plugin_keys"), revoked.get("wheel_sha256")
    if not isinstance(keys, list) or not isinstance(wheels, list):
        raise PluginIndexError("document_invalid", "the revocations are not lists")
    next_pubkey = None
    if raw.get("next_pubkey") is not None:
        next_pubkey = parse_public_key(
            _text(raw.get("next_pubkey"), "the next public key"), code="document_invalid"
        )
    entries = tuple(_entry(entry) for entry in plugins)
    seen: set[str] = set()
    for entry in entries:  # two entries for one key: which wheel is vouched for? Refused.
        if entry.key in seen:
            raise PluginIndexError("document_invalid", f"the plugin {entry.key!r} has two entries")
        seen.add(entry.key)
    revoked_keys = frozenset(_text(k, "a revoked plugin key") for k in keys)
    for entry in entries:  # a successor the document itself revoked replaces nothing
        if entry.replaced_by is not None and entry.replaced_by in revoked_keys:
            raise PluginIndexError(
                "document_invalid",
                f"the replacing key {entry.replaced_by!r} of {entry.key!r} is revoked",
            )
    return IndexDocument(
        format=FORMAT,
        version=version,
        expires=expires,
        plugins=entries,
        revoked_plugin_keys=revoked_keys,
        revoked_wheel_sha256=frozenset(_sha256(w, "a revoked wheel sha256") for w in wheels),
        next_pubkey=next_pubkey,
    )


def _expires(value: Any) -> datetime:
    text = _text(value, "the expiry")
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise PluginIndexError("document_invalid", "the expiry is not an ISO 8601 date") from None
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise PluginIndexError("document_invalid", "the expiry is not an instant")
    return moment.astimezone(UTC)


def trusted_comment_of(document: IndexDocument) -> str:
    """The one trusted comment an index of this host carries: its version and its expiry."""
    return f"shelfhold-index v1 version={document.version} expires={_iso(document.expires)}"


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
