"""The web interface: search, results grouped by book, page viewer, and the "Can't search" page."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .. import __version__
from ..config import Config
from ..db import Database
from ..highlight import query_terms, text_spans
from ..ocr import INSTALL_HELP, OcrUnavailable, check_available, ocr_candidates, ocr_copy_path, ocr_dir
from ..problems import PROBLEMS, detail, group_problems
from ..search import QueryError, fill_snippets, matching_seqs, search
from . import opener, render
from .jobs import OcrBusy, OcrJob
from .kb_pages import add_kb_pages, note_view
from .notes_api import add_notes_routes, file_ids, open_notes_for

HERE = Path(__file__).parent
BOOKS_PER_PAGE = 10
NOTES_PER_PAGE = 20  # searching the KB only
NOTES_ABOVE_BOOKS = 3  # searching both: the best few notes, above the book results
MAX_HITS = 1000
PAGE_DPI = 150
BOOK_SUFFIXES = {".pdf", ".epub"}
# Private-use characters mark matches in snippets, so they survive HTML escaping.
MARK_OPEN, MARK_CLOSE = "", ""
_HEX64 = re.compile(r"[0-9a-f]{64}")


def marked(snippet: str) -> Markup:
    return Markup(str(escape(snippet)).replace(MARK_OPEN, "<mark>").replace(MARK_CLOSE, "</mark>"))


def highlighted(text: str, terms) -> Markup:
    """Plain text, HTML-escaped, with the search terms in <mark>s."""
    parts, pos = [], 0
    for start, end in text_spans(text, terms):
        parts += [escape(text[pos:start]), Markup("<mark>"), escape(text[start:end]), Markup("</mark>")]
        pos = end
    parts.append(escape(text[pos:]))
    return Markup("").join(parts)


def ago(timestamp: str, now: datetime | None = None) -> str:
    """'just now', '5 minutes ago', 'yesterday', '3 weeks ago'... for an ISO timestamp."""
    seconds = ((now or datetime.now(timezone.utc)) - datetime.fromisoformat(timestamp)).total_seconds()
    if seconds < 60:
        return "just now"
    for size, unit in ((31_536_000, "year"), (2_592_000, "month"), (604_800, "week"), (86_400, "day"),
                       (3_600, "hour"), (60, "minute")):
        if seconds >= size:
            n = int(seconds // size)
            if unit == "day" and n == 1:
                return "yesterday"
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"


LOCAL_HOSTS = ["localhost", "127.0.0.1", "[::1]"]


def create_app(config: Config, *, allowed_hosts: list[str] = LOCAL_HOSTS) -> FastAPI:
    app = FastAPI(title="Book-Searchie", docs_url=None, redoc_url=None, openapi_url=None)
    # Local mode has no login, so only answer requests addressed to this computer. This
    # also blocks "DNS rebinding", where a web page tricks the browser into calling us.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["marked"] = marked
    templates.env.filters["ago"] = ago
    templates.env.globals["max_hits"] = MAX_HITS
    templates.env.globals["version"] = __version__
    # Stylesheets and scripts are linked with a fingerprint of their contents, so a
    # browser never keeps using an old copy after they change.
    templates.env.globals["static_version"] = _static_fingerprint()
    # Opening a file happens on the computer running the server, which is only the
    # computer you're using in local mode.
    can_open = config.mode == "local"
    templates.env.globals["can_open"] = can_open
    templates.env.globals["tag_kinds"] = config.tag_kinds
    job = OcrJob(config)

    def open_db() -> Database:
        return Database.open(config.index_path, create=False)

    def page(request: Request, name: str, db: Database, **context) -> HTMLResponse:
        context.setdefault("q", "")
        context.setdefault("topic", "")
        context.setdefault("section", "")
        context.setdefault("scope_books", True)
        context.setdefault("scope_kb", True)
        context["topics"] = db.topics()
        context["problem_count"] = len(db.problem_files())
        return templates.TemplateResponse(request, name, context)

    def book_or_404(db: Database, file_id: int):
        row = db.get_file(file_id)
        if row is None:
            raise HTTPException(404, "That book isn't in the index any more. Try searching again.")
        return row

    def source_path(row) -> Path:
        """The file to display: the OCR copy for OCR'd books, otherwise the original.

        Every path is checked to be inside a library root or the OCR cache, so a
        tampered index can't make us serve other files.
        """
        if row["ocr"] and row["content_hash"] and _HEX64.fullmatch(row["content_hash"]):
            copy = ocr_copy_path(config, row["content_hash"])
            if copy.is_file():
                return copy
        return original_path(row)

    def original_path(row) -> Path:
        path = Path(row["path"]).resolve()
        if not any(path.is_relative_to(root) for root in config.roots) or not path.is_file():
            raise HTTPException(404, "The book's file can't be found. It may have been moved; re-index.")
        return path

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith(("/api/", "/page/", "/asset/")):
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
        return templates.TemplateResponse(
            request, "error.html", {"message": exc.detail, "q": "", "topic": "", "topics": [], "problem_count": 0},
            status_code=exc.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, exc: RequestValidationError):
        problems = []
        for err in exc.errors():
            where = ".".join(str(part) for part in err.get("loc", ()) if part != "body")
            problems.append(f"{where}: {err.get('msg')}" if where else str(err.get("msg")))
        return JSONResponse({"error": "That request isn't right. " + "; ".join(problems)}, status_code=422)

    add_notes_routes(app, config, open_db, _require_app_header)
    add_kb_pages(app, config, open_db, page)

    # --- Search ------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, q: str = "", topic: str = "", page_no: int = 1,
             books: str | None = None, kb: str | None = None):
        q = q.strip()
        want_books, want_kb = search_scope(books, kb)
        scope = {"scope_books": want_books, "scope_kb": want_kb}
        with open_db() as db, open_notes_for(config, create=False) as store:
            if not q:
                history = [
                    {"row": h, "found": history_found(h),
                     "url": "/?" + urlencode({"q": h["query"], "topic": h["topic"],
                                              **scope_params(h["scope"] != "kb", h["scope"] != "books")})}
                    for h in db.recent_searches()
                ]
                return page(request, "search.html", db, topic=topic, stats=db.stats(), history=history,
                            note_count=store.count() if store else 0, **scope)
            try:
                note_total, note_cards, note_pages = 0, [], 1
                if want_kb and store:
                    note_total = store.search_count(q, topic=topic or None)
                    if want_books:  # a few above the book results
                        found = store.search(q, topic=topic or None, limit=NOTES_ABOVE_BOOKS, mark=(MARK_OPEN, MARK_CLOSE))
                    else:
                        note_pages = max(1, -(-note_total // NOTES_PER_PAGE))
                        page_no = min(max(1, page_no), note_pages)
                        found = store.search(q, topic=topic or None, limit=NOTES_PER_PAGE,
                                             offset=(page_no - 1) * NOTES_PER_PAGE, mark=(MARK_OPEN, MARK_CLOSE))
                    ids = file_ids(db, [h.note for h in found])
                    note_cards = [{**note_view(h.note, ids), "snippet": h.snippet} for h in found]
                hits = search(db, q, topic=topic or None, limit=MAX_HITS, with_snippets=False) if want_books else []
            except QueryError as e:
                return page(request, "search.html", db, q=q, topic=topic, error=str(e), **scope)
            if page_no == 1:  # not again for each page of the same results
                db.record_search(q, topic, len(hits), "both" if want_books and want_kb else "books" if want_books else "kb",
                                 notes=note_total if want_kb else None)
            books_found: dict[int, dict] = {}
            for hit in hits:
                books_found.setdefault(hit.file_id, {"hit": hit, "hits": []})["hits"].append(hit)
            book_list = list(books_found.values())
            pages = note_pages if not want_books else max(1, -(-len(book_list) // BOOKS_PER_PAGE))
            page_no = min(max(1, page_no), pages)
            shown = book_list[(page_no - 1) * BOOKS_PER_PAGE : page_no * BOOKS_PER_PAGE]
            # Only the first few hits of each book on this page are displayed; snippet just those.
            fill_snippets(db, q, [h for b in shown for h in b["hits"][:3]], mark=(MARK_OPEN, MARK_CLOSE))
            terms = query_terms(q)
            shown = [{**b, "hits": [_hit_view(h, terms) for h in b["hits"][:3]], "count": len(b["hits"])} for b in shown]
            params = scope_params(want_books, want_kb)
            return page(
                request, "search.html", db, q=q, topic=topic, books=shown, book_count=len(book_list),
                hit_count=len(hits), capped=len(hits) == MAX_HITS, page_no=page_no, pages=pages,
                notes=note_cards, note_total=note_total, **scope,
                all_notes_url="/?" + urlencode({"q": q, "topic": topic, "kb": "1"}),
                all_topics_url="/?" + urlencode({"q": q, **params}),
                page_url=lambda n: "/?" + urlencode({"q": q, "topic": topic, **params, "page_no": n}),
            )

    @app.get("/book/{file_id}", response_class=HTMLResponse)
    def book_matches(request: Request, file_id: int, q: str = ""):
        with open_db() as db:
            row = book_or_404(db, file_id)
            try:
                hits = search(db, q, file_id=file_id, limit=5000, mark=(MARK_OPEN, MARK_CLOSE),
                              in_book_order=True, with_epub_text=True) if q.strip() else []
            except QueryError as e:
                return page(request, "book.html", db, q=q, book=row, hits=[], error=str(e))
            terms = query_terms(q)
            return page(request, "book.html", db, q=q, book=row, hits=[_hit_view(h, terms) for h in hits])

    # --- Viewer ------------------------------------------------------------

    @app.get("/view/{file_id}/{seq}", response_class=HTMLResponse)
    def view(request: Request, file_id: int, seq: int, q: str = ""):
        with open_db() as db:
            row = book_or_404(db, file_id)
            path = source_path(row)
            try:
                matches = matching_seqs(db, q, file_id) if q.strip() else []
            except QueryError:
                matches = []
            prev_match = max((s for s in matches if s < seq), default=None)
            next_match = min((s for s in matches if s > seq), default=None)
            nav = {
                # Past either end, the match buttons wrap round to the other end of the book.
                "prev_match": matches[-1] if prev_match is None and matches else prev_match,
                "next_match": matches[0] if next_match is None and matches else next_match,
                "prev_wraps": prev_match is None,
                "next_wraps": next_match is None,
                "match_number": matches.index(seq) + 1 if seq in matches else None,
                "match_total": len(matches),
            }
            qs = "?" + urlencode({"q": q}) if q else ""
            if row["kind"] == "pdf":
                try:
                    info = render.pdf_page(path, seq, query_terms(q))
                except IndexError:
                    raise HTTPException(404, "That page doesn't exist in this book.") from None
                text = db.section_text(file_id, seq)
                return page(
                    request, "view_pdf.html", db, q=q, book=row, seq=seq, label=info.label,
                    page_count=info.page_count, pdf=info, image_dpi=PAGE_DPI,
                    # For "Save to KB": a PDF page is an image, so its text is shown to select quotes from.
                    page_text=highlighted(text, query_terms(q)) if text else None,
                    qs=qs, image_url=f"/page/{file_id}/{seq}.png{qs}", **nav,
                )
            chapters = db.sections(file_id)
            current = next((c for c in chapters if c["seq"] == seq), None)
            if current is None:
                raise HTTPException(404, "That chapter doesn't exist in this book.")
            position = chapters.index(current)
            content, marks = render.render_epub_chapter(path, current["href"], query_terms(q), render.asset_url_for(file_id))
            return page(
                request, "view_epub.html", db, q=q, book=row, seq=seq, label=current["label"], content=Markup(content),
                marks=marks, qs=qs, chapter_number=position + 1, chapter_total=len(chapters),
                prev_chapter=chapters[position - 1]["seq"] if position > 0 else None,
                next_chapter=chapters[position + 1]["seq"] if position + 1 < len(chapters) else None,
                **nav,
            )

    @app.get("/page/{file_id}/{seq}.png")
    def page_image(file_id: int, seq: int, q: str = "", dpi: int = PAGE_DPI):
        with open_db() as db:
            row = book_or_404(db, file_id)
            if row["kind"] != "pdf":
                raise HTTPException(404, "Not a PDF.")
            path = source_path(row)
        try:
            png, _ = render.render_pdf_page(path, seq, query_terms(q), dpi=min(max(dpi, 50), 300))
        except IndexError:
            raise HTTPException(404, "No such page.") from None
        return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/asset/{file_id}")
    def epub_asset(file_id: int, name: str):
        with open_db() as db:
            row = book_or_404(db, file_id)
            if row["kind"] != "epub":
                raise HTTPException(404, "Not an EPUB.")
            path = source_path(row)
        try:
            data, media_type = render.read_epub_image(path, name)
        except KeyError:
            raise HTTPException(404, "No such image.") from None
        return Response(data, media_type=media_type, headers={"Cache-Control": "private, max-age=86400"})

    @app.post("/api/open/{file_id}")
    def open_file(file_id: int, x_book_searchie: str | None = Header(default=None)):
        _require_app_header(x_book_searchie)
        if not can_open:
            raise HTTPException(404, "Opening files is only available in local mode.")
        with open_db() as db:
            row = book_or_404(db, file_id)
        # Always the original, never the OCR copy. Only book files are ever opened, so a
        # tampered index can't make us run a program.
        path = original_path(row)
        if path.suffix.lower() not in BOOK_SUFFIXES:
            raise HTTPException(400, "That isn't a book file.")
        try:
            opener.open_in_default_app(path)
        except opener.OpenFailed as e:
            raise HTTPException(500, str(e)) from None
        return {"opened": path.name}

    # --- Search history ----------------------------------------------------

    @app.delete("/api/history/{search_id}")
    def forget_search(search_id: int, x_book_searchie: str | None = Header(default=None)):
        _require_app_header(x_book_searchie)
        with open_db() as db:
            if not db.delete_search(search_id):
                raise HTTPException(404, "That search isn't in the history.")
        return {"deleted": search_id}

    @app.delete("/api/history")
    def clear_history(x_book_searchie: str | None = Header(default=None)):
        _require_app_header(x_book_searchie)
        with open_db() as db:
            return {"deleted": db.clear_searches()}

    # --- Can't search / OCR ------------------------------------------------

    @app.get("/problems", response_class=HTMLResponse)
    def problems(request: Request):
        with open_db() as db:
            groups = group_problems(db.problem_files())
            rows = {
                kind: [{"row": r, "detail": detail(kind, r)} for r in members] for kind, members in groups.items()
            }
            try:
                check_available()
                ocr_help = None
            except OcrUnavailable:
                ocr_help = INSTALL_HELP
            waiting = ocr_candidates(db)
            return page(
                request, "problems.html", db, groups=rows, problems=PROBLEMS, ocr_help=ocr_help,
                ocr_waiting=len(waiting), ocr_waiting_pages=sum(r["units"] for r in waiting),
                ocr_count=db.ocr_count(), ocr_dir=ocr_dir(config),
            )

    @app.get("/api/ocr")
    def ocr_status():
        return job.status()

    @app.post("/api/ocr")
    def ocr_start(
        ids: list[int] | None = Body(default=None, embed=True),
        language: str = Body(default="eng", embed=True),
        x_book_searchie: str | None = Header(default=None),
    ):
        _require_app_header(x_book_searchie)
        if not re.fullmatch(r"[A-Za-z_]{3,8}(\+[A-Za-z_]{3,8})*", language):
            raise HTTPException(400, "Language should be a Tesseract code like eng or eng+fra.")
        with open_db() as db:
            waiting = ocr_candidates(db)
        if ids is not None:
            wanted = set(ids)
            waiting = [r for r in waiting if r["id"] in wanted]
        if not waiting:
            raise HTTPException(400, "None of those books are waiting for OCR.")
        try:
            job.start([r["rel"] for r in waiting], language)
        except OcrBusy as e:
            raise HTTPException(409, str(e)) from None
        return job.status()

    @app.post("/api/ocr/cancel")
    def ocr_cancel(x_book_searchie: str | None = Header(default=None)):
        _require_app_header(x_book_searchie)
        job.cancel()
        return job.status()

    return app


def history_found(h) -> str:
    """What a past search found: "12 pages, 3 notes", "no results"..."""
    parts = []
    if h["scope"] != "kb" and h["hits"]:
        parts.append(f"{h['hits']}{'+' if h['hits'] >= MAX_HITS else ''} page{'s' if h['hits'] != 1 else ''}")
    if h["scope"] != "books" and h["notes"]:
        parts.append(f"{h['notes']} note{'s' if h['notes'] != 1 else ''}")
    return ", ".join(parts) or "no results"


def search_scope(books: str | None, kb: str | None) -> tuple[bool, bool]:
    """What to search, from the Books and KB checkboxes: (books, kb).

    A link with neither (an old bookmark, a topic link) searches both, and so does
    unticking both, as at least one has to be searched.
    """
    want_books, want_kb = books == "1", kb == "1"
    if not want_books and not want_kb:
        return True, True
    return want_books, want_kb


def scope_params(want_books: bool, want_kb: bool) -> dict[str, str]:
    """The query string parameters for a search scope."""
    return {**({"books": "1"} if want_books else {}), **({"kb": "1"} if want_kb else {})}


def _static_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in sorted((HERE / "static").iterdir()):
        digest.update(path.name.encode() + path.read_bytes())
    return digest.hexdigest()[:10]


def _require_app_header(value: str | None) -> None:
    # Other websites can't add custom headers to requests sent to us without our
    # permission (CORS), so this stops them from starting OCR behind your back.
    if value != "1":
        raise HTTPException(403, "Missing X-Book-Searchie header.")


def _hit_view(hit, terms) -> dict:
    """A search hit plus where it is, ready for a template."""
    if hit.kind == "pdf":
        return {"hit": hit, "where": f"Page {hit.label}"}
    # Some EPUBs title chapters with a bare number, which would read like a page number.
    where = f"Section {hit.label}" if hit.label.isdigit() else hit.label
    if hit.text:
        spans = text_spans(hit.text, terms)
        if spans:
            pct = int(100 * spans[0][0] / max(len(hit.text), 1))
            where += f" · {pct}%" if pct >= 3 else " · start"
    return {"hit": hit, "where": where}
