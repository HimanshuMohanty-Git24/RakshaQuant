"""
SQLite connection and schema migrations (plan M1.5).

Connections use WAL with ``synchronous=NORMAL`` and a busy timeout, and run in autocommit
mode so callers open explicit ``BEGIN IMMEDIATE`` transactions. Migrations are the numbered
``migrations/NNNN_*.sql`` files, applied in order, each in its own transaction, with the
schema version kept in ``PRAGMA user_version``.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
BUSY_TIMEOUT_MS = 5000

_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class SchemaTooNewError(RuntimeError):
    """The database was written by a newer version of the code."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str


def connect(path: Path) -> sqlite3.Connection:
    """Open (creating if needed) a WAL-mode database at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        path, isolation_level=None, check_same_thread=False, timeout=BUSY_TIMEOUT_MS / 1000
    )
    conn.row_factory = sqlite3.Row
    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        conn.close()
        raise RuntimeError(f"could not enable WAL on {path} (journal_mode={mode})")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    found: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_NAME.match(path.name)
        if match is None:
            raise ValueError(f"badly named migration {path.name} (want NNNN_name.sql)")
        found.append(Migration(int(match.group(1)), path.stem, path.read_text(encoding="utf-8")))
    versions = [m.version for m in found]
    if versions != list(range(1, len(found) + 1)):
        raise ValueError(f"migrations must be numbered 1..n without gaps, got {versions}")
    return found


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection, migrations: list[Migration] | None = None) -> int:
    """Apply pending migrations; return the resulting schema version."""
    pending = load_migrations() if migrations is None else migrations
    latest = pending[-1].version if pending else 0
    current = schema_version(conn)
    if current > latest:
        raise SchemaTooNewError(f"database schema v{current} is newer than this code (v{latest})")
    for migration in pending:
        if migration.version <= current:
            continue
        try:
            conn.executescript(
                f"BEGIN IMMEDIATE;\n{migration.sql}\n"
                f"PRAGMA user_version = {migration.version};\nCOMMIT;"
            )
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return schema_version(conn)
