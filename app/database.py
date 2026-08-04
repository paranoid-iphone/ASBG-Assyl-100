from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine_kwargs = {"connect_args": connect_args}
if settings.database_url == "sqlite:///:memory:":
    engine_kwargs["poolclass"] = StaticPool
engine = create_engine(settings.database_url, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_schema_compatibility():
    """Add small backwards-compatible columns for existing local databases."""
    columns = {column["name"] for column in inspect(engine).get_columns("stages")}
    additions = {
        "system_key": "VARCHAR(40)",
        "default_duration_seconds": "INTEGER NOT NULL DEFAULT 60",
        "default_submission_seconds": "INTEGER NOT NULL DEFAULT 20",
        "default_team_points": "FLOAT NOT NULL DEFAULT 5",
    }
    with engine.begin() as connection:
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(text(f"ALTER TABLE stages ADD COLUMN {name} {definition}"))
    team_columns = {column["name"] for column in inspect(engine).get_columns("teams")}
    if "capacity" not in team_columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE teams ADD COLUMN capacity INTEGER NOT NULL DEFAULT 10"))
    election_columns = {column["name"] for column in inspect(engine).get_columns("captain_elections")}
    if "expires_at" not in election_columns:
        with engine.begin() as connection:
            # TIMESTAMP works in PostgreSQL and SQLite; PostgreSQL has no DATETIME type.
            connection.execute(text("ALTER TABLE captain_elections ADD COLUMN expires_at TIMESTAMP"))
    game_columns = {column["name"] for column in inspect(engine).get_columns("games")}
    game_additions = {
        "screen_history_json": "TEXT NOT NULL DEFAULT '[]'",
        "screen_future_json": "TEXT NOT NULL DEFAULT '[]'",
        "current_slide_id": "INTEGER",
        "slides_initialized": "BOOLEAN NOT NULL DEFAULT FALSE",
    }
    with engine.begin() as connection:
        for name, definition in game_additions.items():
            if name not in game_columns:
                connection.execute(text(f"ALTER TABLE games ADD COLUMN {name} {definition}"))
