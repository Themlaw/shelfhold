"""Alembic environment.

`shelfhold migrate` hands over an open connection. The alembic CLI (development) falls back
to `sqlalchemy.url` from the alembic configuration.
"""

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Connection

from shelfhold.infrastructure.schema import metadata

config = context.config


def run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=metadata,  # `alembic revision --autogenerate` compares against it
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


connection = config.attributes.get("connection")
if connection is not None:
    run(connection)
else:
    engine = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with engine.begin() as connection:
        run(connection)
