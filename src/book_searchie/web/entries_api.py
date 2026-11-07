"""The KB entries API: entries (Nettle), their sub-headings and the pieces of text under them.

Text saved from a book is sent with the book's index id and page (``file_id`` and
``seq``). Where it came from is then taken from the index, never from the request, as
for book notes. It's kept with the piece but the screens don't show it: it's for
"Saved from this page", links back to the book, and spotting book text before publishing.
"""

from __future__ import annotations

from collections.abc import Callable
from html import escape

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .. import markdown
from ..config import Config
from ..db import Database
from ..entries import Entry, EntryHit, EntryInvalid, EntrySummary, Piece
from ..search import QueryError
from .notes_api import book_source, file_ids, open_notes_for

# Marks around matched words in snippets, turned into <mark> after escaping.
_OPEN, _CLOSE = "\x02", "\x03"


class EntryIn(BaseModel):
    title: str
    tags: list[str] = Field(default_factory=list)


class PieceIn(BaseModel):
    title: str  # the entry: made if there isn't one with this title or other name
    heading: str = ""  # empty: the first sub-heading in the config
    body: str
    # from a book: its id in the index and its page (PDF) or spine position (EPUB)
    file_id: int | None = None
    seq: int | None = None
    # from somewhere else (both optional)
    source_ref: str | None = None
    source_url: str | None = None


class PieceEditIn(BaseModel):
    body: str


class PieceMoveIn(BaseModel):
    title: str | None = None  # another entry
    heading: str | None = None  # another sub-heading
    position: int | None = Field(default=None, ge=0)  # 0 = first in its sub-heading


class TitleIn(BaseModel):
    title: str


class TagsIn(BaseModel):
    tags: list[str]


class AliasIn(BaseModel):
    alias: str


class HeadingRenameIn(BaseModel):
    heading: str
    new_heading: str


def snippet_html(snippet: str) -> str:
    return escape(snippet).replace(_OPEN, "<mark>").replace(_CLOSE, "</mark>")


def add_entries_routes(
    app: FastAPI,
    config: Config,
    open_db: Callable[[], Database],
    require_app_header: Callable[[str | None], None],
) -> None:
    def open_notes(*, create: bool = True):
        return open_notes_for(config, create=create)

    def piece_json(piece: Piece, ids: dict[str, int]) -> dict:
        source: dict = {"type": piece.source_type}
        if piece.source_type == "book":
            file_id = ids.get(piece.path) if not piece.source_missing else None
            source.update(
                book_title=piece.book_title, label=piece.label, seq=piece.seq, file_id=file_id,
                missing=piece.source_missing, chapter=bool(piece.href),
                view_url=f"/view/{file_id}/{piece.seq}" if file_id is not None else None,
            )
        elif piece.source_type == "other":
            source.update(ref=piece.source_ref, url=piece.source_url)
        return {
            "id": piece.id, "entry_id": piece.entry_id, "entry_title": piece.entry_title,
            "heading": piece.heading, "position": piece.position, "body": piece.body,
            "body_html": markdown.render(piece.body), "source": source,
            "created": piece.created, "updated": piece.updated,
        }

    def summary_json(entry: EntrySummary) -> dict:
        return {"id": entry.id, "title": entry.title, "aliases": entry.aliases, "tags": entry.tags,
                "headings": entry.headings, "pieces": entry.pieces, "updated": entry.updated}

    def entry_json(db: Database, entry: Entry) -> dict:
        ids = file_ids(db, [p for s in entry.sections for p in s.pieces])
        return {
            "id": entry.id, "title": entry.title, "aliases": entry.aliases, "tags": entry.tags,
            "sections": [{"heading": s.heading, "pieces": [piece_json(p, ids) for p in s.pieces]}
                         for s in entry.sections],
            "created": entry.created, "updated": entry.updated,
        }

    def hit_json(hit: EntryHit) -> dict:
        return {**summary_json(hit.entry), "title_match": hit.title_match,
                "found_in": [{"heading": s.heading, "snippet_html": snippet_html(s.snippet)} for s in hit.sections]}

    def checked(action):
        try:
            return action()
        except EntryInvalid as e:
            raise HTTPException(400, str(e)) from None

    def entry_or_404(store, entry_id: int) -> Entry:
        entry = store.get_entry(entry_id) if store else None
        if entry is None:
            raise HTTPException(404, "That entry isn't in your KB. It may have been deleted or merged.")
        return entry

    # --- Reading -------------------------------------------------------------------
    # Fixed paths come before /api/entries/{entry_id}.

    @app.get("/api/entries")
    def list_entries(tag: str = "", topic: str = ""):
        """Every entry A to Z with its sub-headings, for the KB's left column."""
        with open_notes(create=False) as store:
            found = store.entry_list(tag=tag or None, topic=topic or None) if store else []
        return {"entries": [summary_json(e) for e in found], "headings": config.headings}

    @app.get("/api/entries/titles")
    def entry_titles(q: str = "", limit: int = 10):
        """Titles starting with q (or with another name that does), for autocomplete."""
        with open_notes(create=False) as store:
            titles = store.entry_titles(q, min(max(limit, 1), 100)) if store else []
        return {"titles": titles}

    @app.get("/api/entries/lookup")
    def lookup_entry(title: str):
        """The entry a title (or other name) goes to, or null if typing it would make a new one."""
        with open_notes(create=False) as store:
            entry_id = store.entry_id(title) if store and title.strip() else None
            found = store.entry_summary(entry_id) if entry_id is not None else None
        return {"entry": summary_json(found) if found else None, "headings": config.headings}

    @app.get("/api/entries/search")
    def search_entries(q: str, topic: str = "", limit: int = 50, offset: int = 0):
        with open_notes(create=False) as store:
            if store is None:
                return {"total": 0, "entries": []}
            try:
                hits, total = store.search_entries(q, topic=topic or None, limit=min(max(limit, 1), 200),
                                                   offset=max(offset, 0), mark=(_OPEN, _CLOSE))
            except QueryError as e:
                raise HTTPException(400, str(e)) from None
        return {"total": total, "entries": [hit_json(h) for h in hits]}

    @app.get("/api/entries/tags")
    def entry_tags(q: str = "", limit: int = 20):
        """Entry tags in use, A to Z with how many entries have each, for autocomplete.
        q matches the start of a tag's name or of one of its other names."""
        with open_notes(create=False) as store:
            found = store.entry_tag_counts(q, min(max(limit, 1), 200)) if store else []
        return {"tags": [{"name": name, "entries": n, "aliases": aliases} for name, n, aliases in found]}

    @app.get("/api/entries/{entry_id}")
    def get_entry(entry_id: int):
        with open_db() as db, open_notes(create=False) as store:
            return entry_json(db, entry_or_404(store, entry_id))

    @app.get("/api/pieces")
    def page_pieces(file_id: int, seq: int):
        """Text saved from one page or chapter, for the viewer's "Saved from this page"."""
        with open_db() as db, open_notes(create=False) as store:
            row = db.get_file(file_id)
            if row is None:
                raise HTTPException(404, "That book isn't in the index any more.")
            found = store.pieces_for_page(row["path"], seq) if store else []
            return {"pieces": [piece_json(p, {row["path"]: file_id}) for p in found]}

    # --- Writing -------------------------------------------------------------------

    @app.post("/api/entries", status_code=201)
    def create_entry(data: EntryIn, x_book_searchie: str | None = Header(default=None)):
        require_app_header(x_book_searchie)
        with open_db() as db, open_notes() as store:
            return entry_json(db, checked(lambda: store.create_entry(data.title, data.tags)))

    @app.post("/api/pieces", status_code=201)
    def add_piece(data: PieceIn, x_book_searchie: str | None = Header(default=None)):
        """Add text to the end of an entry's sub-heading, making the entry if it's new."""
        require_app_header(x_book_searchie)
        with open_db() as db:
            piece = Piece(body=data.body, source_ref=data.source_ref, source_url=data.source_url)
            if data.file_id is not None or data.seq is not None:
                if data.file_id is None or data.seq is None:
                    raise HTTPException(400, "Text from a book needs the book and page it came from.")
                for name, value in book_source(db, config, data.file_id, data.seq).items():
                    setattr(piece, name, value)
                piece.source_ref = piece.source_url = None
            with open_notes() as store:
                created = store.entry_id(data.title) is None if data.title.strip() else False
                saved = checked(lambda: store.add_piece(data.title, data.heading or None, piece))
                return {**piece_json(saved, file_ids(db, [saved])), "new_entry": created}

    @app.put("/api/pieces/{piece_id}")
    def edit_piece(piece_id: int, data: PieceEditIn, x_book_searchie: str | None = Header(default=None)):
        """Change a piece's text. Where it came from stays as it was."""
        require_app_header(x_book_searchie)
        with open_db() as db, open_notes(create=False) as store:
            saved = checked(lambda: store.update_piece(piece_id, data.body)) if store else None
            if saved is None:
                raise HTTPException(404, "That text isn't in your KB. It may have been deleted.")
            return piece_json(saved, file_ids(db, [saved]))

    @app.post("/api/pieces/{piece_id}/move")
    def move_piece(piece_id: int, data: PieceMoveIn, x_book_searchie: str | None = Header(default=None)):
        require_app_header(x_book_searchie)
        with open_db() as db, open_notes(create=False) as store:
            saved = checked(lambda: store.move_piece(piece_id, title=data.title, heading=data.heading,
                                                     position=data.position)) if store else None
            if saved is None:
                raise HTTPException(404, "That text isn't in your KB. It may have been deleted.")
            return piece_json(saved, file_ids(db, [saved]))

    @app.delete("/api/pieces/{piece_id}")
    def delete_piece(piece_id: int, x_book_searchie: str | None = Header(default=None)):
        require_app_header(x_book_searchie)
        with open_notes(create=False) as store:
            if store is None or not store.delete_piece(piece_id):
                raise HTTPException(404, "That text isn't in your KB. It may have been deleted.")
        return {"deleted": piece_id}

    @app.delete("/api/entries/aliases")
    def remove_alias(alias: str, x_book_searchie: str | None = Header(default=None)):
        require_app_header(x_book_searchie)
        with open_notes(create=False) as store:
            if store is None or not store.remove_entry_alias(alias):
                raise HTTPException(404, "No entry has that other name.")
        return {"deleted": alias}

    @app.delete("/api/entries/{entry_id}")
    def delete_entry(entry_id: int, x_book_searchie: str | None = Header(default=None)):
        """Delete an entry with all its text."""
        require_app_header(x_book_searchie)
        with open_notes(create=False) as store:
            if store is None or not store.delete_entry(entry_id):
                raise HTTPException(404, "That entry isn't in your KB. It may have been deleted or merged.")
        return {"deleted": entry_id}

    @app.post("/api/entries/{entry_id}/rename")
    def rename_entry(entry_id: int, data: TitleIn, x_book_searchie: str | None = Header(default=None)):
        """Rename an entry. Renaming to another entry's title (or other name) merges the two;
        the id returned is then the entry it was merged into."""
        require_app_header(x_book_searchie)
        with open_notes(create=False) as store:
            entry_or_404(store, entry_id)
            title, merged = checked(lambda: store.rename_entry(entry_id, data.title))
            return {"id": store.entry_id(title), "title": title, "merged": merged}

    @app.put("/api/entries/{entry_id}/tags")
    def set_tags(entry_id: int, data: TagsIn, x_book_searchie: str | None = Header(default=None)):
        require_app_header(x_book_searchie)
        with open_notes(create=False) as store:
            entry_or_404(store, entry_id)
            return {"tags": checked(lambda: store.set_entry_tags(entry_id, data.tags))}

    @app.post("/api/entries/{entry_id}/aliases")
    def add_alias(entry_id: int, data: AliasIn, x_book_searchie: str | None = Header(default=None)):
        require_app_header(x_book_searchie)
        with open_notes(create=False) as store:
            entry_or_404(store, entry_id)
            return {"aliases": checked(lambda: store.add_entry_alias(entry_id, data.alias))}

    @app.post("/api/entries/{entry_id}/headings/rename")
    def rename_heading(entry_id: int, data: HeadingRenameIn, x_book_searchie: str | None = Header(default=None)):
        """Rename one of an entry's sub-headings; renaming to one it has already merges them."""
        require_app_header(x_book_searchie)
        with open_notes(create=False) as store:
            entry_or_404(store, entry_id)
            return {"heading": checked(lambda: store.rename_section(entry_id, data.heading, data.new_heading))}
