from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

# fileConfig() is intentionally omitted — structlog manages all logging.
# Including fileConfig() would reconfigure stdlib logging with plain-text output,
# overriding the JSON structlog setup configured in open_ems.logging_config.

target_metadata = None  # Raw SQL migrations only — no SQLAlchemy ORM models


def get_url() -> str:
    """Return DB URL from Alembic config (overridable by the migration runner)."""
    return config.get_main_option("sqlalchemy.url", default="sqlite:///open_ems.db")


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        {"sqlalchemy.url": get_url()},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
