"""The KB notes API: create, read, edit and delete notes, and tag autocomplete.

Book notes are saved by the viewer with the book's index id and page (``file_id``
and ``seq``). The path, topic, title, page label and chapter link are then taken
from the index, never from the request, so a request can't attach a note to an
arbitrary file.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from ..config import Config
from ..db import Database
from ..notes import Note, NoteInvalid, NotesError, NotesStore, Tag
from . import render


class TagIn(BaseModel):
    kind: str
    name: str


class NoteIn(BaseModel):
    source_type: str
    title: str = ""
    quote: str = ""
    body: str = ""
    topic: str = ""  # other and own notes; book notes take the book's topic
    tags: list[TagIn] = Field(default_factory=list)
    # book notes: the book in the index and its page (PDF) or spine position (EPUB)
    file_id: int | None = None
    seq: int | None = None
    # other notes
    source_ref: str | None = None
    source_url: str | None = None


@contextmanager
def open_notes_for(config: Config, *, create: bool = True):
    """The notes store, or None if there isn't one yet and create is False."""
    if not create and not config.notes_path.is_file():
        yield None
        return
    try:
        store = NotesStore.open(config.notes_path)
    except NotesError as e:
        raise HTTPException(503, str(e)) from None
    try:
        yield store
    finally:
        store.close()


def file_ids(db: Database, notes: list[Note]) -> dict[str, int]:
    """Index ids of the books that book notes came from, by path, to link to the viewer."""
    paths = {n.path for n in notes if n.path and not n.source_missing}
    if not paths:
        return {}
    marks = ",".join("?" * len(paths))
    rows = db.conn.execute(f"SELECT id, path FROM files WHERE path IN ({marks})", list(paths))
    return {r["path"]: r["id"] for r in rows}


class TagRenameIn(BaseModel):
    kind: str
    name: str
    new_name: str


class AliasIn(BaseModel):
    kind: str
    name: str  # the tag
    alias: str  # another name for it


def add_notes_routes(
    app: FastAPI,
    config: Config,
    open_db: Callable[[], Database],
    require_app_header: Callable[[str | None], None],
) -> None:
    def open_notes(*, create: bool = True):
        return open_notes_for(config, create=create)

    def note_json(note: Note, ids: dict[str, int]) -> dict:
        data = {
            "id": note.id,
            "source_type": note.source_type,
            "title": note.title,
            "display_title": note.display_title,
            "quote": note.quote,
            "body": note.body,
            "topic": note.topic,
            "tags": [{"kind": t.kind, "name": t.name} for t in note.tags],
            "source_missing": note.source_missing,
            "created": note.created,
            "updated": note.updated,
        }
        if note.source_type == "book":
            file_id = ids.get(note.path) if not note.source_missing else None
            data.update(
                file_id=file_id, seq=note.seq, label=note.label, book_title=note.book_title,
                view_url=f"/view/{file_id}/{note.seq}" if file_id is not None else None,
            )
        elif note.source_type == "other":
            data.update(source_ref=note.source_ref, source_url=note.source_url)
        return data

    def to_note(db: Database, data: NoteIn, current: Note | None = None) -> Note:
        """Turn a request into a Note, filling in a book note's source from the index."""
        unknown = {t.kind.strip().lower() for t in data.tags} - set(config.tag_kinds)
        if current:
            unknown -= {t.kind for t in current.tags}  # kinds since removed from the config can stay
        if unknown:
            raise HTTPException(
                400, f"Unknown kind of tag: {', '.join(sorted(unknown))}. "
                     f"The kinds are set in the config: {', '.join(config.tag_kinds)}."
            )
        note = Note(
            source_type=data.source_type, title=data.title, quote=data.quote, body=data.body,
            topic=data.topic, tags=[Tag(t.kind, t.name) for t in data.tags],
            source_ref=data.source_ref, source_url=data.source_url,
        )
        if data.source_type != "book":
            topics = {r["topic"].casefold(): r["topic"] for r in db.topics()}
            if current:
                topics.setdefault(current.topic.casefold(), current.topic)
            if data.topic.strip() and data.topic.strip().casefold() not in topics:
                raise HTTPException(400, "Choose one of the library's topics.")
            note.topic = topics.get(data.topic.strip().casefold(), "")
            return note

        if data.file_id is None and current is not None and current.source_type == "book":
            # Editing a book note: its source stays as it was.
            note.topic = current.topic
            for name in ("path", "seq", "label", "book_title", "href"):
                setattr(note, name, getattr(current, name))
            return note
        if data.file_id is None or data.seq is None:
            raise HTTPException(400, "A book note needs the book and page it came from.")
        row = db.get_file(data.file_id)
        if row is None:
            raise HTTPException(400, "That book isn't in the index any more.")
        path = Path(row["path"]).resolve()
        if not any(path.is_relative_to(root) for root in config.roots):
            raise HTTPException(400, "That book isn't in the library.")
        section = next((s for s in db.sections(data.file_id) if s["seq"] == data.seq), None)
        if section is not None:
            note.label, note.href = section["label"], section["href"]
        elif row["kind"] == "pdf" and path.is_file():
            # Pages without text (pictures, blank scans) aren't in the index, but can still have notes.
            try:
                note.label = render.pdf_page(path, data.seq, []).label
            except IndexError:
                raise HTTPException(400, "That page isn't in this book.") from None
        else:
            raise HTTPException(400, "That page isn't in the index for this book.")
        note.path, note.seq, note.topic, note.book_title = row["path"], data.seq, row["topic"], row["title"]
        return note

    def save(store: NotesStore, db: Database, note: Note, note_id: int | None = None) -> dict:
        try:
            saved = store.create(note) if note_id is None else store.update(note_id, note)
        except NoteInvalid as e:
            raise HTTPException(400, str(e)) from None
        if saved is None:
            raise HTTPException(404, "That note doesn't exist.")
        return note_json(saved, file_ids(db, [saved]))

    @app.get("/api/notes")
    def page_notes(file_id: int, seq: int):
        """Notes saved from one page or chapter, for the viewer."""
        with open_db() as db, open_notes(create=False) as store:
            row = db.get_file(file_id)
            if row is None:
                raise HTTPException(404, "That book isn't in the index any more.")
            notes = store.for_page(row["path"], seq) if store else []
            return {"notes": [note_json(n, {row["path"]: file_id}) for n in notes]}

    @app.get("/api/notes/{note_id}")
    def get_note(note_id: int):
        with open_db() as db, open_notes(create=False) as store:
            note = store.get(note_id) if store else None
            if note is None:
                raise HTTPException(404, "That note doesn't exist.")
            return note_json(note, file_ids(db, [note]))

    @app.post("/api/notes", status_code=201)
    def create_note(data: NoteIn, x_book_searchie: str | None = Header(default=None)):
        require_app_header(x_book_searchie)
        with open_db() as db, open_notes() as store:
            return save(store, db, to_note(db, data))

    @app.put("/api/notes/{note_id}")
    def update_note(note_id: int, data: NoteIn, x_book_searchie: str | None = Header(default=None)):
        require_app_header(x_book_searchie)
        with open_db() as db, open_notes(create=False) as store:
            current = store.get(note_id) if store else None
            if current is None:
                raise HTTPException(404, "That note doesn't exist.")
            return save(store, db, to_note(db, data, current), note_id)

    @app.delete("/api/notes/{note_id}")
    def delete_note(note_id: int, x_book_searchie: str | None = Header(default=None)):
        require_app_header(x_book_searchie)
        with open_notes(create=False) as store:
            if store is None or not store.delete(note_id):
                raise HTTPException(404, "That note doesn't exist.")
        return {"deleted": note_id}

    @app.get("/api/tags")
    def tags(kind: str = "", q: str = "", limit: int = 20):
        """Tags in use, most used first, for autocomplete. Also lists the configured kinds."""
        with open_notes(create=False) as store:
            found = store.tags(kind=kind or None, prefix=q, limit=min(max(limit, 1), 200)) if store else []
        return {
            "kinds": config.tag_kinds,
            "tags": [{"kind": t.kind, "name": t.name, "notes": t.notes, "aliases": t.aliases} for t in found],
        }

    # --- Tidying tags ---------------------------------------------------------------

    def tidy(action):
        try:
            return action()
        except NoteInvalid as e:
            raise HTTPException(400, str(e)) from None

    @app.post("/api/tags/rename")
    def rename_tag(data: TagRenameIn, x_book_searchie: str | None = Header(default=None)):
        """Rename a tag on every note; renaming to an existing tag merges the two."""
        require_app_header(x_book_searchie)
        with open_notes(create=False) as store:
            if store is None:
                raise HTTPException(404, "There are no tags yet.")
            name, merged = tidy(lambda: store.rename_tag(data.kind, data.name, data.new_name))
        return {"kind": data.kind.strip().lower(), "name": name, "merged": merged}

    @app.post("/api/tags/aliases")
    def add_alias(data: AliasIn, x_book_searchie: str | None = Header(default=None)):
        require_app_header(x_book_searchie)
        with open_notes(create=False) as store:
            if store is None:
                raise HTTPException(404, "There are no tags yet.")
            aliases = tidy(lambda: store.add_alias(data.kind, data.name, data.alias))
        return {"aliases": aliases}

    @app.delete("/api/tags/aliases")
    def remove_alias(kind: str, alias: str, x_book_searchie: str | None = Header(default=None)):
        require_app_header(x_book_searchie)
        with open_notes(create=False) as store:
            if store is None or not store.remove_alias(kind, alias):
                raise HTTPException(404, "There's no tag with that name.")
        return {"deleted": alias}
