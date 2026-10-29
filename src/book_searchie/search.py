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
    topic: str
    title: str
    path: str
    kind: str
    seq: int
    label: str
    href: str | None
    snippet: str
    score: float


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
    limit: int = 20,
    offset: int = 0,
    mark: tuple[str, str] = ("[", "]"),
) -> list[Hit]:
    sql = """
        SELECT f.topic, f.title, f.path, f.kind, s.seq, s.label, s.href,
               snippet(sections_fts, 0, ?, ?, '…', 24) AS snippet,
               bm25(sections_fts) AS score
        FROM sections_fts
        JOIN sections s ON s.id = sections_fts.rowid
        JOIN files f ON f.id = s.file_id
        WHERE sections_fts MATCH ?
    """
    params: list = [mark[0], mark[1], prepare_query(query)]
    if topic:
        sql += " AND f.topic = ? COLLATE NOCASE"
        params.append(topic)
    sql += " ORDER BY score LIMIT ? OFFSET ?"
    params += [limit, offset]
    try:
        rows = db.conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        raise QueryError(f"Can't understand that search ({e}). Check quotes, brackets and AND/OR/NOT.") from None
    return [Hit(**dict(r)) for r in rows]
