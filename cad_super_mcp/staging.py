"""Stage -> preview -> commit / discard.

A careful drafter does not ink straight onto the final drawing.  Staged geometry is drawn on a
bright review layer; the human looks at a screenshot and says "yes" (commit: the entities move to
their real layers) or "no" (discard: they are erased).  Built purely from primitives the COM
backend already has (draw on a layer, change an entity's layer, erase), so it does not depend on
AutoCAD's UNDO stack.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Any

from .config import RuntimeConfig
from .errors import GatewayError, NeedsClarification
from .journal import Journal
from .router import Router


@dataclass
class StageItem:
    handle: str
    tool: str
    target_layer: str | None
    summary: str
    done: bool = False


@dataclass
class Stage:
    id: str
    label: str
    created: float = field(default_factory=time.time)
    status: str = "open"  # open | committed | discarded | partial
    items: list[StageItem] = field(default_factory=list)

    def pending(self) -> list[StageItem]:
        return [i for i in self.items if not i.done]

    def brief(self) -> dict[str, Any]:
        return {
            "id": self.id, "label": self.label, "status": self.status,
            "pending": len(self.pending()), "total": len(self.items),
            "items": [{"handle": i.handle, "tool": i.tool, "target_layer": i.target_layer, "summary": i.summary, "done": i.done}
                      for i in self.items],
        }


def layer_names(out: dict[str, Any]) -> list[str] | None:
    """Layer names (original case) from a Slacker ``list_layers`` reply; None if the shape is unknown."""

    def walk(node: Any) -> list[str] | None:
        if isinstance(node, list) and node and all(isinstance(x, dict) and "name" in x for x in node):
            return [str(x["name"]) for x in node]
        if isinstance(node, dict):
            for value in node.values():
                found = walk(value)
                if found is not None:
                    return found
        return None

    return walk(out.get("structured"))


def all_handles(node: Any) -> list[str]:
    """Every ``handle`` string found in a payload (e.g. a ``query_entities`` reply)."""

    found: list[str] = []
    if isinstance(node, dict):
        if isinstance(node.get("handle"), str):
            found.append(node["handle"])
        for value in node.values():
            found.extend(all_handles(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(all_handles(value))
    return found


class StageManager:
    def __init__(self, router: Router, journal: Journal, runtime: RuntimeConfig) -> None:
        self.router = router
        self.journal = journal
        self.runtime = runtime
        self.stages: dict[str, Stage] = {}
        self.current: str | None = None
        self._ids = itertools.count(1)

    @property
    def layer(self) -> str:
        return self.runtime.stage_layer

    # -------------------------------------------------------------- lifecycle
    async def ensure_layer(self) -> None:
        out = await self.router.slacker("create_layer", {"name": self.layer, "color_aci": self.runtime.stage_color_aci})
        if not out["ok"]:
            raise GatewayError(f"cannot create the staging layer {self.layer}: {(out['error'] or {}).get('message')}", code="STAGE_LAYER_FAILED")

    def get(self, stage_id: str | None = None) -> Stage:
        sid = stage_id or self.current
        if not sid or sid not in self.stages:
            raise NeedsClarification(
                "there is no open staging session",
                details={"options": ["use a drawing tool with stage=true first, or cad_stage(action='begin')"], "known": sorted(self.stages)},
            )
        return self.stages[sid]

    async def begin(self, label: str | None = None) -> Stage:
        await self.ensure_layer()
        stage = Stage(id=f"s{next(self._ids)}", label=label or "AI staging")
        self.stages[stage.id] = stage
        self.current = stage.id
        return stage

    async def ensure_open(self, label: str | None = None) -> Stage:
        if self.current and self.stages[self.current].status == "open":
            await self.ensure_layer()  # the layer may have been purged since
            return self.stages[self.current]
        return await self.begin(label)

    def add(self, stage: Stage, handle: str, tool: str, target_layer: str | None, summary: str) -> None:
        stage.items.append(StageItem(handle=handle, tool=tool, target_layer=target_layer, summary=summary))

    async def check_target_layer(self, layer: str | None) -> None:
        """Fail early (and helpfully) when a staged entity's real layer does not exist yet."""

        if not layer:
            return
        out = await self.router.slacker("list_layers", {})
        names = layer_names(out) if out["ok"] else None
        if names is not None and layer.lower() not in {n.lower() for n in names}:
            raise NeedsClarification(
                f"target layer {layer!r} does not exist",
                details={"options": [f"create it first: cad_layer(action='create', name='{layer}')", "use an existing layer: " + ", ".join(sorted(names)[:30])],
                         "question": f"Should I create layer {layer!r}, or draw on another layer?"},
            )

    # ---------------------------------------------------------- outcomes
    async def commit(self, stage_id: str | None = None, default_layer: str | None = None) -> dict[str, Any]:
        stage = self.get(stage_id)
        pending = stage.pending()
        if not pending:
            stage.status = "committed"
            return {"stage": stage.id, "committed": 0, "failed": []}
        unresolved = [i.handle for i in pending if not (i.target_layer or default_layer)]
        if unresolved:
            raise NeedsClarification(
                f"{len(unresolved)} staged entities have no target layer",
                details={"handles": unresolved, "question": "Which layer should these entities be committed to? (pass layer=... to commit)"},
            )
        async with self.router.session():
            targets = {i.handle: (i.target_layer or default_layer) for i in pending}
            names = layer_names(await self.router.slacker("list_layers", {}))
            if names is not None:
                existing = {n.lower() for n in names}
                missing = sorted({t for t in targets.values() if t and t.lower() not in existing})
                if missing:
                    raise NeedsClarification(
                        f"target layer(s) do not exist: {', '.join(missing)}",
                        details={"missing": missing, "options": ["create them with cad_layer(action='create', name=...) and commit again"]},
                    )
            failed: list[dict[str, str]] = []
            moved = 0
            with self.journal.quiet():  # finishing the drawing is part of creating it, not a separate step to undo
                for item in pending:
                    out = await self.router.slacker("set_entity_layer", {"handle": item.handle, "layer": targets[item.handle]})
                    if out["ok"]:
                        item.done = True
                        moved += 1
                    else:
                        failed.append({"handle": item.handle, "error": (out["error"] or {}).get("message", "")})
        stage.status = "committed" if not failed else "partial"
        return {"stage": stage.id, "committed": moved, "failed": failed, "layers": sorted({t for t in targets.values() if t})}

    async def discard(self, stage_id: str | None = None) -> dict[str, Any]:
        stage = self.get(stage_id)
        erased = gone = 0
        failed: list[dict[str, str]] = []
        async with self.router.session():
            with self.journal.quiet():
                for item in stage.pending():
                    out = await self.router.slacker("erase_entity", {"handle": item.handle}, confirm=True)
                    message = (out["error"] or {}).get("message", "")
                    if out["ok"]:
                        erased += 1
                    elif "No accessible entity" in message or "\u672a\u627e\u5230" in message:
                        gone += 1  # already removed by hand: that is what we wanted
                    else:
                        failed.append({"handle": item.handle, "error": message})
                        continue
                    item.done = True
                    self.journal.forget_handle(item.handle)
        stage.status = "discarded" if not failed else "partial"
        return {"stage": stage.id, "erased": erased, "already_gone": gone, "failed": failed}

    async def preview(self, stage_id: str | None = None, zoom: bool = False) -> tuple[dict[str, Any], list[dict[str, str]]]:
        stage = self.get(stage_id)
        async with self.router.session():
            if zoom:
                await self.router.slacker("zoom_extents", {})
            shot = await self.router.render({})
        info = stage.brief()
        info["screenshot_ok"] = bool(shot.get("ok"))
        return info, list(shot.get("images") or [])

    async def cleanup(self) -> dict[str, Any]:
        """Erase everything on the stage layer (recovery after a restart / a failed discard)."""

        async with self.router.session():
            out = await self.router.slacker("query_entities", {"layer": self.layer, "limit": 1000})
            if not out["ok"]:
                raise GatewayError((out["error"] or {}).get("message", "failed to query the staging layer"), code="STAGE_QUERY_FAILED")
            handles = all_handles(out.get("structured"))
            erased = 0
            with self.journal.quiet():
                for h in handles:
                    res = await self.router.slacker("erase_entity", {"handle": h}, confirm=True)
                    erased += 1 if res["ok"] else 0
                    self.journal.forget_handle(h)
        for stage in self.stages.values():
            if stage.status == "open":
                stage.status = "discarded"
        return {"layer": self.layer, "found": len(handles), "erased": erased}
