"""KB entries: what the KB is built from, from notes version 3 on.

An entry has a title (Nettle), any number of other names (Urtica dioica), tags on the
title (Headache, Hay fever), a topic (Herbalism: a top-level library folder), and
sub-headings (Description, Tea, Tincture). Each sub-heading holds pieces of text in the
order they were saved.

Each topic has its own ordered list of sub-headings (from notes version 6), so Survival
entries aren't offered Tincture. A topic's list starts empty and grows as sub-headings are
typed; the first on it is where text goes when no sub-heading is given.

A piece keeps where it came from, but that's never shown: it's only used to list
what was saved from a page, link back to the book, and spot book text before
publishing. Where a piece came from is worked out from what's given:

- ``book``: a page or chapter of a book, found by its file path and ``seq``;
- ``other``: anything else, described in ``source_ref`` and/or ``source_url``;
- ``own``: neither, so your own words.

Entry tags live in the same ``tags`` table as the old note tags, with the kind
``tag``, so renaming, merging and other names work for them too.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .search import QueryError, prepare_query

ENTRY_TAG_KIND = "tag"
# When upgrading a KB with no entries, the one list of sub-headings there was (herbal, to
# start with) goes to this topic. With entries, it goes to the topic most of them are in.
FIRST_TOPIC = "Herbalism"
# Converted notes go under this sub-heading.
CONVERTED_HEADING = "Notes"

ENTRY_TITLE_MAX = 200
HEADING_MAX = 80
PIECE_MAX = 50_000
SOURCE_MAX = 500
ENTRY_TAG_MAX = 60
_TITLE_WORDS = 8  # converted notes without a title are named after their first words

ENTRIES_SCHEMA = [
    """CREATE TABLE entries (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL UNIQUE COLLATE NOCASE,
        created TEXT NOT NULL,
        updated TEXT NOT NULL
    )""",
    # Other names for an entry. Never the same as an entry's title or another alias.
    """CREATE TABLE entry_aliases (
        id INTEGER PRIMARY KEY,
        entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
        alias TEXT NOT NULL UNIQUE COLLATE NOCASE
    )""",
    "CREATE INDEX entry_aliases_entry ON entry_aliases(entry_id)",
    """CREATE TABLE sections (
        id INTEGER PRIMARY KEY,
        entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
        heading TEXT NOT NULL COLLATE NOCASE,
        UNIQUE (entry_id, heading)
    )""",
    """CREATE TABLE pieces (
        id INTEGER PRIMARY KEY,
        section_id INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        body TEXT NOT NULL,                  -- light Markdown
        source_type TEXT NOT NULL CHECK (source_type IN ('book', 'other', 'own')),
        topic TEXT,                          -- the book's topic when saved from a book
        path TEXT,                           -- book pieces: the book's file path ...
        seq INTEGER,                         -- ... and page index / spine position
        label TEXT,
        book_title TEXT,
        href TEXT,
        source_ref TEXT,                     -- other pieces: where it came from
        source_url TEXT,
        source_missing INTEGER NOT NULL DEFAULT 0,
        from_note INTEGER UNIQUE,            -- the old note this was converted from
        created TEXT NOT NULL,
        updated TEXT NOT NULL
    )""",
    "CREATE INDEX pieces_section ON pieces(section_id, position)",
    "CREATE INDEX pieces_book ON pieces(path, seq)",
    """CREATE TABLE entry_tags (
        entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
        tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
        PRIMARY KEY (entry_id, tag_id)
    )""",
    "CREATE INDEX entry_tags_tag ON entry_tags(tag_id)",
    # Kept up to date by the store. rowid = entries.id; title includes the other names.
    "CREATE VIRTUAL TABLE entries_fts USING fts5(title, tags, tokenize='unicode61 remove_diacritics 2')",
    # One row per sub-heading, rowid = sections.id, with the entry's title and tags too,
    # so "nettle tea" finds the Tea part of Nettle.
    """CREATE VIRTUAL TABLE sections_fts USING fts5(
        title, tags, heading, body, tokenize='unicode61 remove_diacritics 2'
    )""",
]

# bm25 weights: entries_fts (title, tags) and sections_fts (title, tags, heading, body).
_ENTRY_WEIGHTS = "10.0, 3.0"
_SECTION_WEIGHTS = "10.0, 3.0, 2.0, 1.0"


# The master list of sub-headings, in the order every entry shows them (notes version 4).
HEADINGS_SCHEMA = """CREATE TABLE headings (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    position INTEGER NOT NULL
)"""
# From notes version 6: a list of sub-headings per topic. '' is for entries with no topic.
TOPIC_HEADINGS_SCHEMA = """CREATE TABLE headings (
    id INTEGER PRIMARY KEY,
    topic TEXT NOT NULL COLLATE NOCASE,
    name TEXT NOT NULL COLLATE NOCASE,
    position INTEGER NOT NULL,
    UNIQUE (topic, name)
)"""

# A line starting with # in the text is a sub-heading for the text after it: "# Tea".
_HEADING_LINE = re.compile(r"^[ \t]{0,3}#+[ \t]*(\S.*?)[ \t#]*$")


def split_headings(text: str) -> list[tuple[str | None, str]]:
    """Split text at its # lines into (sub-heading, text) parts. Text before the first #
    line has no sub-heading (None). Parts with no text are left out."""
    parts: list[tuple[str | None, list[str]]] = [(None, [])]
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        m = _HEADING_LINE.match(line)
        if m:
            parts.append((m.group(1), []))
        else:
            parts[-1][1].append(line)
    return [(heading, "\n".join(lines).strip()) for heading, lines in parts if "\n".join(lines).strip()]


class EntryInvalid(ValueError):
    """An entry or piece isn't right, with a message saying why."""


@dataclass
class Piece:
    body: str
    # book pieces
    path: str | None = None
    seq: int | None = None
    label: str | None = None
    book_title: str | None = None
    href: str | None = None
    topic: str | None = None
    # other pieces
    source_ref: str | None = None
    source_url: str | None = None
    # set by the store
    id: int | None = None
    source_type: str = "own"
    entry_id: int | None = None
    entry_title: str | None = None
    heading: str | None = None
    position: int | None = None
    source_missing: bool = False
    created: str | None = None
    updated: str | None = None


@dataclass
class Section:
    id: int
    heading: str
    pieces: list[Piece]


@dataclass
class Entry:
    id: int
    title: str
    aliases: list[str]
    tags: list[str]
    sections: list[Section]
    created: str
    updated: str
    topic: str = ""

    @property
    def headings(self) -> list[str]:
        return [s.heading for s in self.sections]


@dataclass
class EntrySummary:
    """An entry for a list: its title and sub-headings, without the text."""
    id: int
    title: str
    aliases: list[str]
    tags: list[str]
    headings: list[str]
    pieces: int
    updated: str
    topic: str = ""


@dataclass
class SectionHit:
    heading: str
    snippet: str


@dataclass
class EntryHit:
    entry: EntrySummary
    sections: list[SectionHit]  # the sub-headings the words were found in, in the entry's order
    title_match: bool  # the words are in the title or other names: these come first
    score: float


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(value: str | None) -> str:
    return " ".join((value or "").split())


def clean_title(title: str | None) -> str:
    title = _norm(title)
    if not title:
        raise EntryInvalid("An entry needs a title.")
    if len(title) > ENTRY_TITLE_MAX:
        raise EntryInvalid(f"An entry's title can be at most {ENTRY_TITLE_MAX} characters.")
    return title


def clean_heading(heading: str | None) -> str:
    heading = _norm(heading)
    if not heading:
        raise EntryInvalid("A sub-heading needs a name.")
    if len(heading) > HEADING_MAX:
        raise EntryInvalid(f"A sub-heading can be at most {HEADING_MAX} characters.")
    return heading


def clean_entry_tag(name: str | None) -> str:
    name = _norm(name)
    if not name:
        raise EntryInvalid("A tag needs a name.")
    if len(name) > ENTRY_TAG_MAX:
        raise EntryInvalid(f"Tags can be at most {ENTRY_TAG_MAX} characters.")
    return name


def validate_piece(piece: Piece) -> Piece:
    """A tidied copy of the piece, with its source type worked out, or raise EntryInvalid."""
    body = (piece.body or "").strip()
    if not body:
        raise EntryInvalid("There's no text to save.")
    if len(body) > PIECE_MAX:
        raise EntryInvalid(f"A piece of text can be at most {PIECE_MAX:,} characters.")
    path = _norm(piece.path) or None
    source_ref = _norm(piece.source_ref) or None
    source_url = (piece.source_url or "").strip() or None
    topic = _norm(piece.topic) or None
    label = book_title = href = None
    seq = None
    if path:
        if isinstance(piece.seq, bool) or not isinstance(piece.seq, int) or piece.seq < 0:
            raise EntryInvalid("Text from a book needs the page it came from, as a whole number, 0 or more.")
        kind, seq = "book", piece.seq
        label, book_title, href = (_norm(v) or None for v in (piece.label, piece.book_title, piece.href))
        source_ref = source_url = None
    elif source_ref or source_url:
        kind = "other"
        if (source_ref and len(source_ref) > SOURCE_MAX) or (source_url and len(source_url) > SOURCE_MAX):
            raise EntryInvalid(f"The source and link can each be at most {SOURCE_MAX} characters.")
        if source_url:
            parts = urlsplit(source_url)
            if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
                raise EntryInvalid("The link must be a web address starting with http:// or https://.")
    else:
        kind = "own"
    return replace(
        piece, body=body, source_type=kind, path=path, seq=seq, label=label, book_title=book_title,
        href=href, topic=topic, source_ref=source_ref, source_url=source_url,
    )


_PIECE_COLUMNS = (
    "body", "source_type", "topic", "path", "seq", "label", "book_title", "href", "source_ref", "source_url",
)


class EntriesMixin:
    """The entry half of NotesStore. Expects self.conn, and self.seed_headings: the
    sub-headings from the config, which start the master list."""

    conn: sqlite3.Connection
    seed_headings: list[str]

    # --- sub-headings: one ordered list per topic ---

    def _entry_topic(self, entry_id: int | None) -> str:
        if entry_id is None:
            return ""
        try:
            row = self.conn.execute("SELECT topic FROM entries WHERE id = ?", (entry_id,)).fetchone()
        except sqlite3.OperationalError:  # while upgrading from before version 6
            return ""
        return row[0] if row else ""

    def headings_for(self, topic: str | None) -> list[str]:
        """A topic's sub-headings, in the order its entries show them."""
        try:
            return [r[0] for r in self.conn.execute(
                "SELECT name FROM headings WHERE topic = ? ORDER BY position, id", (_norm(topic),))]
        except sqlite3.OperationalError:  # while upgrading from before version 6
            return []

    def heading_topics(self) -> list[str]:
        """The topics that have sub-headings, A to Z, with no topic ('') last."""
        topics = [r[0] for r in self.conn.execute("SELECT DISTINCT topic FROM headings")]
        return sorted(topics, key=lambda t: (t == "", t.casefold()))

    def used_topics(self) -> list[str]:
        """Every topic an entry is in or that has sub-headings, A to Z, with no topic ('')
        left out."""
        topics = {r[0] for r in self.conn.execute(
            "SELECT topic FROM headings UNION SELECT topic FROM entries")} - {""}
        return sorted(topics, key=str.casefold)

    def topic_name(self, topic: str | None, known: list[str] = ()) -> str:
        """A typed topic with the spelling it already has (in the library, given as `known`,
        or in the KB). A new one typed all in small letters gets a capital: "survival" is
        Survival."""
        topic = _norm(topic)
        for name in [*known, *self.used_topics()]:
            if name.casefold() == topic.casefold():
                return name
        return topic[:1].upper() + topic[1:] if topic == topic.lower() else topic

    def _remember_heading(self, topic: str | None, heading: str) -> None:
        """Add a sub-heading to the end of a topic's list, if it isn't on it."""
        try:
            self.conn.execute(
                "INSERT OR IGNORE INTO headings (topic, name, position)"
                " SELECT ?1, ?2, COALESCE(MAX(position), -1) + 1 FROM headings WHERE topic = ?1",
                (_norm(topic), heading))
        except sqlite3.OperationalError:  # while upgrading from before version 6
            pass

    def add_headings(self, topic: str | None, names: list[str]) -> list[str]:
        """Add sub-headings to the end of a topic's list, skipping any on it. Returns the list."""
        names = [clean_heading(n) for n in names]
        with self.conn:
            for name in names:
                self._remember_heading(topic, name)
        return self.headings_for(topic)

    def heading_counts(self, topic: str | None) -> list[tuple[str, int]]:
        """A topic's list, in order, with how many of its entries use each sub-heading."""
        return [(r[0], r[1]) for r in self.conn.execute(
            "SELECT h.name, COUNT(s.id) FROM headings h LEFT JOIN sections s ON s.heading = h.name"
            " AND s.entry_id IN (SELECT id FROM entries WHERE topic = h.topic)"
            " WHERE h.topic = ? GROUP BY h.id ORDER BY h.position, h.id", (_norm(topic),))]

    def _order_key(self, topic: str | None):
        """A sort key for (heading, section id): the topic's order, then any others in the
        order they were made."""
        order = {h.casefold(): i for i, h in enumerate(self.headings_for(topic))}
        return lambda heading, section_id: (
            (order[heading.casefold()], 0) if heading.casefold() in order else (len(order), section_id))

    def default_heading(self, topic: str | None) -> str | None:
        """Where text goes when no sub-heading is given: the first on the topic's list, or
        None if the topic has none yet."""
        found = self.headings_for(topic)
        return found[0] if found else None

    @staticmethod
    def _no_headings(topic: str | None) -> EntryInvalid:
        where = f"{topic} has" if _norm(topic) else "Entries with no topic have"
        return EntryInvalid(f"{where} no sub-headings yet. Start the text with a # line for one, e.g. # Overview.")

    # --- finding entries ---

    def entry_id(self, title: str) -> int | None:
        """The entry with this title, in any capitals, or with it as another name."""
        title = _norm(title)
        row = self.conn.execute("SELECT id FROM entries WHERE title = ?", (title,)).fetchone()
        if row is None:
            row = self.conn.execute("SELECT entry_id FROM entry_aliases WHERE alias = ?", (title,)).fetchone()
        return row[0] if row else None

    def find_entry(self, title: str) -> Entry | None:
        entry_id = self.entry_id(title)
        return self.get_entry(entry_id) if entry_id is not None else None

    def _heading_for(self, entry_id: int | None, heading: str) -> str:
        """How to spell a sub-heading: as the entry's topic list has it, or as the entry
        already has it, or as typed. So "tea" goes under Tea."""
        heading = clean_heading(heading)
        for known in self.headings_for(self._entry_topic(entry_id)):
            if known.casefold() == heading.casefold():
                return known
        if entry_id is not None:
            row = self.conn.execute(
                "SELECT heading FROM sections WHERE entry_id = ? AND heading = ?", (entry_id, heading)
            ).fetchone()
            if row:
                return row[0]
        return heading

    # --- writing ---

    def create_entry(self, title: str, tags: list[str] = (), topic: str | None = None) -> Entry:
        title = clean_title(title)
        topic = self.topic_name(topic)
        with self.conn:
            if self.entry_id(title) is not None:
                raise EntryInvalid(f"There's already an entry called \"{title}\" (or with that as another name).")
            entry_id = self._insert_entry(title, topic)
            self._set_entry_tags(entry_id, tags)
            self._reindex_entry(entry_id)
        return self.get_entry(entry_id)

    def _insert_entry(self, title: str, topic: str | None = None) -> int:
        # Typing "nettle" makes Nettle. A title with capitals of its own is kept as typed.
        if title == title.lower():
            title = title[:1].upper() + title[1:]
        now = _now()
        return self.conn.execute(
            "INSERT INTO entries (title, topic, created, updated) VALUES (?, ?, ?, ?)", (title, _norm(topic), now, now)
        ).lastrowid

    def set_entry_topic(self, entry_id: int, topic: str | None) -> str:
        """Move an entry to another topic. Its sub-headings are added to that topic's list
        if they aren't on it. Returns the topic."""
        topic = self.topic_name(topic)
        with self.conn:
            entry = self.get_entry(entry_id)
            if entry is None:
                raise EntryInvalid("There's no such entry.")
            self.conn.execute("UPDATE entries SET topic = ? WHERE id = ?", (topic, entry_id))
            for heading in entry.headings:
                self._remember_heading(topic, heading)
            self._touch(entry_id)
        return topic

    def _section_id(self, entry_id: int, heading: str) -> int:
        heading = self._heading_for(entry_id, heading)
        row = self.conn.execute(
            "SELECT id FROM sections WHERE entry_id = ? AND heading = ?", (entry_id, heading)
        ).fetchone()
        if row:
            return row[0]
        self._remember_heading(self._entry_topic(entry_id), heading)
        return self.conn.execute(
            "INSERT INTO sections (entry_id, heading) VALUES (?, ?)", (entry_id, heading)
        ).lastrowid

    def _touch(self, entry_id: int) -> None:
        self.conn.execute("UPDATE entries SET updated = ? WHERE id = ?", (_now(), entry_id))

    def add_piece(self, title: str, heading: str | None, piece: Piece, topic: str | None = None) -> Piece:
        """Add text to the end of an entry's sub-heading, making the entry first if it's new
        (in the given topic, or the piece's). With no sub-heading, it goes under the first
        on the entry's topic list."""
        title, piece = clean_title(title), validate_piece(piece)
        with self.conn:
            entry_id = self.entry_id(title)
            if entry_id is None:
                entry_id = self._insert_entry(title, topic if topic is not None else piece.topic)
            if not heading:
                entry_topic = self._entry_topic(entry_id)
                heading = self.default_heading(entry_topic)
                if heading is None:
                    raise self._no_headings(entry_topic)
            piece_id = self._insert_piece(self._section_id(entry_id, heading), piece)
            self._touch(entry_id)
            self._reindex_entry(entry_id)
        return self.get_piece(piece_id)

    def add_text(self, title: str, text: str, source: Piece | None = None,
                 topic: str | None = None) -> tuple[list[Piece], bool]:
        """Add text to an entry, making the entry first if it's new (in the given topic, or
        the source's). Lines starting with # are sub-headings for the text after them
        ("# Tea"); text before any goes under the first sub-heading on the entry's topic list
        (Description), or if the topic has none yet, under the first one typed. Each part is
        added to the end of its sub-heading, with the same source.

        Returns (the pieces added, whether the entry is new)."""
        title = clean_title(title)
        source = source or Piece(body="")
        parts = [(heading, validate_piece(replace(source, body=body))) for heading, body in split_headings(text)]
        if not parts:
            raise EntryInvalid("There's no text to save.")
        for heading, _ in parts:
            if heading is not None:
                clean_heading(heading)
        with self.conn:
            entry_id = self.entry_id(title)
            new = entry_id is None
            if new:
                entry_id = self._insert_entry(title, topic if topic is not None else source.topic)
            default = None
            if any(heading is None for heading, _ in parts):
                entry_topic = self._entry_topic(entry_id)
                default = self.default_heading(entry_topic) or next((h for h, _ in parts if h is not None), None)
                if default is None:
                    raise self._no_headings(entry_topic)
            ids = [self._insert_piece(self._section_id(entry_id, heading or default), piece)
                   for heading, piece in parts]
            self._touch(entry_id)
            self._reindex_entry(entry_id)
        return [self.get_piece(i) for i in ids], new

    def edit_text(self, piece_id: int, text: str) -> list[Piece] | None:
        """Change a piece's text, which may start with a # line to move it to another
        sub-heading. Further # lines add the text after them as new pieces in the same
        entry, with the same source. Returns the piece, then any added, or None if there's
        no such piece."""
        parts = split_headings(text)
        if not parts:
            raise EntryInvalid("There's no text to save. To remove this text, delete it.")
        for heading, body in parts:
            validate_piece(Piece(body=body))
            if heading is not None:
                clean_heading(heading)
        current = self.get_piece(piece_id)
        if current is None:
            return None
        with self.conn:
            (heading, body), rest = parts[0], parts[1:]
            self.conn.execute("UPDATE pieces SET body = ?, updated = ? WHERE id = ?", (body, _now(), piece_id))
            if heading is not None and heading.casefold() != current.heading.casefold():
                self._move_to_section(piece_id, self._section_id(current.entry_id, heading))
            ids = [self._insert_piece(self._section_id(current.entry_id, h), replace(current, body=b))
                   for h, b in rest]
            self._touch(current.entry_id)
            self._reindex_entry(current.entry_id)
        return [self.get_piece(i) for i in [piece_id, *ids]]

    def _move_to_section(self, piece_id: int, section_id: int) -> None:
        """Put a piece at the end of another sub-heading, tidying the one it leaves."""
        old = self.conn.execute("SELECT section_id FROM pieces WHERE id = ?", (piece_id,)).fetchone()[0]
        if old == section_id:
            return
        end = self.conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM pieces WHERE section_id = ?", (section_id,)
        ).fetchone()[0]
        self.conn.execute("UPDATE pieces SET section_id = ?, position = ? WHERE id = ?", (section_id, end, piece_id))
        self._renumber(old)
        self._drop_empty_sections()

    def _insert_piece(self, section_id: int, piece: Piece, from_note: int | None = None,
                      created: str | None = None, updated: str | None = None) -> int:
        now = _now()
        position = self.conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM pieces WHERE section_id = ?", (section_id,)
        ).fetchone()[0]
        return self.conn.execute(
            f"INSERT INTO pieces (section_id, position, {', '.join(_PIECE_COLUMNS)}, source_missing, from_note,"
            f" created, updated) VALUES (?, ?, {', '.join('?' * len(_PIECE_COLUMNS))}, ?, ?, ?, ?)",
            (section_id, position, *(getattr(piece, c) for c in _PIECE_COLUMNS), int(piece.source_missing),
             from_note, created or now, updated or now),
        ).lastrowid

    def update_piece(self, piece_id: int, body: str) -> Piece | None:
        """Change a piece's text. Where it came from stays as it was. Returns None if there's
        no such piece."""
        body = validate_piece(Piece(body=body)).body
        with self.conn:
            entry_id = self._piece_entry(piece_id)
            if entry_id is None:
                return None
            self.conn.execute("UPDATE pieces SET body = ?, updated = ? WHERE id = ?", (body, _now(), piece_id))
            self._touch(entry_id)
            self._reindex_entry(entry_id)
        return self.get_piece(piece_id)

    def move_piece(self, piece_id: int, *, title: str | None = None, heading: str | None = None,
                   position: int | None = None) -> Piece | None:
        """Move a piece to another sub-heading (and entry, given its title), and/or to a place
        in its sub-heading (0 = first). Moved to another sub-heading, it goes at the end unless
        a position is given. An empty sub-heading left behind is removed."""
        with self.conn:
            row = self.conn.execute(
                "SELECT p.section_id, s.entry_id, s.heading FROM pieces p JOIN sections s ON s.id = p.section_id"
                " WHERE p.id = ?", (piece_id,)
            ).fetchone()
            if row is None:
                return None
            old_section, old_entry = row["section_id"], row["entry_id"]
            entry_id = old_entry
            if title is not None:
                title = clean_title(title)
                entry_id = self.entry_id(title)
                if entry_id is None:
                    entry_id = self._insert_entry(title, self._entry_topic(old_entry))
            section_id = self._section_id(entry_id, heading if heading is not None else row["heading"])
            self._move_to_section(piece_id, section_id)
            if position is not None:
                ids = [r[0] for r in self.conn.execute(
                    "SELECT id FROM pieces WHERE section_id = ? AND id != ? ORDER BY position, id",
                    (section_id, piece_id),
                )]
                ids.insert(max(0, min(position, len(ids))), piece_id)
                self._renumber(section_id, ids)
            for e in {old_entry, entry_id}:
                self._touch(e)
                self._reindex_entry(e)
        return self.get_piece(piece_id)

    def delete_piece(self, piece_id: int) -> bool:
        """Delete a piece. Its entry stays, even with no text left."""
        with self.conn:
            row = self.conn.execute(
                "SELECT p.section_id, s.entry_id FROM pieces p JOIN sections s ON s.id = p.section_id"
                " WHERE p.id = ?", (piece_id,)
            ).fetchone()
            if row is None:
                return False
            self.conn.execute("DELETE FROM pieces WHERE id = ?", (piece_id,))
            self._renumber(row["section_id"])
            self._drop_empty_sections()
            self._touch(row["entry_id"])
            self._reindex_entry(row["entry_id"])
        return True

    def delete_entry(self, entry_id: int) -> bool:
        """Delete an entry with all its text."""
        with self.conn:
            sections = [r[0] for r in self.conn.execute("SELECT id FROM sections WHERE entry_id = ?", (entry_id,))]
            if self.conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,)).rowcount == 0:
                return False
            self.conn.execute("DELETE FROM entries_fts WHERE rowid = ?", (entry_id,))
            self.conn.executemany("DELETE FROM sections_fts WHERE rowid = ?", [(s,) for s in sections])
            self._drop_unused_tags()
        return True

    def rename_entry(self, entry_id: int, new_title: str) -> tuple[str, bool]:
        """Rename an entry. If another entry already has the new title (or it's one of its
        other names), the two are merged: text under the same sub-heading is put together,
        tags and other names are combined, and the old title becomes another name, so
        typing it later still finds the entry.

        Returns (the entry's title now, whether it was merged).
        """
        new_title = clean_title(new_title)
        with self.conn:
            row = self.conn.execute("SELECT title FROM entries WHERE id = ?", (entry_id,)).fetchone()
            if row is None:
                raise EntryInvalid("There's no such entry.")
            old_title = row[0]
            target = self.entry_id(new_title)
            if target is None or target == entry_id:
                self.conn.execute("DELETE FROM entry_aliases WHERE alias = ?", (new_title,))
                self.conn.execute("UPDATE entries SET title = ? WHERE id = ?", (new_title, entry_id))
                self._touch(entry_id)
                self._reindex_entry(entry_id)
                return new_title, False
            for section_id, heading in self.conn.execute(
                    "SELECT id, heading FROM sections WHERE entry_id = ? ORDER BY id", (entry_id,)).fetchall():
                into = self._section_id(target, heading)
                end = self.conn.execute(
                    "SELECT COALESCE(MAX(position), -1) + 1 FROM pieces WHERE section_id = ?", (into,)
                ).fetchone()[0]
                self.conn.execute(
                    "UPDATE pieces SET section_id = ?, position = position + ? WHERE section_id = ?",
                    (into, end, section_id),
                )
                self.conn.execute("DELETE FROM sections_fts WHERE rowid = ?", (section_id,))
            self.conn.execute(
                "INSERT OR IGNORE INTO entry_tags (entry_id, tag_id) SELECT ?, tag_id FROM entry_tags WHERE entry_id = ?",
                (target, entry_id),
            )
            self.conn.execute("UPDATE entry_aliases SET entry_id = ? WHERE entry_id = ?", (target, entry_id))
            self.conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
            self.conn.execute("DELETE FROM entries_fts WHERE rowid = ?", (entry_id,))
            self.conn.execute("INSERT INTO entry_aliases (entry_id, alias) VALUES (?, ?)", (target, old_title))
            self._touch(target)
            self._reindex_entry(target)
            return self.conn.execute("SELECT title FROM entries WHERE id = ?", (target,)).fetchone()[0], True

    def add_entry_alias(self, entry_id: int, alias: str) -> list[str]:
        """Give an entry another name. Returns its other names."""
        alias = clean_title(alias)
        with self.conn:
            if self.conn.execute("SELECT 1 FROM entries WHERE id = ?", (entry_id,)).fetchone() is None:
                raise EntryInvalid("There's no such entry.")
            other = self.entry_id(alias)
            if other == entry_id:
                raise EntryInvalid(f"\"{alias}\" is already a name for this entry.")
            if other is not None:
                other_title = self.conn.execute("SELECT title FROM entries WHERE id = ?", (other,)).fetchone()[0]
                raise EntryInvalid(
                    f"\"{alias}\" is already the entry \"{other_title}\" or one of its other names. "
                    "To make them one entry, rename one to the other."
                )
            self.conn.execute("INSERT INTO entry_aliases (entry_id, alias) VALUES (?, ?)", (entry_id, alias))
            self._reindex_entry(entry_id)
        return self._entry_aliases(entry_id)

    def remove_entry_alias(self, alias: str) -> bool:
        alias = _norm(alias)
        with self.conn:
            row = self.conn.execute("SELECT entry_id FROM entry_aliases WHERE alias = ?", (alias,)).fetchone()
            if row is None:
                return False
            self.conn.execute("DELETE FROM entry_aliases WHERE alias = ?", (alias,))
            self._reindex_entry(row[0])
        return True

    def set_entry_tags(self, entry_id: int, tags: list[str]) -> list[str]:
        """Replace an entry's tags. Returns them as stored."""
        with self.conn:
            if self.conn.execute("SELECT 1 FROM entries WHERE id = ?", (entry_id,)).fetchone() is None:
                raise EntryInvalid("There's no such entry.")
            self.conn.execute("DELETE FROM entry_tags WHERE entry_id = ?", (entry_id,))
            self._set_entry_tags(entry_id, tags)
            self._drop_unused_tags()
            self._touch(entry_id)
            self._reindex_entry(entry_id)
        return self._entry_tags(entry_id)

    def rename_section(self, entry_id: int, heading: str, new_heading: str) -> str:
        """Rename one of an entry's sub-headings. If the entry already has the new one, the
        text is moved to the end of it. Returns the sub-heading's name now."""
        with self.conn:
            row = self.conn.execute(
                "SELECT id FROM sections WHERE entry_id = ? AND heading = ?", (entry_id, _norm(heading))
            ).fetchone()
            if row is None:
                raise EntryInvalid(f"This entry has no sub-heading \"{heading}\".")
            new_heading = self._heading_for(entry_id, clean_heading(new_heading))
            self._remember_heading(self._entry_topic(entry_id), new_heading)
            section_id = self._rename_section(entry_id, row[0], new_heading)
            self._touch(entry_id)
            self._reindex_entry(entry_id)
            return self.conn.execute("SELECT heading FROM sections WHERE id = ?", (section_id,)).fetchone()[0]

    def _rename_section(self, entry_id: int, section_id: int, new_heading: str) -> int:
        """Give a section a new heading, or if its entry already has that one, move its text
        to the end of it. Returns the section the text is in now."""
        into = self.conn.execute(
            "SELECT id FROM sections WHERE entry_id = ? AND heading = ?", (entry_id, new_heading)
        ).fetchone()
        if into is None or into[0] == section_id:
            self.conn.execute("UPDATE sections SET heading = ? WHERE id = ?", (new_heading, section_id))
            return section_id
        end = self.conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM pieces WHERE section_id = ?", (into[0],)
        ).fetchone()[0]
        self.conn.execute(
            "UPDATE pieces SET section_id = ?, position = position + ? WHERE section_id = ?",
            (into[0], end, section_id),
        )
        self.conn.execute("DELETE FROM sections WHERE id = ?", (section_id,))
        self.conn.execute("DELETE FROM sections_fts WHERE rowid = ?", (section_id,))
        return into[0]

    # --- a topic's list of sub-headings: order, rename and merge, remove ---

    def _topic_heading(self, topic: str, name: str):
        return self.conn.execute(
            "SELECT id, name FROM headings WHERE topic = ? AND name = ?", (topic, _norm(name))).fetchone()

    def reorder_headings(self, topic: str | None, names: list[str]) -> list[str]:
        """Put a topic's list in this order. Any left out keep their order, after these.
        Returns the list."""
        topic = _norm(topic)
        with self.conn:
            current = self.headings_for(topic)
            given: list[str] = []
            for name in names:
                match = next((h for h in current if h.casefold() == _norm(name).casefold()), None)
                if match is not None and match not in given:
                    given.append(match)
            order = given + [h for h in current if h not in given]
            self.conn.executemany("UPDATE headings SET position = ? WHERE topic = ? AND name = ?",
                                  [(i, topic, h) for i, h in enumerate(order)])
        return self.headings_for(topic)

    def rename_heading(self, topic: str | None, name: str, new_name: str) -> tuple[str, bool]:
        """Rename a sub-heading in every entry in a topic. If the new name is already on the
        topic's list, the two are merged: in each entry, text under the old one goes to the
        end of the new one.

        Returns (the sub-heading's name now, whether it was merged)."""
        topic, new_name = _norm(topic), clean_heading(new_name)
        with self.conn:
            row = self._topic_heading(topic, name)
            if row is None:
                raise EntryInvalid(f"There's no sub-heading called \"{name}\".")
            target = self._topic_heading(topic, new_name)
            merged = target is not None and target[0] != row[0]
            final = target[1] if merged else new_name
            affected = self.conn.execute(
                "SELECT s.id, s.entry_id FROM sections s JOIN entries e ON e.id = s.entry_id"
                " WHERE s.heading = ? AND e.topic = ?", (row[1], topic)).fetchall()
            for section_id, entry_id in affected:
                self._rename_section(entry_id, section_id, final)
            if merged:
                self.conn.execute("DELETE FROM headings WHERE id = ?", (row[0],))
            else:
                self.conn.execute("UPDATE headings SET name = ? WHERE id = ?", (final, row[0]))
            for entry_id in {e for _, e in affected}:
                self._touch(entry_id)
                self._reindex_entry(entry_id)
        return final, merged

    def remove_heading(self, topic: str | None, name: str) -> bool:
        """Take a sub-heading that none of a topic's entries use off its list. Returns False
        if it isn't on it."""
        topic = _norm(topic)
        with self.conn:
            row = self._topic_heading(topic, name)
            if row is None:
                return False
            used = self.conn.execute(
                "SELECT COUNT(*) FROM sections s JOIN entries e ON e.id = s.entry_id"
                " WHERE s.heading = ? AND e.topic = ?", (row[1], topic)).fetchone()[0]
            if used:
                raise EntryInvalid(
                    f"{used} entr{'ies use' if used != 1 else 'y uses'} \"{row[1]}\". "
                    "To combine it with another sub-heading, rename it to that one."
                )
            self.conn.execute("DELETE FROM headings WHERE id = ?", (row[0],))
        return True

    def _set_entry_tags(self, entry_id: int, tags) -> None:
        seen = set()
        for name in tags:
            name = clean_entry_tag(name)
            if name.casefold() in seen:
                continue
            seen.add(name.casefold())
            tag_id = self._tag_id(ENTRY_TAG_KIND, name)
            if tag_id is None:
                tag_id = self.conn.execute(
                    "INSERT INTO tags (kind, name) VALUES (?, ?)", (ENTRY_TAG_KIND, name)
                ).lastrowid
            self.conn.execute("INSERT OR IGNORE INTO entry_tags (entry_id, tag_id) VALUES (?, ?)", (entry_id, tag_id))

    def _renumber(self, section_id: int, ids: list[int] | None = None) -> None:
        if ids is None:
            ids = [r[0] for r in self.conn.execute(
                "SELECT id FROM pieces WHERE section_id = ? ORDER BY position, id", (section_id,))]
        self.conn.executemany("UPDATE pieces SET position = ? WHERE id = ?", [(i, p) for i, p in enumerate(ids)])

    def _drop_empty_sections(self) -> None:
        empty = [r[0] for r in self.conn.execute(
            "SELECT id FROM sections WHERE id NOT IN (SELECT section_id FROM pieces)")]
        self.conn.executemany("DELETE FROM sections_fts WHERE rowid = ?", [(s,) for s in empty])
        self.conn.executemany("DELETE FROM sections WHERE id = ?", [(s,) for s in empty])

    def _reindex_entry(self, entry_id: int) -> None:
        row = self.conn.execute("SELECT title FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if row is None:
            return
        title = " ".join([row[0], *self._entry_aliases(entry_id)])
        # Tags with their other names, so searching "Cephalalgia" finds entries tagged Headache.
        tags = " ".join(r[0] for r in self.conn.execute(
            "SELECT t.name FROM entry_tags et JOIN tags t ON t.id = et.tag_id WHERE et.entry_id = ?1"
            " UNION ALL SELECT a.alias FROM entry_tags et JOIN tag_aliases a ON a.tag_id = et.tag_id"
            " WHERE et.entry_id = ?1", (entry_id,)))
        self.conn.execute("DELETE FROM entries_fts WHERE rowid = ?", (entry_id,))
        self.conn.execute("INSERT INTO entries_fts (rowid, title, tags) VALUES (?, ?, ?)", (entry_id, title, tags))
        for section_id, heading in self.conn.execute(
                "SELECT id, heading FROM sections WHERE entry_id = ?", (entry_id,)).fetchall():
            body = "\n\n".join(r[0] for r in self.conn.execute(
                "SELECT body FROM pieces WHERE section_id = ? ORDER BY position, id", (section_id,)))
            self.conn.execute("DELETE FROM sections_fts WHERE rowid = ?", (section_id,))
            self.conn.execute(
                "INSERT INTO sections_fts (rowid, title, tags, heading, body) VALUES (?, ?, ?, ?, ?)",
                (section_id, title, tags, heading, body),
            )

    def _reindex_entries_with_tag(self, tag_id: int) -> None:
        for (entry_id,) in self.conn.execute(
                "SELECT entry_id FROM entry_tags WHERE tag_id = ?", (tag_id,)).fetchall():
            self._reindex_entry(entry_id)

    # --- reading ---

    def _entry_aliases(self, entry_id: int) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT alias FROM entry_aliases WHERE entry_id = ? ORDER BY alias COLLATE NOCASE", (entry_id,))]

    def _entry_tags(self, entry_id: int) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT t.name FROM entry_tags et JOIN tags t ON t.id = et.tag_id WHERE et.entry_id = ?"
            " ORDER BY t.name COLLATE NOCASE", (entry_id,))]

    def _piece(self, row: sqlite3.Row) -> Piece:
        return Piece(
            **{c: row[c] for c in _PIECE_COLUMNS}, id=row["id"], entry_id=row["entry_id"],
            entry_title=row["entry_title"], heading=row["heading"], position=row["position"],
            source_missing=bool(row["source_missing"]), created=row["created"], updated=row["updated"],
        )

    _PIECE_SELECT = (
        "SELECT p.*, s.heading, s.entry_id, e.title AS entry_title FROM pieces p"
        " JOIN sections s ON s.id = p.section_id JOIN entries e ON e.id = s.entry_id"
    )

    def get_piece(self, piece_id: int) -> Piece | None:
        row = self.conn.execute(f"{self._PIECE_SELECT} WHERE p.id = ?", (piece_id,)).fetchone()
        return self._piece(row) if row else None

    def _piece_entry(self, piece_id: int) -> int | None:
        row = self.conn.execute(
            "SELECT s.entry_id FROM pieces p JOIN sections s ON s.id = p.section_id WHERE p.id = ?", (piece_id,)
        ).fetchone()
        return row[0] if row else None

    def get_entry(self, entry_id: int) -> Entry | None:
        row = self.conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if row is None:
            return None
        pieces: dict[int, list[Piece]] = {}
        for p in self.conn.execute(f"{self._PIECE_SELECT} WHERE s.entry_id = ? ORDER BY p.position, p.id",
                                   (entry_id,)):
            pieces.setdefault(p["section_id"], []).append(self._piece(p))
        sections = [
            Section(s["id"], s["heading"], pieces.get(s["id"], []))
            for s in self.conn.execute("SELECT id, heading FROM sections WHERE entry_id = ?", (entry_id,))
        ]
        topic = row["topic"] if "topic" in row.keys() else ""
        order = self._order_key(topic)
        sections.sort(key=lambda s: order(s.heading, s.id))
        return Entry(row["id"], row["title"], self._entry_aliases(entry_id), self._entry_tags(entry_id),
                     sections, row["created"], row["updated"], topic)

    def _order_keys(self, topics: dict[int, str]):
        """A sort key per entry id, from each entry's topic."""
        keys = {t.casefold(): self._order_key(t) for t in set(topics.values())}
        return {entry_id: keys[t.casefold()] for entry_id, t in topics.items()}

    def _topics(self, ids) -> dict[int, str]:
        ids = list(ids)
        if not ids:
            return {}
        return dict(self.conn.execute(
            f"SELECT id, topic FROM entries WHERE id IN ({', '.join('?' * len(ids))})", ids).fetchall())

    def _summaries(self, ids: list[int]) -> dict[int, EntrySummary]:
        if not ids:
            return {}
        marks = ", ".join("?" * len(ids))
        heads: dict[int, list[tuple]] = {}
        counts: dict[int, int] = {}
        topics = self._topics(ids)
        orders = self._order_keys(topics)
        for r in self.conn.execute(
                f"SELECT s.id, s.entry_id, s.heading, COUNT(p.id) AS n FROM sections s"
                f" JOIN pieces p ON p.section_id = s.id WHERE s.entry_id IN ({marks}) GROUP BY s.id", ids):
            order = orders[r["entry_id"]]
            heads.setdefault(r["entry_id"], []).append((order(r["heading"], r["id"]), r["heading"]))
            counts[r["entry_id"]] = counts.get(r["entry_id"], 0) + r["n"]
        return {
            r["id"]: EntrySummary(
                r["id"], r["title"], self._entry_aliases(r["id"]), self._entry_tags(r["id"]),
                [h for _, h in sorted(heads.get(r["id"], []))], counts.get(r["id"], 0), r["updated"],
                topics.get(r["id"], ""),
            )
            for r in self.conn.execute(f"SELECT id, title, updated FROM entries WHERE id IN ({marks})", ids)
        }

    def entry_summary(self, entry_id: int) -> EntrySummary | None:
        return self._summaries([entry_id]).get(entry_id)

    def entry_list(self, *, tag: str | None = None, topic: str | None = None) -> list[EntrySummary]:
        """Every entry A to Z, with its sub-headings, for the KB's left column. With a tag
        (or one of its other names), only entries with it; with a topic, only entries with
        text from that topic's books."""
        sql, params = "SELECT e.id FROM entries e WHERE 1", []
        if tag is not None:
            tag_id = self._tag_id(ENTRY_TAG_KIND, _norm(tag))
            sql += " AND e.id IN (SELECT entry_id FROM entry_tags WHERE tag_id = ?)"
            params.append(tag_id)
        if topic:
            sql += (" AND EXISTS (SELECT 1 FROM sections s JOIN pieces p ON p.section_id = s.id"
                    " WHERE s.entry_id = e.id AND p.topic = ? COLLATE NOCASE)")
            params.append(topic)
        ids = [r[0] for r in self.conn.execute(sql + " ORDER BY e.title COLLATE NOCASE", params)]
        found = self._summaries(ids)
        return [found[i] for i in ids]

    def entry_titles(self, prefix: str = "", limit: int = 10) -> list[str]:
        """Titles starting with the prefix, or with another name that does, for autocomplete."""
        like = _like_prefix(prefix)
        return [r[0] for r in self.conn.execute(
            "SELECT title FROM entries e WHERE title LIKE ?1 ESCAPE '\\' OR EXISTS (SELECT 1 FROM entry_aliases a"
            " WHERE a.entry_id = e.id AND a.alias LIKE ?1 ESCAPE '\\') ORDER BY title COLLATE NOCASE LIMIT ?2",
            (like, limit))]

    def entry_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

    def all_entries(self) -> list[Entry]:
        """Every entry, A to Z, with all its text (for exporting)."""
        ids = [r[0] for r in self.conn.execute("SELECT id FROM entries ORDER BY title COLLATE NOCASE")]
        return [self.get_entry(i) for i in ids]

    def pieces_for_page(self, path: str, seq: int) -> list[Piece]:
        """Text saved from one page or chapter, oldest first."""
        rows = self.conn.execute(
            f"{self._PIECE_SELECT} WHERE p.path = ? AND p.seq = ? ORDER BY p.created, p.id", (path, seq))
        return [self._piece(r) for r in rows]

    def entry_tag_counts(self, prefix: str = "", limit: int | None = None) -> list[tuple[str, int, list[str]]]:
        """Entry tags in use, A to Z, with how many entries have each and their other names.
        prefix is for autocomplete: it matches the start of a tag's name or of another name."""
        sql = ("SELECT t.id, t.name, COUNT(*) FROM tags t JOIN entry_tags et ON et.tag_id = t.id"
               " WHERE t.kind = ?")
        params: list = [ENTRY_TAG_KIND]
        if prefix.strip():
            sql += (" AND (t.name LIKE ?2 ESCAPE '\\' OR EXISTS (SELECT 1 FROM tag_aliases a"
                    " WHERE a.tag_id = t.id AND a.alias LIKE ?2 ESCAPE '\\'))")
            params.append(_like_prefix(prefix))
        sql += " GROUP BY t.id ORDER BY t.name COLLATE NOCASE"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [(name, count, self.aliases_of(tag_id))
                for tag_id, name, count in self.conn.execute(sql, params).fetchall()]

    def search_entries(self, query: str, *, topic: str | None = None, limit: int = 50, offset: int = 0,
                       mark: tuple[str, str] = ("[", "]")) -> tuple[list[EntryHit], int]:
        """Entries matching the search, entries with the words in their title first, then
        best matches. Each lists the sub-headings the words were found in, with a snippet.
        Returns (this page of hits, how many entries matched in all)."""
        fts = prepare_query(query)
        try:
            entry_scores = dict(self.conn.execute(
                f"SELECT rowid, bm25(entries_fts, {_ENTRY_WEIGHTS}) FROM entries_fts WHERE entries_fts MATCH ?",
                (fts,)).fetchall())
            rows = self.conn.execute(
                f"SELECT s.id, s.entry_id, s.heading, bm25(sections_fts, {_SECTION_WEIGHTS}) AS score,"
                " highlight(sections_fts, 2, ?1, ?2) AS heading_hit,"
                " snippet(sections_fts, 3, ?1, ?2, '…', 24) AS snippet"
                " FROM sections_fts JOIN sections s ON s.id = sections_fts.rowid WHERE sections_fts MATCH ?3",
                (mark[0], mark[1], fts)).fetchall()
        except sqlite3.OperationalError as e:
            raise QueryError(f"Can't understand that search ({e}). Check quotes, brackets and AND/OR/NOT.") from None

        try:
            title_ids = {r[0] for r in self.conn.execute(
                "SELECT rowid FROM entries_fts WHERE entries_fts MATCH ?", (f"title : ({fts})",))}
        except sqlite3.OperationalError:
            title_ids = set()  # a search that can't be limited to titles: rank it all by score

        scores = dict(entry_scores)
        found: dict[int, list[tuple]] = {}
        orders = self._order_keys(self._topics({r["entry_id"] for r in rows}))
        for r in rows:
            scores[r["entry_id"]] = min(scores.get(r["entry_id"], 0.0), r["score"])
            # A sub-heading counts as found in when the words are in its name or its text, not
            # just because every row carries the entry's title.
            if mark[0] in r["heading_hit"] or mark[0] in r["snippet"]:
                found.setdefault(r["entry_id"], []).append(
                    (orders[r["entry_id"]](r["heading"], r["id"]), SectionHit(r["heading"], r["snippet"])))
        if topic:
            allowed = {r[0] for r in self.conn.execute(
                "SELECT DISTINCT s.entry_id FROM sections s JOIN pieces p ON p.section_id = s.id"
                " WHERE p.topic = ? COLLATE NOCASE", (topic,))}
            scores = {k: v for k, v in scores.items() if k in allowed}
        ranked = sorted(scores, key=lambda i: (i not in title_ids, scores[i], i))
        page = ranked[offset:offset + limit]
        summaries = self._summaries(page)
        hits = [
            EntryHit(summaries[i], [h for _, h in sorted(found.get(i, []), key=lambda x: x[0])],
                     i in title_ids, scores[i])
            for i in page
        ]
        return hits, len(ranked)

    # --- keeping book pieces attached to their books (called with the note versions) ---

    def _move_book_pieces(self, old_path: str, new_path: str, topic: str, book_title: str) -> int:
        return self.conn.execute(
            "UPDATE pieces SET path = ?, topic = ?, book_title = ?, source_missing = 0"
            " WHERE source_type = 'book' AND path = ?",
            (new_path, topic, book_title, old_path),
        ).rowcount

    # --- converting old notes (notes versions 1 and 2) ---

    def _convert_notes(self) -> int:
        """Turn notes into pieces, each under an entry named after the note's title (or its
        first words), in a Notes sub-heading, with its quote and text joined. Notes already
        converted are skipped. Returns how many were converted."""
        rows = self.conn.execute(
            "SELECT * FROM notes WHERE id NOT IN (SELECT from_note FROM pieces WHERE from_note IS NOT NULL)"
            " ORDER BY id").fetchall()
        for n in rows:
            title = _norm(n["title"])
            if not title:
                words = (n["quote"] or n["body"]).split()
                title = " ".join(words[:_TITLE_WORDS]) + ("…" if len(words) > _TITLE_WORDS else "")
            title = title[:ENTRY_TITLE_MAX].strip() or "Untitled"
            entry_id = self.entry_id(title)
            if entry_id is None:
                now = n["created"]
                entry_id = self.conn.execute(
                    "INSERT INTO entries (title, created, updated) VALUES (?, ?, ?)", (title, now, n["updated"])
                ).lastrowid
            elif n["updated"] > self.conn.execute("SELECT updated FROM entries WHERE id = ?",
                                                  (entry_id,)).fetchone()[0]:
                self.conn.execute("UPDATE entries SET updated = ? WHERE id = ?", (n["updated"], entry_id))
            body = "\n\n".join(t for t in ((n["quote"] or "").strip(), (n["body"] or "").strip()) if t)
            piece = Piece(
                body=body or title, source_type=n["source_type"], topic=n["topic"], path=n["path"], seq=n["seq"],
                label=n["label"], book_title=n["book_title"], href=n["href"], source_ref=n["source_ref"],
                source_url=n["source_url"], source_missing=bool(n["source_missing"]),
            )
            self._insert_piece(self._section_id(entry_id, CONVERTED_HEADING), piece, from_note=n["id"],
                               created=n["created"], updated=n["updated"])
            for tag_id, name in self.conn.execute(
                    "SELECT t.id, t.name FROM note_tags nt JOIN tags t ON t.id = nt.tag_id WHERE nt.note_id = ?",
                    (n["id"],)).fetchall():
                self._convert_tag(entry_id, tag_id, name)
            self._reindex_entry(entry_id)
        return len(rows)

    def _convert_tag(self, entry_id: int, old_id: int, name: str) -> None:
        """Give the entry the plain tag for an old kinded tag, bringing its other names."""
        tag_id = self._tag_id(ENTRY_TAG_KIND, name)
        if tag_id is None:
            tag_id = self.conn.execute(
                "INSERT INTO tags (kind, name) VALUES (?, ?)", (ENTRY_TAG_KIND, name)).lastrowid
        for (alias,) in self.conn.execute("SELECT alias FROM tag_aliases WHERE tag_id = ?", (old_id,)).fetchall():
            if self._tag_id(ENTRY_TAG_KIND, alias) is None:
                self.conn.execute(
                    "INSERT INTO tag_aliases (tag_id, kind, alias) VALUES (?, ?, ?)", (tag_id, ENTRY_TAG_KIND, alias))
        self.conn.execute("INSERT OR IGNORE INTO entry_tags (entry_id, tag_id) VALUES (?, ?)", (entry_id, tag_id))


def _like_prefix(prefix: str) -> str:
    return re.sub(r"([\\%_])", r"\\\1", _norm(prefix)) + "%"


def migrate_to_heading_list(store) -> None:
    """Notes version 4: the master list of sub-headings, starting with the config's list,
    then any other sub-headings entries already have."""
    store.conn.execute(HEADINGS_SCHEMA)
    others = [r[0] for r in store.conn.execute("SELECT heading FROM sections GROUP BY heading ORDER BY MIN(id)")]
    for heading in [*store.seed_headings, *others]:
        store.conn.execute(
            "INSERT OR IGNORE INTO headings (name, position) SELECT ?, COALESCE(MAX(position), -1) + 1 FROM headings",
            (heading,))


def migrate_to_topic_headings(store) -> None:
    """Notes version 6: a topic on each entry, and a list of sub-headings per topic."""
    conn = store.conn
    conn.execute("ALTER TABLE entries ADD COLUMN topic TEXT NOT NULL DEFAULT '' COLLATE NOCASE")
    old = [r[0] for r in conn.execute("SELECT name FROM headings ORDER BY position, id")]
    conn.execute("DROP TABLE headings")
    conn.execute(TOPIC_HEADINGS_SCHEMA)
    rebuild_heading_lists(store, old, set_topics=True)


def rebuild_heading_lists(store, old: list[str], *, set_topics: bool) -> None:
    """Make the lists of sub-headings from one old list (from before version 6, or a version 2
    backup). With set_topics, each entry first gets the topic most of its text came from.

    The old list goes to the topic most entries are in (or FIRST_TOPIC if none have a
    topic); every other topic gets the sub-headings its entries use, in the old list's order."""
    conn = store.conn
    if set_topics:
        for (entry_id,) in conn.execute("SELECT id FROM entries").fetchall():
            row = conn.execute(
                "SELECT p.topic FROM pieces p JOIN sections s ON s.id = p.section_id"
                " WHERE s.entry_id = ? AND COALESCE(p.topic, '') != ''"
                " GROUP BY p.topic COLLATE NOCASE ORDER BY COUNT(*) DESC, MIN(p.id) LIMIT 1", (entry_id,)).fetchone()
            conn.execute("UPDATE entries SET topic = ? WHERE id = ?", (row[0] if row else "", entry_id))
    conn.execute("DELETE FROM headings")
    topics = [r[0] for r in conn.execute(
        "SELECT topic FROM entries WHERE topic != '' GROUP BY topic ORDER BY COUNT(*) DESC, topic COLLATE NOCASE")]
    first = topics[0] if topics else FIRST_TOPIC
    for heading in old:
        try:
            store._remember_heading(first, clean_heading(heading))
        except (EntryInvalid, TypeError):
            continue
    rank = {h.casefold(): i for i, h in enumerate(old) if isinstance(h, str)}
    for (topic,) in conn.execute("SELECT DISTINCT topic FROM entries").fetchall():
        used = conn.execute(
            "SELECT s.heading, MIN(s.id) FROM sections s JOIN entries e ON e.id = s.entry_id"
            " WHERE e.topic = ? GROUP BY s.heading", (topic,)).fetchall()
        for heading, _ in sorted(used, key=lambda r: (rank.get(r[0].casefold(), len(rank)), r[1])):
            store._remember_heading(topic, heading)


def migrate_to_entries(store) -> None:
    """Notes version 3: add entries, and convert the notes there are into them."""
    for statement in ENTRIES_SCHEMA:
        store.conn.execute(statement)
    store._convert_notes()
