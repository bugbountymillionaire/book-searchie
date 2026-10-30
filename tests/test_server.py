"""Starting the server when its port is already taken."""

import http.server
import threading

import pytest

from book_searchie.indexer import run_index
from book_searchie.web import server


@pytest.fixture
def busy_port():
    """A port with a small web server on it, answering with the given page."""
    servers = []

    def start(page: bytes) -> int:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(page)

            def log_message(self, *args):
                pass

        httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return httpd.server_address[1]

    yield start
    for httpd in servers:
        httpd.shutdown()


@pytest.fixture
def opened(monkeypatch):
    urls = []
    monkeypatch.setattr(server.webbrowser, "open", urls.append)
    return urls


def test_already_running_just_opens_it(config, db, busy_port, opened, capsys):
    run_index(config, db)
    port = busy_port(b"<title>Book-Searchie</title>")
    assert server.serve(config, port=port) == 0
    assert opened == [f"http://localhost:{port}/"]
    assert "already running" in capsys.readouterr().out


def test_port_taken_by_another_program(config, db, busy_port, opened, capsys):
    run_index(config, db)
    port = busy_port(b"<title>Something else</title>")
    assert server.serve(config, port=port) == 2
    out = capsys.readouterr()
    assert "Is another program using it?" in out.err
    assert "is running at" not in out.out and opened == []  # never claims to have started
