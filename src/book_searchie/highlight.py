"""Finding a query's terms in text, for highlighting on PDF pages and in EPUB chapters.

This mirrors how the index matches words (case- and accent-insensitive, split on
punctuation), so what gets highlighted is what the search found.
"""

from __future__ import annotations

import re
import unicodedata

from .search import _TOKEN

_WORD = re.compile(r"\w+")

# A term is a sequence of words (one for a plain word, several for a phrase) and
# whether its last word is a prefix (root* matches "roots").
Term = tuple[tuple[str, ...], bool]


def fold(word: str) -> str:
    """Lower-case and strip accents, the way the index does ("Café" -> "cafe", "ﬁ" -> "fi")."""
    word = unicodedata.normalize("NFKD", word.casefold())
    return "".join(c for c in word if not unicodedata.combining(c))


def query_terms(query: str) -> list[Term]:
    """The terms of a search worth highlighting: everything except operators and NOT-ed terms."""
    tokens = _TOKEN.findall(query)
    terms: list[Term] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if tok == "NOT":
            # Skip the excluded word, phrase or bracketed group.
            if i < len(tokens) and tokens[i] == "(":
                depth = 0
                while i < len(tokens):
                    depth += {"(": 1, ")": -1}.get(tokens[i], 0)
                    i += 1
                    if depth == 0:
                        break
            else:
                i += 1
            continue
        if tok in ("AND", "OR", "(", ")"):
            continue
        prefix = tok.endswith("*")
        words = tuple(fold(w) for w in _WORD.findall(tok.rstrip("*")))
        if words:
            terms.append((words, prefix))
    return terms


def find_spans(tokens: list[str], terms: list[Term]) -> list[tuple[int, int]]:
    """Index ranges [start, end) of folded tokens that match any term."""
    spans = []
    for i in range(len(tokens)):
        for words, prefix in terms:
            n = len(words)
            if i + n > len(tokens):
                continue
            if all(tokens[i + k] == words[k] for k in range(n - 1)):
                last = tokens[i + n - 1]
                if last.startswith(words[-1]) if prefix else last == words[-1]:
                    spans.append((i, i + n))
                    break
    return spans


def text_spans(text: str, terms: list[Term]) -> list[tuple[int, int]]:
    """Character ranges in text that match any term, merged where they overlap."""
    matches = list(_WORD.finditer(text))
    spans = find_spans([fold(m.group()) for m in matches], terms)
    merged: list[tuple[int, int]] = []
    for start, end in sorted((matches[a].start(), matches[b - 1].end()) for a, b in spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged
