"""The web interface: search, results grouped by book, page viewer, and the "Can't search" page."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Body, FastAPI, Header, HTTPException, Request
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
from . import render
from .jobs import OcrBusy, OcrJob

HERE = Path(__file__).parent
BOOKS_PER_PAGE = 10
MAX_HITS = 1000
# Private-use characters mark matches in snippets, so they survive HTML escaping.
MARK_OPEN, MARK_CLOSE = "", ""
_HEX64 = re.compile(r"[0-9a-f]{64}")


def marked(snippet: str) -> Markup:
    return Markup(str(escape(snippet)).replace(MARK_OPEN, "<mark>").replace(MARK_CLOSE, "</mark>"))


LOCAL_HOSTS = ["localhost", "127.0.0.1", "[::1]"]


def create_app(config: Config, *, allowed_hosts: list[str] = LOCAL_HOSTS) -> FastAPI:
    app = FastAPI(title="Book-Searchie", docs_url=None, redoc_url=None, openapi_url=None)
    # Local mode has no login, so only answer requests addressed to this computer. This
    # also blocks "DNS rebinding", where a web page tricks the browser into calling us.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["marked"] = marked
    templates.env.globals["version"] = __version__
    job = OcrJob(config)

    def open_db() -> Database:
        return Database.open(config.index_path, create=False)

    def page(request: Request, name: str, db: Database, **context) -> HTMLResponse:
        context.setdefault("q", "")
        context.setdefault("topic", "")
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

    # --- Search ------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, q: str = "", topic: str = "", page_no: int = 1):
        q = q.strip()
        with open_db() as db:
            if not q:
                return page(request, "search.html", db, topic=topic, stats=db.stats())
            try:
                hits = search(db, q, topic=topic or None, limit=MAX_HITS, with_snippets=False)
            except QueryError as e:
                return page(request, "search.html", db, q=q, topic=topic, error=str(e))
            books: dict[int, dict] = {}
            for hit in hits:
                books.setdefault(hit.file_id, {"hit": hit, "hits": []})["hits"].append(hit)
            book_list = list(books.values())
            pages = max(1, -(-len(book_list) // BOOKS_PER_PAGE))
            page_no = min(max(1, page_no), pages)
            shown = book_list[(page_no - 1) * BOOKS_PER_PAGE : page_no * BOOKS_PER_PAGE]
            # Only the first few hits of each book on this page are displayed; snippet just those.
            fill_snippets(db, q, [h for b in shown for h in b["hits"][:3]], mark=(MARK_OPEN, MARK_CLOSE))
            terms = query_terms(q)
            shown = [{**b, "hits": [_hit_view(h, terms) for h in b["hits"][:3]], "count": len(b["hits"])} for b in shown]
            return page(
                request, "search.html", db, q=q, topic=topic, books=shown, book_count=len(book_list),
                hit_count=len(hits), capped=len(hits) == MAX_HITS, page_no=page_no, pages=pages,
                page_url=lambda n: "/?" + urlencode({"q": q, "topic": topic, "page_no": n}),
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
            nav = {
                "prev_match": max((s for s in matches if s < seq), default=None),
                "next_match": min((s for s in matches if s > seq), default=None),
                "match_number": matches.index(seq) + 1 if seq in matches else None,
                "match_total": len(matches),
            }
            qs = "?" + urlencode({"q": q}) if q else ""
            if row["kind"] == "pdf":
                try:
                    page_count, label = render.pdf_page_count_and_label(path, seq)
                except IndexError:
                    raise HTTPException(404, "That page doesn't exist in this book.") from None
                return page(
                    request, "view_pdf.html", db, q=q, book=row, seq=seq, label=label, page_count=page_count,
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
    def page_image(file_id: int, seq: int, q: str = "", dpi: int = 150):
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
