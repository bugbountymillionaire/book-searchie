from book_searchie.scan import ROOT_TOPIC, compile_pattern, scan_library


def _rels(library, exclude=()):
    return {f.rel: f.topic for f in scan_library([library], ["pdf", "epub"], exclude)}


def test_topics_come_from_top_level_folders(library):
    rels = _rels(library)
    assert rels["Herbalism/Culpeper.pdf"] == "Herbalism"
    assert rels["Gardening/Seasonal/Spring.pdf"] == "Gardening"
    assert rels["Loose.pdf"] == ROOT_TOPIC


def test_underscore_folders_are_skipped_at_any_depth(library):
    rels = _rels(library)
    assert not any("_inbox" in r or "_unsorted" in r for r in rels)


def test_exclude_patterns(library):
    assert "Gardening/Seasonal/Spring.pdf" not in _rels(library, ["seasonal"])  # by name, any depth, any case
    assert "Gardening/Seasonal/Spring.pdf" not in _rels(library, ["Gardening/*"])
    assert "Woodwork/Joinery.pdf" not in _rels(library, ["**/*.pdf"])
    assert "Herbalism/Modern Herbal.epub" in _rels(library, ["**/*.pdf"])


def test_compile_pattern():
    assert compile_pattern("Archive").fullmatch("a/b/archive")
    assert compile_pattern("a/*").fullmatch("a/b")
    assert not compile_pattern("a/*").fullmatch("a/b/c")
    assert compile_pattern("a/**").fullmatch("a/b/c")
    assert not compile_pattern("a/b").fullmatch("x/a/b")


def test_extension_filter(library):
    files = list(scan_library([library], ["epub"], []))
    assert [f.kind for f in files] == ["epub"]
