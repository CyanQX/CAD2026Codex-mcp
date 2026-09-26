"""Append-only JSONL audit log.

Design rules (each one fixes a real defect found while auditing the first version):

* ``write`` never raises.  A locked or full audit file must not turn a *successful*
  CAD write into an error (which invites the model to retry and duplicate geometry).
* Every call gets a ``call_id`` so ``before`` / ``after`` rows can be paired even when
  several gateway processes append to the same file.
* Large arguments are summarised (size + sha256 + preview) instead of bloating the log.
* The file rotates by size so it cannot grow without bound.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("cad_super_mcp.audit")

_lock = threading.Lock()


def new_call_id() -> str:
    return uuid.uuid4().hex[:12]


class AuditLog:
    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        enabled: bool = True,
        max_bytes: int = 5_000_000,
        backups: int = 3,
        max_arg_chars: int = 2000,
    ) -> None:
        self.path = Path(path)
        self.enabled = enabled
        self.max_bytes = max_bytes
        self.backups = max(0, backups)
        self.max_arg_chars = max_arg_chars
        self._last_warn = 0.0

    # ------------------------------------------------------------------ helpers
    def summarize(self, value: Any) -> Any:
        """Return ``value`` unchanged if small, else a compact size+hash+preview stub."""

        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = repr(value)
        if len(text) <= self.max_arg_chars:
            return value
        return {
            "_truncated": True,
            "chars": len(text),
            "sha256": hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16],
            "preview": text[:300],
        }

    def _rotate_if_needed(self) -> None:
        try:
            if self.max_bytes <= 0 or not self.path.exists() or self.path.stat().st_size < self.max_bytes:
                return
            for i in range(self.backups, 0, -1):
                src = self.path if i == 1 else self.path.with_name(f"{self.path.name}.{i - 1}")
                dst = self.path.with_name(f"{self.path.name}.{i}")
                if src.exists():
                    os.replace(src, dst)
        except OSError:
            # Another gateway process may hold the file open (Windows); skip this round.
            pass

    def _warn_once(self, exc: BaseException) -> None:
        now = time.monotonic()
        if now - self._last_warn > 60:
            self._last_warn = now
            logger.warning("audit log unavailable (%s): %s", self.path, exc)

    # -------------------------------------------------------------------- write
    def write(self, event: dict[str, Any]) -> bool:
        """Append one event.  Returns False (never raises) when the log is unavailable."""

        if not self.enabled:
            return True
        row = {"ts": datetime.now(UTC).isoformat(), "pid": os.getpid(), **event}
        try:
            line = json.dumps(row, ensure_ascii=False, default=str) + "\n"
            with _lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_if_needed()
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
            return True
        except Exception as exc:  # noqa: BLE001 - audit must never break a CAD call
            self._warn_once(exc)
            return False


def append_audit(path: str, event: dict[str, Any]) -> None:
    """Backwards-compatible helper (kept for scripts written against v0.1)."""

    AuditLog(path).write(event)
