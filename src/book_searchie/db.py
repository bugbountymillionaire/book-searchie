"""The SQLite index: one row per book in `files`, one per PDF page or EPUB chapter in `sections`."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .extract import Extracted
from .scan import LibraryFile

# Bump when the schema changes. The index is a cache of the library, so an old
# index is simply rebuilt.
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE files (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    root TEXT NOT NULL,
    rel TEXT NOT NULL,
    topic TEXT NOT NULL,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    status TEXT NOT NULL,          -- ok | no_text | error
    message TEXT,
    units INTEGER NOT NULL DEFAULT 0,
    indexed_at TEXT NOT NULL
);
CREATE INDEX files_topic ON files(topic);

CREATE TABLE sections (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id),
    seq INTEGER NOT NULL,
    label TEXT NOT NULL,
    href TEXT,
    text TEXT NOT NULL
);
CREATE INDEX sections_file ON sections(file_id, seq);

CREATE VIRTUAL TABLE sections_fts USING fts5(
    text,
    content='sections',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER sections_ai AFTER INSERT ON sections BEGIN
    INSERT INTO sections_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER sections_ad AFTER DELETE ON sections BEGIN
    INSERT INTO sections_fts(sections_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER sections_au AFTER UPDATE ON sections BEGIN
    INSERT INTO sections_fts(sections_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO sections_fts(rowid, text) VALUES (new.id, new.text);
END;
"""

_TABLES = ("sections_fts", "sections", "files")


class IndexUnavailable(Exception):
    """The index can't be used (missing FTS5, unreadable file, ...)."""


def check_fts5() -> None:
    try:
        sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    except sqlite3.OperationalError:
        raise IndexUnavailable(
            f"This Python's SQLite ({sqlite3.sqlite_version}) was built without FTS5, which "
            "book-searchie needs. Install Python from python.org, or your distribution's "
            "full SQLite package, and try again."
        ) from None


@dataclass
class FileState:
    id: int
    root: str
    size: int
    mtime: float
    status: str


class Database:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @classmethod
    def open(cls, path: Path, *, create: bool = True) -> Database:
        check_fts5()
        if not create and not path.exists():
            raise IndexUnavailable(f"No index at {path}. Run 'book-searchie index' first.")
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        db = cls(conn)
        db._migrate()
        return db

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return
        with self.conn:
            for table in _TABLES:
                self.conn.execute(f"DROP TABLE IF EXISTS {table}")
            self.conn.executescript(_SCHEMA)
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def clear(self) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM sections")
            self.conn.execute("DELETE FROM files")

    def file_states(self) -> dict[str, FileState]:
        rows = self.conn.execute("SELECT id, path, root, size, mtime, status FROM files")
        return {r["path"]: FileState(r["id"], r["root"], r["size"], r["mtime"], r["status"]) for r in rows}

    def remove_file(self, file_id: int) -> None:
        with self.conn:
            self._delete(file_id)

    def _delete(self, file_id: int) -> None:
        self.conn.execute("DELETE FROM sections WHERE file_id = ?", (file_id,))
        self.conn.execute("DELETE FROM files WHERE id = ?", (file_id,))

    def store_file(self, f: LibraryFile, extracted: Extracted | None, error: str | None = None) -> None:
        """Replace everything indexed for this file in one transaction."""
        if error is not None:
            status = "error"
        elif extracted.no_text:
            status = "no_text"
        else:
            status = "ok"
        with self.conn:
            old = self.conn.execute("SELECT id FROM files WHERE path = ?", (str(f.path),)).fetchone()
            if old:
                self._delete(old["id"])
            cur = self.conn.execute(
                "INSERT INTO files (path, root, rel, topic, title, kind, size, mtime, status, message, units, indexed_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(f.path), str(f.root), f.rel, f.topic, f.path.stem, f.kind, f.size, f.mtime,
                    status, error, extracted.units if extracted else 0,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            if extracted:
                self.conn.executemany(
                    "INSERT INTO sections (file_id, seq, label, href, text) VALUES (?, ?, ?, ?, ?)",
                    ((cur.lastrowid, s.seq, s.label, s.href, s.text) for s in extracted.sections),
                )

    def problem_files(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT topic, rel, status, message FROM files WHERE status != 'ok' ORDER BY status, topic, rel"
        ).fetchall()

    def topics(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT topic, COUNT(*) AS books FROM files GROUP BY topic ORDER BY topic COLLATE NOCASE"
        ).fetchall()

    def stats(self) -> sqlite3.Row:
        return self.conn.execute(
            "SELECT COUNT(*) AS books, COALESCE(SUM(units), 0) AS units FROM files"
        ).fetchone()
