"""Alembic environment.

The connection string comes from app.config rather than alembic.ini, so
migrations cannot be pointed at a different database than the application by
editing one file and forgetting the other.

Autogenerate is available but the baseline was written by hand: it executes the
SQL that already built every existing database. Autogenerating a baseline would
have produced whatever SQLAlchemy inferred from the models, which is not
guaranteed to match the enums, partial indexes and check constraints the SQL
actually created - and a "baseline" that differs from the deployed schema is
worse than none, because every later diff is measured against a fiction.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import get_settings
from app.models.core import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it (`alembic upgrade --sql`)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _configure_and_run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        # Enum types are created by the baseline SQL, not by SQLAlchemy.
        # Without this, autogenerate proposes dropping and recreating them
        # on every run.
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # A caller may hand us a live connection through config.attributes - the
    # standard hook for running migrations programmatically. Honouring it is
    # what lets a test migrate a scratch database; without it env.py always
    # builds its own engine from settings and quietly migrates the *live*
    # database instead, which is how a test can appear to pass while having
    # touched nothing it intended to.
    supplied = config.attributes.get("connection")
    if supplied is not None:
        _configure_and_run(supplied)
        return

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _configure_and_run(connection)


def _include_object(obj, name, type_, reflected, compare_to):  # noqa: ANN001, ARG001
    """Keep alembic out of tables it does not own."""
    if type_ == "table" and name in {"alembic_version", "spatial_ref_sys"}:
        return False
    return True


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
