"""Identity tables: users, sessions, API keys, audit, idempotency keys (02 section 3.4)."""

import sqlalchemy as sa

from shelfhold.infrastructure.schema._base import (
    UtcDateTime,
    bool_column,
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
    ulid_check("id"),
    sa.CheckConstraint(
        '"password_hash" IS NULL OR "password_hash" GLOB \'$argon2id$*\'',
        name="password_hash_argon2id",
    ),
    in_check("role", ROLES),
    in_check("theme", THEMES),
    in_check("color_scheme", COLOR_SCHEMES),
    sa.CheckConstraint('"role" <> \'admin\' OR "can_rate"', name="admin_can_rate"),
    sa.UniqueConstraint("username"),
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
    ulid_check("id"),
    prefixed_hash_check("token_hash"),
    sa.UniqueConstraint("token_hash"),
    sa.Index("ix_session_user", "user_id"),
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
