"""The minimal diagnostic bundle (M2 L30; ENF-LEG-009, EF-BIB-041, ADR-0030, ADR-0033, I33).

`build_bundle` writes one zip archive an admin can attach to a support request. It is **closed by
default**: each member is built from a positive list, never from a dump that is cleaned up after.

- `manifest.json`: format, version, schema revision, what was left out and why (counts only);
- `config.json`: every setting, classified (`as_is` for numbers, switches and closed words; `set`
  or `unset` for secrets, URLs and files; `default` or `custom` for the volumes; a count for the
  lists; a class for the bind address), with where each value came from;
- `status.json`: the codes and counts of `shelfhold status`, never its messages (they quote paths
  and names);
- `schema.sql`: the DDL of the database, no row;
- `content.json`: counts and sizes by format; a watermarked file is cited by its hash and its size
  only (EF-BIB-041); no content file ever enters;
- `sources.json`: sub-sources and series counts under `<source-N>` (ENF-LEG-009, GAP-6-22);
- `logs.jsonl`: only with `--log-file`; each JSON line reduced to its allowed fields (event, level,
  logger, timestamp, closed words, numbers, internal ULIDs), user identifiers as `<user-N>`.

Then the last barrier: every member is searched for each value the instance holds that must not
leave it (registered secrets, sealed secrets once opened, titles, paths, source URLs and hosts,
sub-source names, user names and ids, the configured URLs and the machine's host name), in clear,
percent-encoded and in base64, and for any e-mail address, IP address or URL. A hit writes
nothing: the error names the category, never the value.
"""

from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import platform as host_platform
import re
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

import sqlalchemy as sa
from pydantic import SecretStr

from shelfhold import __version__
from shelfhold.config import ResolvedSettings, Settings
from shelfhold.export.domain.provenance_mirror import is_diagnostic_excluded
from shelfhold.infrastructure.archives import ArchiveWriter
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.logging import (
    known_secret_values,
    redact_source_urls,
    register_secret_value,
)
from shelfhold.infrastructure.migrate import current_revision, head_revision
from shelfhold.infrastructure.schema import acquisition, identity, metadata, platform, storage
from shelfhold.infrastructure.secret_text import (
    SecretIndex,
    contains_secret,
    decoded_views,
    secret_forms,
)
from shelfhold.infrastructure.secrets import registry
from shelfhold.infrastructure.secrets.atomic import write_private
from shelfhold.infrastructure.secrets.diagnostics import register_for_scrubbing, store_for
from shelfhold.infrastructure.secrets.errors import KeyringError
from shelfhold.infrastructure.secrets.vault import SecretVault

__all__ = [
    "BUNDLE_FORMAT",
    "SETTING_CLASSES",
    "BundleReport",
    "DiagnosticInputRefusedError",
    "DiagnosticLeakError",
    "DiagnosticOutputExistsError",
    "DiagnosticOutputRefusedError",
    "Pseudonyms",
    "build_bundle",
    "reduce_config",
    "reduce_log_line",
    "reduce_status",
]

BUNDLE_FORMAT: Final = "1.0"
MAX_LOG_BYTES: Final = 64 * 1024 * 1024
MAX_LOG_LINES: Final = 50_000  # the last ones are kept
FIRST_PARTY_PLUGINS: Final = frozenset({"suwayomi", "lncrawl"})  # named as is; others: <plugin-N>

# -- the settings, each in one class: a new setting is in none, and the test of the classes fails
_AS_IS = (
    "port", "log_level", "log_format", "received_cbz_max_bytes", "auth_hardened", "fresh_auth_s",
    "naming_segment_policy_confirmed", "cli_allow_host", "allow_network_data_dir",
    "disk_pause_percent", "disk_pause_gib", "disk_resume_percent", "disk_resume_gib",
    "wake_timeout_s", "wake_session_s", "maintenance_window_start", "maintenance_window_end",
    "timezone", "retention_events_days", "retention_audit_days", "backup_retention",
    "backup_stale_hours",
)  # fmt: skip
_PRESENCE = (
    "public_url", "api_token", "secret_key_file", "frontend_dir", "source_url", "wake_url",
    "wake_token", "backup_dir", "backup_passphrase_file",
)  # fmt: skip
_VOLUMES = ("data_dir", "store_dir", "library_dir")
_COUNTS = ("plugin_repos", "active_sources", "forward_auth_trusted_cidrs", "wake_suwayomi",
           "wake_kavita")  # fmt: skip
SETTING_CLASSES: Final[Mapping[str, str]] = {
    **dict.fromkeys(_AS_IS, "as_is"),
    **dict.fromkeys(_PRESENCE, "presence"),
    **dict.fromkeys(_VOLUMES, "volume"),
    **dict.fromkeys(_COUNTS, "count"),
    "umask": "octal",
    "host": "bind",
}

# -- closed shapes of what may be written ----------------------------------------------------
_CLOSED_WORD = re.compile(r"^(?:[a-z][a-z0-9_.:-]{0,63}|<(?:source|host|user|plugin)-\d+>)$")
_SETTING_WORD = re.compile(r"^[A-Za-z0-9_:/+.-]{1,40}$")  # INFO, Europe/Paris, 03:00
_CLASS_NAME = re.compile(r"^[A-Z][A-Za-z0-9_]{0,63}$")
_LOGGER = re.compile(r"^[a-z_][a-z0-9_.]{0,127}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?(?:Z|[+-]\d\d:\d\d)?$")
_HTTP_METHOD = re.compile(r"^(?:GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS)$")
_ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_NUMERIC_KEY = re.compile(r"^[a-z][a-z0-9_]{0,48}$")
OTHER: Final = "<other>"

_LOG_WORD_KEYS = frozenset({
    "event", "level", "outcome", "kind", "job_kind", "code", "reason", "state", "method", "queue",
    "mode", "format", "media", "retention", "decision", "result", "capability", "transport",
    "status", "phase", "action", "strategy",
})  # fmt: skip
_LOG_ID_KEYS = frozenset({
    "job_id", "unit_id", "series_id", "edition_id", "binding_id", "target_id", "destination_id",
    "root_id", "dst_root_id", "plan_id", "export_series_id", "variant_id", "work_id", "shelf_id",
    "content_id", "intent_id", "item_id", "export_item_id", "remote_library_id",
})  # fmt: skip
_LOG_USER_KEYS = frozenset({"user_id", "actor_id", "account_id", "member_id", "owner_id"})
_LOG_SHAPES: Final[Mapping[str, re.Pattern[str]]] = {
    "timestamp": _TIMESTAMP, "logger": _LOGGER, "error": _CLASS_NAME, "method": _HTTP_METHOD,
    **dict.fromkeys(_LOG_ID_KEYS, _ULID),
}  # fmt: skip

# -- what a bundle must never hold, wherever it is: the last barrier ---------------------------
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_IPV6 = re.compile(r"(?<![\w:])(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{0,4}(?![\w:])")
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[A-Za-z0-9\[]", re.IGNORECASE)
# Columns whose text names something of the household: titles, names, paths, URLs, remote ids.
_SENSITIVE_COLUMN = re.compile(
    r"(title|name|label|url|path|remote_id|username|host|series|memo|settings|alias|value|email)",
)
_NOT_SENSITIVE_COLUMN = re.compile(r"(_by|_hash|_kind|_lang|_state|_at)$")
_SHORT = 12  # a value shorter than this is matched as a whole word only
_CODE_LIKE = re.compile(r"^[a-z0-9_.:-]+$")  # a short lower-case word cannot be told from a code
_PRODUCT_WORDS = frozenset({"shelfhold", "localhost"})


class DiagnosticLeakError(Exception):
    """A member would carry a value that must not leave the instance: nothing is written."""

    def __init__(self, category: str, member: str) -> None:
        super().__init__(f"{category} in {member}")
        self.category = category
        self.member = member


class DiagnosticInputRefusedError(Exception):
    """A `--log-file` the bundle will not read (the reason is a code, never the path)."""

    def __init__(self, reason: str, index: int) -> None:
        super().__init__(f"log file {index}: {reason}")
        self.reason = reason
        self.index = index


class DiagnosticOutputExistsError(Exception):
    """The output file exists: a bundle never overwrites anything."""


class DiagnosticOutputRefusedError(Exception):
    """The output is under /store, the library or a physical root: those are written through
    `fsops` only, and a bundle has nothing to do next to the contents."""


@dataclass(frozen=True)
class BundleReport:
    path: Path
    members: tuple[str, ...]
    log_lines_kept: int
    log_lines_dropped: int


# -- pseudonyms -------------------------------------------------------------------------------


@dataclass
class Pseudonyms:
    """`<source-N>`, `<host-N>`, `<user-N>`, `<plugin-N>`: the same thing always gets the same
    stand-in inside one bundle, and nothing links a stand-in to its value outside it."""

    sources: dict[tuple[str, str], str] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)  # casefolded name or host -> stand-in
    users: dict[str, str] = field(default_factory=dict)
    plugins: dict[str, str] = field(default_factory=dict)
    hosts: dict[str, str] = field(default_factory=dict)

    def source(self, plugin_id: str, source_key: str) -> str:
        key = (plugin_id, source_key)
        if key not in self.sources:
            self.sources[key] = f"<source-{len(self.sources) + 1}>"
        return self.sources[key]

    def alias(self, value: str | None, token: str) -> None:
        if value:
            self.names.setdefault(value.casefold(), token)

    def plugin(self, plugin_id: str) -> str:
        if plugin_id in FIRST_PARTY_PLUGINS:
            return plugin_id
        return self.plugins.setdefault(plugin_id, f"<plugin-{len(self.plugins) + 1}>")

    def user(self, value: str) -> str:
        return self.users.setdefault(value, f"<user-{len(self.users) + 1}>")

    def url(self, url: str) -> str:
        """A URL becomes its host's stand-in: a known source's `<source-N>`, else `<host-N>`."""
        try:
            host = (urlsplit(url).hostname or "").casefold()
        except ValueError:
            host = ""
        if host in self.names:
            return self.names[host]
        return self.hosts.setdefault(host, f"<host-{len(self.hosts) + 1}>")

    def word(self, value: str) -> str:
        """A closed word that happens to be a source's name or host is replaced as well."""
        return self.names.get(value.casefold(), value)


def _host_of(url: str) -> str | None:
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


# -- the members ------------------------------------------------------------------------------


def _setting_value(settings: Settings, name: str, kind: str) -> object:
    value = getattr(settings, name)
    if kind == "presence":
        return "set" if value not in (None, "") else "unset"
    if kind == "volume":
        default = Settings.model_fields[name].default
        return "default" if Path(value) == Path(default) else "custom"
    if kind == "count":
        return len(value)
    if kind == "octal":
        return oct(int(value))
    if kind == "bind":
        try:
            address = ipaddress.ip_address(str(value))
        except ValueError:
            return "name" if value != "localhost" else "loopback"
        if address.is_loopback:
            return "loopback"
        return "all_interfaces" if address.is_unspecified else "address"
    if isinstance(value, SecretStr):  # a secret classified "as is" by mistake is never shown
        return "set"
    if isinstance(value, bool | int | float):
        return value
    text = str(value)
    return text if _SETTING_WORD.fullmatch(text) else OTHER


def reduce_config(resolved: ResolvedSettings) -> dict[str, object]:
    """Every classified setting, and where its value came from (`env`, `toml`, `db`,
    `default`). A setting without a class is not written: `unclassified` counts them."""
    settings = resolved.values
    values = {
        name: _setting_value(settings, name, kind) for name, kind in sorted(SETTING_CLASSES.items())
    }
    origins = {
        name: resolved.fields[name].source
        for name in sorted(SETTING_CLASSES)
        if name in resolved.fields
    }
    unclassified = sum(1 for name in Settings.model_fields if name not in SETTING_CLASSES)
    return {"values": values, "origins": origins, "unclassified": unclassified}


def _code(value: object) -> str:
    return value if isinstance(value, str) and _CLOSED_WORD.fullmatch(value) else OTHER


def reduce_status(report: Mapping[str, object]) -> dict[str, object]:
    """`status` as codes and counts: its messages quote paths, names and URLs, never kept."""
    raw_warnings = report.get("warnings")
    warnings = sorted(
        {_code(w.get("code")) for w in raw_warnings if isinstance(w, Mapping)}
        if isinstance(raw_warnings, list)
        else set()
    )
    raw_waiting = report.get("waiting")
    waiting = Counter(
        _code(item.get("state"))
        for item in (raw_waiting if isinstance(raw_waiting, list) else [])
        if isinstance(item, Mapping)
    )
    counts = {
        key: len(value)
        for key, value in sorted(report.items())
        if isinstance(value, list) and key not in ("warnings", "waiting") and _code(key) == key
    }
    reduced: dict[str, object] = {
        "warnings": warnings,
        "waiting": dict(sorted(waiting.items())),
        "counts": counts,
    }
    where = report.get("jurisdiction")
    if isinstance(where, Mapping):
        reduced["jurisdiction_confirmed"] = bool(where.get("confirmed"))
    known = report.get("known_bad")
    if isinstance(known, Mapping) and known:
        age = known.get("age_days")
        reduced["known_bad"] = {
            "outdated": bool(known.get("outdated")),
            "age_days": age if isinstance(age, int) else None,
        }
    return reduced


def _schema_sql(database: Path) -> str:
    revision = current_revision(database)
    lines = [f"-- shelfhold schema, revision {revision or 'none'}", ""]
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type DESC, name"
        ).fetchall()
    finally:
        connection.close()
    lines.extend(f"{row[0]};" for row in rows)
    return "\n".join(lines) + "\n"


def _content(connection: sa.Connection) -> dict[str, object]:
    """Counts and bytes by format, kind and retention; a watermarked file by hash and size."""
    c = storage.content.c
    by_format = [
        {
            "format": _code(row.format), "kind": _code(row.kind),
            "retention": _code(row.retention), "watermarked": bool(row.watermarked),
            "count": int(row.n), "bytes": int(row.total or 0),
        }
        for row in connection.execute(
            sa.select(
                c.format, c.kind, c.retention, c.watermarked,
                sa.func.count().label("n"), sa.func.sum(c.size).label("total"),
            )
            .group_by(c.format, c.kind, c.retention, c.watermarked)
            .order_by(c.format, c.kind, c.retention, c.watermarked)
        )
    ]  # fmt: skip
    watermarked = [
        {"hash": row.original_sha256, "size": int(row.size)}
        for row in connection.execute(
            sa.select(c.original_sha256, c.size)
            .where(c.watermarked.is_(True))
            .order_by(c.original_sha256, c.size)
        )
    ]
    return {"by_format": by_format, "watermarked": watermarked}


def _sources(connection: sa.Connection, names: Pseudonyms) -> dict[str, object]:
    """Sub-sources and upstream series under `<source-N>`: never a name, a key or a domain."""
    sub = acquisition.sub_source.c
    rows = connection.execute(
        sa.select(sub.plugin_id, sub.source_key, sub.name, sub.lang, sub.enabled).order_by(
            sub.plugin_id, sub.source_key
        )
    ).all()
    sub_sources = []
    for row in rows:
        token = names.source(row.plugin_id, row.source_key)
        names.alias(row.name, token)
        names.alias(row.source_key, token)
        sub_sources.append(
            {
                "source": token,
                "plugin": names.plugin(row.plugin_id),
                "lang": _code(row.lang),
                "enabled": bool(row.enabled),
            }
        )
    item = acquisition.source_item.c
    items: Counter[str] = Counter()
    for seen in connection.execute(
        sa.select(item.plugin_id, item.source_key, item.url).order_by(
            item.plugin_id, item.source_key, item.id
        )
    ):
        token = names.source(seen.plugin_id, seen.source_key)
        names.alias(seen.source_key, token)
        host = _host_of(seen.url) if seen.url else None
        names.alias(host, token)
        items[token] += 1
    return {"sub_sources": sub_sources, "items": dict(sorted(items.items()))}


# -- log lines --------------------------------------------------------------------------------


def _log_word(value: str, names: Pseudonyms) -> str:
    value = redact_source_urls(value, names.url)
    return _code(names.word(value))


def reduce_log_line(
    line: Mapping[str, object], names: Pseudonyms, users: frozenset[str] = frozenset()
) -> dict[str, object]:
    """One JSON log line, reduced to its allowed fields. Anything else is dropped, not masked:
    a free text, a path, a URL, a name and every field this list does not know."""
    kept: dict[str, object] = {}
    for key, value in line.items():
        if not isinstance(key, str):
            continue
        if key in _LOG_USER_KEYS and isinstance(value, str):
            kept[key] = names.user(value)
        elif isinstance(value, str) and value in users:
            kept[key] = names.user(value)  # a user's id under any other name
        elif isinstance(value, str) and (shape := _LOG_SHAPES.get(key)) and shape.fullmatch(value):
            kept[key] = value
        elif key in _LOG_WORD_KEYS and isinstance(value, str):
            kept[key] = _log_word(value, names)
        elif (
            isinstance(value, bool | int | float)
            and _NUMERIC_KEY.fullmatch(key)
            and key not in _LOG_USER_KEYS
        ):
            kept[key] = value
    return kept


def _log_lines(text: str) -> Iterator[dict[str, object] | None]:
    """Each line as `docker compose logs` writes it (`name-1  | {...}`), parsed; None when it is
    not one JSON object (a traceback, a plain-text line: dropped)."""
    for raw in text.splitlines():
        start = raw.find("{")
        if start == -1:
            yield None
            continue
        try:
            parsed = json.loads(raw[start:])
        except ValueError:
            yield None
            continue
        yield parsed if isinstance(parsed, dict) else None


def _under(path: Path, roots: Iterable[Path]) -> bool:
    for root in roots:
        try:
            if path == root or path.is_relative_to(root):
                return True
        except ValueError:
            continue
    return False


_ARCHIVE_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"\x1f\x8b", b"BZh", b"\xfd7zXZ", b"%PDF", b"Rar!")


def _read_log_input(
    path: Path, index: int, *, content_roots: Sequence[Path], content_hashes: frozenset[str]
) -> str:
    """The text of one `--log-file`, or a refusal: the provenance mirror (I33), anything under
    /store, /library or a physical root, an archive, or a file whose hash is a content's."""
    if is_diagnostic_excluded(str(path)):
        raise DiagnosticInputRefusedError("provenance_mirror", index)
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise DiagnosticInputRefusedError("unreadable", index) from None
    if is_diagnostic_excluded(str(resolved)):
        raise DiagnosticInputRefusedError("provenance_mirror", index)
    if not resolved.is_file():
        raise DiagnosticInputRefusedError("not_a_file", index)
    if _under(resolved, content_roots):
        raise DiagnosticInputRefusedError("content_area", index)
    if resolved.stat().st_size > MAX_LOG_BYTES:
        raise DiagnosticInputRefusedError("too_large", index)
    try:
        data = resolved.read_bytes()
    except OSError:
        raise DiagnosticInputRefusedError("unreadable", index) from None
    if data.startswith(_ARCHIVE_MAGIC):
        raise DiagnosticInputRefusedError("archive", index)
    if "sha256:" + hashlib.sha256(data).hexdigest() in content_hashes:
        raise DiagnosticInputRefusedError("content_file", index)
    return data.decode("utf-8", errors="replace")


# -- the last barrier -------------------------------------------------------------------------


@dataclass
class _Forbidden:
    """Values that must not leave, by category: long ones looked up with their encoded forms
    (percent-encoded, base64), short ones as whole words only."""

    long: dict[str, set[str]] = field(default_factory=dict)
    short: dict[str, set[str]] = field(default_factory=dict)
    forms: dict[str, set[str]] = field(default_factory=dict)  # already every form (the registry)
    _compiled: list[tuple[str, SecretIndex | re.Pattern[str]]] | None = None

    def add(self, category: str, value: object, *, word: bool = False) -> None:
        """`word`: a short value is a whole word to look for even when it reads like a code."""
        if isinstance(value, dict | list):
            for item in value.values() if isinstance(value, dict) else value:
                self.add(category, item)
            return
        if not isinstance(value, str):
            return
        text = value.strip()
        if len(text) < 4 or not any(ch.isalpha() for ch in text):
            return
        if len(text) >= _SHORT:
            self.long.setdefault(category, set()).add(text)
        elif word or not _CODE_LIKE.fullmatch(text):
            self.short.setdefault(category, set()).add(text)
        if "://" in text:
            host = _host_of(text)
            if host:
                self.add(f"{category}.host", host, word=True)

    def _patterns(self) -> list[tuple[str, SecretIndex | re.Pattern[str]]]:
        if self._compiled is None:
            compiled: list[tuple[str, SecretIndex | re.Pattern[str]]] = []
            for category, values in sorted(self.long.items()):
                index = SecretIndex(form for value in values for form in _forms(value))
                compiled.append((category, index))
            for category, values in sorted(self.forms.items()):
                compiled.append((category, SecretIndex(values)))
            for category, values in sorted(self.short.items()):
                alternatives = "|".join(re.escape(v) for v in sorted(values, key=len, reverse=True))
                compiled.append((category, re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)")))
            self._compiled = compiled
        return self._compiled

    def check(self, member: str, text: str) -> None:
        views = decoded_views(text)
        for category, pattern in self._patterns():
            if isinstance(pattern, SecretIndex):
                if contains_secret(text, pattern):
                    raise DiagnosticLeakError(category, member)
            elif any(pattern.search(view) for view in views):
                raise DiagnosticLeakError(category, member)
        for category, shape in (("email", _EMAIL), ("ip_address", _IPV4), ("url", _URL)):
            if any(shape.search(view) for view in views):
                raise DiagnosticLeakError(category, member)
        if any(_is_ipv6(m.group()) for view in views for m in _IPV6.finditer(view)):
            raise DiagnosticLeakError("ip_address", member)


def _is_ipv6(text: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(text), ipaddress.IPv6Address)
    except ValueError:
        return False


def _forms(value: str) -> Iterator[str]:
    yield from secret_forms(value)
    folded = value.casefold()
    if folded != value:
        yield from secret_forms(folded)


def _sensitive_columns() -> Iterator[sa.Column[object]]:
    for table in metadata.tables.values():
        for column in table.columns:
            name = column.name
            if not _SENSITIVE_COLUMN.search(name) or _NOT_SENSITIVE_COLUMN.search(name):
                continue
            if name.endswith("_id") and name != "remote_id":
                continue
            yield column


def _collect_forbidden(connection: sa.Connection, forbidden: _Forbidden) -> frozenset[str]:
    """What the database holds of the household. Returns the user ids (kept as `<user-N>`)."""
    for column in _sensitive_columns():
        rows = connection.execute(sa.select(column).where(column.is_not(None)).distinct())
        for (value,) in rows:
            forbidden.add(f"{column.table.name}.{column.name}", value)
    users = frozenset(str(v) for v in connection.execute(sa.select(identity.user.c.id)).scalars())
    for user_id in users:
        forbidden.long.setdefault("user.id", set()).add(user_id)
    return users


def _register_instance_secrets(settings: Settings, connection: sa.Connection) -> dict[str, int]:
    """The secrets a running instance would have registered (L04's scrubber, by value): the
    keyring, the secret settings, and every sealed row opened once."""
    for name in Settings.model_fields:
        value = getattr(settings, name)
        if isinstance(value, SecretStr):
            register_secret_value(value.get_secret_value())
    opened = failed = 0
    try:
        store = store_for(settings)
        keyring = store.load() if store.exists() else None
    except (KeyringError, OSError, ValueError):
        keyring = None
    if keyring is None:
        return {"sealed_opened": 0, "sealed_unreadable": 0, "keyring": 0}
    register_for_scrubbing(keyring)
    vault = SecretVault(keyring)
    for name, sealed in registry.sealed_tables().items():
        ids = connection.execute(
            sa.select(sealed.pk).where(sealed.table.c.secret_kid.is_not(None))
        ).scalars()
        for row_id in ids:
            try:
                vault.open_row(connection, name, str(row_id))
                opened += 1
            except Exception:  # a row that does not open is counted, not quoted
                failed += 1
    return {"sealed_opened": opened, "sealed_unreadable": failed, "keyring": 1}


# -- writing ----------------------------------------------------------------------------------


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _write_zip(out: Path, members: Mapping[str, str]) -> None:
    """Owner-only, never over an existing file (`write_private`: written beside, linked). The
    archive is the deterministic one of `ArchiveWriter`: no timestamp, no host, no tool."""
    buffer = io.BytesIO()
    with ArchiveWriter(buffer) as archive:
        for name, text in members.items():
            archive.add(name, text.encode("utf-8"), compress=True)
    try:
        write_private(out, buffer.getvalue(), overwrite=False)
    except FileExistsError:
        raise DiagnosticOutputExistsError(out.name) from None


def build_bundle(
    resolved: ResolvedSettings,
    out: Path,
    *,
    status: Callable[[], Mapping[str, object]],
    log_files: Sequence[Path] = (),
    now: datetime,
) -> BundleReport:
    """Build, check and write the bundle. `status` gives the report of `shelfhold status`
    (the CLI's), read only. Raises before writing anything when an input is refused, when a
    value that must not leave is found, or when `out` exists."""
    settings = resolved.values
    if out.exists() or out.is_symlink():
        raise DiagnosticOutputExistsError(out.name)
    database = settings.database_path
    migrated = database.exists() and current_revision(database) == head_revision()
    names = Pseudonyms()
    forbidden = _Forbidden()
    users: frozenset[str] = frozenset()
    content_hashes: frozenset[str] = frozenset()
    roots = [settings.store_dir, settings.library_dir]
    secrets: dict[str, int] = {}
    members: dict[str, str] = {}
    sections: dict[str, str] = {}
    if migrated:
        engine = create_db_engine(database)
        try:
            with engine.connect() as connection:
                secrets = _register_instance_secrets(settings, connection)
                users = _collect_forbidden(connection, forbidden)
                content_hashes = frozenset(
                    connection.execute(sa.select(storage.content.c.original_sha256)).scalars()
                )
                roots += [
                    Path(p)
                    for p in connection.execute(
                        sa.select(platform.physical_root.c.path_entry)
                    ).scalars()
                ]
                content = _content(connection)
                sources = _sources(connection, names)
        finally:
            engine.dispose()
    content_roots = [r.resolve() for r in roots]
    if _under(out.parent.resolve(), content_roots):
        raise DiagnosticOutputRefusedError(out.name)
    texts = [
        _read_log_input(path, index, content_roots=content_roots, content_hashes=content_hashes)
        for index, path in enumerate(log_files, start=1)
    ]
    for name in ("public_url", "source_url", "wake_url"):
        forbidden.add(f"settings.{name}", getattr(settings, name))
    forbidden.add("settings.host", settings.host, word=True)
    hostname = host_platform.node()
    if hostname.casefold() not in _PRODUCT_WORDS:  # `shelfhold` names every logger
        forbidden.add("machine.hostname", hostname, word=True)
    forbidden.forms["secret"] = set(known_secret_values())

    members["config.json"] = _dump(reduce_config(resolved))
    try:
        members["status.json"] = _dump(reduce_status(status()))
    except Exception:  # a status that fails is a finding, not a crash
        members["status.json"] = _dump({"unavailable": "status_failed"})
    if database.exists():
        members["schema.sql"] = _schema_sql(database)
    if migrated:
        members["content.json"] = _dump(content)
        members["sources.json"] = _dump(sources)
    else:
        sections["database"] = "absent" if not database.exists() else "not_migrated"
    kept = dropped = 0
    if texts:
        reduced: list[str] = []
        for text in texts:
            for line in _log_lines(text):
                if line is None:
                    dropped += 1
                    continue
                reduced.append(json.dumps(reduce_log_line(line, names, users), sort_keys=True))
        dropped += max(0, len(reduced) - MAX_LOG_LINES)
        reduced = reduced[-MAX_LOG_LINES:]
        kept = len(reduced)
        members["logs.jsonl"] = "".join(f"{line}\n" for line in reduced)
    manifest = {
        "format": BUNDLE_FORMAT,
        "shelfhold_version": __version__,
        "created_at": now.isoformat(),
        "schema_revision": current_revision(database) if database.exists() else None,
        "schema_head": head_revision(),
        "members": sorted(["manifest.json", *members]),
        "sections": sections,
        "log_lines": {"files": len(texts), "kept": kept, "dropped": dropped},
        "secrets_checked": secrets,
        "never_included": [
            "content_files", "watermarked_files", "provenance_mirror", "titles", "paths",
            "source_urls", "sub_source_names", "secrets", "addresses", "user_identifiers",
        ],
    }  # fmt: skip
    members = {"manifest.json": _dump(manifest), **dict(sorted(members.items()))}
    for name, text in members.items():
        forbidden.check(name, name + "\n" + text)
    _write_zip(out, members)
    return BundleReport(out, tuple(members), kept, dropped)
