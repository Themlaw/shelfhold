"""Normaliseurs versionnés (ENF-OPS-021, ENF-I18N-002).

Toute clé d'identité est enregistrée sous `normalizer_id@version`, stockée avec la valeur brute.
Changer le comportement est une nouvelle version ; les lignes d'une ancienne version restent
valides tant qu'un plan de migration explicite (avec l'aperçu des fusions) n'est pas appliqué.
Rien n'est jamais recalculé à la lecture : `resolve_key` rend la valeur stockée.
"""

import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from shelfhold.catalog.domain.numbering import (
    NUMBER_NORMALIZER_NAME,
    NUMBER_NORMALIZER_VERSION,
    parse_number,
)
from shelfhold.catalog.lang import (
    LANG_NORMALIZER_NAME,
    LANG_NORMALIZER_VERSION,
    normalize_lang,
)

NormalizerFn = Callable[[str], str]


@dataclass(frozen=True, order=True)
class NormalizerId:
    name: str
    version: int

    def __str__(self) -> str:
        return f"{self.name}@{self.version}"

    @classmethod
    def parse(cls, text: str) -> "NormalizerId":
        name, sep, version = text.partition("@")
        if not sep or not name or not version.isdecimal() or int(version) < 1 or "@" in version:
            raise ValueError(f"invalid normalizer id: {text!r}")
        return cls(name, int(version))


@dataclass(frozen=True)
class Normalized:
    """Valeur normalisée, valeur brute et normaliseur, stockés ensemble (`*_norm_by`)."""

    value: str
    raw: str
    by: str

    @property
    def key(self) -> str:
        return self.value


def resolve_key(stored: Normalized) -> str:
    """Clé d'identité d'une ligne stockée : celle de sa version, jamais recalculée."""
    return stored.value


@dataclass(frozen=True)
class Merge:
    """Aperçu : des valeurs brutes qui deviendraient une seule clé après migration."""

    new_value: str
    raws: tuple[str, ...]


@dataclass(frozen=True)
class MigrationPlan:
    target: NormalizerId
    changes: tuple[Normalized, ...]  # lignes d'une ancienne version, à recalculer
    results: tuple[Normalized, ...]  # leur valeur sous la version cible, dans le même ordre
    merges: tuple[Merge, ...]

    def apply(self) -> tuple[Normalized, ...]:
        """Les lignes migrées ; les lignes d'origine ne sont pas modifiées."""
        return self.results


class NormalizerRegistry:
    def __init__(self) -> None:
        self._functions: dict[NormalizerId, NormalizerFn] = {}
        self._current: dict[str, int] = {}

    def register(self, name: str, version: int, fn: NormalizerFn) -> NormalizerId:
        nid = NormalizerId(name, version)
        if nid in self._functions:
            raise ValueError(f"{nid} already registered")
        if version != self._current.get(name, 0) + 1:
            raise ValueError(f"{nid} skips a version")
        self._functions[nid] = fn
        self._current[name] = version
        return nid

    def names(self) -> list[str]:
        return sorted(self._current)

    def current(self, name: str) -> NormalizerId:
        return NormalizerId(name, self._current[name])

    def get(self, nid: NormalizerId) -> NormalizerFn:
        return self._functions[nid]

    def normalize(self, name: str, raw: str) -> Normalized:
        nid = self.current(name)
        return Normalized(self._functions[nid](raw), raw, str(nid))

    def plan_migration(self, rows: Iterable[Normalized], name: str) -> MigrationPlan:
        """Aperçu d'une migration vers la version courante de `name`, sans rien appliquer."""
        target = self.current(name)
        changes = tuple(r for r in rows if NormalizerId.parse(r.by) != target)
        results = tuple(
            Normalized(self._functions[target](r.raw), r.raw, str(target)) for r in changes
        )
        groups: dict[str, set[str]] = defaultdict(set)
        for result in results:
            groups[result.value].add(result.raw)
        existing = {r.value for r in results}
        merges = tuple(
            Merge(value, tuple(sorted(raws)))
            for value, raws in sorted(groups.items())
            if len(raws) > 1 and value in existing
        )
        return MigrationPlan(target, changes, results, merges)


def to_nfc(text: str) -> str:
    """Texte d'affichage : NFC, contrôles bidi et ordre logique conservés."""
    return unicodedata.normalize("NFC", text)


def _key_text(text: str) -> str:
    """Clé de rapprochement : NFC, sans caractère de format invisible (bidi), casse pliée."""
    kept = "".join(c for c in to_nfc(text) if unicodedata.category(c) != "Cf")
    return " ".join(kept.casefold().split())


def default_registry() -> NormalizerRegistry:
    registry = NormalizerRegistry()
    for name in ("series_name", "team_label", "text_version"):
        registry.register(name, 1, _key_text)
    # `number` and `lang` are owned by their modules; the registry only lists their version.
    registry.register(
        NUMBER_NORMALIZER_NAME, NUMBER_NORMALIZER_VERSION, lambda s: parse_number(s).norm
    )
    registry.register(
        LANG_NORMALIZER_NAME, LANG_NORMALIZER_VERSION, lambda s: normalize_lang(s).tag
    )
    return registry
