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
SCHEMA_VERSION = 2

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
    status TEXT NOT NULL,          -- ok | no_text | password | drm | error | duplicate
    message TEXT,
    content_hash TEXT,             -- sha256, recorded for scanned PDFs to find their OCR copy
    ocr INTEGER NOT NULL DEFAULT 0, -- 1 if the text came from an OCR copy
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

# Search history isn't a cache of the library, so it's kept when the index is rebuilt
# (it isn't in _TABLES) or cleared. The same search with different capitals is one entry.
_HISTORY = """
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY,
    query TEXT NOT NULL COLLATE NOCASE,
    topic TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
    hits INTEGER NOT NULL,
    searched_at TEXT NOT NULL,
    UNIQUE (query, topic)
);
"""
HISTORY_LIMIT = 200


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
    content_hash: str | None
    ocr: bool


class Database:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @classmethod
    def open(cls, path: Path, *, create: bool = True) -> Database:
        check_fts5()
        if not create and not path.exists():
            raise IndexUnavailable(f"No index at {path}. Run 'book-searchie index' first.")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Wait rather than fail if another process (indexing, OCR) is writing.
        conn = sqlite3.connect(path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        db = cls(conn)
        db._migrate()
        conn.executescript(_HISTORY)
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
        rows = self.conn.execute("SELECT id, path, root, size, mtime, status, content_hash, ocr FROM files")
        return {
            r["path"]: FileState(r["id"], r["root"], r["size"], r["mtime"], r["status"], r["content_hash"], bool(r["ocr"]))
            for r in rows
        }

    def remove_file(self, file_id: int) -> None:
        with self.conn:
            self._delete(file_id)

    def move_file(self, file_id: int, f: LibraryFile) -> None:
        """Point an indexed book at its new location, keeping its text, status and id."""
        with self.conn:
            self.conn.execute(
                "UPDATE files SET path = ?, root = ?, rel = ?, topic = ?, title = ? WHERE id = ?",
                (str(f.path), str(f.root), f.rel, f.topic, f.path.stem, file_id),
            )

    def _delete(self, file_id: int) -> None:
        self.conn.execute("DELETE FROM sections WHERE file_id = ?", (file_id,))
        self.conn.execute("DELETE FROM files WHERE id = ?", (file_id,))

    def store_file(
        self,
        f: LibraryFile,
        extracted: Extracted | None,
        *,
        status: str,
        message: str | None = None,
        content_hash: str | None = None,
        ocr: bool = False,
    ) -> None:
        """Replace everything indexed for this file in one transaction."""
        with self.conn:
            old = self.conn.execute("SELECT id FROM files WHERE path = ?", (str(f.path),)).fetchone()
            if old:
                self._delete(old["id"])
            cur = self.conn.execute(
                "INSERT INTO files (path, root, rel, topic, title, kind, size, mtime, status, message,"
                " content_hash, ocr, units, indexed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(f.path), str(f.root), f.rel, f.topic, f.path.stem, f.kind, f.size, f.mtime,
                    status, message, content_hash, int(ocr), extracted.units if extracted else 0,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            if extracted:
                self.conn.executemany(
                    "INSERT INTO sections (file_id, seq, label, href, text) VALUES (?, ?, ?, ?, ?)",
                    ((cur.lastrowid, s.seq, s.label, s.href, s.text) for s in extracted.sections),
                )

    def store_duplicate(self, f: LibraryFile, used: str) -> None:
        """Record a second copy of a book in another format, without reading its text."""
        self.store_file(f, None, status="duplicate", message=f"The same book as {used}, which is searched instead.")

    def problem_files(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, path, topic, rel, kind, status, message, content_hash, ocr, units FROM files"
            " WHERE status NOT IN ('ok', 'duplicate') ORDER BY status, topic, rel"
        ).fetchall()

    def duplicate_files(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, topic, rel, kind, message FROM files WHERE status = 'duplicate' ORDER BY topic, rel"
        ).fetchall()

    def status_of(self, path: Path) -> str | None:
        row = self.conn.execute("SELECT status FROM files WHERE path = ?", (str(path),)).fetchone()
        return row["status"] if row else None

    def get_file(self, file_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()

    def sections(self, file_id: int) -> list[sqlite3.Row]:
        """A book's indexed pages or chapters (seq, label, href), in order."""
        return self.conn.execute(
            "SELECT seq, label, href FROM sections WHERE file_id = ? ORDER BY seq", (file_id,)
        ).fetchall()

    def record_search(self, query: str, topic: str, hits: int) -> None:
        """Add a search to the history, or move it to the top if it's already there."""
        # Microseconds, so searches made in the same second still come out in order.
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with self.conn:
            self.conn.execute(
                "INSERT INTO searches (query, topic, hits, searched_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (query, topic) DO UPDATE SET"
                " query = excluded.query, hits = excluded.hits, searched_at = excluded.searched_at",
                (query, topic, hits, now),
            )
            self.conn.execute(
                "DELETE FROM searches WHERE id NOT IN"
                " (SELECT id FROM searches ORDER BY searched_at DESC, id DESC LIMIT ?)",
                (HISTORY_LIMIT,),
            )

    def recent_searches(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, query, topic, hits, searched_at FROM searches ORDER BY searched_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def delete_search(self, search_id: int) -> bool:
        with self.conn:
            return self.conn.execute("DELETE FROM searches WHERE id = ?", (search_id,)).rowcount > 0

    def clear_searches(self) -> int:
        with self.conn:
            return self.conn.execute("DELETE FROM searches").rowcount

    def ocr_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM files WHERE ocr = 1").fetchone()[0]

    def topics(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT topic, COUNT(*) AS books FROM files WHERE status != 'duplicate'"
            " GROUP BY topic ORDER BY topic COLLATE NOCASE"
        ).fetchall()

    def stats(self) -> sqlite3.Row:
        return self.conn.execute(
            "SELECT COUNT(*) AS books, COALESCE(SUM(units), 0) AS units FROM files WHERE status != 'duplicate'"
        ).fetchone()
