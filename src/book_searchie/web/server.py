"""Starting the web server ('book-searchie serve')."""

from __future__ import annotations

import socket
import sys
import threading
import time
import urllib.request
import webbrowser

from ..config import Config, ConfigError

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def serve(config: Config, *, port: int | None = None, open_browser: bool = True) -> int:
    try:
        import uvicorn

        from .app import create_app
    except ImportError as e:
        raise ConfigError(f"The web interface's packages are missing ({e.name}). Reinstall book-searchie.") from None

    if config.mode == "network":
        raise ConfigError(
            "Network mode, for using Book-Searchie from other devices, isn't available. It's a possible "
            'future enhancement (see the roadmap in the README). Set mode = "local" in [server].'
        )
    if config.host not in _LOOPBACK:
        raise ConfigError(
            f'Local mode only listens on this computer, but [server] host is "{config.host}". '
            'Set host = "127.0.0.1".'
        )
    if not config.index_path.exists():
        raise ConfigError(f"No index at {config.index_path}. Run 'book-searchie index' first.")

    port = port or config.port
    url = f"http://localhost:{port}/"
    if not _port_free(config.host, port) and _already_running(f"http://{_url_host(config.host)}:{port}/"):
        # Started twice, e.g. a double-clicked shortcut: just show the one that's running.
        print(f"Book-Searchie is already running at {url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        return 0

    server = uvicorn.Server(uvicorn.Config(create_app(config), host=config.host, port=port, log_level="warning"))
    threading.Thread(target=_announce, args=(server, url, open_browser), daemon=True).start()
    try:
        server.run()
    except SystemExit:  # uvicorn exits this way if the port is taken
        print(f"error: couldn't start on port {port}. Is another program using it? Try --port.", file=sys.stderr)
        return 2
    return 0


def _port_free(host: str, port: int) -> bool:
    # Binding is instant, unlike connecting to a closed port, which takes ~2s on Windows.
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def _already_running(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return b"Book-Searchie" in response.read(4096)
    except OSError:
        return False


def _announce(server, url: str, open_browser: bool) -> None:
    """Say where to find us, and open the browser, once the server is actually listening."""
    for _ in range(300):
        if server.started:
            print(f"Book-Searchie is running at {url}  (Ctrl+C to stop)", flush=True)
            if open_browser:
                webbrowser.open(url)
            return
        time.sleep(0.1)
