"""Loading and creating the book-searchie.toml config file."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = "book-searchie.toml"
ENV_PREFIX = "BOOK_SEARCHIE_"
MODES = ("local", "network")
DEFAULT_TAG_KINDS = ["subject", "use"]
_TAG_KIND = re.compile(r"[a-z][a-z0-9_-]*")


class ConfigError(Exception):
    """The config is missing or invalid."""


@dataclass
class Config:
    roots: list[Path]
    extensions: list[str] = field(default_factory=lambda: ["pdf", "epub"])
    exclude: list[str] = field(default_factory=list)
    # When a folder has the same book in several formats ("Herbs.pdf" and "Herbs.epub"),
    # only this format is searched, unless it can't be, when the next one is used.
    prefer: str = "epub"
    index_path: Path = Path("book-searchie.db")
    # Your notes (the KB). Unlike the index, this is your own work, so back it up.
    notes_path: Path = Path("book-searchie-notes.db")
    tag_kinds: list[str] = field(default_factory=lambda: list(DEFAULT_TAG_KINDS))
    mode: str = "local"
    host: str = "127.0.0.1"
    port: int = 8004
    source: Path | None = None  # the config file this was loaded from, if any


def _split_list(value: str, sep: str) -> list[str]:
    return [part.strip() for part in value.split(sep) if part.strip()]


# Environment variables that override config file settings.
# Roots are separated like PATH (";" on Windows, ":" elsewhere); other lists by commas.
_ENV = {
    "ROOTS": ("library", "roots", lambda v: _split_list(v, os.pathsep)),
    "EXTENSIONS": ("library", "extensions", lambda v: _split_list(v, ",")),
    "EXCLUDE": ("library", "exclude", lambda v: _split_list(v, ",")),
    "PREFER": ("library", "prefer", str),
    "INDEX": ("index", "path", str),
    "NOTES": ("notes", "path", str),
    "MODE": ("server", "mode", str),
    "HOST": ("server", "host", str),
    "PORT": ("server", "port", str),
}


def find_config(explicit: str | os.PathLike | None = None) -> Path | None:
    """Return the config file to use: --config, then $BOOK_SEARCHIE_CONFIG, then ./book-searchie.toml."""
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise ConfigError(f"Config file not found: {path}")
        return path
    env = os.environ.get(ENV_PREFIX + "CONFIG")
    if env:
        path = Path(env)
        if not path.is_file():
            raise ConfigError(f"Config file not found: {path} (from {ENV_PREFIX}CONFIG)")
        return path
    path = Path.cwd() / CONFIG_NAME
    return path if path.is_file() else None


def load_config(explicit: str | os.PathLike | None = None) -> Config:
    path = find_config(explicit)
    data: dict = {}
    if path is not None:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path}: {e}") from None

    for name, (section, key, convert) in _ENV.items():
        value = os.environ.get(ENV_PREFIX + name)
        if value is not None:
            data.setdefault(section, {})[key] = convert(value)

    # Relative paths in the config file are relative to the file, not the working directory.
    base = path.parent if path is not None else Path.cwd()
    return _build(data, base, path)


def _build(data: dict, base: Path, source: Path | None) -> Config:
    library = data.get("library", {})
    index = data.get("index", {})
    server = data.get("server", {})
    notes = data.get("notes", {})

    roots = library.get("roots")
    if not roots:
        if source is None:
            raise ConfigError(
                f"No config found. Run 'book-searchie init' to create {CONFIG_NAME}, "
                f"or set {ENV_PREFIX}ROOTS."
            )
        raise ConfigError(f"{source}: [library] roots is empty; add at least one library folder.")
    if isinstance(roots, str):
        roots = [roots]

    extensions = [e.lower().lstrip(".") for e in library.get("extensions", ["pdf", "epub"])]
    unsupported = set(extensions) - {"pdf", "epub"}
    if unsupported:
        raise ConfigError(f"Unsupported extensions: {', '.join(sorted(unsupported))} (only pdf and epub)")

    prefer = str(library.get("prefer", "epub")).lower().lstrip(".")
    if prefer not in ("pdf", "epub"):
        raise ConfigError(f"[library] prefer must be \"pdf\" or \"epub\", not {prefer!r}")

    tag_kinds = notes.get("tag_kinds", DEFAULT_TAG_KINDS)
    if isinstance(tag_kinds, str):
        tag_kinds = [tag_kinds]
    tag_kinds = [str(k).strip().lower() for k in tag_kinds if str(k).strip()]
    if not tag_kinds:
        raise ConfigError("[notes] tag_kinds needs at least one kind of tag, e.g. [\"subject\"]")
    bad = [k for k in tag_kinds if not _TAG_KIND.fullmatch(k)]
    if bad:
        raise ConfigError(f"[notes] tag kinds must be single words (letters, digits, - or _), not {bad[0]!r}")
    if len(set(tag_kinds)) != len(tag_kinds):
        raise ConfigError("[notes] tag_kinds lists the same kind twice")

    mode = server.get("mode", "local")
    if mode not in MODES:
        raise ConfigError(f"[server] mode must be one of {', '.join(MODES)}, not {mode!r}")
    try:
        port = int(server.get("port", 8004))
    except ValueError:
        raise ConfigError(f"[server] port must be a number, not {server.get('port')!r}") from None

    return Config(
        roots=[(base / r).resolve() for r in roots],
        extensions=extensions,
        exclude=list(library.get("exclude", [])),
        prefer=prefer,
        index_path=(base / index.get("path", "book-searchie.db")).resolve(),
        notes_path=(base / notes.get("path", "book-searchie-notes.db")).resolve(),
        tag_kinds=tag_kinds,
        mode=mode,
        host=server.get("host", "0.0.0.0" if mode == "network" else "127.0.0.1"),
        port=port,
        source=source,
    )


def _toml_string(value: str) -> str:
    # TOML basic strings use JSON-compatible escapes.
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_config(roots: list[str]) -> str:
    """The contents of a new book-searchie.toml."""
    roots_toml = ", ".join(_toml_string(Path(r).as_posix()) for r in roots)
    return f"""\
[library]
roots = [{roots_toml}]          # one or more library folders
extensions = ["pdf", "epub"]
exclude = []                      # extra folders or glob patterns to skip, e.g. ["Archive", "**/drafts"]
                                  # (folders starting with "_" are always skipped)
prefer = "epub"                   # same book as PDF and EPUB in one folder: search only this one

[index]
path = "book-searchie.db"         # SQLite index file, relative to this config file

[notes]
path = "book-searchie-notes.db"   # your KB notes: your own work, so back this file up
tag_kinds = ["subject", "use"]    # the kinds of tag you give notes, e.g. ["plant", "condition"]

[server]
mode = "local"                    # only "local" for now
host = "127.0.0.1"
port = 8004
"""
