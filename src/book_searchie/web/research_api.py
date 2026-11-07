"""Research complete: mark a book done (or not) for a search term, from the viewer."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from ..config import Config
from ..db import Database
from ..research import ResearchInvalid, research_term
from .notes_api import open_notes_for


class ResearchIn(BaseModel):
    file_id: int
    q: str
    done: bool = True


def add_research_routes(
    app: FastAPI,
    config: Config,
    open_db: Callable[[], Database],
    require_app_header: Callable[[str | None], None],
) -> None:
    @app.put("/api/research")
    def set_research(data: ResearchIn, x_book_searchie: str | None = Header(default=None)):
        """Mark the book done for the search, or undo it. The book is taken from the index,
        so a request can't name an arbitrary file."""
        require_app_header(x_book_searchie)
        with open_db() as db:
            row = db.get_file(data.file_id)
        if row is None:
            raise HTTPException(400, "That book isn't in the index any more.")
        if not any(Path(row["path"]).resolve().is_relative_to(root) for root in config.roots):
            raise HTTPException(400, "That book isn't in the library.")
        try:
            term = research_term(data.q)
        except ResearchInvalid as e:
            raise HTTPException(400, str(e)) from None
        # Undoing when there's no notes file yet has nothing to undo, so don't make one.
        with open_notes_for(config, create=data.done) as store:
            if store is not None:
                store.set_researched(term, row["path"], data.done, book_title=row["title"], topic=row["topic"])
        return {"file_id": data.file_id, "term": term, "done": data.done}

    @app.delete("/api/research")
    def remove_research(term: str, path: str, x_book_searchie: str | None = Header(default=None)):
        """Undo a record from the research list, which may be for a book that has gone."""
        require_app_header(x_book_searchie)
        with open_notes_for(config, create=False) as store:
            if store is None or not store.remove_research(term, path):
                raise HTTPException(404, "That book isn't marked as researched for that search.")
        return {"deleted": True}
