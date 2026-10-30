"""The book-searchie command line."""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path

from . import __version__
from .config import CONFIG_NAME, Config, ConfigError, load_config, render_config
from .db import Database, IndexUnavailable
from .indexer import run_index
from .ocr import OcrUnavailable, ocr_candidates, run_ocr
from .problems import PROBLEMS, detail, group_problems
from .search import QueryError, search


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

    p = sub.add_parser("serve", help="start the web interface")
    p.add_argument("-p", "--port", type=int, help="port to listen on (default: from config, normally 8004)")
    p.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    p.set_defaults(func=cmd_serve)

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
    # Used by the web UI, which runs OCR as a separate process and follows its progress.
    p.add_argument("--progress-json", action="store_true", help=argparse.SUPPRESS)
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
        f"Done: {report.added} added, {report.updated} updated, {report.moved} moved, {report.removed} removed, "
        f"{report.unchanged} unchanged. Index has {stats['books']} books."
    )
    if report.duplicates:
        print(
            f"{report.duplicates} book(s) are also in their folder in another format; only one copy of each "
            f"is searched. Run 'book-searchie report' to see which."
        )
    groups = group_problems(problems)
    if groups:
        parts = [f"{len(rows)} {PROBLEMS[kind].summary}" for kind, rows in groups.items()]
        print(f"{len(problems)} book(s) can't be searched: {'; '.join(parts)}.")
        print("Run 'book-searchie report' for the list and how to fix each.")
    return 0


def cmd_serve(args) -> int:
    from .web.server import serve  # the web dependencies are only needed here

    return serve(load_config(args.config), port=args.port, open_browser=not args.no_browser)


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
        duplicates = db.duplicate_files()
    wrap = textwrap.TextWrapper(width=100, initial_indent="  ", subsequent_indent="  ", break_on_hyphens=False)
    if not problems:
        print("Every indexed book is searchable.\n")
    for kind, rows in group_problems(problems).items():
        problem = PROBLEMS[kind]
        print(f"{problem.title}: {len(rows)}")
        for row in rows:
            extra = detail(kind, row)
            print(f"  {row['rel']}" + (f"  ({extra})" if extra else ""))
        print(wrap.fill(f"{problem.explanation} Fix: {problem.fix}") + "\n")
    if duplicates:
        print(f"Not searched, as the same book is in the folder in another format: {len(duplicates)}")
        for row in duplicates:
            print(f"  {row['rel']}")
        print(wrap.fill(
            f"Only one copy of each book is searched, so results aren't doubled up: the {config.prefer.upper()}, "
            "unless it can't be searched, when another copy is used instead. Change the preferred format "
            "with 'prefer' in the [library] section of the config."
        ) + "\n")
    return 0


def cmd_ocr(args) -> int:
    config = load_config(args.config)
    as_json = args.progress_json

    def emit(event: dict) -> None:
        if as_json:
            print(json.dumps(event), flush=True)
        elif event["type"] == "book":
            print(f"[{event['index']}/{event['total']}] {event['rel']} ({event['pages']} pages)", flush=True)

    with _open_index(config) as db:
        try:
            rows = ocr_candidates(db, args.books)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        if not rows:
            if not as_json:
                print("No scanned books are waiting for OCR.")
            return 0
        if not as_json:
            pages = sum(r["units"] for r in rows)
            print(
                f"OCR {len(rows)} book(s), {pages} pages. At roughly 1-3 seconds per page this can take a while.\n"
                "Ctrl+C stops at any point; finished books are kept and the next run carries on.\n"
            )
        try:
            report = run_ocr(config, rows, language=args.language, events=emit, page_progress=as_json)
        finally:
            # Index whatever was finished, even after Ctrl+C or a missing dependency.
            run_index(config, db)
        still = {r["rel"] for r in db.problem_files()}

    empty = [rel for rel in report.done if rel in still]
    if as_json:
        emit({"type": "finished", "searchable": len(report.done) - len(empty), "empty": empty})
        return 1 if report.failed else 0
    print(f"\nDone: {len(report.done) - len(empty)} of {len(rows)} book(s) are now searchable.")
    for rel in empty:
        print(f"  OCR found no readable text in: {rel}")
    for rel, reason in report.failed:
        print(f"  Failed: {rel}: {reason}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
