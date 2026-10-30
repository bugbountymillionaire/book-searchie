"""Starting the web server ('book-searchie serve')."""

from __future__ import annotations

import sys
import threading
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
    print(f"Book-Searchie is running at {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    try:
        uvicorn.run(create_app(config), host=config.host, port=port, log_level="warning")
    except SystemExit:  # uvicorn exits this way if the port is taken
        print(f"error: couldn't start on port {port}. Is it already in use? Try --port.", file=sys.stderr)
        return 2
    return 0
