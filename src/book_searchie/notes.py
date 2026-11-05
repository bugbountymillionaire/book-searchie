"""The KB: your own notes, kept in their own SQLite file.

Unlike the index, which is a cache of the library and is rebuilt whenever its
schema changes, notes are your own work. So this file is only ever changed by
migrations that keep every note, and a copy is made before each migration.

A note comes from one of three kinds of source:

- ``book``: a page or chapter of a book in the library, found by its file path
  and ``seq`` (never ``files.id``, which can change when the index is rebuilt);
- ``other``: anything else (a lecture, website, course, person), described in
  ``source_ref``, with an optional ``source_url``;
- ``own``: your own knowledge, with no outside source.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .db import check_fts5
from .search import QueryError, prepare_query

SOURCE_TYPES = ("book", "other", "own")

TITLE_MAX = 200
TEXT_MAX = 50_000  # quote and body, each
REF_MAX = 500  # source_ref and source_url
TAG_MAX = 60
DEFAULT_TITLE_WORDS = 8

# Each entry upgrades the file from version i to i + 1. Only ever add to the end:
# never edit or remove a migration that has been released.
_MIGRATIONS = [
    """
    CREATE TABLE notes (
        id INTEGER PRIMARY KEY,
        source_type TEXT NOT NULL CHECK (source_type IN ('book', 'other', 'own')),
        title TEXT NOT NULL DEFAULT '',
        quote TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL DEFAULT '',       -- light Markdown
        topic TEXT NOT NULL,
        path TEXT,                           -- book notes: the book's file path ...
        seq INTEGER,                         -- ... and page index / spine position
        label TEXT,                          -- page label or chapter title when saved
        book_title TEXT,
        href TEXT,                           -- EPUB chapter, for the viewer
        source_ref TEXT,                     -- other notes: where it came from
        source_url TEXT,
        source_missing INTEGER NOT NULL DEFAULT 0,  -- book notes whose book has gone
        created TEXT NOT NULL,
        updated TEXT NOT NULL
    );
    CREATE INDEX notes_book ON notes(path, seq);
    CREATE INDEX notes_topic ON notes(topic COLLATE NOCASE);
    CREATE INDEX notes_updated ON notes(updated);

    CREATE TABLE tags (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL COLLATE NOCASE,
        name TEXT NOT NULL COLLATE NOCASE,
        UNIQUE (kind, name)
    );

    CREATE TABLE note_tags (
        note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
        tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
        PRIMARY KEY (note_id, tag_id)
    );
    CREATE INDEX note_tags_tag ON note_tags(tag_id);

    -- Kept up to date by NotesStore, not triggers, because the tags column
    -- comes from other tables. rowid = notes.id.
    CREATE VIRTUAL TABLE notes_fts USING fts5(
        title, quote, body, source_ref, tags,
        tokenize='unicode61 remove_diacritics 2'
    );
    """,
    # 2: other names for a tag ("Urtica dioica" for Nettle). Unique within a kind, and
    # never the same as a tag of that kind.
    """
    CREATE TABLE tag_aliases (
        id INTEGER PRIMARY KEY,
        tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
        kind TEXT NOT NULL COLLATE NOCASE,
        alias TEXT NOT NULL COLLATE NOCASE,
        UNIQUE (kind, alias)
    );
    CREATE INDEX tag_aliases_tag ON tag_aliases(tag_id);
    """,
]

# bm25 weights for title, quote, body, source_ref, tags.
_WEIGHTS = "5.0, 1.0, 1.0, 2.0, 3.0"


class NotesError(Exception):
    """The notes file can't be used."""


class NoteInvalid(ValueError):
    """A note doesn't have what its source type needs."""


@dataclass(frozen=True)
class Tag:
    kind: str
    name: str


@dataclass
class Note:
    source_type: str
    topic: str
    title: str = ""
    quote: str = ""
    body: str = ""
    tags: list[Tag] = field(default_factory=list)
    # book notes
    path: str | None = None
    seq: int | None = None
    label: str | None = None
    book_title: str | None = None
    href: str | None = None
    # other notes
    source_ref: str | None = None
    source_url: str | None = None
    # set by the store
    id: int | None = None
    source_missing: bool = False
    created: str | None = None
    updated: str | None = None

    @property
    def display_title(self) -> str:
        """The title, or for a book note without one, the start of its quote."""
        if self.title:
            return self.title
        words = (self.quote or self.body).split()
        text = " ".join(words[:DEFAULT_TITLE_WORDS])
        return text + "…" if len(words) > DEFAULT_TITLE_WORDS else text


@dataclass
class NoteHit:
    note: Note
    snippet: str
    score: float


@dataclass
class TagCount:
    kind: str
    name: str
    notes: int
    aliases: list[str] = field(default_factory=list)


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _norm_tag(value: str) -> str:
    return " ".join(value.split())


def clean_tag_name(name: str) -> str:
    """A tag name or alias, tidied, or raise NoteInvalid."""
    name = _norm_tag(name or "")
    if not name:
        raise NoteInvalid("A tag needs a name.")
    if len(name) > TAG_MAX:
        raise NoteInvalid(f"Tags can be at most {TAG_MAX} characters.")
    return name


def validate(note: Note) -> Note:
    """Return a tidied copy of the note, or raise NoteInvalid saying what's wrong."""
    kind = note.source_type
    if kind not in SOURCE_TYPES:
        raise NoteInvalid(f"Unknown source type {kind!r}; use one of {', '.join(SOURCE_TYPES)}.")

    title, quote, body, topic = (_clean(v) for v in (note.title, note.quote, note.body, note.topic))
    if len(title) > TITLE_MAX:
        raise NoteInvalid(f"The title is too long (at most {TITLE_MAX} characters).")
    if len(quote) > TEXT_MAX or len(body) > TEXT_MAX:
        raise NoteInvalid(f"The quote and note text can each be at most {TEXT_MAX:,} characters.")
    if not topic:
        raise NoteInvalid("Every note needs a topic.")
    if not quote and not body:
        raise NoteInvalid("A note needs a quote or some text of your own.")

    path = label = book_title = href = source_ref = source_url = None
    seq = None
    if kind == "book":
        path = _clean(note.path)
        if not path or note.seq is None:
            raise NoteInvalid("A book note needs the book and page it came from.")
        if isinstance(note.seq, bool) or not isinstance(note.seq, int) or note.seq < 0:
            raise NoteInvalid("A book note's page must be a whole number, 0 or more.")
        seq = note.seq
        label = _clean(note.label) or None
        book_title = _clean(note.book_title) or None
        href = _clean(note.href) or None
    else:
        if not title:
            raise NoteInvalid("This note needs a title.")
        if kind == "other":
            source_ref = _clean(note.source_ref)
            if not source_ref:
                raise NoteInvalid("Say where this came from (a lecture, website, course or person).")
            source_url = _clean(note.source_url) or None
            if len(source_ref) > REF_MAX or (source_url and len(source_url) > REF_MAX):
                raise NoteInvalid(f"The source and link can each be at most {REF_MAX} characters.")
            if source_url:
                parts = urlsplit(source_url)
                if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
                    raise NoteInvalid("The link must be a web address starting with http:// or https://.")
        elif not body:
            raise NoteInvalid("A note of your own knowledge needs some text.")

    tags: list[Tag] = []
    seen: set[tuple[str, str]] = set()
    for tag in note.tags:
        tag_kind, name = _clean(tag.kind).lower(), _norm_tag(tag.name)
        if not tag_kind or not name:
            continue
        if len(name) > TAG_MAX:
            raise NoteInvalid(f"Tags can be at most {TAG_MAX} characters.")
        key = (tag_kind, name.casefold())
        if key not in seen:
            seen.add(key)
            tags.append(Tag(tag_kind, name))

    return replace(
        note, title=title, quote=quote, body=body, topic=topic, tags=tags,
        path=path, seq=seq, label=label, book_title=book_title, href=href,
        source_ref=source_ref, source_url=source_url,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_NOTE_COLUMNS = (
    "source_type", "title", "quote", "body", "topic", "path", "seq", "label",
    "book_title", "href", "source_ref", "source_url",
)


def _best_snippet(row: sqlite3.Row, mark_open: str) -> str:
    for name in ("quote_snippet", "body_snippet"):
        if mark_open in (row[name] or ""):
            return row[name]
    return row["quote_snippet"] or row["body_snippet"] or row["any_snippet"] or ""


class NotesStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @classmethod
    def open(cls, path: Path) -> NotesStore:
        check_fts5()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        store = cls(conn)
        try:
            store._migrate(path)
        except BaseException:
            conn.close()
            raise
        return store

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> NotesStore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def _migrate(self, path: Path) -> None:
        version = self.schema_version
        target = len(_MIGRATIONS)
        if version > target:
            raise NotesError(
                f"{path} was saved by a newer version of book-searchie (notes version {version}). "
                "Update book-searchie to use it; it hasn't been changed."
            )
        if version == target:
            return
        if version > 0:
            # Keep a copy of the notes as they were, in case a migration goes wrong.
            backup = sqlite3.connect(path.with_name(f"{path.name}.v{version}.bak"))
            try:
                self.conn.backup(backup)
            finally:
                backup.close()
        for i in range(version, target):
            # executescript commits first, so run each migration in its own explicit transaction.
            self.conn.executescript(f"BEGIN; {_MIGRATIONS[i]}; PRAGMA user_version = {i + 1}; COMMIT;")

    # --- writing ---

    def create(self, note: Note) -> Note:
        note = validate(note)
        now = _now()
        with self.conn:
            cur = self.conn.execute(
                f"INSERT INTO notes ({', '.join(_NOTE_COLUMNS)}, created, updated)"
                f" VALUES ({', '.join('?' * len(_NOTE_COLUMNS))}, ?, ?)",
                (*(getattr(note, c) for c in _NOTE_COLUMNS), now, now),
            )
            note_id = cur.lastrowid
            self._set_tags(note_id, note.tags)
            self._reindex(note_id)
        return self.get(note_id)

    def update(self, note_id: int, note: Note) -> Note | None:
        """Replace a note's contents. Returns None if there's no such note."""
        note = validate(note)
        with self.conn:
            cur = self.conn.execute(
                f"UPDATE notes SET {', '.join(c + ' = ?' for c in _NOTE_COLUMNS)}, updated = ? WHERE id = ?",
                (*(getattr(note, c) for c in _NOTE_COLUMNS), _now(), note_id),
            )
            if cur.rowcount == 0:
                return None
            self.conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
            self._set_tags(note_id, note.tags)
            self._drop_unused_tags()
            self._reindex(note_id)
        return self.get(note_id)

    def delete(self, note_id: int) -> bool:
        with self.conn:
            if self.conn.execute("DELETE FROM notes WHERE id = ?", (note_id,)).rowcount == 0:
                return False
            self.conn.execute("DELETE FROM notes_fts WHERE rowid = ?", (note_id,))
            self._drop_unused_tags()
        return True

    # --- tidying tags ---

    def resolve_tag(self, kind: str, name: str) -> str | None:
        """The tag's own name, given its name in any capitals or one of its aliases."""
        tag_id = self._tag_id(kind.strip().lower(), _norm_tag(name))
        if tag_id is None:
            return None
        return self.conn.execute("SELECT name FROM tags WHERE id = ?", (tag_id,)).fetchone()[0]

    def rename_tag(self, kind: str, name: str, new_name: str) -> tuple[str, bool]:
        """Rename a tag on every note that has it. If a tag (or alias) of that kind already
        has the new name, the two are merged, and the old name becomes an alias of the tag
        it was merged into, so typing it later still finds it.

        Returns (the tag's name now, whether it was merged).
        """
        kind, new_name = kind.strip().lower(), clean_tag_name(new_name)
        with self.conn:
            tag_id = self._tag_id(kind, _norm_tag(name), aliases=False)
            if tag_id is None:
                raise NoteInvalid(f"There's no {kind} tag called \"{name}\".")
            old_name = self.conn.execute("SELECT name FROM tags WHERE id = ?", (tag_id,)).fetchone()[0]
            target = self._tag_id(kind, new_name)
            if target is None or target == tag_id:
                # A new name, or the same one in different capitals.
                self.conn.execute("DELETE FROM tag_aliases WHERE kind = ? AND alias = ?", (kind, new_name))
                self.conn.execute("UPDATE tags SET name = ? WHERE id = ?", (new_name, tag_id))
                self._reindex_tag(tag_id)
                return new_name, False
            self.conn.execute(
                "INSERT OR IGNORE INTO note_tags (note_id, tag_id) SELECT note_id, ? FROM note_tags WHERE tag_id = ?",
                (target, tag_id),
            )
            self.conn.execute("UPDATE tag_aliases SET tag_id = ? WHERE tag_id = ?", (target, tag_id))
            self.conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
            self.conn.execute(
                "INSERT OR IGNORE INTO tag_aliases (tag_id, kind, alias) VALUES (?, ?, ?)", (target, kind, old_name)
            )
            self._reindex_tag(target)
            return self.conn.execute("SELECT name FROM tags WHERE id = ?", (target,)).fetchone()[0], True

    def add_alias(self, kind: str, name: str, alias: str) -> list[str]:
        """Give a tag another name. Returns the tag's aliases."""
        kind, alias = kind.strip().lower(), clean_tag_name(alias)
        with self.conn:
            tag_id = self._tag_id(kind, _norm_tag(name), aliases=False)
            if tag_id is None:
                raise NoteInvalid(f"There's no {kind} tag called \"{name}\".")
            other = self._tag_id(kind, alias)
            if other == tag_id:
                raise NoteInvalid(f"\"{alias}\" is already a name for this tag.")
            if other is not None:
                other_name = self.conn.execute("SELECT name FROM tags WHERE id = ?", (other,)).fetchone()[0]
                raise NoteInvalid(
                    f"\"{alias}\" is already the {kind} tag \"{other_name}\" or one of its other names. "
                    "To make them one tag, rename one to the other."
                )
            self.conn.execute("INSERT INTO tag_aliases (tag_id, kind, alias) VALUES (?, ?, ?)", (tag_id, kind, alias))
            self._reindex_tag(tag_id)
        return self.aliases_of(tag_id)

    def remove_alias(self, kind: str, alias: str) -> bool:
        kind, alias = kind.strip().lower(), _norm_tag(alias)
        with self.conn:
            row = self.conn.execute(
                "SELECT tag_id FROM tag_aliases WHERE kind = ? AND alias = ?", (kind, alias)
            ).fetchone()
            if row is None:
                return False
            self.conn.execute("DELETE FROM tag_aliases WHERE kind = ? AND alias = ?", (kind, alias))
            self._reindex_tag(row[0])
            self._drop_unused_tags()
        return True

    def aliases_of(self, tag_id: int) -> list[str]:
        rows = self.conn.execute("SELECT alias FROM tag_aliases WHERE tag_id = ? ORDER BY alias", (tag_id,))
        return [r[0] for r in rows]

    # --- keeping book notes attached to their books (called by the indexer) ---

    def move_book(self, old_path: str, new_path: str, topic: str, book_title: str) -> int:
        """A book was moved or renamed: its notes follow it. Returns how many notes moved."""
        with self.conn:
            # Not a change to the notes themselves, so `updated` stays as it was.
            return self.conn.execute(
                "UPDATE notes SET path = ?, topic = ?, book_title = ?, source_missing = 0"
                " WHERE source_type = 'book' AND path = ?",
                (new_path, topic, book_title, old_path),
            ).rowcount

    def sync_books(self, present: set[str], unavailable_roots: list[str] = ()) -> int:
        """Mark book notes whose book isn't in the library as "source missing", and unmark
        those whose book is back. Notes under an unavailable root (an unplugged drive) are
        left as they are. Returns how many notes are now missing their book."""
        roots = [Path(r) for r in unavailable_roots]
        paths = [r[0] for r in self.conn.execute("SELECT DISTINCT path FROM notes WHERE source_type = 'book'")]
        with self.conn:
            for path in paths:
                if path in present:
                    missing = 0
                elif any(Path(path).is_relative_to(root) for root in roots):
                    continue
                else:
                    missing = 1
                self.conn.execute(
                    "UPDATE notes SET source_missing = ? WHERE source_type = 'book' AND path = ?", (missing, path)
                )
        return self.missing_count()

    def missing_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM notes WHERE source_missing = 1").fetchone()[0]

    def _set_tags(self, note_id: int, tags: list[Tag]) -> None:
        for tag in tags:
            # An alias stands for its tag. Otherwise a tag keeps the spelling it was first
            # given: "Nettle" and "nettle" are one tag.
            tag_id = self._tag_id(tag.kind, tag.name)
            if tag_id is None:
                tag_id = self.conn.execute(
                    "INSERT INTO tags (kind, name) VALUES (?, ?)", (tag.kind, tag.name)
                ).lastrowid
            self.conn.execute("INSERT OR IGNORE INTO note_tags (note_id, tag_id) VALUES (?, ?)", (note_id, tag_id))

    def _tag_id(self, kind: str, name: str, *, aliases: bool = True) -> int | None:
        """The tag with this name, or that has it as an alias."""
        row = self.conn.execute("SELECT id FROM tags WHERE kind = ? AND name = ?", (kind, name)).fetchone()
        if row is None and aliases:
            row = self.conn.execute(
                "SELECT tag_id FROM tag_aliases WHERE kind = ? AND alias = ?", (kind, name)
            ).fetchone()
        return row[0] if row else None

    def _drop_unused_tags(self) -> None:
        # Tags with aliases are kept, so the aliases aren't lost while no note has the tag.
        self.conn.execute(
            "DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM note_tags)"
            " AND id NOT IN (SELECT tag_id FROM tag_aliases)"
        )

    def _reindex_tag(self, tag_id: int) -> None:
        for (note_id,) in self.conn.execute("SELECT note_id FROM note_tags WHERE tag_id = ?", (tag_id,)).fetchall():
            self._reindex(note_id)

    def _reindex(self, note_id: int) -> None:
        row = self.conn.execute(
            "SELECT title, quote, body, COALESCE(source_ref, '') AS source_ref FROM notes WHERE id = ?", (note_id,)
        ).fetchone()
        # A note's tags and their aliases, so searching "Urtica dioica" finds notes tagged Nettle.
        names = self.conn.execute(
            "SELECT t.name FROM note_tags nt JOIN tags t ON t.id = nt.tag_id WHERE nt.note_id = ?1"
            " UNION ALL SELECT a.alias FROM note_tags nt JOIN tag_aliases a ON a.tag_id = nt.tag_id"
            " WHERE nt.note_id = ?1",
            (note_id,),
        ).fetchall()
        tags = " ".join(r[0] for r in names)
        self.conn.execute("DELETE FROM notes_fts WHERE rowid = ?", (note_id,))
        self.conn.execute(
            "INSERT INTO notes_fts (rowid, title, quote, body, source_ref, tags) VALUES (?, ?, ?, ?, ?, ?)",
            (note_id, row["title"], row["quote"], row["body"], row["source_ref"], tags),
        )

    # --- reading ---

    def _tags_of(self, note_id: int) -> list[Tag]:
        rows = self.conn.execute(
            "SELECT t.kind, t.name FROM note_tags nt JOIN tags t ON t.id = nt.tag_id"
            " WHERE nt.note_id = ? ORDER BY t.kind, t.name",
            (note_id,),
        )
        return [Tag(r["kind"], r["name"]) for r in rows]

    def _note(self, row: sqlite3.Row) -> Note:
        values = {c: row[c] for c in _NOTE_COLUMNS}
        return Note(
            **values, id=row["id"], source_missing=bool(row["source_missing"]),
            created=row["created"], updated=row["updated"], tags=self._tags_of(row["id"]),
        )

    def get(self, note_id: int) -> Note | None:
        row = self.conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        return self._note(row) if row else None

    def recent(self, limit: int = 20, offset: int = 0) -> list[Note]:
        rows = self.conn.execute(
            "SELECT * FROM notes ORDER BY updated DESC, id DESC LIMIT ? OFFSET ?", (limit, offset)
        )
        return [self._note(r) for r in rows]

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]

    def with_tag(self, kind: str, name: str) -> list[Note]:
        """Notes with a tag, given its name or one of its aliases."""
        tag_id = self._tag_id(kind.strip().lower(), _norm_tag(name))
        rows = self.conn.execute(
            "SELECT n.* FROM notes n JOIN note_tags nt ON nt.note_id = n.id"
            " WHERE nt.tag_id = ? ORDER BY n.updated DESC, n.id DESC",
            (tag_id,),
        )
        return [self._note(r) for r in rows]

    def all_notes(self) -> list[Note]:
        """Every note, oldest first (for exporting)."""
        return [self._note(r) for r in self.conn.execute("SELECT * FROM notes ORDER BY id")]

    def for_page(self, path: str, seq: int) -> list[Note]:
        """Book notes saved from one page or chapter, oldest first."""
        rows = self.conn.execute(
            "SELECT * FROM notes WHERE source_type = 'book' AND path = ? AND seq = ? ORDER BY id", (path, seq)
        )
        return [self._note(r) for r in rows]

    def tags(
        self, kind: str | None = None, prefix: str = "", limit: int | None = None, *, by_name: bool = False
    ) -> list[TagCount]:
        """Tags in use with their note counts and aliases, most used first (or A to Z).

        prefix is for autocomplete: it matches the start of a tag's name or of one of its aliases.
        """
        sql = (
            "SELECT t.id, t.kind, t.name, COUNT(nt.note_id) AS notes FROM tags t"
            " JOIN note_tags nt ON nt.tag_id = t.id WHERE 1"
        )
        params: list = []
        if kind:
            sql += " AND t.kind = ?"
            params.append(kind.strip().lower())
        if prefix.strip():
            like = re.sub(r"([\\%_])", r"\\\1", _norm_tag(prefix)) + "%"
            sql += (
                " AND (t.name LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM tag_aliases a"
                " WHERE a.tag_id = t.id AND a.alias LIKE ? ESCAPE '\\'))"
            )
            params += [like, like]
        sql += " GROUP BY t.id ORDER BY " + ("t.name" if by_name else "notes DESC, t.name")
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [
            TagCount(r["kind"], r["name"], r["notes"], self.aliases_of(r["id"]))
            for r in self.conn.execute(sql, params).fetchall()
        ]

    def search(
        self,
        query: str,
        *,
        topic: str | None = None,
        limit: int = 20,
        offset: int = 0,
        mark: tuple[str, str] = ("[", "]"),
    ) -> list[NoteHit]:
        """Best matches first. Uses the same query syntax as the book search."""
        # Snippets come from the quote or the note text, as the title is shown anyway: the one
        # with a match, or failing that (only the title, source or tags matched) the start of one.
        sql = f"""
            SELECT n.*, snippet(notes_fts, 1, ?1, ?2, '…', 24) AS quote_snippet,
                   snippet(notes_fts, 2, ?1, ?2, '…', 24) AS body_snippet,
                   snippet(notes_fts, -1, ?1, ?2, '…', 24) AS any_snippet,
                   bm25(notes_fts, {_WEIGHTS}) AS score
            FROM notes_fts JOIN notes n ON n.id = notes_fts.rowid
            WHERE notes_fts MATCH ?3
        """
        params: list = [mark[0], mark[1], prepare_query(query)]
        if topic:
            sql += " AND n.topic = ?4 COLLATE NOCASE"
            params.append(topic)
        sql += f" ORDER BY score LIMIT ?{len(params) + 1} OFFSET ?{len(params) + 2}"
        params += [limit, offset]
        try:
            rows = self.conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as e:
            raise QueryError(f"Can't understand that search ({e}). Check quotes, brackets and AND/OR/NOT.") from None
        return [NoteHit(self._note(r), _best_snippet(r, mark[0]), r["score"]) for r in rows]

    def search_count(self, query: str, *, topic: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM notes_fts JOIN notes n ON n.id = notes_fts.rowid WHERE notes_fts MATCH ?"
        params: list = [prepare_query(query)]
        if topic:
            sql += " AND n.topic = ? COLLATE NOCASE"
            params.append(topic)
        try:
            return self.conn.execute(sql, params).fetchone()[0]
        except sqlite3.OperationalError as e:
            raise QueryError(f"Can't understand that search ({e}). Check quotes, brackets and AND/OR/NOT.") from None
