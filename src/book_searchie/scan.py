"""Walking library roots to find books, applying topic and exclusion rules."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

# Topic for books placed directly in a library root rather than in a topic folder.
ROOT_TOPIC = "General"


@dataclass(frozen=True)
class LibraryFile:
    path: Path  # absolute
    root: Path
    rel: str  # path relative to the root, with forward slashes
    topic: str
    kind: str  # "pdf" or "epub"
    size: int
    mtime: float


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """Turn an exclude glob into a regex matched against root-relative paths.

    A pattern without a slash matches a name at any depth ("Archive" is the same
    as "**/Archive"). "*" and "?" stay within one path segment; "**" crosses them.
    Matching is case-insensitive.
    """
    p = pattern.strip().replace("\\", "/").strip("/")
    if "/" not in p:
        p = "**/" + p
    out = []
    i = 0
    while i < len(p):
        if p.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif p.startswith("**", i):
            out.append(".*")
            i += 2
        elif p[i] == "*":
            out.append("[^/]*")
            i += 1
        elif p[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(p[i]))
            i += 1
    return re.compile("".join(out), re.IGNORECASE)


class Excluder:
    def __init__(self, patterns: Iterable[str]):
        self._patterns = [compile_pattern(p) for p in patterns if p.strip()]

    def skip_dir(self, name: str, rel: str) -> bool:
        return name.startswith("_") or self._matches(rel)

    def skip_file(self, rel: str) -> bool:
        return self._matches(rel)

    def _matches(self, rel: str) -> bool:
        return any(p.fullmatch(rel) for p in self._patterns)


def scan_library(roots: Iterable[Path], extensions: Iterable[str], exclude: Iterable[str]) -> Iterator[LibraryFile]:
    """Yield every book under the roots, skipping excluded folders and files."""
    wanted = {"." + e.lower() for e in extensions}
    excluder = Excluder(exclude)
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            here = Path(dirpath)
            rel_dir = here.relative_to(root).as_posix()
            rel_dir = "" if rel_dir == "." else rel_dir + "/"
            # Prune in place so os.walk never descends into excluded folders.
            dirnames[:] = sorted(d for d in dirnames if not excluder.skip_dir(d, rel_dir + d))
            for name in sorted(filenames):
                suffix = os.path.splitext(name)[1].lower()
                if suffix not in wanted:
                    continue
                rel = rel_dir + name
                if excluder.skip_file(rel):
                    continue
                path = here / name
                try:
                    st = path.stat()
                except OSError:
                    continue
                topic = rel.split("/", 1)[0] if "/" in rel else ROOT_TOPIC
                yield LibraryFile(path, root, rel, topic, suffix[1:], st.st_size, st.st_mtime)
