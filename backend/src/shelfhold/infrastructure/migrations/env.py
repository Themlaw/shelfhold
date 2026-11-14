"""Alembic environment.

`shelfhold migrate` hands over an open connection. The alembic CLI (development) falls back
to `sqlalchemy.url` from the alembic configuration.
"""

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Connection

from shelfhold.infrastructure.db import (
    check_foreign_keys,
    configure_engine,
    foreign_keys_disabled,
)
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
    engine = configure_engine(
        engine_from_config(
            config.get_section(config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )
    )  # the same pragmas and the same BEGIN as the application's engine
    # a data migration reads and then writes: the write lock first, and waited for
    immediate = engine.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
    with immediate.connect() as connection, foreign_keys_disabled(connection):
        run(connection)
        check_foreign_keys(connection)
        connection.commit()
