"""The v1 document of a signed plugin index (EF-SRC-017, D14): parsed strictly, nothing guessed.

`version` is the monotone number the trusted comment repeats; `expires` bounds how long the
index may be believed (and never by more than 90 days); each entry carries the sha256 of its
wheel, so a download is checked against what was signed, not against what was served; `revoked`
names plugin keys and wheel fingerprints; `next_pubkey` exists only during a rotation and
designates the key the pin moves to once the same document is signed by both keys.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from shelfhold.plugin_host.index.minisign import PinnedKey, PluginIndexError, parse_public_key

__all__ = [
    "MAX_AHEAD",
    "IndexDocument",
    "IndexEntry",
    "parse_document",
    "trusted_comment_of",
]

FORMAT = 1
MAX_AHEAD = timedelta(days=90)  # an index is never believed for more than three months


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
    replaced_by = raw.get("replaced_by")
    if replaced_by is not None:
        replaced_by = _text(replaced_by, "the replacing plugin key")
    return IndexEntry(
        key=_text(raw.get("key"), "a plugin key"),
        version=_text(raw.get("version"), "a plugin version"),
        api=_positive_int(raw.get("api"), "the api version"),
        rpc_protocol=_positive_int(raw.get("rpc_protocol"), "the rpc protocol"),
        wheel_url=_web_url(raw.get("wheel_url"), "the wheel address"),
        sha256=_sha256(raw.get("sha256"), "the wheel sha256"),
        min_host=_text(raw.get("min_host"), "the minimum host version"),
        deprecated=raw.get("deprecated") is True,
        replaced_by=replaced_by,
    )


def parse_document(data: bytes) -> IndexDocument:
    """Parse and check a v1 index document; anything unexpected is a refusal, never a guess."""
    try:
        raw = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise PluginIndexError("document_invalid", "the index is not a JSON document") from None
    if not isinstance(raw, dict):
        raise PluginIndexError("document_invalid", "the index is not a JSON object")
    if raw.get("format") != FORMAT:
        raise PluginIndexError("document_invalid", "the index format is not 1")
    version = raw.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise PluginIndexError("document_invalid", "the index version is not a positive integer")
    expires = _expires(raw.get("expires"))
    plugins = raw.get("plugins")
    if not isinstance(plugins, list):
        raise PluginIndexError("document_invalid", "the plugins are not a list")
    revoked = raw.get("revoked")
    if not isinstance(revoked, dict):
        raise PluginIndexError("document_invalid", "the revocations are not an object")
    keys, wheels = revoked.get("plugin_keys"), revoked.get("wheel_sha256")
    if not isinstance(keys, list) or not isinstance(wheels, list):
        raise PluginIndexError("document_invalid", "the revocations are not lists")
    next_pubkey = None
    if raw.get("next_pubkey") is not None:
        next_pubkey = parse_public_key(
            _text(raw.get("next_pubkey"), "the next public key"), code="document_invalid"
        )
    return IndexDocument(
        format=FORMAT,
        version=version,
        expires=expires,
        plugins=tuple(_entry(entry) for entry in plugins),
        revoked_plugin_keys=frozenset(_text(k, "a revoked plugin key") for k in keys),
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
