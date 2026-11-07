"""Optional AI tag suggestions (KB step 8), against a fake Ollama server."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from book_searchie import ai
from book_searchie.config import ConfigError, load_config
from book_searchie.indexer import run_index
from book_searchie.notes import Note, NotesStore, Tag
from book_searchie.web.app import create_app
from .conftest import seed_headings

H = {"X-Book-Searchie": "1"}


class FakeOllama:
    """Answers /api/chat with `reply` (the model's text) or `status` and `error`."""

    def __init__(self):
        self.requests = []
        self.reply = '{"tags": []}'
        self.status = 200
        self.error = ""
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append({"path": self.path, "body": body})
                if fake.status != 200:
                    data = json.dumps({"error": fake.error}).encode()
                else:
                    data = json.dumps({"message": {"role": "assistant", "content": fake.reply}, "done": True}).encode()
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def ollama():
    fake = FakeOllama()
    yield fake
    fake.close()


@pytest.fixture
def ai_config(config, ollama):
    config.tag_kinds = ["plant", "condition"]
    config.ai_enabled, config.ai_endpoint, config.ai_model = True, ollama.url, "test-model"
    return config


@pytest.fixture
def store(ai_config):
    with NotesStore.open(ai_config.notes_path) as s:
        s.create(Note(source_type="own", topic="Herbalism", title="Old", body="x",
                      tags=[Tag("plant", "Nettle"), Tag("condition", "Arthritis")]))
        s.add_alias("plant", "Nettle", "Urtica dioica")
        yield s


def test_suggestions_prefer_existing_tags(ai_config, store, ollama):
    ollama.reply = json.dumps({"tags": [
        {"kind": "condition", "name": "Joint pain"},
        {"kind": "plant", "name": "urtica  dioica"},  # an alias: becomes the tag it stands for
        {"kind": "condition", "name": "arthritis"},
        {"kind": "colour", "name": "Green"},  # not a configured kind
        {"kind": "plant", "name": "x" * 61},  # too long
        {"kind": "plant", "name": "Nettle"},  # a repeat
        "nonsense",
    ]})
    found = ai.suggest_tags(ai_config, store, title="Nettle for joints", body="Nettle tea eases arthritis.")
    assert [(s.kind, s.name, s.existing) for s in found] == [
        ("plant", "Nettle", True), ("condition", "Arthritis", True), ("condition", "Joint pain", False)]

    sent = ollama.requests[0]
    assert sent["path"] == "/api/chat"
    assert (sent["body"]["model"], sent["body"]["stream"], sent["body"]["format"]) == ("test-model", False, "json")
    system, user = (m["content"] for m in sent["body"]["messages"])
    assert "The kinds are: plant, condition." in system
    assert "- plant: Nettle" in user and "- condition: Arthritis" in user
    assert "Title: Nettle for joints" in user and "Notes: Nettle tea eases arthritis." in user


def test_tags_already_on_the_note_are_not_suggested(ai_config, store, ollama):
    ollama.reply = '{"tags": [{"kind": "plant", "name": "Nettle"}, {"kind": "plant", "name": "Dock"}]}'
    found = ai.suggest_tags(ai_config, store, body="Nettle and dock.", current=[("plant", "nettle")])
    assert [s.name for s in found] == ["Dock"]


def test_at_most_eight_and_a_bare_list_is_accepted(ai_config, store, ollama):
    ollama.reply = json.dumps([{"kind": "plant", "name": f"Herb {n}"} for n in range(12)])
    assert len(ai.suggest_tags(ai_config, store, body="Lots of herbs.")) == 8


def test_an_empty_note_isnt_sent(ai_config, store, ollama):
    assert ai.suggest_tags(ai_config, store, title=" ", body="") == []
    assert ollama.requests == []


def test_only_the_note_is_sent_and_long_notes_are_cut(ai_config, store, ollama):
    ai.suggest_tags(ai_config, store, body="word " * 5000)
    user = ollama.requests[0]["body"]["messages"][1]["content"]
    assert len(user.split("The note:\n", 1)[1]) == ai.MAX_NOTE_CHARS


@pytest.mark.parametrize(
    "setup, message",
    [
        (lambda o, c: setattr(c, "ai_enabled", False), "suggestions are off"),
        (lambda o, c: (setattr(o, "status", 404), setattr(o, "error", "model not found")), "ollama pull test-model"),
        (lambda o, c: (setattr(o, "status", 500), setattr(o, "error", "out of memory")), "error (500: out of memory)"),
        (lambda o, c: setattr(o, "reply", "Sure! Here are some tags: nettle"), "expected form"),
    ],
)
def test_problems_are_explained(ai_config, store, ollama, setup, message):
    setup(ollama, ai_config)
    with pytest.raises(ai.AiUnavailable, match=message.replace("(", r"\(").replace(")", r"\)")):
        ai.suggest_tags(ai_config, store, body="Nettle.")


def test_not_running(ai_config, store, ollama):
    ollama.close()
    with pytest.raises(ai.AiUnavailable, match="isn't running"):
        ai.suggest_tags(ai_config, store, body="Nettle.")


# --- config ---


def test_ai_config(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[library]\nroots = ["x"]\n', encoding="utf-8")
    config = load_config(path)
    assert (config.ai_enabled, config.ai_endpoint, config.ai_model) == (False, "http://localhost:11434", "llama3.2")
    path.write_text('[library]\nroots = ["x"]\n[ai]\nenabled = true\nendpoint = "http://127.0.0.1:9999/"\nmodel = "phi3"\n',
                    encoding="utf-8")
    config = load_config(path)
    assert (config.ai_enabled, config.ai_endpoint, config.ai_model) == (True, "http://127.0.0.1:9999", "phi3")
    for host in ("localhost", "[::1]", "127.0.0.5"):
        path.write_text(f'[library]\nroots = ["x"]\n[ai]\nendpoint = "http://{host}:11434"\n', encoding="utf-8")
        load_config(path)


@pytest.mark.parametrize(
    "ai_section, message",
    [
        ('endpoint = "http://192.168.1.20:11434"', "isn't on this computer"),
        ('endpoint = "https://ai.example.com"', "isn't on this computer"),
        ('endpoint = "localhost:11434"', "web address"),
        ('enabled = "yes"', "true or false"),
        ('enabled = true\nmodel = ""', "model is empty"),
    ],
)
def test_bad_ai_config(tmp_path, ai_section, message):
    path = tmp_path / "c.toml"
    path.write_text(f'[library]\nroots = ["x"]\n[ai]\n{ai_section}\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_remote_endpoint_only_when_allowed(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[library]\nroots = ["x"]\n[ai]\nendpoint = "http://192.168.1.20:11434"\nallow_remote = true\n',
                    encoding="utf-8")
    assert load_config(path).ai_endpoint == "http://192.168.1.20:11434"


# --- web ---


@pytest.fixture
def client(ai_config, db):
    run_index(ai_config, db)
    return TestClient(create_app(ai_config, allowed_hosts=["testserver"]))


def test_api(client, store, ollama):
    ollama.reply = '{"tags": [{"kind": "plant", "name": "Nettle"}, {"kind": "condition", "name": "Gout"}]}'
    body = {"title": "Nettle", "body": "For gout.", "tags": [{"kind": "condition", "name": "Arthritis"}]}
    assert client.post("/api/ai/suggest-tags", json=body).status_code == 403
    res = client.post("/api/ai/suggest-tags", json=body, headers=H)
    assert res.json() == {"suggestions": [{"kind": "plant", "name": "Nettle", "existing": True},
                                          {"kind": "condition", "name": "Gout", "existing": False}]}
    ollama.close()
    res = client.post("/api/ai/suggest-tags", json=body, headers=H)
    assert res.status_code == 503 and "isn't running" in res.json()["error"]


def test_button_only_when_switched_on(client, db, ai_config):
    assert 'id="kb-suggest"' in client.get("/kb/new").text
    ai_config.ai_enabled = False
    off = TestClient(create_app(ai_config, allowed_hosts=["testserver"]))
    assert 'id="kb-suggest"' not in off.get("/kb/new").text
    res = off.post("/api/ai/suggest-tags", json={"body": "x"}, headers=H)
    assert res.status_code == 404 and "suggestions are off" in res.json()["error"]


# --- Entries (KB v3) ---


@pytest.fixture
def entry_store(ai_config):
    with NotesStore.open(ai_config.notes_path) as s:
        seed_headings(s)
        s.add_text("Nettle", "Stings.\n# Tea\nFor hay fever and aching joints.")
        s.add_text("Yarrow", "x")
        s.set_entry_tags(s.find_entry("Yarrow").id, ["Joint pain", "Wounds"])
        s.set_entry_tags(s.find_entry("Nettle").id, ["Wounds"])
        s.add_alias("tag", "Joint pain", "Arthritis")
        yield s


def test_entry_suggestions(ai_config, entry_store, ollama):
    # A plain list or {"name": ...} items; other names and capitals are tidied to the tag.
    ollama.reply = json.dumps({"tags": ["Hay fever", {"name": "arthritis"}, "wounds", "Hay Fever", "", "x" * 61]})
    found = ai.suggest_entry_tags(ai_config, entry_store, title="Nettle", text="For hay fever.", current=["Wounds"])
    assert [(s.kind, s.name, s.existing) for s in found] == [("tag", "Joint pain", True), ("tag", "Hay fever", False)]
    prompt = ollama.requests[-1]["body"]["messages"][1]["content"]
    assert prompt.startswith("Existing tags: Wounds, Joint pain") and "The entry: Nettle\n\nFor hay fever." in prompt
    assert ai.suggest_entry_tags(ai_config, entry_store, title="Nettle", text="  ") == []  # nothing sent
    assert len(ollama.requests) == 1


def test_entry_suggestions_api(client, entry_store, ollama):
    nettle = entry_store.find_entry("Nettle").id
    ollama.reply = '{"tags": ["Hay fever", "Wounds"]}'
    assert client.post(f"/api/entries/{nettle}/suggest-tags").status_code == 403
    res = client.post(f"/api/entries/{nettle}/suggest-tags", headers=H)
    assert res.json() == {"suggestions": [{"name": "Hay fever", "existing": False}]}
    prompt = ollama.requests[-1]["body"]["messages"][1]["content"]
    assert "Description:\nStings.\n\nTea:\nFor hay fever and aching joints." in prompt
    assert client.post("/api/entries/999/suggest-tags", headers=H).status_code == 404
    page = client.get(f"/kb/entry/{nettle}").text
    assert 'data-act="suggest-tags"' in page and 'id="kb-suggested"' in page


def test_entry_suggestions_off(ai_config, db, entry_store):
    ai_config.ai_enabled = False
    run_index(ai_config, db)
    off = TestClient(create_app(ai_config, allowed_hosts=["testserver"]))
    nettle = entry_store.find_entry("Nettle").id
    assert 'data-act="suggest-tags"' not in off.get(f"/kb/entry/{nettle}").text
    res = off.post(f"/api/entries/{nettle}/suggest-tags", headers=H)
    assert res.status_code == 404 and "suggestions are off" in res.json()["error"]
