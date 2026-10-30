"""Search history: recorded from the web search, listed on the home page, deletable."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from book_searchie import db as db_module
from book_searchie.db import Database
from book_searchie.indexer import run_index
from book_searchie.web.app import ago, create_app

H = {"X-Book-Searchie": "1"}


@pytest.fixture
def client(config, db):
    run_index(config, db)
    return TestClient(create_app(config, allowed_hosts=["testserver"]))


def _queries(db):
    return [(r["query"], r["topic"]) for r in db.recent_searches()]


def test_searches_are_recorded_newest_first(client, db):
    client.get("/", params={"q": "cohosh"})
    client.get("/", params={"q": "dovetail", "topic": "Woodwork"})
    client.get("/", params={"q": "nothing-matches-this"})
    assert _queries(db) == [("nothing-matches-this", ""), ("dovetail", "Woodwork"), ("cohosh", "")]
    assert [r["hits"] for r in db.recent_searches()] == [0, 1, 1]


def test_repeating_a_search_moves_it_to_the_top(client, db):
    client.get("/", params={"q": "cohosh"})
    client.get("/", params={"q": "dovetail"})
    client.get("/", params={"q": "COHOSH"})  # same search, different capitals
    assert _queries(db) == [("COHOSH", ""), ("dovetail", "")]


def test_failed_and_later_pages_are_not_recorded(client, db):
    client.get("/", params={"q": "root AND"})  # a search it can't run
    client.get("/", params={"q": "cohosh", "page_no": 2})
    assert _queries(db) == []


def test_home_page_lists_history(client):
    client.get("/", params={"q": "dovetail", "topic": "Woodwork"})
    home = client.get("/").text
    assert "Recent searches" in home and 'id="clear-history"' in home and "history.js" in home
    assert 'href="/?q=dovetail&amp;topic=Woodwork"' in home and "1 page" in home
    assert "Recent searches" not in client.get("/", params={"q": "cohosh"}).text  # only on the home page


def test_delete_one_search(client, db):
    client.get("/", params={"q": "cohosh"})
    client.get("/", params={"q": "dovetail"})
    first = db.recent_searches()[0]["id"]
    assert client.delete(f"/api/history/{first}").status_code == 403  # needs the app's header
    assert client.delete(f"/api/history/{first}", headers=H).status_code == 200
    assert _queries(db) == [("cohosh", "")]
    assert client.delete(f"/api/history/{first}", headers=H).status_code == 404


def test_clear_history(client, db):
    client.get("/", params={"q": "cohosh"})
    client.get("/", params={"q": "dovetail"})
    assert client.delete("/api/history").status_code == 403
    assert client.delete("/api/history", headers=H).json() == {"deleted": 2}
    assert _queries(db) == []
    assert "Recent searches" not in client.get("/").text


def test_history_is_capped(db, monkeypatch):
    monkeypatch.setattr(db_module, "HISTORY_LIMIT", 3)
    for n in range(5):
        db.record_search(f"query {n}", "", 1)
    assert [q for q, _ in _queries(db)] == ["query 4", "query 3", "query 2"]


def test_history_survives_index_rebuilds(config, db):
    db.record_search("cohosh", "", 1)
    run_index(config, db, full=True)
    db.conn.execute("PRAGMA user_version = 0")  # as if the index format changed
    db.conn.commit()
    with Database.open(config.index_path) as reopened:
        assert _queries(reopened) == [("cohosh", "")]


def test_ago():
    now = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
    when = lambda **kw: (now - timedelta(**kw)).isoformat()  # noqa: E731
    assert ago(when(seconds=20), now) == "just now"
    assert ago(when(minutes=1), now) == "1 minute ago"
    assert ago(when(hours=5), now) == "5 hours ago"
    assert ago(when(days=1, hours=3), now) == "yesterday"
    assert ago(when(days=10), now) == "1 week ago"
    assert ago(when(days=400), now) == "1 year ago"
