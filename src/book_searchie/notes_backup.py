"""Exporting the KB (JSON for a full backup, Markdown for reading) and restoring a JSON backup.

Version 3 of the JSON backup has the KB's entries (with their topic, sub-headings, text,
tags and other names), each topic's list of sub-headings, and research records, as well as
the older notes. Older backups can still be restored: in a version 2 backup there's one
list of sub-headings and entries have no topic, and a version 1 backup has notes only,
which are turned into entries. Both are sorted into topics as when the notes file was
upgraded.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .entries import (ENTRY_TAG_KIND, Entry, EntryInvalid, Piece, clean_entry_tag, clean_heading, clean_title,
                      rebuild_heading_lists, validate_piece)
from .notes import SOURCE_TYPES, Note, NoteInvalid, NotesStore, Tag, clean_tag_name, validate

FORMAT = "book-searchie-notes"
VERSION = 3


class RestoreError(ValueError):
    """The file isn't a backup that can be restored."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- JSON ------------------------------------------------------------------------


def _note_dict(note: Note) -> dict:
    data = {
        "id": note.id, "source_type": note.source_type, "title": note.title, "quote": note.quote,
        "body": note.body, "topic": note.topic, "tags": [{"kind": t.kind, "name": t.name} for t in note.tags],
        "created": note.created, "updated": note.updated,
    }
    if note.source_type == "book":
        data["source"] = {
            "path": note.path, "seq": note.seq, "label": note.label, "book_title": note.book_title,
            "href": note.href, "missing": note.source_missing,
        }
    elif note.source_type == "other":
        data["source"] = {"ref": note.source_ref, "url": note.source_url}
    return data


def _aliases(store: NotesStore) -> list[dict]:
    rows = store.conn.execute(
        "SELECT a.kind, t.name, a.alias FROM tag_aliases a JOIN tags t ON t.id = a.tag_id ORDER BY a.kind, t.name, a.alias"
    )
    return [{"kind": r["kind"], "tag": r["name"], "alias": r["alias"]} for r in rows]


def _source_dict(p) -> dict | None:
    if p.source_type == "book":
        return {"path": p.path, "seq": p.seq, "label": p.label, "book_title": p.book_title,
                "href": p.href, "missing": p.source_missing}
    if p.source_type == "other":
        return {"ref": p.source_ref, "url": p.source_url}
    return None


def _entry_dict(entry: Entry) -> dict:
    return {
        "id": entry.id, "title": entry.title, "topic": entry.topic, "aliases": entry.aliases, "tags": entry.tags,
        "created": entry.created, "updated": entry.updated,
        "sections": [{
            "heading": s.heading,
            "pieces": [{
                "id": p.id, "body": p.body, "source_type": p.source_type, "topic": p.topic,
                **({"source": _source_dict(p)} if _source_dict(p) else {}),
                "created": p.created, "updated": p.updated,
            } for p in s.pieces],
        } for s in entry.sections],
    }


def export_json(store: NotesStore) -> dict:
    """Everything in the KB: entries with their topic, text, sources, tags and other names;
    each topic's list of sub-headings; research records; the older notes; and other names
    for tags."""
    return {
        "format": FORMAT,
        "version": VERSION,
        "exported": _now(),
        "headings": [{"topic": t, "names": store.headings_for(t)} for t in store.heading_topics()],
        "entries": [_entry_dict(e) for e in store.all_entries()],
        "research": [{"term": r.term, "path": r.path, "book_title": r.book_title, "topic": r.topic,
                      "done": r.done, "missing": r.source_missing} for r in store.research_records()],
        "notes": [_note_dict(n) for n in store.all_notes()],
        "aliases": _aliases(store),
    }


def export_json_text(store: NotesStore) -> str:
    return json.dumps(export_json(store), ensure_ascii=False, indent=2) + "\n"


# --- Markdown --------------------------------------------------------------------


def _file_name(name: str) -> str:
    """A tag or entry name made safe as a file name on Windows, macOS and Linux."""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return safe or "_"


BOOK_TEXT = ("mark", "omit")  # what an entry's Markdown does with text saved from a book


def _piece_source_line(p: Piece) -> str:
    if p.source_type == "book":
        where = p.label or ""
        where = where if p.href or not where else f"p. {where}"
        book = ", ".join(part for part in (p.book_title or "a book", where) if part)
        return f"*From {book}: book text, to rewrite in your own words before publishing.*"
    if p.source_type == "other" and (p.source_ref or p.source_url):
        if p.source_url:
            return f"*Source: [{p.source_ref or p.source_url}]({p.source_url})*"
        return f"*Source: {p.source_ref}*"
    return ""


def entry_markdown(entry: Entry, book_text: str = "mark") -> str:
    """An entry as Markdown: its title, sub-headings as headings, then its text. Text saved
    from a book is marked as such ("mark") or left out ("omit"), so what's left is ready to
    publish once the marked text is rewritten."""
    if book_text not in BOOK_TEXT:
        raise ValueError(f"book_text must be one of {', '.join(BOOK_TEXT)}")
    parts = [f"# {entry.title}", ""]
    if entry.aliases:
        parts += [f"Also called: {', '.join(entry.aliases)}", ""]
    if entry.tags:
        parts += [f"Tags: {', '.join(entry.tags)}", ""]
    left_out = 0
    for section in entry.sections:
        pieces = [p for p in section.pieces if not (book_text == "omit" and p.source_type == "book")]
        left_out += len(section.pieces) - len(pieces)
        if not pieces:
            continue
        parts += [f"## {section.heading}", ""]
        for p in pieces:
            line = _piece_source_line(p)
            if line:
                parts += [line, ""]
            parts += [p.body.strip(), ""]
    if left_out:
        parts += [f"*{left_out} piece{'s' if left_out != 1 else ''} of book text left out.*", ""]
    return "\n".join(parts).rstrip() + "\n"


def export_markdown_per_entry(store: NotesStore, book_text: str = "mark") -> dict[str, str]:
    """One Markdown file per entry ("Nettle.md"), by file name."""
    files: dict[str, str] = {}
    taken: set[str] = set()
    for entry in store.all_entries():
        base = _file_name(entry.title)
        name, n = f"{base}.md", 2
        while name.casefold() in taken:  # two titles made the same by the file-name rules
            name, n = f"{base} ({n}).md", n + 1
        taken.add(name.casefold())
        files[name] = entry_markdown(entry, book_text)
    return files


# --- Restoring a JSON backup ------------------------------------------------------


def _to_note(data: dict) -> Note:
    source = data.get("source") or {}
    note = Note(
        source_type=data.get("source_type", ""), topic=data.get("topic", ""), title=data.get("title", ""),
        quote=data.get("quote", ""), body=data.get("body", ""),
        tags=[Tag(t["kind"], t["name"]) for t in data.get("tags", [])],
    )
    if note.source_type == "book":
        note.path, note.seq, note.label = source.get("path"), source.get("seq"), source.get("label")
        note.book_title, note.href = source.get("book_title"), source.get("href")
        note.source_missing = bool(source.get("missing"))
    elif note.source_type == "other":
        note.source_ref, note.source_url = source.get("ref"), source.get("url")
    return note


def restore(store: NotesStore, data: dict, *, replace: bool = False, notes_path: Path | None = None) -> int:
    """Restore a JSON backup into the KB, all or nothing. Returns how many notes were restored.

    Only into an empty KB, unless replace is True, when everything in the KB is replaced;
    a copy of it is saved first, next to notes_path, if given. Notes keep their ids and dates.
    """
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise RestoreError("This isn't a Book-Searchie notes backup (made with 'book-searchie notes export').")
    if not isinstance(data.get("version"), int) or data["version"] > VERSION:
        raise RestoreError("This backup was made by a newer version of Book-Searchie. Update it to restore this.")
    has = _kb_size(store)
    if has and not replace:
        raise RestoreError(
            f"Your KB already has {has}. Restoring would replace it all; "
            "to do that, use --replace (a copy of the current KB is saved first)."
        )

    notes = []
    for i, item in enumerate(data.get("notes", []), 1):
        try:
            note = validate(_to_note(item))
            if note.source_type not in SOURCE_TYPES:
                raise NoteInvalid("unknown source type")
            note.id = int(item["id"]) if item.get("id") is not None else None
            note.created, note.updated = item.get("created") or _now(), item.get("updated") or _now()
            note.source_missing = bool((item.get("source") or {}).get("missing"))
        except (NoteInvalid, KeyError, TypeError, ValueError) as e:
            raise RestoreError(f"Note {i} in the backup can't be restored: {e}") from None
        notes.append(note)
    entries = _entries_from(data) if data["version"] >= 2 else None

    if has and notes_path is not None:
        copy = sqlite3.connect(notes_path.with_name(f"{notes_path.name}.before-restore-{datetime.now():%Y%m%d-%H%M%S}.bak"))
        try:
            store.conn.backup(copy)
        finally:
            copy.close()

    try:
        _replace_all(store, notes, data.get("aliases", []), entries, data)
    except sqlite3.IntegrityError as e:
        raise RestoreError(f"The backup can't be restored as it is ({e}). Nothing was changed.") from None
    return store.entry_count()


def _restore_heading_lists(store: NotesStore, data: dict, entries: list[dict]) -> None:
    """Each topic's list of sub-headings from a version 3 backup, or from the one list in a
    version 2 backup. Every sub-heading an entry uses ends up on its topic's list."""
    headings = data.get("headings")
    if data["version"] < 3:
        rebuild_heading_lists(store, headings if isinstance(headings, list) else [], set_topics=True)
        return
    for group in headings if isinstance(headings, list) else []:
        if not isinstance(group, dict) or not isinstance(group.get("names"), list):
            continue
        topic = " ".join(str(group.get("topic") or "").split())
        for name in group["names"]:
            try:
                store._remember_heading(topic, clean_heading(name))
            except (EntryInvalid, TypeError):
                continue
    for e in entries:
        for section in e["sections"]:
            store._remember_heading(e["topic"], section["heading"])


def _kb_size(store: NotesStore) -> str:
    """What's in the KB, for "Your KB already has ...", or "" if it's empty."""
    entries, notes = store.entry_count(), store.count()
    if entries:
        return f"{entries} entr{'ies' if entries != 1 else 'y'}"
    return f"{notes} note{'s' if notes != 1 else ''}" if notes else ""


def _entries_from(data: dict) -> list[dict]:
    """The entries in a version 2 or 3 backup, checked, or raise RestoreError."""
    entries = []
    for i, item in enumerate(data.get("entries", []), 1):
        try:
            entry = {
                "id": int(item["id"]) if item.get("id") is not None else None,
                "title": clean_title(item["title"]),
                "topic": " ".join(str(item.get("topic") or "").split()),
                "aliases": [clean_title(a) for a in item.get("aliases", [])],
                "tags": [clean_entry_tag(t) for t in item.get("tags", [])],
                "created": item.get("created") or _now(), "updated": item.get("updated") or _now(),
                "sections": [],
            }
            for section in item.get("sections", []):
                pieces = []
                for p in section.get("pieces", []):
                    source = p.get("source") or {}
                    piece = validate_piece(Piece(
                        body=p.get("body", ""), path=source.get("path"), seq=source.get("seq"),
                        label=source.get("label"), book_title=source.get("book_title"), href=source.get("href"),
                        topic=p.get("topic"), source_ref=source.get("ref"), source_url=source.get("url"),
                    ))
                    if p.get("source_type") in SOURCE_TYPES and p["source_type"] != piece.source_type:
                        raise EntryInvalid(f"its source doesn't match its type ({p['source_type']})")
                    piece.id = int(p["id"]) if p.get("id") is not None else None
                    piece.source_missing = bool(source.get("missing"))
                    piece.created, piece.updated = p.get("created") or _now(), p.get("updated") or _now()
                    pieces.append(piece)
                entry["sections"].append({"heading": clean_heading(section["heading"]), "pieces": pieces})
        except (EntryInvalid, KeyError, TypeError, ValueError) as e:
            raise RestoreError(f"Entry {i} in the backup can't be restored: {e}") from None
        entries.append(entry)
    return entries


def _replace_all(store: NotesStore, notes: list[Note], aliases: list, entries: list[dict] | None, data: dict) -> None:
    conn = store.conn
    with conn:
        for table in ("note_tags", "entry_tags", "tag_aliases", "tags", "notes", "notes_fts", "pieces", "sections",
                      "entry_aliases", "entries", "entries_fts", "sections_fts", "research", "headings"):
            conn.execute(f"DELETE FROM {table}")
        for note in notes:
            cur = conn.execute(
                "INSERT INTO notes (id, source_type, title, quote, body, topic, path, seq, label, book_title, href,"
                " source_ref, source_url, source_missing, created, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (note.id, note.source_type, note.title, note.quote, note.body, note.topic, note.path, note.seq,
                 note.label, note.book_title, note.href, note.source_ref, note.source_url, int(note.source_missing),
                 note.created, note.updated),
            )
            note.id = cur.lastrowid
            store._set_tags(note.id, note.tags)
        for a in aliases:
            try:
                kind, tag, alias = str(a["kind"]).lower(), clean_tag_name(a["tag"]), clean_tag_name(a["alias"])
            except (KeyError, NoteInvalid):
                continue
            tag_id = store._tag_id(kind, tag, aliases=False)
            if tag_id is None:
                tag_id = conn.execute("INSERT INTO tags (kind, name) VALUES (?, ?)", (kind, tag)).lastrowid
            if store._tag_id(kind, alias) is None:
                conn.execute("INSERT INTO tag_aliases (tag_id, kind, alias) VALUES (?, ?, ?)", (tag_id, kind, alias))
        for note in notes:
            store._reindex(note.id)

        if entries is None:  # a version 1 backup: its notes become entries
            store._convert_notes()
            rebuild_heading_lists(store, store.seed_headings, set_topics=True)
            return
        for e in entries:
            entry_id = conn.execute(
                "INSERT INTO entries (id, title, topic, created, updated) VALUES (?, ?, ?, ?, ?)",
                (e["id"], e["title"], e["topic"], e["created"], e["updated"])).lastrowid
            for alias in e["aliases"]:
                conn.execute("INSERT INTO entry_aliases (entry_id, alias) VALUES (?, ?)", (entry_id, alias))
            store._set_entry_tags(entry_id, e["tags"])
            for section in e["sections"]:
                section_id = conn.execute("INSERT INTO sections (entry_id, heading) VALUES (?, ?)",
                                          (entry_id, section["heading"])).lastrowid
                for position, p in enumerate(section["pieces"]):
                    conn.execute(
                        "INSERT INTO pieces (id, section_id, position, body, source_type, topic, path, seq, label,"
                        " book_title, href, source_ref, source_url, source_missing, created, updated)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (p.id, section_id, position, p.body, p.source_type, p.topic, p.path, p.seq, p.label,
                         p.book_title, p.href, p.source_ref, p.source_url, int(p.source_missing), p.created, p.updated))
        _restore_heading_lists(store, data, entries)
        for e in entries:  # after all aliases, so tags' other names are in place
            store._reindex_entry(conn.execute("SELECT id FROM entries WHERE title = ?", (e["title"],)).fetchone()[0])
        for r in data.get("research", []):
            if not isinstance(r, dict) or not r.get("term") or not r.get("path"):
                continue
            conn.execute(
                "INSERT OR IGNORE INTO research (term, path, book_title, topic, done, source_missing)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (" ".join(str(r["term"]).split()).casefold(), str(r["path"]), str(r.get("book_title") or ""),
                 str(r.get("topic") or ""), str(r.get("done") or _now()), int(bool(r.get("missing")))))
