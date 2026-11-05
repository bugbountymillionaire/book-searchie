import pytest

from book_searchie.markdown import render


@pytest.mark.parametrize(
    "text, html",
    [
        ("Plain text.", "<p>Plain text.</p>"),
        ("One\ntwo\n\nThree", "<p>One<br>two</p><p>Three</p>"),
        ("**Bold** and *italic* and _also_", "<p><strong>Bold</strong> and <em>italic</em> and <em>also</em></p>"),
        ("- one\n- two\n* three", "<ul><li>one</li><li>two</li><li>three</li></ul>"),
        ("1. one\n2) two", "<ol><li>one</li><li>two</li></ol>"),
        ("Before\n- a\n  carried on\nAfter", "<p>Before</p><ul><li>a carried on</li></ul><p>After</p>"),
        ("- a\n1. b", "<ul><li>a</li></ul><ol><li>b</li></ol>"),
        ("[Herb society](https://example.org/a_b_c?x=1&y=2)",
         '<p><a href="https://example.org/a_b_c?x=1&amp;y=2" rel="noopener noreferrer" target="_blank">Herb society</a></p>'),
        ("[**Bold** link](https://example.org/_x_)",
         '<p><a href="https://example.org/_x_" rel="noopener noreferrer" target="_blank"><strong>Bold</strong> link</a></p>'),
        ("snake_case_name and 2*3*4", "<p>snake_case_name and 2*3*4</p>"),
        ("* not italic *", "<ul><li>not italic *</li></ul>"),
        ("", ""),
    ],
)
def test_render(text, html):
    assert str(render(text)) == html


@pytest.mark.parametrize(
    "text",
    [
        "<script>alert(1)</script>",
        '<img src=x onerror="alert(1)">',
        "[click](javascript:alert(1))",
        '[x](https://example.org/" onmouseover="alert(1))',
        "[x](data:text/html,<script>alert(1)</script>)",
        "**<b>bold</b>**",
        "0 placeholder lookalike",
    ],
)
def test_nothing_unsafe_gets_through(text):
    html = str(render(text))
    assert "<script" not in html and "<img" not in html and "<b>" not in html
    assert 'href="javascript' not in html and 'href="data' not in html
    assert "onmouseover=" not in html or "&#34;" in html  # only ever as escaped text
    assert "onerror=" not in html or "&lt;img" in html
    assert "<a " not in html or 'href="https://' in html
