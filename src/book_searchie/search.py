"""Running full-text queries against the index."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from .db import Database

_TOKEN = re.compile(r'"[^"]*"\*?|[()]|[^\s()"]+')
_OPERATORS = {"AND", "OR", "NOT"}
_BAREWORD = re.compile(r"\w+")


class QueryError(ValueError):
    """The search query can't be run."""


@dataclass
class Hit:
    section_id: int
    file_id: int
    topic: str
    title: str
    path: str
    kind: str
    seq: int
    label: str
    href: str | None
    snippet: str
    score: float
    text: str | None = None  # EPUB chapter text, to work out how far into the chapter the match is
    ocr: bool = False


def prepare_query(query: str) -> str:
    """Turn user input into a safe FTS5 query.

    Phrases ("..."), AND / OR / NOT, parentheses and prefix* pass through. Words
    with punctuation in them (st-john's, 1.5, c:\\) become phrases, so they search
    as the user expects instead of being an FTS5 syntax error.
    """
    parts = []
    for tok in _TOKEN.findall(query):
        if tok.startswith('"') or tok in ("(", ")") or tok in _OPERATORS:
            parts.append(tok)
            continue
        star = tok.endswith("*")
        word = tok.rstrip("*")
        if not word:
            continue
        if _BAREWORD.fullmatch(word):
            parts.append(word + ("*" if star else ""))
        else:
            parts.append(f'"{word}"' + ("*" if star else ""))
    if not any(p not in _OPERATORS and p not in ("(", ")") for p in parts):
        raise QueryError("Enter something to search for.")
    return " ".join(parts)


def search(
    db: Database,
    query: str,
    *,
    topic: str | None = None,
    file_id: int | None = None,
    limit: int = 20,
    offset: int = 0,
    mark: tuple[str, str] = ("[", "]"),
    in_book_order: bool = False,
    with_epub_text: bool = False,
    with_snippets: bool = True,
) -> list[Hit]:
    """Best matches first, or in page order with in_book_order (useful with file_id).

    Snippets are the slow part; with_snippets=False skips them so they can be
    added later, with fill_snippets, for just the hits on screen.
    """
    snippet = "snippet(sections_fts, 0, ?, ?, '…', 24)" if with_snippets else "''"
    sql = f"""
        SELECT s.id AS section_id, f.id AS file_id, f.topic, f.title, f.path, f.kind, s.seq, s.label, s.href,
               {snippet} AS snippet,
               bm25(sections_fts) AS score,
               {"CASE WHEN f.kind = 'epub' THEN s.text END" if with_epub_text else "NULL"} AS text,
               f.ocr
        FROM sections_fts
        JOIN sections s ON s.id = sections_fts.rowid
        JOIN files f ON f.id = s.file_id
        WHERE sections_fts MATCH ?
    """
    params: list = ([mark[0], mark[1]] if with_snippets else []) + [prepare_query(query)]
    if topic:
        sql += " AND f.topic = ? COLLATE NOCASE"
        params.append(topic)
    if file_id is not None:
        sql += " AND s.file_id = ?"
        params.append(file_id)
    sql += " ORDER BY " + ("s.seq" if in_book_order else "score") + " LIMIT ? OFFSET ?"
    params += [limit, offset]
    return [Hit(**dict(r)) for r in _run(db, sql, params)]


def fill_snippets(db: Database, query: str, hits: list[Hit], *, mark: tuple[str, str] = ("[", "]")) -> None:
    """Add snippets (and EPUB chapter text) to hits fetched with with_snippets=False."""
    if not hits:
        return
    by_id = {h.section_id: h for h in hits}
    marks = ",".join("?" * len(by_id))
    sql = f"""
        SELECT s.id, snippet(sections_fts, 0, ?, ?, '…', 24) AS snippet,
               CASE WHEN f.kind = 'epub' THEN s.text END AS text
        FROM sections_fts
        JOIN sections s ON s.id = sections_fts.rowid
        JOIN files f ON f.id = s.file_id
        WHERE sections_fts MATCH ? AND sections_fts.rowid IN ({marks})
    """
    for row in _run(db, sql, [mark[0], mark[1], prepare_query(query), *by_id]):
        hit = by_id[row["id"]]
        hit.snippet = row["snippet"]
        hit.text = row["text"]


def matching_seqs(db: Database, query: str, file_id: int) -> list[int]:
    """Page indexes (PDF) or spine positions (EPUB) in one book that match, in order."""
    sql = """
        SELECT s.seq FROM sections_fts
        JOIN sections s ON s.id = sections_fts.rowid
        WHERE sections_fts MATCH ? AND s.file_id = ?
        ORDER BY s.seq
    """
    return [r[0] for r in _run(db, sql, [prepare_query(query), file_id])]


def _run(db: Database, sql: str, params: list) -> list[sqlite3.Row]:
    try:
        return db.conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        raise QueryError(f"Can't understand that search ({e}). Check quotes, brackets and AND/OR/NOT.") from None
