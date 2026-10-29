"""The book-searchie command line."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import CONFIG_NAME, Config, ConfigError, load_config, render_config
from .db import Database, IndexUnavailable
from .indexer import run_index
from .ocr import OcrUnavailable, ocr_candidates, run_ocr
from .search import QueryError, search

# What each problem means and how to fix it, in report order.
_PROBLEMS = {
    "no_text": (
        "Scanned books (pages are pictures, with no text to search)",
        "Fix: run 'book-searchie ocr' to read the text from the page images. Your files aren't changed;\n"
        "an OCR'd copy is kept next to the index. It takes roughly 1-3 seconds per page.",
    ),
    "ocr_empty": (
        "Scanned books where OCR found no readable text",
        "These may be photos, handwriting or very poor scans. Try a better copy of the book, or OCR\n"
        "again in the right language: book-searchie ocr --language deu \"Topic/Book.pdf\"",
    ),
    "password": (
        "Password-protected PDFs (need a password to open)",
        "Fix: remove the password once and replace the file, e.g. open it, enter the password and\n"
        "'Print to PDF', or: qpdf --decrypt --password=PASSWORD locked.pdf unlocked.pdf",
    ),
    "drm": (
        "DRM-protected books (locked to a shop's reading app)",
        "The text is encrypted so only the seller's app can open it, and Book-Searchie can't read it.\n"
        "Fix: get a DRM-free copy. Many publishers and shops sell them, and older books are often free\n"
        "from Project Gutenberg or Standard Ebooks. Meanwhile, use the reading app's own search.",
    ),
    "error": (
        "Damaged or unreadable files",
        "Fix: re-download or replace the file. The reason is shown next to each one.",
    ),
}


def _problem_kind(row) -> str:
    if row["status"] == "no_text":
        if row["ocr"]:
            return "ocr_empty"
        if row["kind"] != "pdf":
            return "error"  # an EPUB with no text; OCR can't help
    return row["status"]


def _group_problems(rows) -> dict[str, list]:
    groups: dict[str, list] = {kind: [] for kind in _PROBLEMS}
    for row in rows:
        groups[_problem_kind(row)].append(row)
    return {kind: rows for kind, rows in groups.items() if rows}


def main(argv: list[str] | None = None) -> int:
    # Book text can contain characters the Windows console can't show; don't crash on them.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    parser = _build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    try:
        return args.func(args) or 0
    except (ConfigError, IndexUnavailable, QueryError, OcrUnavailable) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted. Books indexed so far are saved; run the command again to continue.", file=sys.stderr)
        return 130


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="book-searchie",
        description="Turn a folder of PDFs and EPUBs into a searchable knowledge base.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", metavar="FILE", help=f"config file (default: ./{CONFIG_NAME})")
    sub = parser.add_subparsers(metavar="command")

    p = sub.add_parser("init", help=f"create {CONFIG_NAME} in the current folder")
    p.add_argument("--root", action="append", metavar="FOLDER", help="library folder (repeat for several)")
    p.add_argument("--force", action="store_true", help="overwrite an existing config file")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("index", help="build or update the search index")
    p.add_argument("--full", action="store_true", help="re-read every book instead of only new and changed ones")
    p.add_argument("-q", "--quiet", action="store_true", help="only print the summary")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("search", help="search the index from the command line")
    p.add_argument("query", nargs="+", help='search terms, e.g. "black cohosh" AND root*')
    p.add_argument("-t", "--topic", help="only search this topic")
    p.add_argument("-n", "--limit", type=int, default=20, help="maximum results (default 20)")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("topics", help="list topics and how many books each has")
    p.set_defaults(func=cmd_topics)

    p = sub.add_parser("report", help="list books that can't be searched, with how to fix each")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser(
        "ocr",
        help="make scanned books searchable (needs the OCR extras)",
        description="Read the text from scanned PDFs with OCRmyPDF. Originals are never changed: "
        "OCR'd copies are kept next to the index and used for searching.",
    )
    p.add_argument("books", nargs="*", help="books to OCR, as listed by 'report' (default: all scanned books)")
    p.add_argument("-l", "--language", default="eng", help="Tesseract language code(s), e.g. eng or eng+fra (default eng)")
    p.set_defaults(func=cmd_ocr)
    return parser


def _open_index(config: Config) -> Database:
    return Database.open(config.index_path, create=False)


def cmd_init(args) -> int:
    path = Path(args.config) if args.config else Path.cwd() / CONFIG_NAME
    if path.exists() and not args.force:
        print(f"{path} already exists. Use --force to replace it.", file=sys.stderr)
        return 1
    roots = args.root
    if not roots:
        answer = input("Where is your library? (folder path; separate several with commas): ").strip()
        roots = [r.strip().strip('"') for r in answer.split(",") if r.strip()]
    if not roots:
        print("No library folder given; nothing written.", file=sys.stderr)
        return 1
    for root in roots:
        if not Path(root).is_dir():
            print(f"warning: {root} is not a folder (yet)", file=sys.stderr)
    path.write_text(render_config(roots), encoding="utf-8")
    print(f"Wrote {path}")
    print("Next: run 'book-searchie index' to build the search index.")
    return 0


def cmd_index(args) -> int:
    config = load_config(args.config)
    progress = (lambda msg: None) if args.quiet else (lambda msg: print(msg, flush=True))
    with Database.open(config.index_path) as db:
        report = run_index(config, db, full=args.full, progress=progress)
        stats = db.stats()
        problems = db.problem_files()

    for root in report.missing_roots:
        print(f"warning: library folder not found, skipped: {root}", file=sys.stderr)
    print(
        f"Done: {report.added} added, {report.updated} updated, {report.removed} removed, "
        f"{report.unchanged} unchanged. Index has {stats['books']} books."
    )
    _print_problem_summary(problems)
    return 0


def _print_problem_summary(problems) -> None:
    groups = _group_problems(problems)
    if not groups:
        return
    counts = {
        "no_text": "scanned (fix: book-searchie ocr)",
        "ocr_empty": "scanned, OCR found no text",
        "password": "password-protected",
        "drm": "DRM-protected",
        "error": "damaged or unreadable",
    }
    parts = [f"{len(rows)} {counts[kind]}" for kind, rows in groups.items()]
    print(f"{len(problems)} book(s) can't be searched: {'; '.join(parts)}.")
    print("Run 'book-searchie report' for the list and how to fix each.")


def cmd_search(args) -> int:
    config = load_config(args.config)
    query = " ".join(args.query)
    color = sys.stdout.isatty()
    mark = ("\x1b[1;33m", "\x1b[0m") if color else ("[", "]")
    with _open_index(config) as db:
        hits = search(db, query, topic=args.topic, limit=args.limit, mark=mark)
    if not hits:
        print("No results.")
        return 0
    for hit in hits:
        where = f"p. {hit.label}" if hit.kind == "pdf" else hit.label
        heading = f"{hit.topic} · {hit.title} · {where}"
        print(f"\x1b[1m{heading}\x1b[0m" if color else heading)
        print(f"    {hit.snippet}\n")
    more = " (use --limit for more)" if len(hits) == args.limit else ""
    print(f"{len(hits)} result(s){more}.")
    return 0


def cmd_topics(args) -> int:
    config = load_config(args.config)
    with _open_index(config) as db:
        topics = db.topics()
    if not topics:
        print("The index is empty. Run 'book-searchie index'.")
    for row in topics:
        print(f"{row['books']:>6}  {row['topic']}")
    return 0


def cmd_report(args) -> int:
    config = load_config(args.config)
    with _open_index(config) as db:
        problems = db.problem_files()
    if not problems:
        print("Every indexed book is searchable.")
        return 0
    for kind, rows in _group_problems(problems).items():
        title, fix = _PROBLEMS[kind]
        print(f"{title}: {len(rows)}")
        for row in rows:
            detail = ""
            if kind == "error":
                detail = f"  ({row['message'] or 'no text found'})"
            elif kind == "drm" and row["message"] != "DRM protected":
                detail = f"  ({row['message'].removeprefix('DRM protected (').rstrip(')')})"
            elif kind in ("no_text", "ocr_empty"):
                detail = f"  ({row['units']} pages)"
            print(f"  {row['rel']}{detail}")
        print("  " + fix.replace("\n", "\n  ") + "\n")
    return 0


def cmd_ocr(args) -> int:
    config = load_config(args.config)
    with _open_index(config) as db:
        try:
            rows = ocr_candidates(db, args.books)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        if not rows:
            print("No scanned books are waiting for OCR.")
            return 0
        pages = sum(r["units"] for r in rows)
        print(
            f"OCR {len(rows)} book(s), {pages} pages. At roughly 1-3 seconds per page this can take a while.\n"
            "Ctrl+C stops at any point; finished books are kept and the next run carries on.\n"
        )
        try:
            report = run_ocr(config, rows, language=args.language, progress=lambda m: print(m, flush=True))
        finally:
            # Index whatever was finished, even after Ctrl+C or a missing dependency.
            run_index(config, db)
        still = {r["rel"] for r in db.problem_files()}

    searchable = [rel for rel in report.done if rel not in still]
    print(f"\nDone: {len(searchable)} of {len(rows)} book(s) are now searchable.")
    for rel in report.done:
        if rel in still:
            print(f"  OCR found no readable text in: {rel}")
    for rel, reason in report.failed:
        print(f"  Failed: {rel}: {reason}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
