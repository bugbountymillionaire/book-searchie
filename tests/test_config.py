import os

import pytest

from book_searchie.config import ConfigError, load_config, render_config


def test_render_and_load(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("BOOK_SEARCHIE_"):
            monkeypatch.delenv(key)
    path = tmp_path / "book-searchie.toml"
    path.write_text(render_config(["books", r"C:\My Books"]), encoding="utf-8")
    config = load_config(path)
    assert config.roots[0] == (tmp_path / "books").resolve()  # relative to the config file
    assert config.roots[1].name == "My Books"
    assert config.index_path == (tmp_path / "book-searchie.db").resolve()
    assert config.mode == "local" and config.port == 8004


def test_env_overrides(tmp_path, monkeypatch):
    path = tmp_path / "book-searchie.toml"
    path.write_text(render_config(["books"]), encoding="utf-8")
    monkeypatch.setenv("BOOK_SEARCHIE_MODE", "network")
    monkeypatch.setenv("BOOK_SEARCHIE_PORT", "9000")
    monkeypatch.setenv("BOOK_SEARCHIE_EXCLUDE", "Archive, drafts")
    config = load_config(path)
    assert (config.mode, config.port, config.exclude) == ("network", 9000, ["Archive", "drafts"])


def test_no_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOOK_SEARCHIE_ROOTS", raising=False)
    monkeypatch.delenv("BOOK_SEARCHIE_CONFIG", raising=False)
    with pytest.raises(ConfigError, match="init"):
        load_config()


def test_bad_mode(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[library]\nroots = ["x"]\n[server]\nmode = "public"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="mode"):
        load_config(path)
