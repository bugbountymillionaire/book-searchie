"""Running OCR from the web UI.

OCR runs as a separate 'book-searchie ocr --progress-json' process, so a slow or
crashing OCR never takes the web server down, and it can be cancelled. Its JSON
progress lines are collected here for the page to poll.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections import deque

from ..config import Config
from ..db import Database
from ..indexer import run_index


class OcrBusy(Exception):
    """An OCR run is already in progress."""


class OcrJob:
    def __init__(self, config: Config):
        self.config = config
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._state = self._fresh_state(running=False)

    @staticmethod
    def _fresh_state(running: bool, total: int = 0) -> dict:
        return {
            "running": running,
            "cancelled": False,
            "total": total,  # books in this run
            "index": 0,  # 1-based number of the current book
            "current": None,  # its path, as shown in the report
            "pages": 0,
            "pages_done": 0,
            "step": "",
            "done": [],
            "failed": [],  # [{"rel", "error"}]
            "empty": [],  # OCR'd but no text found
            "log": [],  # last lines of unexpected output, for diagnosing failures
            "finished": False,
        }

    def status(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._state))  # a copy safe to hand out

    def start(self, rels: list[str], language: str = "eng") -> None:
        with self._lock:
            if self._state["running"]:
                raise OcrBusy("OCR is already running.")
            self._state = self._fresh_state(running=True, total=len(rels))
            cmd = [sys.executable, "-m", "book_searchie"]
            if self.config.source is not None:
                cmd += ["--config", str(self.config.source)]
            cmd += ["ocr", "--progress-json", "--language", language, "--", *rels]
            # A list of arguments, never a shell string, so book names can't be run as commands.
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        threading.Thread(target=self._follow, args=(self._proc,), daemon=True).start()

    def cancel(self) -> None:
        with self._lock:
            if self._proc is not None and self._state["running"]:
                self._state["cancelled"] = True
                self._proc.terminate()

    def _follow(self, proc: subprocess.Popen) -> None:
        log: deque[str] = deque(maxlen=15)
        for line in proc.stdout:
            line = line.strip()
            try:
                event = json.loads(line) if line.startswith("{") else None
            except json.JSONDecodeError:
                event = None
            with self._lock:
                if event is None:
                    if line:
                        log.append(line)
                        self._state["log"] = list(log)
                else:
                    self._apply(event)
        code = proc.wait()

        # A cancelled run is killed before it can update the index itself.
        with self._lock:
            cancelled = self._state["cancelled"]
        if cancelled:
            with Database.open(self.config.index_path) as db:
                run_index(self.config, db)

        with self._lock:
            s = self._state
            if code != 0 and not cancelled and not s["failed"] and not s["finished"]:
                s["failed"].append({"rel": s["current"] or "OCR", "error": "\n".join(log) or f"exit code {code}"})
            s["running"] = False
            s["finished"] = True
            s["current"] = None

    def _apply(self, event: dict) -> None:
        s = self._state
        kind = event.get("type")
        if kind == "book":
            s.update(index=event["index"], total=event["total"], current=event["rel"],
                     pages=event["pages"], pages_done=0, step="Starting")
        elif kind == "pages":
            s["step"] = event.get("step") or ""
            s["pages_done"] = event.get("done") or 0
            if event.get("total"):
                s["pages"] = event["total"]
        elif kind == "done":
            s["done"].append(event["rel"])
        elif kind == "failed":
            s["failed"].append({"rel": event["rel"], "error": event["error"]})
        elif kind == "finished":
            s["empty"] = event.get("empty", [])
