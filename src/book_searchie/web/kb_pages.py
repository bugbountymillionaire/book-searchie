"""The KB pages: entries in two columns (titles on the left, the open entry on the right),
the list of sub-headings, and the older pages for notes."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from markupsafe import Markup

from .. import markdown
from ..config import Config
from ..db import Database
from ..entries import Entry
from ..notes import Note
from ..notes_backup import export_json_text, export_markdown
from ..search import QueryError
from .entries_api import _CLOSE, _OPEN, snippet_html
from .notes_api import file_ids, open_notes_for

NOTES_PER_PAGE = 20
BADGES = {"book": "Book", "other": "Other source", "own": "Own knowledge"}


def tag_url(kind: str, name: str) -> str:
    return f"/kb/tag/{quote(kind, safe='')}/{quote(name, safe='')}"


def note_view(note: Note, ids: dict[str, int]) -> dict:
    """A note, ready for the note card template."""
    source: dict = {"badge": BADGES[note.source_type], "type": note.source_type}
    if note.source_type == "book":
        file_id = ids.get(note.path)
        where = note.label or ""
        if not note.href:  # a PDF page rather than an EPUB chapter
            where = f"p. {where}"
        source.update(
            title=note.book_title or "Unknown book", where=where, missing=note.source_missing,
            url=f"/view/{file_id}/{note.seq}" if file_id is not None and not note.source_missing else None,
        )
    elif note.source_type == "other":
        source.update(title=note.source_ref, url=note.source_url)
    return {
        "note": note,
        "title": note.display_title,
        "body": markdown.render(note.body) if note.body else None,
        "source": source,
        "tags": [{"kind": t.kind, "name": t.name, "url": tag_url(t.kind, t.name)} for t in note.tags],
    }


def heading_id(heading: str) -> str:
    """The id of a sub-heading on an entry's page, for links like #h-infused-oil."""
    return "h-" + (re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-") or "x")


def entry_view(db: Database, entry: Entry) -> dict:
    """An entry, ready for the entry template."""
    ids = file_ids(db, [p for s in entry.sections for p in s.pieces])
    sections = []
    for s in entry.sections:
        pieces = []
        for p in s.pieces:
            file_id = ids.get(p.path) if p.source_type == "book" and not p.source_missing else None
            pieces.append({
                "id": p.id, "body": p.body, "html": Markup(markdown.render(p.body)),
                "book_url": f"/view/{file_id}/{p.seq}" if file_id is not None else None,
            })
        sections.append({"heading": s.heading, "id": heading_id(s.heading), "pieces": pieces})
    return {"entry": entry, "sections": sections}


def add_kb_pages(app: FastAPI, config: Config, open_db: Callable[[], Database], page: Callable) -> None:
    def views(db: Database, notes: list[Note]) -> list[dict]:
        ids = file_ids(db, notes)
        return [note_view(n, ids) for n in notes]

    def note_or_404(store, note_id: int) -> Note:
        note = store.get(note_id) if store else None
        if note is None:
            raise HTTPException(404, "That note isn't in your KB. It may have been deleted.")
        return note

    # --- Entries: titles on the left, the open entry on the right ------------------------

    def kb_view(request: Request, entry_id: int | None, q: str, tag: str, part: str):
        """The KB page. part="entry" or "list" gives just that column, for the page's script
        to swap in after a change."""
        q, tag = q.strip(), " ".join(tag.split())
        with open_db() as db, open_notes_for(config, create=False) as store:
            entries, hits, error = [], [], ""
            if store and q:
                try:
                    found, _ = store.search_entries(q, limit=200, mark=(_OPEN, _CLOSE))
                except QueryError as e:
                    error, found = str(e), []
                hits = [{
                    "entry": h.entry, "title_match": h.title_match,
                    "found_in": [{"heading": s.heading, "id": heading_id(s.heading),
                                  "snippet": Markup(snippet_html(s.snippet))} for s in h.sections],
                } for h in found]
            elif store:
                entries = store.entry_list(tag=tag or None)
                if tag:
                    tag = store.resolve_tag("tag", tag) or tag  # the tag's own name, for the heading
            current = None
            if entry_id is not None:
                found_entry = store.get_entry(entry_id) if store else None
                if found_entry is None:
                    raise HTTPException(404, "That entry isn't in your KB. It may have been deleted or merged.")
                current = entry_view(db, found_entry)
            elif store and (hits or entries):
                current = entry_view(db, store.get_entry((hits[0]["entry"] if hits else entries[0]).id))
            context = dict(
                section="kb", kb_q=q, tag=tag, entries=entries, hits=hits, error=error, current=current,
                total=store.entry_count() if store else 0, heading_id=heading_id,
            )
            name = {"entry": "_kb_entry.html", "list": "_kb_list.html"}.get(part, "kb.html")
            return page(request, name, db, **context)

    @app.get("/kb")
    def kb_home(request: Request, q: str = "", tag: str = "", part: str = ""):
        return kb_view(request, None, q, tag, part)

    @app.get("/kb/entry/{entry_id}")
    def kb_entry(request: Request, entry_id: int, q: str = "", tag: str = "", part: str = ""):
        return kb_view(request, entry_id, q, tag, part)

    @app.get("/kb/headings")
    def kb_headings(request: Request):
        """The master list of sub-headings: reorder, rename and merge, remove."""
        with open_db() as db, open_notes_for(config, create=False) as store:
            found = store.heading_counts() if store else [(h, 0) for h in config.headings]
            return page(request, "kb_headings.html", db, section="kb",
                        headings=[{"name": n, "entries": c} for n, c in found])

    @app.get("/kb/new")
    def kb_new(request: Request):
        with open_db() as db:
            return page(request, "kb_form.html", db, section="kb", note=None, source=None, tags=[],
                        form_kinds=config.tag_kinds)

    @app.get("/kb/note/{note_id}")
    def kb_note(request: Request, note_id: int):
        with open_db() as db, open_notes_for(config, create=False) as store:
            note = note_or_404(store, note_id)
            return page(request, "kb_note.html", db, section="kb", card=views(db, [note])[0])

    @app.get("/kb/note/{note_id}/edit")
    def kb_edit(request: Request, note_id: int):
        with open_db() as db, open_notes_for(config, create=False) as store:
            note = note_or_404(store, note_id)
            card = views(db, [note])[0]
            # A field for every kind on the note, so tags of kinds since removed from the config aren't lost.
            kinds = config.tag_kinds + sorted({t.kind for t in note.tags} - set(config.tag_kinds))
            return page(
                request, "kb_form.html", db, section="kb", note=note, source=card["source"],
                tags=[{"kind": t.kind, "name": t.name} for t in note.tags], form_kinds=kinds,
            )

    @app.get("/kb/tags")
    def kb_tags(request: Request):
        """Tidying tags (rename, merge, other names) and backing up the KB."""
        with open_db() as db, open_notes_for(config, create=False) as store:
            kinds = {kind: [] for kind in config.tag_kinds}
            lookup: dict[str, dict[str, str]] = {}  # per kind: any name, in lower case -> the tag's name
            for t in store.tags(by_name=True) if store else []:
                kinds.setdefault(t.kind, []).append(
                    {"name": t.name, "notes": t.notes, "aliases": t.aliases, "url": tag_url(t.kind, t.name)}
                )
                for n in [t.name, *t.aliases]:
                    lookup.setdefault(t.kind, {})[n.lower()] = t.name
            return page(
                request, "kb_tags.html", db, section="kb", kinds=kinds, lookup=lookup,
                total=store.count() if store else 0,
                notes_path=config.notes_path,
            )

    @app.get("/kb/export")
    def kb_export(format: str = "json"):
        """Download the KB: JSON (a full backup) or Markdown (for reading)."""
        if format not in ("json", "md"):
            raise HTTPException(400, "The format must be json or md.")
        with open_notes_for(config, create=False) as store:
            if store is None:
                raise HTTPException(404, "There are no notes to export yet.")
            text = export_json_text(store) if format == "json" else export_markdown(store)
        name = f"book-searchie-notes-{date.today():%Y-%m-%d}.{format}"
        return Response(
            text, media_type="application/json" if format == "json" else "text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
        )

    @app.get("/kb/tag/{kind}/{name:path}")
    def kb_tag(request: Request, kind: str, name: str, by: str = ""):
        with open_db() as db, open_notes_for(config, create=False) as store:
            # Another name for a tag (an alias, or other capitals) goes to the tag's own page.
            main = store.resolve_tag(kind, name) if store else None
            if main is not None and (main != name or kind != kind.lower()):
                url = tag_url(kind.lower(), main) + (f"?by={quote(by)}" if by else "")
                return RedirectResponse(url, status_code=307)
            notes = store.with_tag(kind, name) if store else []
            if not notes:
                raise HTTPException(404, f"No notes in your KB have the tag \"{name}\".")
            kind = kind.lower()
            # The tag's own spelling, whatever the address said.
            wanted = " ".join(name.split()).casefold()
            name = next((t.name for t in notes[0].tags if t.kind == kind and t.name.casefold() == wanted), name)
            used = {t.kind for n in notes for t in n.tags}
            other_kinds = [k for k in config.tag_kinds if k != kind] + sorted(used - set(config.tag_kinds) - {kind})
            if by == "none":
                by = ""
            elif by not in other_kinds:
                by = next((k for k in other_kinds if k in used), "")
            cards = views(db, notes)
            groups: dict[str, list[dict]] = {}
            ungrouped = []
            for card in cards:
                names = [t.name for t in card["note"].tags if t.kind == by] if by else []
                for group in names:
                    groups.setdefault(group, []).append(card)
                if not names:
                    ungrouped.append(card)
            grouped = [{"name": g, "url": tag_url(by, g), "cards": groups[g]} for g in sorted(groups, key=str.casefold)]
            if ungrouped:
                grouped.append({"name": f"No {by}" if by and grouped else "", "url": None, "cards": ungrouped})
            return page(
                request, "kb_tag.html", db, section="kb", kind=kind, tag_name=name, count=len(notes),
                by=by, other_kinds=other_kinds, groups=grouped,
                by_url=lambda k: f"{tag_url(kind, name)}?by={quote(k)}",
            )
