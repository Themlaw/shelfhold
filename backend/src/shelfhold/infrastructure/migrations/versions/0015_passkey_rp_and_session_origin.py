"""Review PR #95 (PLAN-M1b L05): the RP ID a passkey was made for, and the passkey behind a session.

- `webauthn_credential.rp_id`: the domain the credential is bound to. `shelfhold status` warns
  when the instance's RP ID is no longer the one of the registered passkeys (ADR-0050, risk
  "change of domain"). Rows that exist get the RP ID of the current `SHELFHOLD_PUBLIC_URL`
  (what they were registered under); NULL when the instance has none.
- `session.passkey_id`: the passkey that opened the session, so revoking the passkey closes it
  (no foreign key, see `upgrade`).
"""

from urllib.parse import urlsplit

import sqlalchemy as sa
from alembic import context, op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def _current_rp_id() -> str | None:
    url = context.config.attributes.get("public_url")
    try:
        host = urlsplit(str(url)).hostname if url else None
        return host.encode("idna").decode("ascii").lower() if host else None
    except (ValueError, UnicodeError):
        return None


def upgrade() -> None:
    op.add_column("webauthn_credential", sa.Column("rp_id", sa.String(), nullable=True))
    rp_id = _current_rp_id()
    if rp_id:
        op.execute(sa.text("UPDATE webauthn_credential SET rp_id = :rp").bindparams(rp=rp_id))
    # No foreign key: SQLite cannot add one without rebuilding the table, and a passkey row is
    # never deleted (it is revoked), except with its user, whose sessions go too.
    op.add_column("session", sa.Column("passkey_id", sa.String(26), nullable=True))
    op.create_index("ix_session_passkey", "session", ["passkey_id"])


def downgrade() -> None:
    op.drop_index("ix_session_passkey", table_name="session")
    op.drop_column("session", "passkey_id")
    op.drop_column("webauthn_credential", "rp_id")
