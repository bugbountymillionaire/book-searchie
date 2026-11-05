"""A small, safe Markdown subset for KB notes.

Everything is HTML-escaped first; only these are then turned into HTML:
paragraphs (blank lines between), line breaks, bullet lists ("- " or "* "),
numbered lists ("1. " or "1) "), **bold**, *italic* or _italic_, and
[links](https://...) to http(s) addresses. Anything else shows as typed.
"""

from __future__ import annotations

import re

from markupsafe import Markup, escape

_BULLET = re.compile(r"^\s*[-*]\s+(.*)$")
_NUMBER = re.compile(r"^\s*\d{1,3}[.)]\s+(.*)$")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s()<>]+)\)")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_ITALIC_STAR = re.compile(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])")
_ITALIC_UNDERSCORE = re.compile(r"(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])")
# Private-use characters stand in for links while bold and italics are applied.
_OPEN, _CLOSE = "", ""


def _emphasis(html: str) -> str:
    html = _BOLD.sub(r"<strong>\1</strong>", html)
    html = _ITALIC_STAR.sub(r"<em>\1</em>", html)
    return _ITALIC_UNDERSCORE.sub(r"<em>\1</em>", html)


def _inline(text: str) -> str:
    """One line or paragraph of text as HTML: escaped, with bold, italic and links."""
    links: list[str] = []

    def keep_link(m: re.Match) -> str:
        # Links are swapped out first, so * and _ in web addresses aren't taken as italics.
        links.append(f'<a href="{m.group(2)}" rel="noopener noreferrer" target="_blank">{_emphasis(m.group(1))}</a>')
        return f"{_OPEN}{len(links) - 1}{_CLOSE}"

    html = str(escape(text.replace(_OPEN, "").replace(_CLOSE, "")))
    html = _emphasis(_LINK.sub(keep_link, html))
    for i, link in enumerate(links):
        html = html.replace(f"{_OPEN}{i}{_CLOSE}", link)
    return html


def render(text: str) -> Markup:
    """Note text as safe HTML."""
    out: list[str] = []
    para: list[str] = []
    items: list[str] = []
    list_tag = ""

    def end_para() -> None:
        if para:
            out.append("<p>" + "<br>".join(_inline(line) for line in para) + "</p>")
            para.clear()

    def end_list() -> None:
        nonlocal list_tag
        if items:
            out.append(f"<{list_tag}>" + "".join(f"<li>{_inline(i)}</li>" for i in items) + f"</{list_tag}>")
            items.clear()
        list_tag = ""

    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        bullet, number = _BULLET.match(line), _NUMBER.match(line)
        if not line.strip():
            end_para()
            end_list()
        elif bullet or number:
            end_para()
            tag = "ul" if bullet else "ol"
            if list_tag != tag:
                end_list()
                list_tag = tag
            items.append((bullet or number).group(1))
        elif items and line.startswith(("  ", "\t")):
            items[-1] += " " + line.strip()  # an indented line carries on the list item
        else:
            end_list()
            para.append(line.strip())
    end_para()
    end_list()
    return Markup("".join(out))
