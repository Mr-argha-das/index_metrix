"""Structured logging with an in-memory ring buffer for the Logs page.

Rules:
* JSON-ish single-line records with level, logger, message, and any extra
  structured fields (user_id, job_id, pdf_id, duration_ms, status, error).
* Secrets are never passed to log calls by the application; this module also
  scrubs a small blocklist of sensitive key names as a defence in depth.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import threading
import traceback
from collections import deque

_RING_SIZE = 500
_SENSITIVE_RE = re.compile(
    r"(password|passwd|secret|token|api_key|apikey|authorization|session)["
    r"=:]?\s*\S+",
    re.IGNORECASE,
)


class RingLogHandler(logging.Handler):
    def __init__(self, size: int = _RING_SIZE):
        super().__init__()
        self._records: deque[dict] = deque(maxlen=size)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        entry = {
            "ts": self.format(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = "".join(traceback.format_exception(*record.exc_info))[-500:]
        for key in ("user_id", "job_id", "pdf_id", "duration_ms", "status", "event", "error"):
            value = getattr(record, key, None)
            if value is not None:
                entry[key] = value
        self._scrub(entry)
        with self._lock:
            self._records.append(entry)

    def _scrub(self, entry: dict) -> None:
        for key in ("message", "exc"):
            if key in entry:
                entry[key] = _SENSITIVE_RE.sub(f"{key.split('=')[0]}=[REDACTED]", str(entry[key]))

    def lines(self, limit: int = 200, level: str | None = None) -> list[dict]:
        with self._lock:
            items = list(self._records)
        if level:
            wanted = level.upper()
            items = [i for i in items if i["level"] == wanted]
        return items[-limit:][::-1]


def setup_logging(level: str = "INFO") -> RingLogHandler:
    handler = RingLogHandler()
    handler.setFormatter(logging.Formatter("%(asctime)sZ"))

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # avoid duplicate handlers on reloads (uvicorn --reload / tests)
    for h in list(root.handlers):
        if isinstance(h, RingLogHandler):
            root.removeHandler(h)
    root.addHandler(handler)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, RingLogHandler) for h in root.handlers):
        root.addHandler(console)

    # keep third-party noise down
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("pymupdf").setLevel(logging.WARNING)
    logging.getLogger("munch").setLevel(logging.WARNING)
    return handler
