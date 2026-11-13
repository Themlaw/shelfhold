"""Household profiles: who may sign in, with which role, ceiling, right to classify and policy
per source (EF-USR-001, EF-USR-005, ENF-SEC-008).

Managing profiles is a **reserved action** (`profile.*`: a fresh session in every profile, refused
to every key and token). The HTTP guard says so first; this service says it again, so that no other
caller (the host socket, a test, a later route that forgets the guard) can escalate a role:

- the caller is the host (`by=None` with the server stopped, or `local_cli`), or a **session of an
  admin** that the database still knows as an admin; a key, a token or any other role is refused;
- a profile never changes its own role, never removes itself, and the last admin never goes (the
  count is decided in the transaction that writes);
- a child always has an age ceiling and never the right to classify; an adult has no ceiling;
- there are at most two admins (the local one and an optional second);
- a child's policy on a source that is not marked "family" is always "forbidden" (D13, D14).
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

import structlog

from shelfhold.identity.application.delete_user import MAX_EXPORT_ROWS, build_export
from shelfhold.identity.application.ports import (
    AuditEntry,
    IdentityStore,
    PasswordHasherPort,
    ProfileRecord,
    SourcePolicyRecord,
)
from shelfhold.identity.domain.credentials import Credential, Principal, check_password_shape
from shelfhold.identity.domain.policy import check_source_policy, effective_source_policy
from shelfhold.identity.domain.roles import (
    MAX_ADMINS,
    ProfileError,
    check_child_ceiling,
    normalise_profile,
)
from shelfhold.identity.domain.visibility import effective_visibility
from shelfhold.infrastructure.ids import new_ulid

Audit = Callable[[str, Principal | None, str | None], None]
Entry = Callable[[str, Principal | None, str | None], AuditEntry]
MAX_USERNAME = 128
MAX_SOURCE_KEY = 200


class ProfileService:
    def __init__(
        self,
        store: IdentityStore,
        hasher: PasswordHasherPort,
        now: Callable[[], datetime],
        audit: Audit,
        entry: Entry | None = None,
    ) -> None:
        self._entry = entry
        self._store = store
        self._hasher = hasher
        self._now = now
        self._audit = audit

    # --- who may manage ----------------------------------------------------------------------

    def _manager(self, by: Principal | None) -> Principal | None:
        """`by=None`: the host CLI with the server stopped (it holds the lock of the instance).
        Otherwise the host socket or a session of an admin the database still holds as one."""
        if by is None:
            return None
        if by.is_host:
            if by.role != "admin":
                raise ProfileError("not_allowed")
            return by
        if by.credential is not Credential.SESSION or by.role != "admin":
            raise ProfileError("not_allowed")  # keys and device tokens are never admitted
        current = self._store.profile(by.user_id)
        if current is None or current.role != "admin":  # demoted or removed since the request began
            raise ProfileError("not_allowed")
        return by

    @staticmethod
    def _actor_id(by: Principal | None) -> str | None:
        """Whose admin role the store re-reads under its lock: a session's user (the host has no
        row to read)."""
        return by.user_id if by is not None and by.credential is Credential.SESSION else None

    @staticmethod
    def _is_self(by: Principal | None, profile_id: str) -> bool:
        return by is not None and by.credential is Credential.SESSION and by.user_id == profile_id

    def _creator_ceiling(self, by: Principal | None) -> int | None:
        if by is None or by.credential is not Credential.SESSION:
            return None
        creator = self._store.profile(by.user_id)
        return creator.max_age_rating if creator else None

    def _existing(self, profile_id: str) -> ProfileRecord:
        profile = self._store.profile(profile_id)
        if profile is None:
            raise ProfileError("not_found")
        return profile

    # --- the profiles ------------------------------------------------------------------------

    def list(self, *, by: Principal | None = None) -> Sequence[ProfileRecord]:
        self._manager(by)
        return self._store.profiles()

    def add(self, *args: Any, **kwargs: Any) -> str:
        return self.add_profile(*args, **kwargs).id

    def add_profile(
        self,
        username: str,
        password: str,
        role: str,
        *,
        max_age_rating: int | None = None,
        can_rate: bool | None = None,
        by: Principal | None = None,
    ) -> ProfileRecord:
        """Create a profile; returns it. `ValueError` for a username or a password that has
        no acceptable shape (the same refusals as the first admin's)."""
        self._manager(by)
        username = username.strip()
        if not username or len(username) > MAX_USERNAME:
            raise ValueError("invalid_username")
        check_password_shape(password)
        # the right to classify is given, never assumed: an admin has it, a member only if told
        wanted = role == "admin" if can_rate is None else can_rate
        ceiling, rate = normalise_profile(role, max_age_rating, can_rate=wanted)
        if ceiling is not None:
            check_child_ceiling(self._creator_ceiling(by), ceiling)
        profile_id = new_ulid()
        self._store.create_profile(
            id=profile_id, username=username, role=role, password_hash=self._hasher.hash(password),
            max_age_rating=ceiling, can_rate=rate, now=self._now(), max_admins=MAX_ADMINS,
            actor_id=self._actor_id(by),
        )  # fmt: skip
        self._audit("profile.created", by, profile_id)
        return ProfileRecord(profile_id, username, role, ceiling, rate)

    def set(
        self,
        profile_id: str,
        *,
        role: str | None = None,
        max_age_rating: int | None = None,
        can_rate: bool | None = None,
        by: Principal | None = None,
    ) -> ProfileRecord:
        """Change the role, the ceiling or the right to classify of a profile. What is not given is
        kept, except where the new role takes it away (a child has no right to classify, an adult
        no ceiling; a changed role starts from the default of the new one)."""
        self._manager(by)
        current = self._existing(profile_id)
        new_role = role or current.role
        if new_role != current.role and self._is_self(by, profile_id):
            raise ProfileError("cannot_change_own_role")
        if new_role == "child":
            ceiling = max_age_rating if max_age_rating is not None else current.max_age_rating
        else:
            ceiling = max_age_rating  # an adult with a ceiling is refused below
        if can_rate is not None:
            rate = can_rate
        elif new_role == current.role:
            rate = current.can_rate
        else:
            rate = new_role == "admin"  # a new role starts from its default, an admin may classify
        ceiling, rate = normalise_profile(new_role, ceiling, can_rate=rate)
        if (new_role, ceiling, rate) == (current.role, current.max_age_rating, current.can_rate):
            return current
        if ceiling is not None:
            check_child_ceiling(self._creator_ceiling(by), ceiling)
        written = self._store.update_profile(
            profile_id, expected_role=current.role,
            expected_max_age_rating=current.max_age_rating, expected_can_rate=current.can_rate,
            role=new_role, max_age_rating=ceiling, can_rate=rate, max_admins=MAX_ADMINS,
            now=self._now(), actor_id=self._actor_id(by),
        )  # fmt: skip
        self._audit("profile.updated", by, profile_id)
        return written

    def remove(self, profile_id: str, *, by: Principal | None = None) -> None:
        """Delete a profile; its traces in the audit stay, under a pseudonym (EF-USR-013)."""
        self._manager(by)
        self._existing(profile_id)
        if self._is_self(by, profile_id):
            raise ProfileError("cannot_remove_self")
        pseudonym = self._store.delete_user(
            profile_id, keep_one_admin=True, actor_id=self._actor_id(by)
        )
        if pseudonym is None:
            raise ProfileError("not_found")
        self._audit("profile.removed", by, pseudonym)

    def export(self, profile_id: str, *, by: Principal | None = None) -> dict[str, Any]:
        """Everything that is hers, as one document (EF-USR-013): a profile management action (an
        admin session or the host, never a key, never the person herself nor a child). The audit
        line names the profile, never what the document holds."""
        self._manager(by)
        profile = self._existing(profile_id)
        rows = self._store.export_rows(profile_id, limit=MAX_EXPORT_ROWS)
        if rows is None:  # removed between the two reads
            raise ProfileError("not_found")
        document = build_export(profile, rows)
        self._audit("profile.exported", by, profile_id)
        return document

    # --- the visibility of a profile (EF-USR-016) -----------------------------------------------

    def visibility_of(self, profile_id: str) -> str:
        """What holds now for this profile (the choice, else the default of its role); an unknown
        profile has the closed default, `private`."""
        state = self._store.visibility_state(profile_id)
        return "private" if state is None else effective_visibility(*state)

    def set_visibility(
        self, profile_id: str, visibility: object, *, by: Principal | None = None
    ) -> str:
        """Change the visibility of someone else's profile: a profile management action (an admin
        session or the host, never a key), audited when it changes what holds. The audit line holds
        the profile and the word, nothing else."""
        self._manager(by)
        self._existing(profile_id)

        def trace(before: str, after: str) -> AuditEntry | None:
            """Written in the transaction of the change, with what held and what holds."""
            if before == after or self._entry is None:
                return None
            return self._entry("profile.visibility_set", by, f"{profile_id}|{before}>{after}")

        before, after = self._store.set_visibility(
            profile_id, visibility, actor_id=self._actor_id(by), by_admin=by is not None,
            audit=trace,
        )  # fmt: skip
        if before != after:
            self._log("profile.visibility_set", by)
        return after

    def set_family_search(
        self,
        profile_id: str,
        hide_unrated_remote: bool,
        subject_filter: tuple[str, ...],
        *,
        by: Principal | None = None,
    ) -> ProfileRecord:
        """Change the family search of someone's profile (EF-USR-018): a profile management action
        (an admin session or the host, never a key), audited when it changes what holds. The audit
        entry names the profile and the two modes, and how many words, never the words."""
        self._manager(by)
        self._existing(profile_id)

        def trace(before: ProfileRecord, after: ProfileRecord) -> AuditEntry | None:
            if (before.hide_unrated_remote, before.subject_filter) == (
                after.hide_unrated_remote,
                after.subject_filter,
            ) or self._entry is None:
                return None
            target = (
                f"{profile_id}|hide={int(before.hide_unrated_remote)}>"
                f"{int(after.hide_unrated_remote)}|words={len(before.subject_filter)}>"
                f"{len(after.subject_filter)}"
            )
            return self._entry("profile.family_search_set", by, target)

        before, after = self._store.set_family_search(
            profile_id, hide_unrated_remote, subject_filter, actor_id=self._actor_id(by),
            audit=trace,
        )  # fmt: skip
        if (before.hide_unrated_remote, before.subject_filter) != (
            after.hide_unrated_remote,
            after.subject_filter,
        ):
            self._log("profile.family_search_set", by)
        return after

    def set_own_visibility(self, by: Principal, visibility: object) -> str:
        """A person changes their own visibility: theirs to do, so no audit of "someone else's".
        A session only: a key or a token is a program, it has no visibility to choose."""
        if by.credential is not Credential.SESSION:
            raise ProfileError("not_allowed")
        _, after = self._store.set_visibility(by.user_id, visibility)
        return after

    def _log(self, action: str, by: Principal | None) -> None:
        """The log line of an audit written elsewhere: the word of the action, nothing else."""
        structlog.get_logger().info(
            "auth.audit", action=action, actor_id=None if by is None else by.user_id,
            role=getattr(by, "role", None),
        )  # fmt: skip

    # --- the policy per source ---------------------------------------------------------------

    def set_source_policy(
        self, profile_id: str, source_key: str, policy: str, *, by: Principal | None = None
    ) -> SourcePolicyRecord:
        self._manager(by)
        profile = self._existing(profile_id)
        if not source_key or len(source_key) > MAX_SOURCE_KEY:
            raise ProfileError("invalid_policy")
        check_source_policy(profile.role, policy, family=self._store.source_family(source_key))
        written = self._store.set_source_policy(
            profile_id, source_key, policy, now=self._now(), actor_id=self._actor_id(by)
        )
        self._audit("profile.policy_set", by, f"{profile_id}|{source_key}|{policy}")
        return written

    def source_policies(
        self, profile_id: str, *, by: Principal | None = None
    ) -> Sequence[SourcePolicyRecord]:
        self._manager(by)
        self._existing(profile_id)
        stored: Mapping[str, str] = self._store.stored_source_policies(profile_id)
        return [
            SourcePolicyRecord(
                source_key=key,
                policy=policy,
                effective=self.effective_source_policy(profile_id, key),  # one statement each
            )
            for key, policy in stored.items()
        ]

    def effective_source_policy(self, profile_id: str, source_key: str) -> str:
        """What `profile_id` may do with a source now: for the callers that decide whether an
        addition is direct, a request, or refused. An unknown profile has nothing."""
        state = self._store.source_policy_state(profile_id, source_key)
        if state is None:
            return "forbidden"
        role, stored, family = state
        return effective_source_policy(role, stored, family=family)
