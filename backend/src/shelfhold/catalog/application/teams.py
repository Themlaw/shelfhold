"""Teams belong to an Edition, never to a binding (EF-BIB-035; I5, I27)."""

from datetime import datetime

from shelfhold.catalog.application.dto import BindingRef, TeamRef
from shelfhold.catalog.application.ports import TeamStore
from shelfhold.catalog.domain.identity import normalize_team_label


def check_binding(store: TeamStore, binding: BindingRef) -> None:
    """The binding serves the Edition it claims (the schema cannot say it, decisions L03 n.7)."""
    if store.binding_edition(binding.id) != binding.edition_id:
        raise ValueError(f"binding {binding.id} does not belong to edition {binding.edition_id}")


def resolve_team(store: TeamStore, binding: BindingRef, label: str, seen_at: datetime) -> TeamRef:
    """The Team `(edition, normalized label)`; the raw label seen on this binding is an alias.

    A relink or a source migration brings the same label on a new binding: same Team, one more
    alias, and the priority list of the Edition is untouched.
    """
    check_binding(store, binding)
    return team_of_checked_binding(store, binding, label, seen_at)


def team_of_checked_binding(
    store: TeamStore, binding: BindingRef, label: str, seen_at: datetime
) -> TeamRef:
    """`resolve_team` for a binding the caller has already checked (once per item)."""
    normalized = normalize_team_label(label)
    team_id = store.find(binding.edition_id, normalized.value)
    created = team_id is None
    if team_id is None:
        team_id = store.insert(binding.edition_id, normalized)
    store.add_alias(team_id, binding.id, label, seen_at)
    return TeamRef(team_id, binding.edition_id, normalized.value, created)


def set_team_priority(store: TeamStore, edition_id: str, team_ids: list[str]) -> None:
    if len(set(team_ids)) != len(team_ids):
        raise ValueError("a team appears twice in the priority list")
    store.set_priority(edition_id, team_ids)


def prefer_team(store: TeamStore, edition_id: str, team_id: str) -> None:
    """Make `team_id` the preferred team of the Edition (EF-BIB-005): first of its priority list,
    the others keep their order. A team of another Edition is refused. The files already served
    are replaced by `switch_to_preferred_team` (an upgrade, under intent), never here."""
    current = store.priority(edition_id)
    if team_id not in current and not store.has_team(edition_id, team_id):
        raise ValueError("not a team of this edition")
    store.set_priority(edition_id, [team_id, *(t for t in current if t != team_id)])
