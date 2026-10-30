"""Opening a book in the computer's default app for its file type."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


class OpenFailed(Exception):
    pass


def open_in_default_app(path: Path) -> None:
    """Ask the operating system to open the file, as if it had been double-clicked.

    This opens it on the computer running Book-Searchie, so it only makes sense in
    local mode, where that's the computer you're using.
    """
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606 (path comes from the index and is checked by the caller)
        else:
            command = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen(
                [command, str(path)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except OSError as e:
        raise OpenFailed(f"Couldn't open the file: {e.strerror or e}") from None
