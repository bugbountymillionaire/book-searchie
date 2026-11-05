"""Exporting the KB (JSON for a full backup, Markdown for reading) and restoring a JSON backup."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .notes import SOURCE_TYPES, Note, NoteInvalid, NotesStore, Tag, clean_tag_name, validate

FORMAT = "book-searchie-notes"
VERSION = 1
BADGES = {"book": "Book", "other": "Other source", "own": "Own knowledge"}


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


def export_json(store: NotesStore) -> dict:
    """Everything in the KB: notes with their sources and tags, and tag aliases."""
    return {
        "format": FORMAT,
        "version": VERSION,
        "exported": _now(),
        "notes": [_note_dict(n) for n in store.all_notes()],
        "aliases": _aliases(store),
    }


def export_json_text(store: NotesStore) -> str:
    return json.dumps(export_json(store), ensure_ascii=False, indent=2) + "\n"


# --- Markdown --------------------------------------------------------------------


def _source_line(note: Note) -> str:
    if note.source_type == "book":
        where = note.label or ""
        where = where if note.href else f"p. {where}"
        line = f"**Book:** {note.book_title or 'Unknown book'}, {where}"
        return line + (" (source missing)" if note.source_missing else "")
    if note.source_type == "other":
        ref = note.source_ref or ""
        return f"**Other source:** [{ref}]({note.source_url})" if note.source_url else f"**Other source:** {ref}"
    return "**Own knowledge**"


def _note_markdown(note: Note) -> str:
    parts = [f"## {note.display_title}", "", f"{_source_line(note)} · {note.topic}", ""]
    if note.quote:
        parts += ["\n".join("> " + line if line else ">" for line in note.quote.splitlines()), ""]
    if note.body:
        parts += [note.body, ""]
    if note.tags:
        by_kind: dict[str, list[str]] = defaultdict(list)
        for t in note.tags:
            by_kind[t.kind].append(t.name)
        parts += ["Tags: " + "; ".join(f"{k}: {', '.join(names)}" for k, names in by_kind.items()), ""]
    parts += [f"*Saved {note.created[:10]}" + (f", changed {note.updated[:10]}*" if note.updated[:10] != note.created[:10] else "*"), ""]
    return "\n".join(parts)


def export_markdown(store: NotesStore, notes: list[Note] | None = None, heading: str = "KB notes") -> str:
    """Notes as one readable Markdown document, newest first."""
    notes = sorted(store.all_notes() if notes is None else notes, key=lambda n: (n.updated, n.id), reverse=True)
    head = [f"# {heading}", "", f"{len(notes)} note{'s' if len(notes) != 1 else ''}, exported from Book-Searchie on {_now()[:10]}.", ""]
    return "\n".join(head) + "\n---\n\n".join(_note_markdown(n) for n in notes)


def _file_name(name: str) -> str:
    """A tag name made safe as a file name on Windows, macOS and Linux."""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return safe or "_"


def export_markdown_per_tag(store: NotesStore) -> dict[str, str]:
    """One Markdown file per tag ("plant/Nettle.md"), plus "untagged.md" for notes with no tags."""
    files: dict[str, str] = {}
    notes = store.all_notes()
    for tag in store.tags(by_name=True):
        tagged = [n for n in notes if Tag(tag.kind, tag.name) in n.tags]
        heading = f"{tag.name} ({tag.kind})"
        if tag.aliases:
            heading += f", also called {', '.join(tag.aliases)}"
        files[f"{_file_name(tag.kind)}/{_file_name(tag.name)}.md"] = export_markdown(store, tagged, heading)
    untagged = [n for n in notes if not n.tags]
    if untagged:
        files["untagged.md"] = export_markdown(store, untagged, "Notes without tags")
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
    if store.count() and not replace:
        raise RestoreError(
            f"Your KB already has {store.count()} notes. Restoring would replace them all; "
            "to do that, use --replace (a copy of the current notes is saved first)."
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

    if store.count() and notes_path is not None:
        copy = sqlite3.connect(notes_path.with_name(f"{notes_path.name}.before-restore-{datetime.now():%Y%m%d-%H%M%S}.bak"))
        try:
            store.conn.backup(copy)
        finally:
            copy.close()

    try:
        _replace_all(store, notes, data.get("aliases", []))
    except sqlite3.IntegrityError as e:
        raise RestoreError(f"The backup can't be restored as it is ({e}). Nothing was changed.") from None
    return len(notes)


def _replace_all(store: NotesStore, notes: list[Note], aliases: list) -> None:
    conn = store.conn
    with conn:
        for table in ("note_tags", "tag_aliases", "tags", "notes", "notes_fts"):
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
