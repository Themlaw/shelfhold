"""The device registry (M5 L02, EF-USR-008, ENF-SEC-013): one `device` per pairing.

`device` holds what a person sees of an e-reader or a client: the reader profile it reads as, a
label, a kind from a closed list (`koreader`, `kobo`, `opds`, `browser`, `other`), when it was
created and last seen, and when it was retired. `device_token` gains `device_id`: a rotation puts
a new secret on the same device, a retirement ends every token of the device.

Every token already paired receives a device of its own (kind `other`, its name as the label, its
last use as the last seen, its revocation as the retirement), so that nothing paired before this
migration is left outside the registry; no digest, no identifier and no row of any other table is
changed. `device_token` is rebuilt (the foreign key is named, as the metadata names it) and its
rows are copied as they are. Reversible: the downgrade drops the registry and the column.

Number: 0100 is the one PLAN-M5 reserved for this lot (0099 stays with the lots of M4 in flight).
"""
# ruff: noqa: E501  (the DDL is the one SQLAlchemy writes, kept as it is)

import os
import time

from alembic import op

revision = "0100"
down_revision = "0098"
branch_labels = None
depends_on = None

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_TOKEN_COLUMNS = (
    "id", "public_id", "name", "subject_user_id", "issued_by", "secret_digest", "expires_at",
    "last_used_at", "revoked_at", "created_at",
)  # fmt: skip


def _ulid() -> str:
    """A 26-sign identifier in the alphabet the `ulid` checks accept (frozen here: a migration
    never imports what the application may change)."""
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(_CROCKFORD[(value >> (5 * i)) & 31] for i in range(25, -1, -1))


def _create_device_token(with_device: bool) -> None:
    device_column = "\tdevice_id VARCHAR(26),\n" if with_device else ""
    device_fk = (
        "\tCONSTRAINT fk_device_token_device_id_device FOREIGN KEY(device_id) REFERENCES device (id) ON DELETE CASCADE,\n"
        if with_device
        else ""
    )
    op.execute(
        f"""
CREATE TABLE device_token (
	id VARCHAR(26) NOT NULL,
	public_id VARCHAR(8) NOT NULL,
	name VARCHAR,
	subject_user_id VARCHAR(26) NOT NULL,
	issued_by VARCHAR(26),
{device_column}	secret_digest VARCHAR NOT NULL,
	expires_at DATETIME,
	last_used_at DATETIME,
	revoked_at DATETIME,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_device_token PRIMARY KEY (id),
	CONSTRAINT uq_device_token_public_id UNIQUE (public_id),
	CONSTRAINT ck_device_token_id_ulid CHECK (length("id") = 26 AND "id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_device_token_secret_digest_sha256_prefixed CHECK (length("secret_digest") = 71 AND "secret_digest" GLOB 'sha256:*' AND substr("secret_digest", 8) NOT GLOB '*[^0-9a-f]*'),
	CONSTRAINT ck_device_token_public_id_length CHECK (length("public_id") = 8),
{device_fk}	CONSTRAINT fk_device_token_issued_by_user FOREIGN KEY(issued_by) REFERENCES user (id) ON DELETE SET NULL,
	CONSTRAINT fk_device_token_subject_user_id_user FOREIGN KEY(subject_user_id) REFERENCES user (id) ON DELETE CASCADE
)
"""
    )
    op.execute("CREATE INDEX ix_device_token_subject ON device_token (subject_user_id)")


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE device (
	id VARCHAR(26) NOT NULL,
	subject_user_id VARCHAR(26) NOT NULL,
	label VARCHAR,
	kind VARCHAR DEFAULT 'other' NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	last_seen_at DATETIME,
	revoked_at DATETIME,
	CONSTRAINT pk_device PRIMARY KEY (id),
	CONSTRAINT ck_device_id_ulid CHECK (length("id") = 26 AND "id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_device_kind_values CHECK ("kind" IN ('koreader', 'kobo', 'opds', 'browser', 'other')),
	CONSTRAINT fk_device_subject_user_id_user FOREIGN KEY(subject_user_id) REFERENCES user (id) ON DELETE CASCADE
)
"""
    )
    op.execute("CREATE INDEX ix_device_subject ON device (subject_user_id)")
    bind = op.get_bind()
    rows = bind.exec_driver_sql(f"SELECT {', '.join(_TOKEN_COLUMNS)} FROM device_token").fetchall()  # noqa: S608
    op.execute("ALTER TABLE device_token RENAME TO device_token_old")
    op.execute("DROP INDEX ix_device_token_subject")
    _create_device_token(with_device=True)
    for row in rows:
        token = dict(zip(_TOKEN_COLUMNS, row, strict=True))
        device_id = _ulid()
        bind.exec_driver_sql(
            "INSERT INTO device (id, subject_user_id, label, kind, created_at, last_seen_at, revoked_at) "
            "VALUES (?, ?, ?, 'other', ?, ?, ?)",
            (
                device_id,
                token["subject_user_id"],
                token["name"],
                token["created_at"],
                token["last_used_at"],
                token["revoked_at"],
            ),
        )
        columns = ", ".join((*_TOKEN_COLUMNS, "device_id"))
        bind.exec_driver_sql(
            f"INSERT INTO device_token ({columns}) VALUES ({', '.join('?' * (len(_TOKEN_COLUMNS) + 1))})",  # noqa: S608
            (*row, device_id),
        )
    op.execute("DROP TABLE device_token_old")


def downgrade() -> None:
    bind = op.get_bind()
    rows = bind.exec_driver_sql(f"SELECT {', '.join(_TOKEN_COLUMNS)} FROM device_token").fetchall()  # noqa: S608
    op.execute("ALTER TABLE device_token RENAME TO device_token_new")
    op.execute("DROP INDEX ix_device_token_subject")
    _create_device_token(with_device=False)
    for row in rows:
        bind.exec_driver_sql(
            f"INSERT INTO device_token ({', '.join(_TOKEN_COLUMNS)}) VALUES ({', '.join('?' * len(_TOKEN_COLUMNS))})",  # noqa: S608
            tuple(row),
        )
    op.execute("DROP TABLE device_token_new")
    op.execute("DROP INDEX ix_device_subject")
    op.execute("DROP TABLE device")
