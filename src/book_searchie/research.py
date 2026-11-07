"""Research complete: books you've finished researching for a search term.

A record is a search term and a book, found by its file path (like book pieces), so it
follows the book when it's moved or renamed. The term is the search with spaces tidied
and capitals ignored, so "Nettle" and "nettle " are the same.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

TERM_MAX = 500

RESEARCH_SCHEMA = """
CREATE TABLE research (
    id INTEGER PRIMARY KEY,
    term TEXT NOT NULL,                  -- research_term(): tidied, lower case
    path TEXT NOT NULL,                  -- the book's file path
    book_title TEXT NOT NULL DEFAULT '',
    topic TEXT NOT NULL DEFAULT '',
    done TEXT NOT NULL,                  -- when it was marked complete
    source_missing INTEGER NOT NULL DEFAULT 0,
    UNIQUE (term, path)
);
CREATE INDEX research_path ON research(path);
"""


class ResearchInvalid(ValueError):
    """The term can't be used."""


def research_term(query: str) -> str:
    """The term a search counts as: spaces tidied, capitals ignored."""
    term = " ".join((query or "").split()).casefold()
    if not term:
        raise ResearchInvalid("There's no search term to mark the book done for.")
    if len(term) > TERM_MAX:
        raise ResearchInvalid(f"A search term can be at most {TERM_MAX} characters.")
    return term


@dataclass
class ResearchRecord:
    term: str
    path: str
    book_title: str
    topic: str
    done: str
    source_missing: bool


class ResearchMixin:
    """Research records, for NotesStore. Expects `conn`."""

    def is_researched(self, query: str, path: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM research WHERE term = ? AND path = ?", (research_term(query), path)
        ).fetchone() is not None

    def set_researched(self, query: str, path: str, done: bool, *, book_title: str = "", topic: str = "") -> bool:
        """Mark a book done (or not) for a search term. Returns whether it's now done."""
        term = research_term(query)
        with self.conn:
            if done:
                self.conn.execute(
                    "INSERT INTO research (term, path, book_title, topic, done) VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT (term, path) DO NOTHING",
                    (term, path, book_title, topic, datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )
            else:
                self.conn.execute("DELETE FROM research WHERE term = ? AND path = ?", (term, path))
        return done

    def researched_paths(self, query: str) -> set[str]:
        """The books marked done for this search."""
        try:
            term = research_term(query)
        except ResearchInvalid:
            return set()
        return {r[0] for r in self.conn.execute("SELECT path FROM research WHERE term = ?", (term,))}

    def research_records(self) -> list[ResearchRecord]:
        """Every record, by term, then book."""
        rows = self.conn.execute(
            "SELECT term, path, book_title, topic, done, source_missing FROM research"
            " ORDER BY term, book_title COLLATE NOCASE, path")
        return [ResearchRecord(r[0], r[1], r[2], r[3], r[4], bool(r[5])) for r in rows]

    # --- following books (called from NotesStore.move_book and sync_books) --------------

    def _move_book_research(self, old_path: str, new_path: str, topic: str, book_title: str) -> None:
        # If the book was already done for a term under its new path, keep that record.
        self.conn.execute(
            "DELETE FROM research WHERE path = ? AND term IN (SELECT term FROM research WHERE path = ?)",
            (old_path, new_path),
        )
        self.conn.execute(
            "UPDATE research SET path = ?, topic = ?, book_title = ?, source_missing = 0 WHERE path = ?",
            (new_path, topic, book_title, old_path),
        )
