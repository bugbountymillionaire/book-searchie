"""The book-searchie command line."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import CONFIG_NAME, Config, ConfigError, load_config, render_config
from .db import Database, IndexUnavailable
from .indexer import run_index
from .search import QueryError, search

_STATUS_TEXT = {"no_text": "no text layer (scanned? try ocrmypdf)", "error": "could not be read"}


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
    except (ConfigError, IndexUnavailable, QueryError) as e:
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

    p = sub.add_parser("report", help="list books that couldn't be searched (scanned, damaged, DRM)")
    p.set_defaults(func=cmd_report)
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
    if problems:
        print(f"{len(problems)} book(s) can't be searched; run 'book-searchie report' to see them.")
    return 0


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
    for row in problems:
        reason = row["message"] if row["status"] == "error" else _STATUS_TEXT[row["status"]]
        print(f"{row['rel']}: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
