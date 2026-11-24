"""SQLite + SQLAlchemy setup."""
from __future__ import annotations

from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

settings.data_dir.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False, "timeout": 15},
    future=True,
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=15000")
    cur.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


SCHEMA_VERSION = 2

# Migration 2 adds the claim lifecycle columns to an existing ``claims`` table.
# New tables are created by ``create_all``; SQLite needs ALTER TABLE for new columns.
_CLAIM_COLUMNS_V2 = [
    ("claim_number", "VARCHAR(40)"), ("member_id", "VARCHAR(40)"), ("policy_id", "VARCHAR(40)"),
    ("category", "VARCHAR(20)"), ("claimed_halalas", "INTEGER"), ("currency", "VARCHAR(3)"),
    ("stage", "VARCHAR(20)"), ("action_required", "BOOLEAN NOT NULL DEFAULT 0"),
    ("decision", "VARCHAR(10)"), ("approved_halalas", "INTEGER"), ("decision_reason", "TEXT"),
    ("decided_at", "DATETIME"), ("payout_status", "VARCHAR(10) NOT NULL DEFAULT 'unpaid'"),
    ("paid_at", "DATETIME"), ("paid_halalas", "INTEGER"), ("check_round", "INTEGER NOT NULL DEFAULT 0"),
    ("review_started_at", "DATETIME"), ("review_due_at", "DATETIME"), ("review_paused_at", "DATETIME"),
    ("last_update_at", "DATETIME"), ("version", "INTEGER NOT NULL DEFAULT 1"),
    ("origin", "VARCHAR(10) NOT NULL DEFAULT 'app'"), ("scenario", "VARCHAR(40)"),
]


def migrate() -> int:
    """Bring an existing database to SCHEMA_VERSION. Safe to run repeatedly."""
    from sqlalchemy import inspect, text

    from . import models  # noqa: F401  (register tables)

    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        existing = {c["name"] for c in inspect(conn).get_columns("claims")}
        for name, ddl in _CLAIM_COLUMNS_V2:
            if name not in existing:
                conn.execute(text(f"ALTER TABLE claims ADD COLUMN {name} {ddl}"))
        # Tables added in this version may exist from an earlier development run with fewer
        # columns; add any missing nullable column so the schema converges.
        insp = inspect(conn)
        for table in Base.metadata.sorted_tables:
            if table.name == "claims" or not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for column in table.columns:
                if column.name not in have and column.nullable:
                    ddl = column.type.compile(dialect=conn.dialect)
                    conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {column.name} {ddl}"))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_claims_claim_number ON claims (claim_number)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_claims_member_stage ON claims (member_id, stage)"))
        row = conn.execute(text("SELECT version FROM schema_version WHERE id = 1")).first()
        if row is None:
            conn.execute(text("INSERT INTO schema_version (id, version) VALUES (1, :v)"), {"v": SCHEMA_VERSION})
        elif row[0] < SCHEMA_VERSION:
            conn.execute(text("UPDATE schema_version SET version = :v WHERE id = 1"), {"v": SCHEMA_VERSION})
    return SCHEMA_VERSION


def init_db() -> None:
    migrate()


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
