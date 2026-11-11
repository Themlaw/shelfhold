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

import base64
import functools
import hashlib
import io
import ipaddress
import json
import platform as host_platform
import re
import sqlite3
import unicodedata
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
from shelfhold.infrastructure.secrets.errors import ExportError, KeyringError
from shelfhold.infrastructure.secrets.export import read_operator_file
from shelfhold.infrastructure.secrets.vault import IDENTIFIER_FIELDS, SecretVault

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
MAX_LOG_BYTES: Final = 64 * 1024 * 1024  # one file
MAX_LOG_TOTAL_BYTES: Final = 128 * 1024 * 1024  # all of them
MAX_LOG_FILES: Final = 8
MAX_LINE_BYTES: Final = 64 * 1024
_VIRTUAL_ROOTS: Final = (Path("/proc"), Path("/sys"), Path("/dev"))
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
_PSEUDONYM = re.compile(r"^<(?:source|host|user|plugin)-\d+>$")
_SETTING_WORD = re.compile(r"^[A-Za-z0-9_:/+.-]{1,40}$")  # INFO, Europe/Paris, 03:00
_LOGGER = re.compile(r"^shelfhold(?:\.[a-z_][a-z0-9_]*)*$")
_TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?(?:Z|[+-]\d\d:\d\d)?$")
_HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
_ULID = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_EPOCH_MS = (1_577_836_800_000, 4_102_444_800_000)  # 2020-01-01 .. 2100-01-01
OTHER: Final = "<other>"
_VOCABULARY_FILE: Final = Path(__file__).with_name("diagnostic_vocabulary.json")
_LOG_USER_KEYS = frozenset({"user_id", "actor_id", "account_id", "member_id", "owner_id"})


@dataclass(frozen=True)
class Vocabulary:
    """What the code writes in a log line (`tools/diagnostic_vocabulary.py`, checked in):
    a value outside it never enters the bundle."""

    events: frozenset[str]
    keys: frozenset[str]
    words: Mapping[str, frozenset[str]]
    errors: frozenset[str]
    loggers: frozenset[str]

    @classmethod
    def load(cls) -> Vocabulary:
        raw = json.loads(_VOCABULARY_FILE.read_text(encoding="utf-8"))
        shared = frozenset(raw["shared_words"])
        return cls(
            events=frozenset(raw["events"]),
            keys=frozenset(raw["keys"]),
            words={key: frozenset(values) | shared for key, values in raw["words"].items()},
            errors=frozenset(raw["errors"]),
            loggers=frozenset(raw["loggers"]),
        )

    def word(self, key: str, value: str) -> str:
        return value if value in self.words.get(key, frozenset()) else OTHER


@functools.cache
def vocabulary() -> Vocabulary:
    return Vocabulary.load()


def _plausible_ulid(value: str) -> bool:
    """A ULID whose time part falls in this century: a hexadecimal or base32 text that happens to
    have the shape of one (a title smuggled under an id field) does not."""
    if not _ULID.fullmatch(value):
        return False
    millis = 0
    for char in value[:10]:
        millis = millis * 32 + _CROCKFORD.index(char)
    return _ULID_EPOCH_MS[0] <= millis <= _ULID_EPOCH_MS[1]


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
# Names and titles (and the keys that name a source): a whole word, whatever the length.
_WORD_COLUMN = re.compile(r"(title|name|label|username|alias|source_key|plugin_id)")
_REFERENCE_TABLES = frozenset(
    {"work_kind", "work_relation_kind", "normalizer", "safeguard_backlog"}
)
_NOT_HOUSEHOLD_COLUMNS = frozenset({"title_lang_pref"})
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


def _content(connection: sa.Connection, words: Vocabulary) -> dict[str, object]:
    """Counts and bytes by format, kind and retention; a watermarked file by hash and size."""
    c = storage.content.c
    by_format = [
        {
            "format": words.word("format", row.format), "kind": words.word("kind", row.kind),
            "retention": words.word("retention", row.retention),
            "watermarked": bool(row.watermarked),
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


def _log_word(key: str, value: str, names: Pseudonyms, words: Vocabulary) -> str:
    """A word field: a URL or a source's name becomes its stand-in; otherwise the value must be a
    word the code writes under this key, else `<other>`."""
    replaced = names.word(redact_source_urls(value, names.url))
    if _PSEUDONYM.fullmatch(replaced):
        return replaced
    return words.word(key, value)


def reduce_log_line(
    line: Mapping[str, object],
    names: Pseudonyms,
    users: frozenset[str] = frozenset(),
    words: Vocabulary | None = None,
) -> dict[str, object]:
    """One JSON log line, reduced to the closed vocabulary of the code. A field the code never
    writes is dropped (its name included); a value outside the field's vocabulary becomes
    `<other>`: free text, paths, URLs, names, a title spelled as a slug or as a class name."""
    words = words or vocabulary()
    kept: dict[str, object] = {}
    for key, value in line.items():
        if not isinstance(key, str):
            continue
        if key in _LOG_USER_KEYS or (isinstance(value, str) and value in users):
            if isinstance(value, str):
                kept[key] = names.user(value)  # a user's id, under any name
            continue
        if key == "timestamp":
            kept[key] = value if isinstance(value, str) and _TIMESTAMP.fullmatch(value) else OTHER
        elif key == "event":
            kept[key] = value if isinstance(value, str) and value in words.events else OTHER
        elif key == "logger":
            known = isinstance(value, str) and _LOGGER.fullmatch(value) and value in words.loggers
            kept[key] = value if known else OTHER
        elif key == "error":
            kept[key] = value if isinstance(value, str) and value in words.errors else OTHER
        elif key == "method":
            kept[key] = value if value in _HTTP_METHODS else OTHER
        elif key not in words.keys and key not in words.words:
            continue  # a field the code never writes: not even its name enters
        elif key.endswith("_id"):
            kept[key] = value if isinstance(value, str) and _plausible_ulid(value) else OTHER
        elif isinstance(value, str):
            kept[key] = _log_word(key, value, names, words) if key in words.words else OTHER
        elif isinstance(value, bool | int | float) or value is None:
            kept[key] = value
        else:
            kept[key] = OTHER  # a list or a mapping: never walked
    return kept


def _log_lines(text: str, index: int) -> Iterator[dict[str, object] | None]:
    """Each line as `docker compose logs` writes it (`name-1  | {...}`), parsed; None when it is
    not one JSON object (a traceback, a plain-text line) or is longer than a log line can be:
    dropped. A line nested past what the parser can follow refuses the whole file."""
    for raw in text.splitlines():
        start = raw.find("{")
        if start == -1 or len(raw) - start > MAX_LINE_BYTES:
            yield None
            continue
        try:
            parsed = json.loads(raw[start:])
        except ValueError:
            yield None
            continue
        except RecursionError:
            raise DiagnosticInputRefusedError("nested_too_deep", index) from None
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
    path: Path,
    index: int,
    *,
    forbidden_roots: Sequence[Path],
    content_hashes: frozenset[str],
    budget: list[int],
) -> str:
    """The text of one `--log-file`, or a refusal: the provenance mirror (I33); anything under
    `/data`, `/store`, the library or a physical root (the database, the keyring, contents);
    `/proc`, `/sys`, `/dev`; a link; an archive; a file whose hash is a content's; a file past
    the bound, or past what is left of the total (`budget`)."""
    if is_diagnostic_excluded(str(path)):
        raise DiagnosticInputRefusedError("provenance_mirror", index)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise DiagnosticInputRefusedError("unreadable", index) from None
    if is_diagnostic_excluded(str(resolved)):
        raise DiagnosticInputRefusedError("provenance_mirror", index)
    if _under(resolved, forbidden_roots):
        raise DiagnosticInputRefusedError("protected_area", index)
    limit = min(MAX_LOG_BYTES, budget[0])
    try:
        data, status = read_operator_file(path, limit=limit, what="log file")
    except ExportError:
        raise DiagnosticInputRefusedError("not_a_regular_file", index) from None
    try:
        same = resolved.stat()
    except OSError:
        raise DiagnosticInputRefusedError("unreadable", index) from None
    if (same.st_dev, same.st_ino) != (status.st_dev, status.st_ino):
        raise DiagnosticInputRefusedError("not_a_regular_file", index)  # swapped under us
    if len(data) > limit:
        raise DiagnosticInputRefusedError("too_large", index)
    budget[0] -= len(data)
    if data.startswith(_ARCHIVE_MAGIC):
        raise DiagnosticInputRefusedError("archive", index)
    if "sha256:" + hashlib.sha256(data).hexdigest() in content_hashes:
        raise DiagnosticInputRefusedError("content_file", index)
    return data.decode("utf-8", errors="replace")


# -- the last barrier -------------------------------------------------------------------------


_Word = tuple[str, re.Pattern[str]]
_SEPARATORS = re.compile(r"[\s\-_.:/+]+")


def canonical(text: str) -> str:
    """The form two spellings of a name share: accents dropped (NFKD), case folded, every run of
    separators (space, `-`, `_`, `.`, `:`, `/`, `+`) one space. `Phare Oublié` and
    `phare_oublie` are one value."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _SEPARATORS.sub(" ", stripped.casefold()).strip()


@dataclass
class _Forbidden:
    """Values that must not leave, by category, compared in the canonical form (any case, any
    accent, slug or spaced). A long value is found anywhere, and in its encoded forms
    (percent-encoded, base64, hexadecimal, base32); a short one as a whole word."""

    long: dict[str, set[str]] = field(default_factory=dict)
    short: dict[str, set[str]] = field(default_factory=dict)
    encoded: dict[str, set[str]] = field(default_factory=dict)
    forms: dict[str, set[str]] = field(default_factory=dict)  # already every form (the registry)
    _compiled: (
        tuple[list[tuple[str, SecretIndex]], list[tuple[str, SecretIndex]], list[_Word]] | None
    ) = None

    def add(self, category: str, value: object, *, word: bool = False) -> None:
        """`word`: a name or a title, looked for as a whole word whatever its length or shape
        (false positives are accepted: the positive list has already replaced everything)."""
        if isinstance(value, dict | list):
            for item in value.values() if isinstance(value, dict) else value:
                self.add(category, item, word=word)
            return
        if not isinstance(value, str):
            return
        text = value.strip()
        folded = canonical(text)
        if len(folded) < (3 if word else 4) or not any(ch.isalpha() for ch in folded):
            return
        self._compiled = None
        if len(folded) >= _SHORT:
            self.long.setdefault(category, set()).add(folded)
            self.encoded.setdefault(category, set()).add(text)
        elif word or not _CODE_LIKE.fullmatch(text):
            self.short.setdefault(category, set()).add(folded)
        if "://" in text:
            host = _host_of(text)
            if host:
                self.add(f"{category}.host", host, word=True)

    def _patterns(
        self,
    ) -> tuple[list[tuple[str, SecretIndex]], list[tuple[str, SecretIndex]], list[_Word]]:
        if self._compiled is None:
            raw = [
                (category, SecretIndex(form for value in values for form in _forms(value)))
                for category, values in sorted(self.encoded.items())
            ]
            raw += [
                (category, SecretIndex(values)) for category, values in sorted(self.forms.items())
            ]
            folded = [
                (category, SecretIndex(values)) for category, values in sorted(self.long.items())
            ]
            words = []
            for category, values in sorted(self.short.items()):
                alternatives = "|".join(re.escape(v) for v in sorted(values, key=len, reverse=True))
                words.append(
                    (category, re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE))
                )
            self._compiled = (raw, folded, words)
        return self._compiled

    def check(self, member: str, text: str, words_in: Iterable[str] | None = None) -> None:
        """`words_in`: the texts a short value is looked for in (the values of a JSON member);
        None: the whole text."""
        views = decoded_views(text)
        folded_views = [canonical(view) for view in views]
        word_views = [
            canonical(view)
            for item in ([text] if words_in is None else words_in)
            for view in decoded_views(item)
        ]
        raw, folded, words = self._patterns()
        for category, index in raw:
            if contains_secret(text, index):
                raise DiagnosticLeakError(category, member)
        for category, index in folded:
            if any(index.find(view) for view in folded_views):
                raise DiagnosticLeakError(category, member)
        for category, pattern in words:
            if any(pattern.search(view) for view in word_views):
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
    """The value and its case-folded form, each as a URL or base64 carries it, in hexadecimal
    (either case) and in base32 (either case, padded or not)."""
    for form in dict.fromkeys((value, value.casefold(), canonical(value))):
        yield from secret_forms(form)
        raw = form.encode("utf-8")
        yield raw.hex()
        yield raw.hex().upper()
        b32 = base64.b32encode(raw).decode("ascii")
        yield from (b32, b32.rstrip("="), b32.lower(), b32.rstrip("=").lower())


def _sensitive_columns() -> Iterator[tuple[sa.Column[object], bool]]:
    """Each column whose text names something of the household, and whether it is a name or a
    title (looked for as a whole word, whatever its length)."""
    for table in metadata.tables.values():
        if table.name in _REFERENCE_TABLES:
            continue
        for column in table.columns:
            name = column.name
            if name in _NOT_HOUSEHOLD_COLUMNS or _NOT_SENSITIVE_COLUMN.search(name):
                continue
            word = bool(_WORD_COLUMN.search(name))
            if name.endswith("_id") and name not in ("remote_id", "plugin_id"):
                continue
            if word or _SENSITIVE_COLUMN.search(name):
                yield column, word


def _collect_forbidden(connection: sa.Connection, forbidden: _Forbidden) -> frozenset[str]:
    """What the database holds of the household. Returns the user ids (kept as `<user-N>`)."""
    for column, word in _sensitive_columns():
        rows = connection.execute(sa.select(column).where(column.is_not(None)).distinct())
        for (value,) in rows:
            if column.name == "plugin_id" and value in FIRST_PARTY_PLUGINS:
                continue  # named as is in `sources.json`
            forbidden.add(f"{column.table.name}.{column.name}", value, word=word)
    users = frozenset(str(v) for v in connection.execute(sa.select(identity.user.c.id)).scalars())
    for user_id in users:
        forbidden.add("user.id", user_id, word=True)
    return users


def _register_instance_secrets(
    settings: Settings, connection: sa.Connection, forbidden: _Forbidden
) -> dict[str, int]:
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
                plaintext = vault.open_row(connection, name, str(row_id))
                opened += 1
            except Exception:  # a row that does not open is counted, not quoted
                failed += 1
                continue
            try:  # the user name next to a password is no secret, but it names somebody
                document = json.loads(plaintext)
            except ValueError:
                continue
            if isinstance(document, dict):
                for field_name in IDENTIFIER_FIELDS:
                    forbidden.add(
                        f"{name}.secret.{field_name}", document.get(field_name), word=True
                    )
    return {"sealed_opened": opened, "sealed_unreadable": failed, "keyring": 1}


# -- writing ----------------------------------------------------------------------------------


def _json_strings(name: str, text: str) -> Iterator[str]:
    """The string values of a JSON member (one document, or one per line): where a short name
    is looked for. Keys are the code's own; `schema.sql` is the code's DDL and holds no value."""
    if not name.endswith((".json", ".jsonl")):
        return
    documents = text.splitlines() if name.endswith(".jsonl") else [text]
    pending: list[object] = [json.loads(document) for document in documents if document]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


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
    words = vocabulary()
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
                secrets = _register_instance_secrets(settings, connection, forbidden)
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
                content = _content(connection, words)
                sources = _sources(connection, names)
        finally:
            engine.dispose()
    content_roots = [r.resolve() for r in roots]
    if _under(out.parent.resolve(), content_roots):
        raise DiagnosticOutputRefusedError(out.name)
    if len(log_files) > MAX_LOG_FILES:
        raise DiagnosticInputRefusedError("too_many_files", MAX_LOG_FILES + 1)
    protected = [*content_roots, settings.data_dir.resolve(), *_VIRTUAL_ROOTS]
    budget = [MAX_LOG_TOTAL_BYTES]
    texts = [
        _read_log_input(
            path, index, forbidden_roots=protected, content_hashes=content_hashes, budget=budget
        )
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
        for index, text in enumerate(texts, start=1):
            for line in _log_lines(text, index):
                if line is None:
                    dropped += 1
                    continue
                reduced.append(
                    json.dumps(reduce_log_line(line, names, users, words), sort_keys=True)
                )
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
        forbidden.check(name, name + "\n" + text, words_in=[name, *_json_strings(name, text)])
    _write_zip(out, members)
    return BundleReport(out, tuple(members), kept, dropped)
