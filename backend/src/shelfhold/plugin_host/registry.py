"""Plugin registry: validates manifests, negotiates ``api``, loads plugins behind the guard.

Third-party plugins are disabled by default and their code is never imported (ENF-SEC-009,
EF-SRC-019): origin is decided from the distribution that ships the entry point, before anything
is loaded. A third-party plugin loads only when the caller hands the registry a grant, which
``plugin_host.public.start_plugins`` builds from the environment variable, the typed confirmation
and the digest of the installed files (``plugin_host.install``). A granted plugin runs in the host
process with the host's rights: it is the unsandboxed-plugin exception, isolation comes in M6.

Third-party entry points are read from the separate third-party ``site-packages`` only
(``metadata.distributions(path=[...])``), never from ``sys.path``, and only for the distributions
that the installer recorded: a stray or fake ``*.dist-info`` changes nothing. That directory is
never put on ``sys.path`` (so no ``.pth`` is processed and nothing in it is importable by
accident): a finder at the *end* of ``sys.meta_path`` resolves only the top-level modules of the
plugins that were granted, from source files only, so the host's own modules always win.

Compatibility (EF-SRC-001): the host accepts ``api == N`` natively and ``api == N-1`` through a shim
that rewrites the old manifest into the current shape; any other value is refused with a reason and
the remaining plugins still load.

Every loaded plugin is handed out behind the direct-I/O detector (``plugin_host.audit_hook``,
EF-SRC-002), and a manifest whose DomainRules or ``url_patterns`` do not parse is refused: a
pattern that cannot be read is never guessed at (EF-SRC-003). ``resolve_url`` gives a pasted URL
to the first active plugin whose ``url_patterns`` match it (EF-SRC-004).

A manifest whose ``settings_schema`` the host does not accept is refused (EF-SRC-013, reserved
network settings included). A trust value declared by a plugin that is not first-party is only a
proposed default (EF-EXP-065 (5), GAP-11-09): the record's manifest carries the safe value and
``proposed`` what the plugin asked for, until ``apply_attestations`` is handed the admin's word.
"""

import importlib.abc
import importlib.machinery
import importlib.util
import os
import re
import sys
import threading
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from importlib import metadata
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import CodeType
from typing import Any, Final, Protocol
from urllib.parse import urlsplit

import structlog

from shelfhold.infrastructure.fetcher.guard import (
    DomainPattern,
    DomainPatternError,
    compile_domain_pattern,
)
from shelfhold.plugin_host.attestation import retain
from shelfhold.plugin_host.audit_hook import allow_code_root, audited, host_io, importing
from shelfhold.plugin_host.errors import NoSourceForUrl
from shelfhold.plugin_host.settings import SettingsSchemaError, parse_settings_schema
from shelfhold.sdk.events import check_plugin_event, plugin_event_type
from shelfhold.sdk.models import API_VERSION, PluginKind, PluginManifest
from shelfhold.sdk.refs import SeriesRef, UnitInfo
from shelfhold.sdk.source import Capability, SourceManifest

ENTRY_POINT_GROUP = "shelfhold.plugins"
log = structlog.get_logger(__name__)
FIRST_PARTY_DISTRIBUTIONS = frozenset({"shelfhold"})

_LOADED = frozenset({"loaded", "loaded_shim"})
ManifestShim = Callable[[dict[str, Any]], dict[str, Any]]


class PluginOrigin(StrEnum):
    FIRST_PARTY = "first_party"
    THIRD_PARTY = "third_party"


class PluginStatus(StrEnum):
    LOADED = "loaded"
    LOADED_SHIM = "loaded_shim"  # api N-1, translated by the shim
    DISABLED = "disabled"  # third party without the guard's grant
    INCOMPATIBLE = "incompatible"  # api out of range, invalid manifest, load failure


@dataclass(frozen=True)
class ApiVerdict:
    status: PluginStatus
    reason: str | None = None


def negotiate_api(declared: int, current: int = API_VERSION) -> ApiVerdict:
    """N loads, N-1 loads through the shim, everything else is incompatible."""
    if declared == current:
        return ApiVerdict(PluginStatus.LOADED)
    if declared == current - 1:
        return ApiVerdict(PluginStatus.LOADED_SHIM)
    return ApiVerdict(
        PluginStatus.INCOMPATIBLE,
        f"api {declared} is incompatible: this host accepts {current} and {current - 1}",
    )


@dataclass(frozen=True)
class PluginCandidate:
    """A plugin found by discovery. `load` is called only for first-party plugins and returns an
    object whose ``manifest`` attribute is a manifest model or its raw dict."""

    key: str
    origin: PluginOrigin
    load: Callable[[], Any]
    dist: str | None = None  # normalised distribution name, for third-party candidates
    blocked: str | None = None  # why the guard refuses it (shown, never loaded)


@dataclass(frozen=True)
class PluginRecord:
    key: str
    origin: PluginOrigin
    status: PluginStatus
    reason: str | None = None
    manifest: PluginManifest | None = None
    instance: Any = None
    # Trust values the plugin declared and the host withholds until the admin attests them
    proposed: Mapping[str, str] = field(default_factory=dict)


def _trust_values(manifest: PluginManifest) -> dict[str, str]:
    """The declared values the attestation rule covers (M2: a source's `content_trust`)."""
    if isinstance(manifest, SourceManifest):
        return {"content_trust": manifest.content_trust}
    return {}


def _normalise(name: str) -> str:
    """PEP 503 normalisation of a distribution name (also applied to plugin keys)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def normalise_name(name: str) -> str:
    return _normalise(name)


def candidates_from_entry_points(entry_points: Iterable[Any]) -> list[PluginCandidate]:
    """Classify entry points by the distribution that ships them; nothing is imported here."""
    first_party = {_normalise(n) for n in FIRST_PARTY_DISTRIBUTIONS}
    found = []
    for ep in entry_points:
        dist = getattr(ep, "dist", None)
        dist_name = getattr(dist, "name", None)
        trusted = isinstance(dist_name, str) and _normalise(dist_name) in first_party
        origin = PluginOrigin.FIRST_PARTY if trusted else PluginOrigin.THIRD_PARTY
        found.append(PluginCandidate(key=ep.name, origin=origin, load=ep.load))
    return found


@dataclass(frozen=True)
class InstalledPlugin:
    """What the installer recorded for one third-party distribution (``plugin_host.install``)."""

    name: str  # normalised
    top_levels: tuple[str, ...]


SECRET_ENV = re.compile(r"SECRET|PASSWORD|PASSPHRASE|TOKEN|KEY|CREDENTIAL|_FILE$", re.IGNORECASE)


def third_party_candidates(
    site_dir: Path, installed: Iterable[InstalledPlugin], *, blocked: Mapping[str, str]
) -> list[PluginCandidate]:
    """Entry points of the recorded distributions, read from `site_dir` alone; nothing is
    imported. `blocked` maps a normalised distribution name to the reason it must not load."""
    known = {p.name: p for p in installed}
    pairs: list[tuple[str, Any]] = []
    for dist in metadata.distributions(path=[str(site_dir)]):
        raw = dist.metadata["Name"] if dist.metadata else None
        name = _normalise(raw) if isinstance(raw, str) else ""
        if name in known:  # not installed by the guard: ignored, whatever it declares
            pairs.extend((name, ep) for ep in dist.entry_points if ep.group == ENTRY_POINT_GROUP)
    found = []
    for (name, ep), candidate in zip(
        pairs, candidates_from_entry_points(ep for _, ep in pairs), strict=True
    ):
        if candidate.origin is PluginOrigin.FIRST_PARTY:  # a recorded plugin is never the host
            continue
        reason = blocked.get(name)
        if reason is None and ep.module.split(".")[0] not in known[name].top_levels:
            reason = f"entry point {ep.name!r} points outside the plugin's own modules"
        found.append(
            replace(
                candidate,
                load=_third_party_loader(site_dir, ep, known[name].top_levels),
                dist=name,
                blocked=reason,
            )
        )
    return found


class _SourceOnlyLoader(importlib.machinery.SourceFileLoader):
    """Always compiles from the source file: a bytecode cache is neither read nor written."""

    def get_code(self, fullname: str) -> CodeType:
        path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(path), path)


class ThirdPartyFinder(importlib.abc.MetaPathFinder):
    """Resolves the top-level modules of granted plugins, and what lies under them, in one
    directory. It sits after every other finder, so a name the host can import is never ours."""

    def __init__(self, site: Path) -> None:
        self._site = site.resolve()
        self._granted: set[str] = set()
        self._lock = threading.Lock()

    def grant(self, top_levels: Iterable[str]) -> None:
        with self._lock:
            self._granted.update(top_levels)

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> ModuleSpec | None:
        top = fullname.split(".")[0]
        if top not in self._granted:
            return None
        if path is None:
            if "." in fullname:
                return None
            dirs = [self._site]
        else:
            dirs = [Path(p) for p in path]
        leaf = fullname.rsplit(".", 1)[-1]
        for base in dirs:
            for file, package in (
                (base / leaf / "__init__.py", True),
                (base / f"{leaf}.py", False),
            ):
                spec = self._spec(fullname, top, file, package)
                if spec is not None:
                    return spec
        return None

    def _spec(self, fullname: str, top: str, file: Path, package: bool) -> ModuleSpec | None:
        try:
            if file.is_symlink() or not file.is_file():
                return None
            real = file.resolve()
            root = self._site / (top if (self._site / top).is_dir() else f"{top}.py")
            if real != root and root not in real.parents:
                return None  # never a file outside the plugin's own tree
        except OSError:
            return None
        return importlib.util.spec_from_file_location(
            fullname,
            real,
            loader=_SourceOnlyLoader(fullname, str(real)),
            submodule_search_locations=[str(real.parent)] if package else None,
        )


_FINDERS: dict[Path, ThirdPartyFinder] = {}


def finder_for(site: Path) -> ThirdPartyFinder:
    """The one finder of a directory, appended once to the end of `sys.meta_path`."""
    key = site.resolve()
    if key not in _FINDERS:
        _FINDERS[key] = ThirdPartyFinder(key)
    finder = _FINDERS[key]
    allow_code_root(key)  # a granted plugin's own modules may be imported by its code (EF-SRC-002)
    if finder not in sys.meta_path:
        sys.meta_path.append(finder)
    return finder


def _third_party_loader(site_dir: Path, ep: Any, top_levels: Iterable[str]) -> Callable[[], Any]:
    granted = tuple(top_levels)

    def load() -> Any:
        """Called with no argument and handed nothing: no settings, no secret, no host object.
        Only now, for a granted plugin, are its own top-level modules made importable. The
        process environment is the one channel left; names that look like secrets are taken
        out of it while the plugin's code is imported."""
        with host_io():  # the registry may run this under the plugin's import scope
            finder_for(site_dir).grant(granted)
            hidden = {k: v for k, v in os.environ.items() if SECRET_ENV.search(k)}
            for k in hidden:
                del os.environ[k]
        try:
            return ep.load()
        finally:
            with host_io():
                os.environ.update(hidden)

    return load


def _pattern_problem(manifest: PluginManifest) -> str | None:
    for rule in manifest.permissions.domains:
        try:
            compile_domain_pattern(rule.pattern)
        except DomainPatternError as exc:
            return f"invalid domain rule: {exc}"
    if isinstance(manifest, SourceManifest):
        for pattern in manifest.url_patterns:
            try:
                compile_url_pattern(pattern)
            except UrlPatternError as exc:
                return f"invalid url_patterns: {exc}"
    return None


# -- url_patterns (EF-SRC-004) ---------------------------------------------------------------------
#
# `<scheme>://<host pattern>[<path pattern>]`. The scheme is `http`, `https` or `*` (both). The
# host pattern has the DomainRule grammar (`example.org`, `.example.org`, `*.example.org`) and the
# same canonical comparison. In the path, `*` stands for one segment (no `/`) and `**` for any
# run; everything else is literal; no path means any path. The query and the fragment of the URL
# are not looked at; a URL with a userinfo part or a non-default port matches nothing.

_URL_PATTERN: Final = re.compile(r"(?P<scheme>https?|\*)://(?P<host>[^/?#]+)(?P<path>/[^?#]*)?")
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}
_MAX_JOKERS: Final = 4  # path jokers per pattern: bounds the matching time of a hostile pattern
_MAX_URL: Final = 4096


class UrlPatternError(ValueError):
    """A url_patterns entry that does not follow the grammar."""


@dataclass(frozen=True)
class UrlPattern:
    schemes: frozenset[str]
    host: DomainPattern
    path: re.Pattern[str] | None

    def matches(self, url: str) -> bool:
        if not isinstance(url, str) or len(url) > _MAX_URL:
            return False
        try:
            parts = urlsplit(url)
            port = parts.port
        except (ValueError, TypeError):
            return False
        scheme = parts.scheme.lower()
        if scheme not in self.schemes or "@" in parts.netloc or not parts.hostname:
            return False
        if port is not None and port != _DEFAULT_PORTS[scheme]:
            return False
        if not self.host.matches(parts.hostname):
            return False
        return self.path is None or self.path.fullmatch(parts.path or "/") is not None


def compile_url_pattern(pattern: str) -> UrlPattern:
    found = _URL_PATTERN.fullmatch(pattern) if isinstance(pattern, str) else None
    if found is None:
        raise UrlPatternError(f"{pattern!r} is not <scheme>://<host>[/<path>]")
    scheme = found["scheme"]
    try:
        host = compile_domain_pattern(found["host"])
    except DomainPatternError as exc:
        raise UrlPatternError(f"{pattern!r}: {exc}") from exc
    path = found["path"]
    regex = None
    if path is not None:
        pieces = re.split(r"(\*\*|\*)", path)
        if sum(p in ("*", "**") for p in pieces) > _MAX_JOKERS:
            raise UrlPatternError(f"{pattern!r}: more than {_MAX_JOKERS} jokers in the path")
        body = "".join(".*" if p == "**" else "[^/]*" if p == "*" else re.escape(p) for p in pieces)
        regex = re.compile(body)
    schemes = frozenset(_DEFAULT_PORTS) if scheme == "*" else frozenset({scheme})
    return UrlPattern(schemes, host, regex)


@dataclass(frozen=True)
class UrlResolution:
    key: str  # the plugin that recognised the URL
    ref: SeriesRef | UnitInfo


class EventPublisher(Protocol):
    """What `events.bus.EventBus` offers; named here so that the host gate needs no import of it."""

    def publish(
        self, connection: Any, event_type: str, payload: Mapping[str, object] | None = None
    ) -> Any: ...


def publish_plugin_event(
    bus: EventPublisher,
    connection: Any,
    key: str,
    name: str,
    payload: Mapping[str, object] | None = None,
) -> Any:
    """The only way a plugin's event reaches the bus (ENF-OBS-003): the host builds the type from
    the key it loaded the plugin under, so a plugin cannot speak for the core or another plugin.
    `PluginEventError` otherwise, before anything is published. The bus checks the payload."""
    event_type = plugin_event_type(key, name)
    check_plugin_event(key, event_type)
    return bus.publish(connection, event_type, payload)


class PluginRegistry:
    def __init__(
        self,
        *,
        api_version: int = API_VERSION,
        shims: Mapping[int, ManifestShim] | None = None,
        active_keys: Collection[str] | None = None,
        granted_distributions: Collection[str] = (),
    ) -> None:
        self._granted = frozenset(_normalise(n) for n in granted_distributions)
        self._api = api_version
        self._shims = dict(shims or {})
        self._active_keys = None if active_keys is None else frozenset(active_keys)
        self._records: list[PluginRecord] = []

    @property
    def records(self) -> list[PluginRecord]:
        return list(self._records)

    def get(self, key: str) -> PluginRecord | None:
        """The record that holds the key: the loaded one if any, else the first seen."""
        matching = [r for r in self._records if r.key == key]
        loaded = [r for r in matching if r.status in _LOADED]
        return (loaded or matching)[0] if matching else None

    def active(self) -> list[PluginRecord]:
        """Loaded plugins that are switched on; a fresh install switches none on (ENF-LEG-001)."""
        return [
            r
            for r in self._records
            if r.status in _LOADED and (self._active_keys is None or r.key in self._active_keys)
        ]

    def load_all(self, candidates: Iterable[PluginCandidate]) -> list[PluginRecord]:
        """Load every candidate; one failing plugin never prevents the others from loading.

        Candidates are grouped by key before any ``load()``: a key is decided on origins alone, so
        a homonym can neither shadow a first-party plugin nor make two plugins load. One first-party
        candidate wins and its third-party homonyms are refused unloaded; several first-party
        candidates with one key are all refused.
        """
        items = list(candidates)
        groups: dict[str, list[PluginCandidate]] = {}
        for candidate in items:  # keys are compared after PEP 503 normalisation
            groups.setdefault(_normalise(candidate.key), []).append(candidate)
        decided: dict[int, PluginRecord] = {}
        for key, group in groups.items():
            firsts = [c for c in group if c.origin is PluginOrigin.FIRST_PARTY]
            taken = next((r for r in self._records if _normalise(r.key) == key), None)
            held = taken is not None and taken.status in _LOADED
            thirds = [c for c in group if c.origin is not PluginOrigin.FIRST_PARTY]
            for c in group:
                if len(firsts) > 1 and c.origin is PluginOrigin.FIRST_PARTY:
                    why = f"duplicate_key: {len(firsts)} first-party plugins declare {c.key!r}"
                elif held or (firsts and c.origin is not PluginOrigin.FIRST_PARTY):
                    why = f"duplicate_key: {c.key!r} is already held by a first-party plugin"
                elif not firsts and len(thirds) > 1:
                    why = f"duplicate_key: {len(thirds)} third-party plugins declare {c.key!r}"
                else:
                    continue
                decided[id(c)] = PluginRecord(c.key, c.origin, PluginStatus.INCOMPATIBLE, why)
        records = [decided.get(id(c)) or self._load(c) for c in items]
        self._records.extend(records)
        return records

    def _load(self, candidate: PluginCandidate) -> PluginRecord:
        key, origin = candidate.key, candidate.origin
        if origin is not PluginOrigin.FIRST_PARTY and (
            candidate.blocked or candidate.dist is None or candidate.dist not in self._granted
        ):
            reason = candidate.blocked or "third-party plugins are disabled by default"
            return PluginRecord(key, origin, PluginStatus.DISABLED, reason)
        try:
            with importing(key):  # import-time I/O is logged too (report mode, never blocked)
                instance = candidate.load()
            manifest, status = self._manifest(getattr(instance, "manifest", None))
        except Exception as exc:  # a broken plugin is reported, never raised
            return PluginRecord(key, origin, PluginStatus.INCOMPATIBLE, f"load failed: {exc}")
        if manifest is None:
            return PluginRecord(key, origin, PluginStatus.INCOMPATIBLE, status.reason)
        if manifest.key != key:
            reason = f"manifest key {manifest.key!r} differs from entry point {key!r}"
            return PluginRecord(key, origin, PluginStatus.INCOMPATIBLE, reason)
        problem = _pattern_problem(manifest)
        if problem is not None:
            return PluginRecord(key, origin, PluginStatus.INCOMPATIBLE, problem)
        try:
            parse_settings_schema(manifest.settings_schema)
        except SettingsSchemaError as exc:
            return PluginRecord(key, origin, PluginStatus.INCOMPATIBLE, f"settings_schema: {exc}")
        kept = retain(origin, _trust_values(manifest), {})
        if kept.proposed:
            manifest = manifest.model_copy(update=kept.values)
        return PluginRecord(
            key, origin, status.status, None, manifest, audited(instance, key), kept.proposed
        )

    def apply_attestations(self, attested: Mapping[str, Mapping[str, str]]) -> None:
        """Give each withheld value back where the admin attested it (`AttestationStore.valid()`,
        which already checked the installed version); a value no longer attested is withdrawn."""
        records = []
        for record in self._records:
            manifest = record.manifest
            if manifest is None or record.origin is PluginOrigin.FIRST_PARTY:
                records.append(record)
                continue
            declared = {**_trust_values(manifest), **record.proposed}
            kept = retain(record.origin, declared, attested.get(record.key, {}))
            if kept.values != _trust_values(manifest) or kept.proposed != record.proposed:
                manifest = manifest.model_copy(update=kept.values)
                record = replace(record, manifest=manifest, proposed=kept.proposed)
            records.append(record)
        self._records = records

    async def resolve_url(
        self, url: str, fetcher_for: Callable[[PluginRecord], Any]
    ) -> "UrlResolution":
        """The reference behind a pasted URL (EF-SRC-004), from the first active plugin that
        declares URL_RESOLVE and whose ``url_patterns`` match; NoSourceForUrl (422) otherwise,
        also when that plugin resolves the URL to nothing. The plugin runs under the detector."""
        for record in self.active():
            manifest = record.manifest
            if not isinstance(manifest, SourceManifest):
                continue
            if Capability.URL_RESOLVE not in manifest.capabilities:
                continue
            if not any(compile_url_pattern(p).matches(url) for p in manifest.url_patterns):
                continue
            ref = await record.instance.resolve_url(fetcher_for(record), url)
            if not isinstance(ref, SeriesRef | UnitInfo):
                # the plugin's key is for the admin's log, never for the user's 422
                log.warning("source.no_match", plugin_id=record.key, reason="resolved_to_nothing")
                raise NoSourceForUrl()
            return UrlResolution(record.key, ref)
        log.info("source.no_match", plugin_id=None, reason="no_pattern_matched")
        raise NoSourceForUrl()

    def _manifest(self, raw: Any) -> tuple[PluginManifest | None, ApiVerdict]:
        data = raw.model_dump(mode="json") if isinstance(raw, PluginManifest) else raw
        if not isinstance(data, Mapping):
            return None, ApiVerdict(PluginStatus.INCOMPATIBLE, "plugin exposes no manifest")
        declared = data.get("api")
        if not isinstance(declared, int) or isinstance(declared, bool):
            return None, ApiVerdict(PluginStatus.INCOMPATIBLE, "manifest declares no integer api")
        verdict = negotiate_api(declared, self._api)
        if verdict.status is PluginStatus.INCOMPATIBLE:
            return None, verdict
        migrated = dict(data)
        if verdict.status is PluginStatus.LOADED_SHIM:
            shim = self._shims.get(declared)
            migrated = shim(migrated) if shim else migrated
            migrated["api"] = self._api
        model: type[PluginManifest] = (
            SourceManifest if migrated.get("kind") == PluginKind.SOURCE else PluginManifest
        )
        return model.model_validate(migrated), verdict
