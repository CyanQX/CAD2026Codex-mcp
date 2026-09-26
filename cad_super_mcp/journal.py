"""Task journal: what the gateway changed in the drawing, and how to take it back.

AutoCAD's own UNDO stack is shared with the human sitting at the keyboard; rolling it back from
a script could undo *their* work.  The journal instead remembers only what the gateway itself
created or moved, so "undo what you just did" is exact and never touches anything else.
"""

from __future__ import annotations

import contextvars
import itertools
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from . import policy as P

CREATE_SLACKER = frozenset(
    {"draw_line", "draw_circle", "draw_arc", "draw_ellipse", "draw_polyline", "draw_point", "draw_text",
     "insert_block", "add_linear_dimension", "copy_entity"}
)
BEST_CREATE = P.LANES["draw"] | P.LANES["annotate"]
_HANDLE_TEXT = re.compile(r"""handle['"]?\s*[:=]\s*['"]?([0-9A-Fa-f]{1,16})""")

_current_group: contextvars.ContextVar[str | None] = contextvars.ContextVar("cad_super_journal_group", default=None)
_quiet: contextvars.ContextVar[bool] = contextvars.ContextVar("cad_super_journal_quiet", default=False)


def find_handle(obj: Any) -> str | None:
    """The handle of the entity a backend just created/returned (any of the shapes we have seen)."""

    if isinstance(obj, dict):
        entity = obj.get("entity")
        if isinstance(entity, dict) and isinstance(entity.get("handle"), str):
            return entity["handle"]
        if isinstance(obj.get("handle"), str) and obj["handle"]:
            return obj["handle"]
        for value in obj.values():
            found = find_handle(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_handle(value)
            if found:
                return found
    elif isinstance(obj, str):
        m = _HANDLE_TEXT.search(obj)
        if m:
            return m.group(1).upper()
    return None


def handle_of(out: dict[str, Any]) -> str | None:
    return find_handle(out.get("structured")) or find_handle(out.get("content"))


@dataclass
class JournalEntry:
    seq: int
    ts: float
    group: str | None
    backend: str
    tool: str
    op: str  # create | move | erase | layer | edit
    handle: str | None
    args: dict[str, Any]
    inverse: dict[str, Any] | None = None
    note: str | None = None
    undone: bool = False

    def brief(self) -> dict[str, Any]:
        return {"seq": self.seq, "group": self.group, "tool": self.tool, "op": self.op, "handle": self.handle,
                "undone": self.undone, "reversible": self.inverse is not None or (self.op == "create" and bool(self.handle))}


@dataclass
class UndoAction:
    seq: int
    tool: str
    args: dict[str, Any]
    describe: str


class Journal:
    def __init__(self, max_entries: int = 5000) -> None:
        self._entries: list[JournalEntry] = []
        self._seq = itertools.count(1)
        self._groups = itertools.count(1)
        self.max_entries = max_entries

    # ------------------------------------------------------------------ groups
    def new_group(self, prefix: str = "g") -> str:
        return f"{prefix}{next(self._groups)}"

    @contextmanager
    def group(self, name: str | None = None) -> Iterator[str]:
        """Tag every write made inside the block with one group id (so it can be undone together)."""

        gid = name or self.new_group()
        token = _current_group.set(gid)
        try:
            yield gid
        finally:
            _current_group.reset(token)

    @contextmanager
    def quiet(self) -> Iterator[None]:
        """Do not journal the gateway's own housekeeping (committing a stage, discarding, undoing).

        Without this, "undo the last thing" after a stage commit would find the internal layer move
        (irreversible) instead of the entity the human actually cares about.
        """

        token = _quiet.set(True)
        try:
            yield
        finally:
            _quiet.reset(token)

    # ------------------------------------------------------------------- record
    def on_event(self, event: dict[str, Any]) -> None:
        """Router hook: called after every successful write-like backend call."""

        if _quiet.get():
            return
        backend, tool, args, out = event["backend"], event["tool"], event.get("args") or {}, event["result"]
        entry: JournalEntry | None = None
        base = dict(seq=next(self._seq), ts=time.time(), group=_current_group.get(), backend=backend, tool=tool, args=dict(args))
        if backend == "slacker":
            if tool in CREATE_SLACKER:
                entry = JournalEntry(op="create", handle=handle_of(out), **base)
            elif tool == "move_entity":
                inverse = None
                if "from_mm" in args and "to_mm" in args:
                    inverse = {"handle": args["handle"], "from_mm": args["to_mm"], "to_mm": args["from_mm"]}
                entry = JournalEntry(op="move", handle=args.get("handle"), inverse=inverse, **base)
            elif tool == "erase_entity":
                entry = JournalEntry(op="erase", handle=args.get("handle"), note="erased; cannot be restored automatically (use UNDO inside AutoCAD)", **base)
            elif tool == "set_entity_layer":
                entry = JournalEntry(op="layer", handle=args.get("handle"), note="layer change did not record the previous layer; cannot be undone automatically", **base)
        elif backend == "best":
            if tool in BEST_CREATE:
                entry = JournalEntry(op="create", handle=handle_of(out), **base)
            elif tool in P.LANES["edit"]:
                entry = JournalEntry(op="edit", handle=args.get("handle"), note="best-backend edits cannot be undone automatically", **base)
        if entry is None:
            return
        self._entries.append(entry)
        if len(self._entries) > self.max_entries:
            del self._entries[: len(self._entries) - self.max_entries]

    # -------------------------------------------------------------------- query
    def entries(self, *, group: str | None = None, limit: int | None = None, include_undone: bool = True) -> list[JournalEntry]:
        out = [e for e in self._entries if (group is None or e.group == group) and (include_undone or not e.undone)]
        return out[-limit:] if limit else out

    def created_handles(self, group: str | None = None) -> list[str]:
        return [e.handle for e in self.entries(group=group, include_undone=False) if e.op == "create" and e.handle]

    # --------------------------------------------------------------------- undo
    def plan_undo(self, *, steps: int | None = None, group: str | None = None) -> tuple[list[UndoAction], list[str]]:
        """What would be done to take back the last ``steps`` changes (or a whole group), newest first."""

        candidates = [e for e in reversed(self._entries) if not e.undone and (group is None or e.group == group)]
        if group is None:
            candidates = candidates[: (steps if steps else 1)]
        actions: list[UndoAction] = []
        skipped: list[str] = []
        for e in candidates:
            if e.op == "create" and e.handle:
                actions.append(UndoAction(e.seq, "erase_entity", {"handle": e.handle}, f"erase {e.handle} (created by {e.tool})"))
            elif e.op == "move" and e.inverse:
                actions.append(UndoAction(e.seq, "move_entity", dict(e.inverse), f"move {e.handle} back to its previous position"))
            else:
                skipped.append(f"{e.tool} {e.handle or ''}: {e.note or 'cannot be undone automatically'}".strip())
        return actions, skipped

    def mark_undone(self, seq: int) -> None:
        for e in self._entries:
            if e.seq == seq:
                e.undone = True
                return

    def forget_handle(self, handle: str) -> None:
        """An entity is gone (erased/undone): its creation entries no longer need undoing."""

        for e in self._entries:
            if e.handle == handle and e.op in ("create", "move"):
                e.undone = True
