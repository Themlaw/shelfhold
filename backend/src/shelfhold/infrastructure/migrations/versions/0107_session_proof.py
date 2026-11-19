"""What proved a session: `session.proof` (M5 L04, EF-USR-024, #362 review MAJ-1).

A session is fresh because of a proof: a password or a passkey typed here (`local`), or a sign-in
at the identity provider (`provider`). They are not worth the same. Whoever knows an account's
credentials at the provider can open a session through it and make it fresh again by signing in
there, so a freshness proved only by the provider must not be enough to add a way in that outlives
the session (a passkey, a key or a token, a link). The column says which proof the session holds,
and the rule reads it.

`proof` is `local` or `provider`, NOT NULL. Rows that exist when this migration runs are filled
from what is known of them: a session the provider opened (an `auth.oidc_login` line of the audit
names it) or that never had a local proof (its `auth_time` is still the epoch) is `provider`;
every other one is `local`. The default is chosen on the safe side: a session wrongly marked
`provider` only has to prove itself locally once more before it adds a way in, while one wrongly
marked `local` would let a provider-only proof through. The ones that are marked wrongly are the
rare sessions opened by the provider, proved again there and kept alive since, once the audit
line has been purged: they were not distinguishable before this column. Reversible: the downgrade
drops the column.

Number: 0107, after 0102 (the reader positions): the numbers between are held by the other reader
lots, so the chain is re-linked on the merge, whichever of them goes first.
"""
# ruff: noqa: E501  (the DDL is the one SQLAlchemy writes, kept as it is)

from alembic import op

revision = "0107"
down_revision = "0102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE session ADD COLUMN proof VARCHAR DEFAULT 'local' NOT NULL "
        "CONSTRAINT ck_session_proof_values CHECK (\"proof\" IN ('local', 'provider'))"
    )
    op.execute(
        """
UPDATE session SET proof = 'provider'
WHERE id IN (SELECT session_id FROM audit WHERE action = 'auth.oidc_login' AND session_id IS NOT NULL)
   OR auth_time < '1971-01-01'
"""
    )


def downgrade() -> None:
    op.execute("ALTER TABLE session DROP COLUMN proof")
