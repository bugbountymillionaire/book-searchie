"""Optional tag suggestions from a local AI model, through Ollama's API.

Off unless [ai] enabled = true. Only the note being written and the names of your
existing tags are sent, and only to the configured endpoint, which has to be on this
computer unless the config explicitly allows otherwise. Suggestions are only ever
offered: the user picks which to add.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import Config
from .entries import ENTRY_TAG_KIND
from .notes import TAG_MAX, NotesStore

MAX_SUGGESTIONS = 8
MAX_NOTE_CHARS = 4000  # of the note's text sent to the model
MAX_EXISTING = 300  # existing tag names listed for the model, most used first
TIMEOUT = 120  # seconds: a model can be slow the first time it loads

_SYSTEM = """You suggest tags for a note in someone's personal knowledge base.
Each tag has a kind. The kinds are: {kinds}.
Prefer tags from the existing list when they fit; only suggest a new tag when none fits.
Suggest at most {most} tags, only ones clearly supported by the note. Tags are short (one to three words).
Reply with JSON only, in this form: {{"tags": [{{"kind": "<one of the kinds>", "name": "<tag>"}}]}}"""


class AiUnavailable(Exception):
    """The model couldn't be asked: switched off, not running, or not installed."""


@dataclass
class Suggestion:
    kind: str
    name: str
    existing: bool  # a tag already in the KB


def _existing_tags(store: NotesStore | None, kinds: list[str]) -> dict[str, list[str]]:
    existing: dict[str, list[str]] = {k: [] for k in kinds}
    if store is not None:
        for t in store.tags(limit=MAX_EXISTING):
            if t.kind in existing:
                existing[t.kind].append(t.name)
    return existing


def _prompt(title: str, quote: str, body: str, existing: dict[str, list[str]]) -> str:
    text = "\n\n".join(part for part in (
        f"Title: {title.strip()}" if title.strip() else "",
        f"Quote: {quote.strip()}" if quote.strip() else "",
        f"Notes: {body.strip()}" if body.strip() else "",
    ) if part)[:MAX_NOTE_CHARS]
    lists = "\n".join(f"- {kind}: {', '.join(names) if names else '(none yet)'}" for kind, names in existing.items())
    return f"Existing tags:\n{lists}\n\nThe note:\n{text}"


def _ask(config: Config, system: str, prompt: str) -> str:
    """Send one chat request to Ollama and return the reply's text."""
    request = urllib.request.Request(
        f"{config.ai_endpoint}/api/chat",
        data=json.dumps({
            "model": config.ai_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.2},
        }).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as res:
            reply = json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", "")
        except (ValueError, AttributeError):
            pass
        if e.code == 404:
            raise AiUnavailable(
                f"The model \"{config.ai_model}\" isn't installed. Run: ollama pull {config.ai_model}"
            ) from None
        raise AiUnavailable(f"The AI model gave an error ({e.code}{': ' + detail if detail else ''}).") from None
    except (urllib.error.URLError, ConnectionError) as e:
        if isinstance(getattr(e, "reason", None), (socket.timeout, TimeoutError)):
            raise AiUnavailable("The AI model took too long to answer. Try again in a moment.") from None
        raise AiUnavailable(
            f"Ollama isn't running at {config.ai_endpoint}. Start it, or turn suggestions off with "
            "[ai] enabled = false. Everything else works without it."
        ) from None
    except (TimeoutError, socket.timeout):
        raise AiUnavailable("The AI model took too long to answer. Try again in a moment.") from None
    except ValueError:
        raise AiUnavailable("The AI model's answer couldn't be read.") from None
    return (reply.get("message") or {}).get("content", "") if isinstance(reply, dict) else ""


def suggest_tags(
    config: Config,
    store: NotesStore | None,
    *,
    title: str = "",
    quote: str = "",
    body: str = "",
    current: list[tuple[str, str]] = (),
) -> list[Suggestion]:
    """Tags the model suggests for a note, existing tags first. Tags the note already has
    (`current`, as (kind, name)) aren't suggested again."""
    if not config.ai_enabled:
        raise AiUnavailable("AI tag suggestions are off. Turn them on with [ai] enabled = true in the config.")
    if not (title.strip() or quote.strip() or body.strip()):
        return []
    kinds = list(config.tag_kinds)
    existing = _existing_tags(store, kinds)
    content = _ask(config, _SYSTEM.format(kinds=", ".join(kinds), most=MAX_SUGGESTIONS), _prompt(title, quote, body, existing))
    try:
        data = json.loads(content)
    except ValueError:
        raise AiUnavailable("The AI model didn't answer in the expected form. Try again.") from None
    items = data.get("tags", []) if isinstance(data, dict) else data if isinstance(data, list) else []

    have = {(k.lower(), n.casefold()) for k, n in current}
    seen: set[tuple[str, str]] = set()
    suggestions: list[Suggestion] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "")).strip().lower()
        name = " ".join(str(item.get("name", "")).split())
        if kind not in kinds or not name or len(name) > TAG_MAX:
            continue
        main = store.resolve_tag(kind, name) if store is not None else None  # its own spelling, or an alias's tag
        name = main or name
        key = (kind, name.casefold())
        if key in seen or key in have:
            continue
        seen.add(key)
        suggestions.append(Suggestion(kind, name, main is not None))
    suggestions.sort(key=lambda s: not s.existing)  # tags you already use first
    return suggestions[:MAX_SUGGESTIONS]


# --- Entries ------------------------------------------------------------------------

_ENTRY_SYSTEM = """You suggest tags for an entry in someone's personal knowledge base, such as a plant.
Tags say what the entry is useful for or related to, such as conditions it helps with.
Prefer tags from the existing list when they fit; only suggest a new tag when none fits.
Suggest at most {most} tags, only ones clearly supported by the entry's text. Tags are short (one to three words).
Reply with JSON only, in this form: {{"tags": ["<tag>", "<tag>"]}}"""


def suggest_entry_tags(
    config: Config,
    store: NotesStore,
    *,
    title: str,
    text: str,
    current: list[str] = (),
) -> list[Suggestion]:
    """Tags the model suggests for an entry, from its title and text, tags already used in
    the KB first. Tags the entry already has aren't suggested again. Each Suggestion's kind
    is "tag", as entries have one plain list of tags."""
    if not config.ai_enabled:
        raise AiUnavailable("AI tag suggestions are off. Turn them on with [ai] enabled = true in the config.")
    if not text.strip():
        return []
    existing = [name for name, _, _ in sorted(store.entry_tag_counts(), key=lambda t: -t[1])[:MAX_EXISTING]]
    prompt = (f"Existing tags: {', '.join(existing) if existing else '(none yet)'}\n\n"
              f"The entry: {title.strip()}\n\n{text.strip()[:MAX_NOTE_CHARS]}")
    content = _ask(config, _ENTRY_SYSTEM.format(most=MAX_SUGGESTIONS), prompt)
    try:
        data = json.loads(content)
    except ValueError:
        raise AiUnavailable("The AI model didn't answer in the expected form. Try again.") from None
    items = data.get("tags", []) if isinstance(data, dict) else data if isinstance(data, list) else []

    have = {t.casefold() for t in current}
    seen: set[str] = set()
    suggestions: list[Suggestion] = []
    for item in items:
        name = item.get("name", "") if isinstance(item, dict) else item
        name = " ".join(str(name).split())
        if not name or len(name) > TAG_MAX:
            continue
        main = store.resolve_tag(ENTRY_TAG_KIND, name)  # its own spelling, or an alias's tag
        name = main or name
        if name.casefold() in seen or name.casefold() in have:
            continue
        seen.add(name.casefold())
        suggestions.append(Suggestion(ENTRY_TAG_KIND, name, main is not None))
    suggestions.sort(key=lambda s: not s.existing)
    return suggestions[:MAX_SUGGESTIONS]
