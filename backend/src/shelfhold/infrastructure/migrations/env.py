"""Alembic environment: the runner hands over an open connection."""

from alembic import context

config = context.config
connection = config.attributes["connection"]

context.configure(connection=connection, target_metadata=None, render_as_batch=True)
with context.begin_transaction():
    context.run_migrations()
