"""The OIDC identity (M5 L03, EF-USR-004): an account linked to a person of an identity provider.

`oidc_identity` holds `(issuer, sub)` for an account that already exists: a sign-in through the
provider finds the account by that pair and never creates one. The pair is unique (one person is
one account), an account holds one identity per provider, an empty `sub` or issuer is no identity,
and the identity goes with its account. Nothing of any other table changes. Reversible: the
downgrade drops the table (the accounts keep their password and passkeys).

Number: 0101 is the one PLAN-M5 reserved for this lot, chained on 0100 (the device registry), and
it merges another day than 0100.
"""
# ruff: noqa: E501  (the DDL is the one SQLAlchemy writes, kept as it is)

from alembic import op

revision = "0101"
down_revision = "0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE oidc_identity (
	id VARCHAR(26) NOT NULL,
	user_id VARCHAR(26) NOT NULL,
	issuer VARCHAR NOT NULL,
	sub VARCHAR NOT NULL,
	linked_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_oidc_identity PRIMARY KEY (id),
	CONSTRAINT ck_oidc_identity_id_ulid CHECK (length("id") = 26 AND "id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_oidc_identity_nonempty CHECK (length("sub") > 0 AND length("issuer") > 0),
	CONSTRAINT uq_oidc_identity_issuer_sub UNIQUE (issuer, sub),
	CONSTRAINT uq_oidc_identity_user_issuer UNIQUE (user_id, issuer),
	CONSTRAINT fk_oidc_identity_user_id_user FOREIGN KEY(user_id) REFERENCES user (id) ON DELETE CASCADE
)
"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE oidc_identity")
