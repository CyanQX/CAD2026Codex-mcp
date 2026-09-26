"""Cross-process interlock.

The per-process ``asyncio.Lock`` cannot protect AutoCAD when several MCP hosts (Codex,
Claude Desktop, ...) each start their own gateway.  This module adds an OS-level file
lock that every gateway process on the machine contends for, so at most one CAD call
chain is inside AutoCAD at any moment.  The OS releases the lock automatically if the
holder crashes, so a dead gateway can never wedge the others.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from .errors import LockTimeout

if os.name == "nt":  # pragma: no cover - exercised on Windows
    import msvcrt
else:  # pragma: no cover
    import fcntl

_META_OFFSET = 16  # lock byte 0; holder info lives beyond it so waiters can read it


class CrossProcessLock:
    def __init__(self, path: str | os.PathLike[str], *, poll_interval: float = 0.05) -> None:
        self.path = Path(path)
        self.poll_interval = poll_interval
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    # ------------------------------------------------------------ low level
    def _try_lock(self, fd: int) -> bool:
        try:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(self, fd: int) -> None:
        try:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass

    def holder(self) -> dict[str, Any] | None:
        """Best-effort description of the current holder (for error messages)."""

        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except OSError:
            return None
        try:
            os.lseek(fd, _META_OFFSET, os.SEEK_SET)
            raw = os.read(fd, 256).split(b"\0", 1)[0]
            return json.loads(raw.decode("utf-8")) if raw else None
        except Exception:  # noqa: BLE001
            return None
        finally:
            os.close(fd)

    def _write_holder(self, fd: int) -> None:
        try:
            meta = json.dumps({"pid": os.getpid(), "since": time.strftime("%H:%M:%S")}).encode("utf-8")
            os.lseek(fd, _META_OFFSET, os.SEEK_SET)
            os.write(fd, meta.ljust(128, b"\0"))
        except OSError:
            pass

    # ---------------------------------------------------------------- public
    async def acquire(self, timeout: float | None = None) -> None:
        if self._fd is not None:
            raise RuntimeError("CrossProcessLock is not re-entrant; use Router.session()")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        try:
            while True:
                if self._try_lock(fd):
                    self._fd = fd
                    self._write_holder(fd)
                    return
                if deadline is not None and loop.time() >= deadline:
                    who = self.holder()
                    detail = f" (holder PID {who['pid']}, since {who['since']})" if who and "pid" in who else ""
                    raise LockTimeout(f"Timed out waiting for the AutoCAD interlock lock after {timeout:g}s{detail}", details={"holder": who})
                await asyncio.sleep(self.poll_interval)
        except BaseException:
            if self._fd is None:
                os.close(fd)
            raise

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            self._unlock(fd)
            try:
                os.close(fd)
            except OSError:
                pass
