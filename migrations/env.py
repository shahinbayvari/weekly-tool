from alembic import context

from app.db import make_engine
from app.models import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_online() -> None:
    engine = make_engine(config.attributes.get("db_path"))  # default: WEEKLY_DB_PATH
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations_online()
