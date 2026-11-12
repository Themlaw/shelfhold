"""Identity tables: users, sessions, API keys, audit, idempotency keys (02 section 3.4)."""

import sqlalchemy as sa

from shelfhold.infrastructure.schema._base import (
    UtcDateTime,
    bool_column,
    clock_time_check,
    in_check,
    json_shape_check,
    metadata,
    now_column,
    positive_check,
    prefixed_hash_check,
    ulid_check,
    ulid_column,
)

ROLES = ("admin", "member", "child")
THEMES = ("bibliotheque", "console", "galerie", "calme")
COLOR_SCHEMES = ("auto", "light", "dark")
NOTIFY_MODES = ("digest", "immediate")
SOURCE_POLICIES = ("add_directly", "request", "forbidden")
REQUEST_TARGET_KINDS = ("work_id", "source_ref", "isbn", "free_text")
REQUEST_STATES = ("pending", "approved", "rejected", "not_found", "cancelled", "arrived")

user = sa.Table(
    "user",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column("username", sa.String(collation="NOCASE"), nullable=False),
    sa.Column(
        "password_hash", sa.String(), nullable=True
    ),  # argon2id; null: forward-auth, OIDC, passkey only
    sa.Column("role", sa.String(), nullable=False),
    bool_column("can_rate", default=False),
    sa.Column("locale", sa.String(), nullable=False, server_default="fr"),
    sa.Column("timezone", sa.String(), nullable=False, server_default="UTC"),
    sa.Column("title_lang_pref", sa.JSON(), nullable=False, server_default="[]"),
    sa.Column("theme", sa.String(), nullable=True),  # null: the instance default
    sa.Column("color_scheme", sa.String(), nullable=False, server_default="auto"),
    now_column(),
    # Parental ceiling (EF-USR-015): NULL for an adult; a child always has one, NULL is never
    # "all ages" (check `child_has_age_ceiling`).
    sa.Column("max_age_rating", sa.Integer(), nullable=True),
    sa.Column("kavita_username", sa.String(), nullable=True),  # declarative link (EF-USR-017)
    sa.Column("notify_mode", sa.String(), nullable=False, server_default="digest"),
    sa.Column("quiet_start", sa.String(), nullable=True),  # local `HH:MM`, with `quiet_end`
    sa.Column("quiet_end", sa.String(), nullable=True),
    bool_column("hide_unrated_remote", default=True),  # EF-USR-018, the safe side
    sa.Column("subject_filter", sa.JSON(), nullable=False, server_default="[]"),
    # What the profile's requests and activity show to the others (EF-USR-016): `private` or
    # `household`; NULL is the default of the role. Read through `effective_visibility`.
    sa.Column("visibility", sa.String(), nullable=True),
    ulid_check("id"),
    sa.CheckConstraint(
        '"password_hash" IS NULL OR "password_hash" GLOB \'$argon2id$*\'',
        name="password_hash_argon2id",
    ),
    in_check("role", ROLES),
    in_check("theme", THEMES),
    in_check("color_scheme", COLOR_SCHEMES),
    in_check("notify_mode", NOTIFY_MODES),
    sa.CheckConstraint(
        '"max_age_rating" IS NULL OR (typeof("max_age_rating") = \'integer\' '
        'AND "max_age_rating" BETWEEN 0 AND 99)',
        name="max_age_rating_min_age",
    ),
    sa.CheckConstraint(
        '"role" <> \'child\' OR "max_age_rating" IS NOT NULL', name="child_has_age_ceiling"
    ),
    sa.CheckConstraint(
        '"kavita_username" IS NULL OR "kavita_username" <> \'\'', name="kavita_username_not_empty"
    ),
    clock_time_check("quiet_start"),
    clock_time_check("quiet_end"),
    sa.CheckConstraint(
        '("quiet_start" IS NULL) = ("quiet_end" IS NULL)', name="quiet_hours_both_or_none"
    ),
    json_shape_check("subject_filter", "array"),
    sa.CheckConstraint('"role" <> \'admin\' OR "can_rate"', name="admin_can_rate"),
    sa.UniqueConstraint("username"),
    sa.UniqueConstraint("kavita_username"),
)

# A passkey (EF-USR-025, ADR-0050). `credential_id` is the raw WebAuthn identifier, unique across
# users; `sign_count` is the authenticator's counter (a regression flags a cloned key);
# `revoked_at` retires the credential without deleting its history.
webauthn_credential = sa.Table(
    "webauthn_credential",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column(
        "user_id", sa.String(26), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column("credential_id", sa.LargeBinary(), nullable=False),
    sa.Column("public_key", sa.LargeBinary(), nullable=False),
    sa.Column("sign_count", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("transports", sa.JSON(), nullable=False, server_default="[]"),
    sa.Column("aaguid", sa.String(), nullable=True),
    sa.Column("rp_id", sa.String(), nullable=True),  # the domain it is bound to (`status` compares)
    sa.Column("name", sa.String(), nullable=False),
    now_column(),
    sa.Column("last_used_at", UtcDateTime(), nullable=True),
    sa.Column("revoked_at", UtcDateTime(), nullable=True),
    ulid_check("id"),
    positive_check("sign_count"),
    json_shape_check("transports", "array"),
    sa.CheckConstraint("\"name\" <> ''", name="name_not_empty"),
    sa.UniqueConstraint("credential_id"),
    sa.Index("ix_webauthn_credential_user", "user_id"),
)

# `token_hash` is what the cookie value is checked against; the raw token is never stored.
session = sa.Table(
    "session",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column(
        "user_id", sa.String(26), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column("token_hash", sa.String(), nullable=False),
    sa.Column("csrf_secret", sa.String(), nullable=False),
    now_column(),
    sa.Column("last_seen_at", UtcDateTime(), nullable=False),
    sa.Column("auth_time", UtcDateTime(), nullable=False),  # last password entry (EF-USR-022)
    sa.Column("absolute_expiry", UtcDateTime(), nullable=False),
    bool_column("remember", default=False),
    sa.Column("user_agent_label", sa.String(), nullable=True),
    sa.Column("ip", sa.String(), nullable=True),
    sa.Column("revoked_at", UtcDateTime(), nullable=True),
    # The passkey that opened it: revoking the passkey closes the session (review PR #95).
    sa.Column("passkey_id", sa.String(26), nullable=True),  # no FK: see migration 0015
    ulid_check("id"),
    prefixed_hash_check("token_hash"),
    sa.UniqueConstraint("token_hash"),
    sa.Index("ix_session_user", "user_id"),
    sa.Index("ix_session_passkey", "passkey_id"),
    sa.Index("ix_session_expiry", "absolute_expiry"),
)

api_key = sa.Table(
    "api_key",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column(
        "user_id", sa.String(26), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column("name", sa.String(), nullable=True),
    sa.Column("prefix", sa.String(), nullable=False),  # displayable
    sa.Column("hash", sa.String(), nullable=False),  # the secret is shown once, never stored
    sa.Column("scopes", sa.JSON(), nullable=False),
    bool_column("read_only", default=False),
    sa.Column("expires_at", UtcDateTime(), nullable=True),
    sa.Column("last_used_at", UtcDateTime(), nullable=True),
    sa.Column("rotated_from", sa.String(26), sa.ForeignKey("api_key.id", ondelete="SET NULL")),
    sa.Column("revoked_at", UtcDateTime(), nullable=True),
    sa.Column("subject_user_id", sa.String(26), sa.ForeignKey("user.id", ondelete="CASCADE")),
    sa.Column("issued_by", sa.String(26), sa.ForeignKey("user.id", ondelete="SET NULL")),
    now_column(),
    ulid_check("id"),
    prefixed_hash_check("hash"),
    sa.CheckConstraint("json_type(\"scopes\") = 'array'", name="scopes_array"),
    sa.UniqueConstraint("prefix"),
    sa.Index("ix_api_key_user", "user_id"),
)

# `actor_id` is deliberately not a foreign key: it becomes an opaque token when the user is
# deleted (EF-USR-013). An entry never holds a title (I36).
audit = sa.Table(
    "audit",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column("actor_id", sa.String(), nullable=True),
    sa.Column("action", sa.String(), nullable=False),
    sa.Column("target", sa.String(), nullable=True),
    sa.Column("at", UtcDateTime(), nullable=False, server_default=sa.func.current_timestamp()),
    # Session and last password entry behind the action (EF-USR-022 (3)); no foreign key either.
    ulid_column("session_id", nullable=True),
    sa.Column("auth_time", UtcDateTime(), nullable=True),
    ulid_check("id"),
    ulid_check("session_id"),
    sa.Index("ix_audit_at", "at"),
    sa.Index("ix_audit_actor", "actor_id", "at"),
)

# Client idempotency keys, kept 24 hours (EF-INT-001); a null status means "still running".
idempotency_key = sa.Table(
    "idempotency_key",
    metadata,
    sa.Column("actor_id", sa.String(), nullable=False),
    sa.Column("key", sa.String(), nullable=False),
    sa.Column("method", sa.String(), nullable=False),
    sa.Column("path", sa.String(), nullable=False),
    sa.Column("request_hash", sa.String(), nullable=False),
    sa.Column("response_status", sa.Integer(), nullable=True),
    sa.Column("response_body", sa.JSON(), nullable=True),
    now_column(),
    sa.Column("expires_at", UtcDateTime(), nullable=False),
    sa.PrimaryKeyConstraint("actor_id", "key"),
    prefixed_hash_check("request_hash"),
    sa.Index("ix_idempotency_key_expiry", "expires_at"),
)

# What a profile may do with a source (EF-USR-005): add directly, ask, or nothing. The defaults of
# D14 are applied where no row exists; a row is only an explicit choice.
profile_source_policy = sa.Table(
    "profile_source_policy",
    metadata,
    sa.Column(
        "user_id", sa.String(26), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column("source_key", sa.String(), nullable=False),
    sa.Column("policy", sa.String(), nullable=False),
    now_column("updated_at"),
    sa.PrimaryKeyConstraint("user_id", "source_key"),
    in_check("policy", SOURCE_POLICIES),
    sa.CheckConstraint("\"source_key\" <> ''", name="source_key_not_empty"),
)

# Follow, favourite and read state of one user on one Work (EF-USR-007); `stamp` is the server
# instant of the last write (`Stamped.received_at`), the clock of the last-write-wins merge.
user_state = sa.Table(
    "user_state",
    metadata,
    sa.Column(
        "user_id", sa.String(26), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column(
        "work_id", sa.String(26), sa.ForeignKey("work.id", ondelete="CASCADE"), nullable=False
    ),
    bool_column("followed", default=False),
    bool_column("favorite", default=False),
    sa.Column("read_at", UtcDateTime(), nullable=True),
    now_column("stamp"),
    sa.PrimaryKeyConstraint("user_id", "work_id"),
    sa.Index("ix_user_state_work", "work_id"),
)

# A request of a profile (EF-USR-010). The target is polymorphic and only becomes a Work at
# approval; `reject_reason` exists only on a refusal.
request = sa.Table(
    "request",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column(
        "requester_user_id",
        sa.String(26),
        sa.ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("target_kind", sa.String(), nullable=False),
    sa.Column("target_value", sa.String(), nullable=False),
    sa.Column("state", sa.String(), nullable=False, server_default="pending"),
    sa.Column("reject_reason", sa.String(), nullable=True),
    now_column(),
    now_column("updated_at"),
    # M3 L35 (migration 0057). `visibility` is what `effective_visibility` said at creation. The
    # identifiers are plain columns (no foreign key: a purge removes the Work, not the request).
    sa.Column("visibility", sa.String(), nullable=False, server_default="private"),
    sa.Column("work_id", sa.String(26), nullable=True),  # resolved at approval
    sa.Column("edition_id", sa.String(26), nullable=True),
    sa.Column("shelf_id", sa.String(26), nullable=True),
    sa.Column("decided_by", sa.String(), nullable=True),  # `user.id` of the admin, opaque
    sa.Column("decided_at", UtcDateTime(), nullable=True),
    sa.Column("approved_age", sa.Integer(), nullable=True),  # a child's request: applied on arrival
    bool_column("approved_x", default=False),
    sa.Column("arrived_at", UtcDateTime(), nullable=True),
    sa.Column("started_at", UtcDateTime(), nullable=True),  # the effects of the approval are made
    sa.Column("shown_title", sa.String(), nullable=True),  # what a child was shown, kept as is
    ulid_check("id"),
    in_check("target_kind", REQUEST_TARGET_KINDS),
    in_check("state", REQUEST_STATES),
    sa.CheckConstraint("\"target_value\" <> ''", name="target_value_not_empty"),
    sa.CheckConstraint(
        '"target_kind" <> \'work_id\' OR (length("target_value") = 26 '
        "AND \"target_value\" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*')",
        name="work_target_is_ulid",
    ),
    sa.CheckConstraint(
        '"reject_reason" IS NULL OR "state" = \'rejected\'', name="reason_only_on_refusal"
    ),
    sa.Index("ix_request_requester", "requester_user_id", "state"),
    sa.Index("ix_request_work", "work_id"),
)

# The digest being built for a user, sent at `due_at` (EF-INT-021).
notification_digest = sa.Table(
    "notification_digest",
    metadata,
    sa.Column(
        "user_id", sa.String(26), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column("due_at", UtcDateTime(), nullable=False),
    sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
    sa.PrimaryKeyConstraint("user_id", "due_at"),
    json_shape_check("payload", "object"),
)
