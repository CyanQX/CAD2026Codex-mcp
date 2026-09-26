"""Shared test helpers: config builders and a scriptable in-memory backend manager."""

from __future__ import annotations

import asyncio
import itertools
import sys
from pathlib import Path
from typing import Any

from cad_super_mcp.backend import CallResult
from cad_super_mcp.config import AppConfig, parse_config

FAKE_SERVER = Path(__file__).with_name("fake_mcp_server.py")


def stdio_spec(**overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "enabled": True,
        "kind": "stdio",
        "command": sys.executable,
        "args": [str(FAKE_SERVER)],
        "timeout_seconds": 30,
    }
    spec.update(overrides)
    return spec


def make_config(tmp_path: Path, *, backends: dict[str, Any] | None = None, safety: dict | None = None,
                runtime: dict | None = None) -> AppConfig:
    disabled = {n: {"enabled": False} for n in ("best", "slacker", "official", "felix", "product_help")}
    disabled.update(backends or {})
    rt = {"cross_process_lock": False}
    rt.update(runtime or {})
    return parse_config(
        {
            "workspace_root": str(tmp_path / "ws"),
            "backends": disabled,
            "safety": safety or {},
            "runtime": rt,
        }
    )


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeAutoCAD:
    """A tiny model of what the Slacker COM backend does (handles, layers, entities)."""

    def __init__(self) -> None:
        self.layers: dict[str, dict[str, Any]] = {"0": {"name": "0", "color_aci": 7}}
        self.entities: dict[str, dict[str, Any]] = {}
        self._h = itertools.count(0x2A0)
        self.drawing = {"name": "test.dwg", "path": "C:/tmp/test.dwg", "saved": True, "readonly": False,
                        "insunits": 4, "insunits_name": "millimetres", "coordinates_contract": "millimetres",
                        "unit_warning": None}
        self.running = True

    def handle(self) -> str:
        return format(next(self._h), "X")


class FakeBackends:
    """Drop-in for BackendManager: records calls and answers like the real backends would.

    ``script`` maps ``"backend.tool"`` to a callable ``(args) -> structured`` (or an exception to raise).
    Slacker's drawing tools are modelled by :class:`FakeAutoCAD`.
    """

    def __init__(self, cad: FakeAutoCAD | None = None, script: dict[str, Any] | None = None) -> None:
        self.cad = cad or FakeAutoCAD()
        self.script = dict(script or {})
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.read_only_flags: list[bool] = []
        self.delay = 0.0

    # --- BackendManager surface -------------------------------------------------
    def spec(self, name: str):  # pragma: no cover - not used by Router
        raise NotImplementedError

    def is_persistent(self, name: str) -> bool:
        return False

    def session_info(self) -> dict[str, Any]:
        return {}

    async def list_tools(self, name: str) -> dict[str, Any]:
        return {"backend": name, "ok": True, "protocol_version": "test", "server_name": name, "tools": []}

    async def close(self) -> None:
        return None

    async def call(self, name: str, tool: str, arguments: dict[str, Any] | None = None, *, read_only: bool = False):
        args = dict(arguments or {})
        self.calls.append((name, tool, args))
        self.read_only_flags.append(read_only)
        if self.delay:
            await asyncio.sleep(self.delay)
        key = f"{name}.{tool}"
        if key in self.script:
            handler = self.script[key]
            if isinstance(handler, BaseException):
                raise handler
            payload = handler(args) if callable(handler) else handler
            if isinstance(payload, CallResult):
                return payload
            return CallResult(backend=name, tool=tool, ok=True, structured=payload, content=[])
        payload = self._model(name, tool, args)
        return CallResult(backend=name, tool=tool, ok=True, structured=payload, content=[])

    # --- a small model of Slacker -------------------------------------------------
    def _entity_result(self, message: str, handle: str, obj: str, layer: str, **extra: Any) -> dict[str, Any]:
        data = {"entity": {"handle": handle, "object_name": obj, "layer": layer, "color_aci": 256, "linetype": "ByLayer"}}
        data.update(extra)
        return {"ok": True, "message": message, "data": data}

    def _model(self, name: str, tool: str, a: dict[str, Any]) -> Any:
        cad = self.cad
        if name != "slacker" and name != "best":
            return {"ok": True}
        if not cad.running and name == "slacker":
            return {"ok": False, "message": "No running AutoCAD session was found. (-2147221021, 'The operation is unavailable')"}
        if name == "slacker":
            if tool in ("autocad_status", "get_active_drawing_info"):
                key = "active_document" if tool == "autocad_status" else "drawing"
                return {"ok": True, "message": "info", "data": {key: dict(cad.drawing)}}
            if tool == "list_layers":
                return {"ok": True, "message": "layers", "data": {"layers": list(cad.layers.values())}}
            if tool == "list_layouts":
                return {"ok": True, "message": "layouts", "data": {"layouts": [{"name": "Model"}, {"name": "A3"}]}}
            if tool == "list_blocks":
                return {"ok": True, "message": "blocks", "data": {"blocks": [{"name": "DOOR"}]}}
            if tool == "list_open_drawings":
                return {"ok": True, "message": "open", "data": {"drawings": [dict(cad.drawing)]}}
            if tool == "create_layer":
                cad.layers.setdefault(a["name"], {"name": a["name"], "color_aci": a.get("color_aci", 7)})
                return {"ok": True, "message": "layer", "data": {"layer": cad.layers[a["name"]]}}
            if tool.startswith("draw_") or tool in ("insert_block", "add_linear_dimension"):
                layer = a.get("layer") or "0"
                if layer not in cad.layers:
                    return {"ok": False, "message": f"Layer {layer!r} does not exist"}
                h = cad.handle()
                obj = {"draw_line": "AcDbLine", "draw_circle": "AcDbCircle", "draw_arc": "AcDbArc",
                       "draw_polyline": "AcDbPolyline", "draw_text": "AcDbText"}.get(tool, "AcDbEntity")
                cad.entities[h] = {"handle": h, "tool": tool, "args": a, "layer": layer, "object_name": obj}
                return self._entity_result("Added.", h, obj, layer)
            if tool == "set_entity_layer":
                e = cad.entities.get(a["handle"])
                if e is None or a["layer"] not in cad.layers:
                    return {"ok": False, "message": "No such entity/layer"}
                e["layer"] = a["layer"]
                return self._entity_result("Updated entity layer.", e["handle"], e["object_name"], e["layer"])
            if tool == "erase_entity":
                if a["handle"] not in cad.entities:
                    return {"ok": False, "message": f"No accessible entity exists with handle {a['handle']!r}"}
                del cad.entities[a["handle"]]
                return {"ok": True, "message": "Erased entity."}
            if tool == "move_entity":
                e = cad.entities.get(a["handle"])
                if e is None:
                    return {"ok": False, "message": "No such entity"}
                return self._entity_result("Moved entity.", e["handle"], e["object_name"], e["layer"])
            if tool == "copy_entity":
                src = cad.entities.get(a["handle"])
                if src is None:
                    return {"ok": False, "message": "No such entity"}
                h = cad.handle()
                cad.entities[h] = {**src, "handle": h}
                return self._entity_result("Copied entity.", h, src["object_name"], src["layer"])
            if tool == "query_entities":
                items = [e for e in cad.entities.values() if not a.get("layer") or e["layer"] == a["layer"]]
                return {"ok": True, "message": "q", "data": {"entities": [
                    {"handle": e["handle"], "object_name": e["object_name"], "layer": e["layer"]} for e in items]}}
            if tool == "capture_screenshot":
                return {"ok": True, "message": "shot"}
            if tool == "zoom_extents":
                return {"ok": True, "message": "zoomed"}
            return {"ok": True, "message": tool}
        # best: geometry read-back is scripted per test
        return {"success": True}
