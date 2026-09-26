"""What each ``cad_*`` tool actually does, independent of the MCP transport.

Every drawing operation follows one pipeline::

    resolve points (exact mm, anchors, units)  ->  write through the COM backend
      ->  read the entity back and compare with what was asked  ->  narrate in one sentence

so the model never does coordinate arithmetic, a wrong result is *noticed*, and a human gets
an answer they can read aloud ("drew a 3600 mm line on A-WALL, handle 2A3, verified").
"""

from __future__ import annotations

import inspect
import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import geometry as G
from .errors import GatewayError, InvalidArgument, NotAllowed, to_gateway_error
from .geometry import Pt
from .journal import Journal, handle_of
from .precision import (
    GRAMMAR,
    EntityGeometry,
    PointResolver,
    RouterGeometry,
    describe_check_failures,
    load_units,
    unit_warnings,
    verify_geometry,
)
from .results import Reply, from_call
from .router import Router, drawing_identity
from .staging import StageManager
from .units import INSUNITS_NAMES, fmt, parse_length_mm, parse_number, to_drawing_units, units_per_mm

ALIGN_CODES = {
    "left": 0, "center": 1, "right": 2, "middle": 4,
    "top_left": 6, "top_center": 7, "top_right": 8,
    "middle_left": 9, "middle_center": 10, "middle_right": 11,
    "bottom_left": 12, "bottom_center": 13, "bottom_right": 14,
}


# ------------------------------------------------------------------ shared state
class NameRegistry:
    """Human names for entities ("west wall" -> handle 2A3), remembered per drawing."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, dict[str, str]] = {}
        self._loaded = False

    def _load(self) -> None:
        if not self._loaded:
            self._loaded = True
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - first run / unreadable: start empty
                self._data = {}

    def for_drawing(self, drawing_id: str) -> dict[str, str]:
        self._load()
        return self._data.setdefault(drawing_id, {})

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass


class GatewayState:
    """Everything the gateway remembers between tool calls (kept out of the model's head)."""

    def __init__(self, router: Router) -> None:
        self.router = router
        self.journal = Journal()
        self.stages = StageManager(router, self.journal, router.config.runtime)
        self.names = NameRegistry(Path(router.config.workspace_root) / ".cad_super" / "names.json")
        self._geom: dict[str, dict[str, EntityGeometry]] = {}
        router.add_hook(self.journal.on_event)
        router.add_hook(self._on_write)

    def geom_cache(self, drawing_id: str) -> dict[str, EntityGeometry]:
        return self._geom.setdefault(drawing_id, {})

    def _on_write(self, event: dict[str, Any]) -> None:
        if event["tool"] in ("move_entity", "erase_entity", "set_entity_layer"):
            handle = (event.get("args") or {}).get("handle")
            if handle:
                for cache in self._geom.values():
                    cache.pop(str(handle).upper(), None)  # what we remembered drawing is no longer true


class Ctx:
    """Per-request helper bundle: units, exact geometry access, alias resolution."""

    def __init__(self, state: GatewayState) -> None:
        self.state = state
        self.upm: float | None = None
        self.ident: dict[str, Any] | None = None
        self.warnings: list[str] = []
        self.provider: RouterGeometry | None = None
        self.names: dict[str, str] = {}
        self.resolver = PointResolver(None)
        self.tol = state.router.config.runtime.verify_tolerance_mm

    @classmethod
    async def open(cls, state: GatewayState) -> Ctx:
        ctx = cls(state)
        router = state.router
        ctx.upm, ctx.ident, ctx.warnings = await load_units(router)
        did = ctx.ident["id"] if ctx.ident else "unknown"
        ctx.names = state.names.for_drawing(did)
        if router.config.best.enabled:
            ctx.provider = RouterGeometry(router, ctx.upm, state.geom_cache(did))
        ctx.resolver = PointResolver(ctx.provider, ctx.names)
        return ctx

    def du(self, mm: float) -> float:
        return to_drawing_units(mm, self.upm)

    def handle(self, ref: Any) -> str:
        return self.names.get(str(ref), str(ref))


# ---------------------------------------------------------------------- helpers
def _pt_text(p: Pt) -> str:
    return f"({fmt(p.x)}, {fmt(p.y)}" + (f", {fmt(p.z)})" if abs(p.z) > 1e-9 else ")")


def _layer_text(layer: str | None) -> str:
    return f"layer {layer}" if layer else "the current layer"


def _failure(out: dict[str, Any], *, layer_hint: bool = False) -> Reply:
    reply = from_call(out)
    err = reply.error
    if err and layer_hint and "layer" in str(err.get("message", "")).lower() and not err.get("hint"):
        err["hint"] = "The layer does not exist or is unavailable: check cad_list_layers first; if needed, create it with cad_layer(action='create', name=...)."
    return reply


def _positive(value: Any, name: str) -> float:
    v = parse_length_mm(value, name)
    if v <= 0:
        raise InvalidArgument(f"{name} must be greater than 0 (got {fmt(v)})")
    return v


def _geometry_from_expected(handle: str, expected: dict[str, Any], layer: str | None) -> EntityGeometry | None:
    kind = expected.get("kind")
    g = EntityGeometry(handle=handle, kind=str(kind), layer=layer)
    if kind == "line":
        g.points = {"start": expected["start"], "end": expected["end"], "mid": G.midpoint(expected["start"], expected["end"])}
    elif kind == "circle":
        g.points = {"center": expected["center"]}
        g.radius = expected["radius"]
    elif kind == "arc":
        g.points = {k: expected[k] for k in ("center", "start", "end")}
        g.radius = expected["radius"]
    elif kind == "polyline":
        g.vertices = list(expected["vertices"])
        g.closed = bool(expected.get("closed"))
    else:
        return None
    return g


def _verify_note(v: dict[str, Any] | None) -> str:
    if v is None:
        return ""
    if v["verified"] is True:
        return f", read-back verification passed (max deviation {fmt(v['max_deviation_mm'])} mm)"
    if v["verified"] is False:
        return f", WARNING: read-back verification FAILED - {describe_check_failures(v)}"
    return f" (could not verify by read-back: {v.get('reason') or v.get('note') or 'read-back channel unavailable'})"


def _compact_verification(v: dict[str, Any] | None) -> dict[str, Any] | None:
    if v is None:
        return None
    out = {k: v[k] for k in ("verified", "max_deviation_mm", "tolerance_mm", "reason", "note") if k in v}
    if v.get("verified") is not True:
        out["checks"] = v.get("checks", [])
    return out


async def _emit(
    state: GatewayState,
    ctx: Ctx,
    *,
    tool: str,
    args: dict[str, Any],
    layer: str | None,
    stage: bool,
    verify: bool | None,
    expected: dict[str, Any] | None,
    label: str,
    describe: str,
    data: dict[str, Any],
) -> Reply:
    """Write one primitive through Slacker, optionally staged, then read it back and narrate."""

    router = state.router
    warnings = list(ctx.warnings)
    drawn_layer = layer
    stage_obj = None
    if stage:
        stage_obj = await state.stages.ensure_open()
        await state.stages.check_target_layer(layer)
        drawn_layer = state.stages.layer
    if drawn_layer:
        args = {**args, "layer": drawn_layer}
    out = await router.slacker(tool, args)
    if not out["ok"]:
        return _failure(out, layer_hint=True)
    handle = handle_of(out)
    if handle and expected is not None and ctx.ident:
        cached = _geometry_from_expected(handle, expected, drawn_layer)
        if cached is not None:
            state.geom_cache(ctx.ident["id"])[handle.upper()] = cached
    verification = None
    want_verify = router.config.runtime.verify_writes if verify is None else verify
    if want_verify and handle and expected is not None:
        verification = await verify_geometry(ctx.provider, handle, {**expected, "layer": drawn_layer}, ctx.tol)
        if verification["verified"] is False:
            warnings.append("Read-back verification failed; report data.verification.checks to the user honestly and do not claim success.")
    if stage_obj is not None and handle:
        state.stages.add(stage_obj, handle, tool, layer, f"{label} {handle}")
    payload: dict[str, Any] = {"handle": handle, **data, "layer": drawn_layer}
    if stage_obj is not None:
        payload.update(staged=True, stage=stage_obj.id, target_layer=layer)
    compact = _compact_verification(verification)
    if compact is not None:
        payload["verification"] = compact
    where = _layer_text(drawn_layer)
    summary = f"Drew {label} on {where}: {describe} (handle {handle})"
    if stage_obj is not None:
        summary += f"; parked on staging layer {state.stages.layer} (not committed; target {_layer_text(layer)}; run cad_stage commit to keep it or discard to drop it)"
    summary += _verify_note(verification)
    meta = {k: out[k] for k in ("backend", "tool", "call_id", "elapsed_ms") if out.get(k) is not None}
    return Reply(data=payload, summary=summary, warnings=warnings, meta=meta)


def _vertices_lists(vs: list[Pt]) -> list[list[float]]:
    three_d = any(abs(v.z) > 1e-9 for v in vs)
    return [v.as_list(dims=3 if three_d else 2) for v in vs]


# ------------------------------------------------------------------- primitives
async def op_line(state: GatewayState, start_mm: Any, end_mm: Any, layer: str | None = None, space: str = "model",
                  stage: bool = False, verify: bool | None = None) -> Reply:
    async with state.router.session():
        ctx = await Ctx.open(state)
        p1 = await ctx.resolver.resolve(start_mm, "start_mm")
        p2 = await ctx.resolver.resolve(end_mm, "end_mm")
        length = p1.dist(p2)
        if length < 1e-9:
            raise InvalidArgument("start and end points coincide; cannot draw a line")
        return await _emit(
            state, ctx, tool="draw_line", args={"start_mm": p1.as_list(), "end_mm": p2.as_list(), "space": space},
            layer=layer, stage=stage, verify=verify, expected={"kind": "line", "start": p1, "end": p2}, label="line",
            describe=f"{_pt_text(p1)} -> {_pt_text(p2)}, length {fmt(length)} mm, direction {fmt(G.angle_deg(p1, p2))}°",
            data={"start_mm": p1.as_list(), "end_mm": p2.as_list(), "length_mm": round(length, 6),
                  "angle_deg": round(G.angle_deg(p1, p2), 6)},
        )


async def op_circle(state: GatewayState, center_mm: Any, radius_mm: Any = None, diameter_mm: Any = None,
                    layer: str | None = None, space: str = "model", stage: bool = False, verify: bool | None = None) -> Reply:
    if (radius_mm is None) == (diameter_mm is None):
        raise InvalidArgument("pass exactly one of radius_mm or diameter_mm")
    r = _positive(radius_mm, "radius_mm") if radius_mm is not None else _positive(diameter_mm, "diameter_mm") / 2
    async with state.router.session():
        ctx = await Ctx.open(state)
        c = await ctx.resolver.resolve(center_mm, "center_mm")
        return await _emit(
            state, ctx, tool="draw_circle", args={"center_mm": c.as_list(), "radius_mm": round(r, 9), "space": space},
            layer=layer, stage=stage, verify=verify, expected={"kind": "circle", "center": c, "radius": r}, label="circle",
            describe=f"center {_pt_text(c)}, radius {fmt(r)} mm (diameter {fmt(2 * r)})",
            data={"center_mm": c.as_list(), "radius_mm": round(r, 6), "diameter_mm": round(2 * r, 6),
                  "area_mm2": round(math.pi * r * r, 3)},
        )


async def op_arc(state: GatewayState, center_mm: Any, radius_mm: Any, start_angle_deg: Any, end_angle_deg: Any,
                 layer: str | None = None, space: str = "model", stage: bool = False, verify: bool | None = None) -> Reply:
    r = _positive(radius_mm, "radius_mm")
    a0, a1 = parse_number(start_angle_deg, "start_angle_deg"), parse_number(end_angle_deg, "end_angle_deg")
    async with state.router.session():
        ctx = await Ctx.open(state)
        c = await ctx.resolver.resolve(center_mm, "center_mm")
        pts = G.arc_points(c, r, a0, a1)
        return await _emit(
            state, ctx, tool="draw_arc",
            args={"center_mm": c.as_list(), "radius_mm": round(r, 9), "start_angle_deg": a0, "end_angle_deg": a1, "space": space},
            layer=layer, stage=stage, verify=verify,
            expected={"kind": "arc", "center": c, "radius": r, "start": pts["start"], "end": pts["end"]}, label="arc",
            describe=f"center {_pt_text(c)}, radius {fmt(r)} mm, {fmt(a0)}° -> {fmt(a1)}° (counter-clockwise, sweep {fmt(pts['sweep_deg'])}°)",
            data={"center_mm": c.as_list(), "radius_mm": round(r, 6), "start_mm": pts["start"].as_list(),
                  "end_mm": pts["end"].as_list(), "mid_mm": pts["mid"].as_list(), "sweep_deg": round(pts["sweep_deg"], 6)},
        )


async def op_polyline(state: GatewayState, vertices_mm: list[Any], closed: bool = False, layer: str | None = None,
                      space: str = "model", stage: bool = False, verify: bool | None = None) -> Reply:
    if not isinstance(vertices_mm, list) or len(vertices_mm) < 2:
        raise InvalidArgument("vertices_mm needs at least 2 vertices", hint=GRAMMAR)
    async with state.router.session():
        ctx = await Ctx.open(state)
        vs = [await ctx.resolver.resolve(v, f"vertices_mm[{i}]") for i, v in enumerate(vertices_mm)]
        length = G.path_length(vs, closed)
        data: dict[str, Any] = {"vertices_mm": [v.as_list() for v in vs], "vertex_count": len(vs), "closed": closed,
                                "length_mm": round(length, 6)}
        text = f"{len(vs)} vertices, {'closed, ' if closed else ''}perimeter/length {fmt(length)} mm"
        if closed and len(vs) >= 3:
            area = G.polygon_area(vs)
            data["area_mm2"] = round(area, 3)
            data["area_m2"] = round(area / 1e6, 4)
            text += f", area {fmt(area / 1e6)} m²"
        return await _emit(
            state, ctx, tool="draw_polyline", args={"vertices_mm": _vertices_lists(vs), "closed": closed, "space": space},
            layer=layer, stage=stage, verify=verify, expected={"kind": "polyline", "vertices": vs, "closed": closed},
            label="polyline", describe=text, data=data,
        )


async def op_rect(state: GatewayState, width: Any, height: Any, corner: Any = None, center: Any = None,
                  rotation_deg: Any = 0.0, layer: str | None = None, space: str = "model", stage: bool = False,
                  verify: bool | None = None) -> Reply:
    if (corner is None) == (center is None):
        raise InvalidArgument("pass exactly one of corner (bottom-left) or center")
    w, h = _positive(width, "width"), _positive(height, "height")
    rot = parse_number(rotation_deg, "rotation_deg")
    async with state.router.session():
        ctx = await Ctx.open(state)
        anchor = await ctx.resolver.resolve(corner if corner is not None else center, "corner" if corner is not None else "center")
        vs = G.rect_vertices(anchor, w, h, anchor_kind="corner" if corner is not None else "center", rotation=rot)
        area = w * h
        ctr = G.polygon_centroid(vs)
        return await _emit(
            state, ctx, tool="draw_polyline", args={"vertices_mm": _vertices_lists(vs), "closed": True, "space": space},
            layer=layer, stage=stage, verify=verify, expected={"kind": "polyline", "vertices": vs, "closed": True},
            label="rectangle", describe=f"{fmt(w)} x {fmt(h)} mm, {'rotated ' + fmt(rot) + '°, ' if rot else ''}center {_pt_text(ctr)}, area {fmt(area / 1e6)} m²",
            data={"vertices_mm": [v.as_list() for v in vs], "width_mm": round(w, 6), "height_mm": round(h, 6),
                  "rotation_deg": rot, "center_mm": ctr.as_list(), "area_mm2": round(area, 3), "area_m2": round(area / 1e6, 4),
                  "perimeter_mm": round(2 * (w + h), 6)},
        )


async def op_wall(state: GatewayState, centerline: list[Any], thickness: Any, closed: bool = False, justify: str = "center",
                  layer: str | None = None, space: str = "model", stage: bool = False, verify: bool | None = None) -> Reply:
    """A wall of given thickness along a centre line: the gateway computes the mitred outline exactly."""

    if not isinstance(centerline, list) or len(centerline) < 2:
        raise InvalidArgument("centerline needs at least 2 points", hint=GRAMMAR)
    t = _positive(thickness, "thickness")
    if justify not in ("center", "left", "right"):
        raise InvalidArgument(f"unknown justify {justify!r}", hint="center | left | right (relative to the travel direction)")
    async with state.router.session():
        ctx = await Ctx.open(state)
        pts = [await ctx.resolver.resolve(p, f"centerline[{i}]") for i, p in enumerate(centerline)]
        try:
            shape = G.wall_outline(pts, t, closed=closed, justify=justify)
        except ValueError as exc:
            raise InvalidArgument(f"cannot build the wall outline: {exc}") from exc
        loops = [shape["left"], shape["right"]] if closed else [shape["outline"]]
        length = G.path_length(pts, closed)
        replies: list[Reply] = []
        for i, loop in enumerate(loops):
            reply = await _emit(
                state, ctx, tool="draw_polyline", args={"vertices_mm": _vertices_lists(loop), "closed": True, "space": space},
                layer=layer, stage=stage, verify=verify, expected={"kind": "polyline", "vertices": loop, "closed": True},
                label="wall outline" if not closed else ("wall inner loop" if i == 0 else "wall outer loop"),
                describe=f"{fmt(t)} mm thick, centreline length {fmt(length)} mm, {len(loop)} vertices",
                data={"vertices_mm": [v.as_list() for v in loop], "area_mm2": round(G.polygon_area(loop), 3)},
            )
            if not reply.ok:
                return reply
            replies.append(reply)
        handles = [r.data["handle"] for r in replies]
        verified = [r.data.get("verification", {}).get("verified") for r in replies]
        return Reply(
            data={"handles": handles, "thickness_mm": round(t, 6), "centerline_length_mm": round(length, 6), "closed": closed,
                  "justify": justify, "outlines": [r.data for r in replies]},
            summary=f"Wall drawn: {fmt(t)} mm thick, centreline length {fmt(length)} mm, {'inner + outer loops' if closed else 'one closed outline'} (handles {', '.join(handles)})"
                    + ("; read-back verification passed for all outlines" if all(v is True for v in verified) else ("; WARNING: some outlines failed read-back verification" if any(v is False for v in verified) else "")),
            warnings=[w for r in replies for w in r.warnings], meta=replies[0].meta,
        )


async def op_dimension(state: GatewayState, start_mm: Any, end_mm: Any, offset: Any = 500, orientation: str = "aligned",
                       layer: str | None = None, space: str = "model", stage: bool = False) -> Reply:
    """A linear dimension whose dimension-line position is computed by the gateway (offset from the measured points)."""

    if orientation not in ("aligned", "horizontal", "vertical"):
        raise InvalidArgument(f"unknown orientation {orientation!r}", hint="aligned | horizontal | vertical")
    off = parse_length_mm(offset, "offset")
    async with state.router.session():
        ctx = await Ctx.open(state)
        p1 = await ctx.resolver.resolve(start_mm, "start_mm")
        p2 = await ctx.resolver.resolve(end_mm, "end_mm")
        if p1.dist(p2) < 1e-9:
            raise InvalidArgument("the two dimension points coincide")
        mid = G.midpoint(p1, p2)
        if orientation == "aligned":
            length = p1.dist2d(p2)
            nx, ny = -(p2.y - p1.y) / length, (p2.x - p1.x) / length          # left normal of p1->p2
            dim_pt = Pt(mid.x + nx * off, mid.y + ny * off, mid.z)
            measured = length
        elif orientation == "horizontal":
            dim_pt = Pt(mid.x, max(p1.y, p2.y) + off, mid.z)
            measured = abs(p2.x - p1.x)
        else:
            dim_pt = Pt(max(p1.x, p2.x) + off, mid.y, mid.z)
            measured = abs(p2.y - p1.y)
        if measured < 1e-9:
            raise InvalidArgument(f"the two points are 0 apart in that direction; cannot make a {orientation} dimension", hint="use aligned, or pick different points")
        return await _emit(
            state, ctx, tool="add_linear_dimension",
            args={"start_mm": p1.as_list(), "end_mm": p2.as_list(), "dimension_line_point_mm": dim_pt.as_list(),
                  "orientation": orientation, "space": space},
            layer=layer, stage=stage, verify=False, expected=None, label="dimension",
            describe=f"{_pt_text(p1)} <-> {_pt_text(p2)}, measured {fmt(measured)} mm ({orientation}), dimension line offset {fmt(off)} mm",
            data={"start_mm": p1.as_list(), "end_mm": p2.as_list(), "dimension_line_point_mm": dim_pt.as_list(),
                  "orientation": orientation, "measured_mm": round(measured, 6)},
        )


async def op_text(state: GatewayState, text: str, at: Any, height_mm: Any, layer: str | None = None, space: str = "model",
                  rotation_deg: Any = 0.0, align: str | None = None, stage: bool = False, verify: bool | None = None) -> Reply:
    if not isinstance(text, str) or not text.strip():
        raise InvalidArgument("text must not be empty")
    h = _positive(height_mm, "height_mm")
    rot = parse_number(rotation_deg, "rotation_deg")
    if align is not None and align not in ALIGN_CODES:
        raise InvalidArgument(f"unknown alignment {align!r}", hint="available: " + ", ".join(ALIGN_CODES))
    simple = rot == 0 and align in (None, "left")
    async with state.router.session():
        ctx = await Ctx.open(state)
        p = await ctx.resolver.resolve(at, "at")
        expected = {"kind": "text", "text": text, "height": h, "insertion": p if simple else None}
        data = {"text": text, "height_mm": round(h, 6), "at_mm": p.as_list(), "rotation_deg": rot, "align": align or "left"}
        if simple:
            return await _emit(
                state, ctx, tool="draw_text", args={"text": text, "insertion_point_mm": p.as_list(), "height_mm": round(h, 9), "space": space},
                layer=layer, stage=stage, verify=verify, expected=expected, label="text",
                describe=f'"{text}", height {fmt(h)} mm, at {_pt_text(p)}', data=data,
            )
        # Rotation / alignment: Slacker has neither, so draw with best (drawing units!) and fix up.
        if space != "model":
            raise InvalidArgument("rotated/aligned text is currently model-space only; in paper space use unrotated, left-aligned text")
        return await _text_via_best(state, ctx, text, p, h, rot, align, layer, stage, verify, expected, data)


async def _text_via_best(state: GatewayState, ctx: Ctx, text: str, p: Pt, h: float, rot: float, align: str | None,
                         layer: str | None, stage: bool, verify: bool | None, expected: dict[str, Any], data: dict[str, Any]) -> Reply:
    router = state.router
    warnings = list(ctx.warnings) + ["rotated/aligned text goes through the best backend and has not been verified on a real AutoCAD: eyeball it, or preview with stage=true first."]
    stage_obj = None
    target_layer = layer
    if stage:
        stage_obj = await state.stages.ensure_open()
        await state.stages.check_target_layer(layer)
        target_layer = state.stages.layer
    # No `layer` is passed to best on purpose: best would make that layer *current* as a side effect.
    out = await router.best(
        "draw_text",
        {"text": text, "insert_x": ctx.du(p.x), "insert_y": ctx.du(p.y), "z": ctx.du(p.z), "height": ctx.du(h), "rotation": rot},
        lane="draw",
    )
    if not out["ok"]:
        return from_call(out)
    handle = handle_of(out)
    if not handle:
        raise GatewayError("best drew the text but returned no handle, so alignment/layer cannot be set; find it with cad_query_entities", code="HANDLE_UNKNOWN")
    if align and ALIGN_CODES[align] != 0:
        aligned = await router.best(
            "set_text_alignment",
            {"handle": handle, "alignment": ALIGN_CODES[align], "align_x": ctx.du(p.x), "align_y": ctx.du(p.y), "align_z": ctx.du(p.z)},
            lane="edit",
        )
        if not aligned["ok"]:
            warnings.append(f"text was drawn (handle {handle}) but setting the alignment failed: {(aligned['error'] or {}).get('message')}")
    if target_layer:
        moved = await router.slacker("set_entity_layer", {"handle": handle, "layer": target_layer})
        if not moved["ok"]:
            warnings.append(f"text was drawn (handle {handle}) but moving it to layer {target_layer} failed: {(moved['error'] or {}).get('message')}")
    verification = None
    if (router.config.runtime.verify_writes if verify is None else verify):
        verification = await verify_geometry(ctx.provider, handle, {**expected, "layer": target_layer}, ctx.tol)
    if stage_obj is not None:
        state.stages.add(stage_obj, handle, "draw_text", layer, f"text {handle}")
    payload = {"handle": handle, **data, "layer": target_layer, "via": "best"}
    if stage_obj is not None:
        payload.update(staged=True, stage=stage_obj.id, target_layer=layer)
    compact = _compact_verification(verification)
    if compact is not None:
        payload["verification"] = compact
    summary = (f'Wrote text "{text}" on {_layer_text(target_layer)}: height {fmt(h)} mm, at {_pt_text(p)}'
               f"{', rotated ' + fmt(rot) + '°' if rot else ''}{', align ' + align if align else ''} (handle {handle})")
    if stage_obj is not None:
        summary += f"; parked on the staging layer (not committed; target {_layer_text(layer)})"
    summary += _verify_note(verification)
    return Reply(data=payload, summary=summary, warnings=warnings, meta={"backend": "best", "tool": "draw_text"})


# ------------------------------------------------------------------------ edits
async def op_move(state: GatewayState, handle: str, from_mm: Any, to_mm: Any) -> Reply:
    async with state.router.session():
        ctx = await Ctx.open(state)
        h = ctx.handle(handle)
        a = await ctx.resolver.resolve(from_mm, "from_mm")
        b = await ctx.resolver.resolve(to_mm, "to_mm")
        d = b - a
        if d.dist(Pt(0, 0, 0)) < 1e-9:
            raise InvalidArgument("displacement is zero; nothing to move")
        out = await state.router.slacker("move_entity", {"handle": h, "from_mm": a.as_list(), "to_mm": b.as_list()})
        if not out["ok"]:
            return _failure(out)
        return Reply(
            data={"handle": h, "from_mm": a.as_list(), "to_mm": b.as_list(), "displacement_mm": d.as_list()},
            summary=f"Moved {h} by ({fmt(d.x)}, {fmt(d.y)}) mm (displacement {fmt(d.dist(Pt(0, 0, 0)))} mm; reversible with cad_undo)",
            warnings=list(ctx.warnings), meta={"backend": "slacker", "tool": "move_entity", "call_id": out["call_id"]},
        )


async def op_copy(state: GatewayState, handle: str, displacement_mm: Any = None) -> Reply:
    async with state.router.session():
        ctx = await Ctx.open(state)
        h = ctx.handle(handle)
        args: dict[str, Any] = {"handle": h}
        disp = None
        if displacement_mm is not None:
            if not isinstance(displacement_mm, (list, tuple)) or len(displacement_mm) not in (2, 3):
                raise InvalidArgument("displacement_mm needs [dx, dy] or [dx, dy, dz]")
            disp = Pt.of([parse_length_mm(v, f"displacement_mm[{i}]") for i, v in enumerate(displacement_mm)])
            args["displacement_mm"] = disp.as_list()
        out = await state.router.slacker("copy_entity", args)
        if not out["ok"]:
            return _failure(out)
        new = handle_of(out)
        where = f"offset ({fmt(disp.x)}, {fmt(disp.y)}) mm" if disp else "in place (overlapping the original)"
        return Reply(data={"source": h, "handle": new, "displacement_mm": disp.as_list() if disp else None},
                     summary=f"Copied {h}; new handle {new}, {where}", warnings=list(ctx.warnings),
                     meta={"backend": "slacker", "tool": "copy_entity", "call_id": out["call_id"]})


async def op_erase(state: GatewayState, handle: str, confirm: bool = False) -> Reply:
    state.router.require_confirm("erase_entity", confirm)  # cheapest check first: no I/O for a refusal
    async with state.router.session():
        ctx = await Ctx.open(state)
        h = ctx.handle(handle)
        out = await state.router.slacker("erase_entity", {"handle": h}, confirm=confirm)
        if not out["ok"]:
            return _failure(out)
        return Reply(data={"handle": h}, summary=f"Erased {h} (cad_undo cannot restore it; use UNDO inside AutoCAD)",
                     meta={"backend": "slacker", "tool": "erase_entity", "call_id": out["call_id"]})


async def op_layer(state: GatewayState, action: str, name: str | None = None, color_aci: int | None = None,
                   locked: bool | None = None, frozen: bool | None = None, visible: bool | None = None,
                   make_current: bool | None = None) -> Reply:
    router = state.router
    if not name:
        raise InvalidArgument("name must not be empty")
    async with router.session():
        if action == "create":
            args: dict[str, Any] = {"name": name}
            if color_aci is not None:
                args["color_aci"] = color_aci
            if make_current is not None:
                args["make_current"] = make_current
            out = await router.slacker("create_layer", args)
            text = f"layer {name} is ready (returned as-is if it already exists)"
        elif action == "set_current":
            out = await router.slacker("set_current_layer", {"name": name})
            text = f"layer {name} is now current"
        elif action == "set_props":
            props = {k: v for k, v in (("color_aci", color_aci), ("locked", locked), ("frozen", frozen), ("visible", visible)) if v is not None}
            if not props:
                raise InvalidArgument("set_props needs at least one of color_aci / locked / frozen / visible")
            out = await router.slacker("set_layer_properties", {"name": name, **props})
            text = f"layer {name} updated: " + ", ".join(f"{k}={v}" for k, v in props.items())
        else:
            raise InvalidArgument(f"unknown action {action!r}", hint="create | set_current | set_props")
        if not out["ok"]:
            return _failure(out)
        return Reply(data=out.get("structured"), summary=text, meta={"backend": "slacker", "call_id": out["call_id"]})


async def op_document(state: GatewayState, action: str, path: str | None = None, template_path: str | None = None) -> Reply:
    router = state.router
    async with router.session():
        if action == "open":
            if not path:
                raise InvalidArgument("open needs path")
            roots = router.config.safety.allowed_open_roots
            if roots:
                resolved = Path(path).expanduser().resolve()
                if not any(resolved.is_relative_to(Path(r).expanduser().resolve()) for r in roots):
                    raise NotAllowed(f"{path} is outside the directories allowed for opening", details={"allowed_open_roots": roots},
                                     hint="Add that directory to safety.allowed_open_roots in config.json, or use a file inside an allowed directory.")
            out = await router.slacker("open_drawing", {"path": path})
        elif action == "new":
            out = await router.slacker("create_new_drawing", {"template_path": template_path} if template_path else {})
        else:
            raise InvalidArgument(f"unknown action {action!r}", hint="open | new")
        if not out["ok"]:
            return _failure(out)
        ident = drawing_identity(out.get("structured"))
        name = ident["name"] if ident else path or "new drawing"
        warnings = unit_warnings(ident.get("insunits")) if ident else []
        return Reply(data=out.get("structured"), summary=f"{'Opened' if action == 'open' else 'Created'} drawing {name}; subsequent writes will target it", warnings=warnings,
                     meta={"backend": "slacker", "call_id": out["call_id"]})


# ---------------------------------------------------------------------- context
def _first_list(data: Any) -> list[Any]:
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                return v
    return data if isinstance(data, list) else []


def _payload(out: dict[str, Any]) -> Any:
    s = out.get("structured")
    return s.get("data", s) if isinstance(s, dict) else s


async def op_context(state: GatewayState, pin: bool = False, max_layers: int = 200) -> Reply:
    """One call that grounds the model: is AutoCAD up, which drawing, what units, which layers/layouts."""

    router = state.router
    async with router.session():
        status = await router.slacker("autocad_status", {})
        if not status["ok"]:
            return _failure(status)
        ident = drawing_identity(status.get("structured"))
        layers_out = await router.slacker("list_layers", {})
        layouts_out = await router.slacker("list_layouts", {})
        blocks_out = await router.slacker("list_blocks", {})
        opens_out = await router.slacker("list_open_drawings", {})
        info = _payload(status) if isinstance(_payload(status), dict) else {}
        insunits = ident.get("insunits") if ident else None
        upm = units_per_mm(insunits) if isinstance(insunits, int) else None
        layers = _first_list(_payload(layers_out)) if layers_out["ok"] else []
        layouts = _first_list(_payload(layouts_out)) if layouts_out["ok"] else []
        blocks = _first_list(_payload(blocks_out)) if blocks_out["ok"] else []
        opens = _first_list(_payload(opens_out)) if opens_out["ok"] else []
        warnings = unit_warnings(insunits)
        drawing = (info.get("active_document") if isinstance(info.get("active_document"), dict) else {}) or {}
        if drawing.get("readonly"):
            warnings.append("the current drawing is read-only: writes will fail.")
        if pin and ident:
            router.pin(ident)
        pending = [s.brief() for s in state.stages.stages.values() if s.status == "open"]
        data = {
            "autocad": {"version": info.get("version"), "caption": info.get("caption")},
            "drawing": {**(ident or {}), "units": INSUNITS_NAMES.get(insunits, "unknown") if isinstance(insunits, int) else None,
                        "saved": drawing.get("saved"), "readonly": drawing.get("readonly")},
            "units": {"insunits": insunits, "drawing_units_per_mm": upm, "note": "the gateway always speaks millimetres on the outside and converts to drawing units automatically"},
            "layers": layers[:max_layers], "layers_total": len(layers),
            "layouts": layouts, "blocks": [b.get("name", b) if isinstance(b, dict) else b for b in blocks][:200], "open_drawings": opens,
            "pinned": bool(router.pinned), "open_stages": pending,
            "journal_entries": len(state.journal.entries()),
            "point_spec_help": GRAMMAR,
        }
        name = (ident or {}).get("name") or "(unnamed)"
        unit_name = INSUNITS_NAMES.get(insunits, "unknown units") if isinstance(insunits, int) else "unknown units"
        summary = (f"AutoCAD {info.get('version') or ''} is connected; current drawing {name} ({unit_name}), "
                   f"{len(layers)} layers, {len(layouts)} layouts" + ("; drawing pinned (writes to another drawing will be refused)" if pin and ident else ""))
        return Reply(data=data, summary=summary, warnings=warnings, meta={"backend": "slacker"})


# ---------------------------------------------------------------------- measure
class _MathOnly:
    """Stand-in context when AutoCAD is unreachable: plain numeric points still work, geometry lookups re-raise why."""

    def __init__(self, reason: GatewayError) -> None:
        self.reason = reason
        self.warnings = ["AutoCAD is currently unavailable: this was a pure-math computation; no entities were read."]
        self.provider = None
        self.names: dict[str, str] = {}
        self.resolver = _MathResolver(reason)

    def handle(self, ref: Any) -> str:
        return str(ref)


class _MathResolver(PointResolver):
    def __init__(self, reason: GatewayError) -> None:
        super().__init__(None)
        self._reason = reason

    async def _geom(self, ref: Any) -> EntityGeometry:  # any handle/name reference needs AutoCAD
        raise self._reason


async def op_measure(state: GatewayState, kind: str, a: Any = None, b: Any = None, points: list[Any] | None = None,
                     handle: str | None = None) -> Reply:
    async with state.router.session():
        try:
            ctx: Any = await Ctx.open(state)
        except GatewayError as exc:
            if kind == "entity":
                raise
            ctx = _MathOnly(exc)
        if kind == "distance":
            if a is None or b is None:
                raise InvalidArgument("distance needs both points a and b")
            p, q = await ctx.resolver.resolve(a, "a"), await ctx.resolver.resolve(b, "b")
            d = q - p
            data = {"a_mm": p.as_list(), "b_mm": q.as_list(), "distance_mm": round(p.dist(q), 6), "dx_mm": round(d.x, 6),
                    "dy_mm": round(d.y, 6), "angle_deg": round(G.angle_deg(p, q), 6), "midpoint_mm": G.midpoint(p, q).as_list()}
            return Reply(data=data, summary=f"distance {fmt(p.dist(q))} mm (dx {fmt(d.x)}, dy {fmt(d.y)}), direction {fmt(G.angle_deg(p, q))}°",
                         warnings=ctx.warnings)
        if kind in ("area", "length"):
            if not points or len(points) < (3 if kind == "area" else 2):
                raise InvalidArgument(f"{kind} needs points ({'>=3' if kind == 'area' else '>=2'} of them)")
            vs = [await ctx.resolver.resolve(x, f"points[{i}]") for i, x in enumerate(points)]
            lo, hi = G.bbox(vs)
            if kind == "area":
                area = G.polygon_area(vs)
                data = {"area_mm2": round(area, 3), "area_m2": round(area / 1e6, 4), "perimeter_mm": round(G.path_length(vs, True), 6),
                        "centroid_mm": G.polygon_centroid(vs).as_list(), "bbox_mm": [lo.as_list(), hi.as_list()]}
                return Reply(data=data, summary=f"area {fmt(area / 1e6)} m², perimeter {fmt(G.path_length(vs, True))} mm", warnings=ctx.warnings)
            total = G.path_length(vs, False)
            return Reply(data={"length_mm": round(total, 6), "bbox_mm": [lo.as_list(), hi.as_list()]}, summary=f"polyline length {fmt(total)} mm",
                         warnings=ctx.warnings)
        if kind == "entity":
            if not handle:
                raise InvalidArgument("entity needs a handle (or an alias created with cad_name)")
            if ctx.provider is None:
                raise GatewayError("the best backend is not enabled; cannot read entity geometry", code="GEOMETRY_UNAVAILABLE")
            geom = await ctx.provider.entity(ctx.handle(handle))
            pts = geom.vertices or list(geom.points.values())
            extra: dict[str, Any] = {}
            if pts:
                lo, hi = G.bbox(pts)
                extra["bbox_mm"] = [lo.as_list(), hi.as_list()]
            return Reply(data={**geom.summary(), **extra}, summary=f"{geom.kind} {geom.handle} (layer {geom.layer}): geometry read", warnings=ctx.warnings)
        raise InvalidArgument(f"unknown kind {kind!r}", hint="distance | area | length | entity")


# -------------------------------------------------------------------- names
async def op_name(state: GatewayState, action: str, name: str | None = None, handle: str | None = None) -> Reply:
    async with state.router.session():
        ctx = await Ctx.open(state)
        names = ctx.names
        if action == "list":
            return Reply(data={"names": dict(names)}, summary=f"{len(names)} alias(es)" + (": " + ", ".join(names) if names else ""))
        if not name:
            raise InvalidArgument("name must not be empty")
        if action == "set":
            if not handle:
                raise InvalidArgument("set needs a handle")
            names[name] = handle
            state.names.save()
            return Reply(data={"name": name, "handle": handle}, summary=f"named {handle} as \"{name}\"; afterwards use {{\"name\":\"{name}\",\"snap\":...}} or pass \"{name}\" directly wherever a handle is expected")
        if action == "get":
            if name not in names:
                raise InvalidArgument(f"no alias named {name!r}", hint="existing: " + ", ".join(names))
            return Reply(data={"name": name, "handle": names[name]}, summary=f"\"{name}\" = {names[name]}")
        if action == "remove":
            existed = names.pop(name, None)
            state.names.save()
            return Reply(data={"removed": existed is not None}, summary=f"removed alias \"{name}\"" if existed else f"there is no alias \"{name}\"")
        raise InvalidArgument(f"unknown action {action!r}", hint="set | get | list | remove")


# --------------------------------------------------------------------- staging
async def op_stage(state: GatewayState, action: str, stage_id: str | None = None, label: str | None = None,
                   layer: str | None = None) -> Reply:
    sm = state.stages
    if action == "begin":
        stage = await sm.begin(label)
        return Reply(data=stage.brief(), summary=f"staging {stage.id} started: drawing calls with stage=true will draw onto {sm.layer} (bright colour) and leave real layers untouched")
    if action == "commit":
        res = await sm.commit(stage_id, default_layer=layer)
        msg = f"committed {res['committed']} entities to {', '.join(res.get('layers', [])) or 'the target layers'}"
        if res["failed"]:
            msg += f"; {len(res['failed'])} failed (they stay on the staging layer; fix and commit again)"
        return Reply(data=res, summary=msg, ok=not res["failed"],
                     error=None if not res["failed"] else {"code": "STAGE_PARTIAL", "message": "some entities failed to commit", "hint": "See data.failed; fix (e.g. create the layer first) and commit again."})
    if action == "discard":
        res = await sm.discard(stage_id)
        return Reply(data=res, summary=f"staging discarded: erased {res['erased']} entities" + (f", {res['already_gone']} were already gone" if res["already_gone"] else ""),
                     ok=not res["failed"])
    if action == "cleanup":
        res = await sm.cleanup()
        return Reply(data=res, summary=f"staging layer {res['layer']} cleaned: found {res['found']}, erased {res['erased']}")
    raise InvalidArgument(f"unknown action {action!r}", hint="begin | commit | discard | cleanup")


async def op_stage_view(state: GatewayState, action: str = "status", stage_id: str | None = None, zoom: bool = False) -> Reply:
    sm = state.stages
    if action == "status":
        stages = [s.brief() for s in sm.stages.values()]
        open_n = sum(1 for s in stages if s["status"] == "open")
        return Reply(data={"stages": stages, "stage_layer": sm.layer, "current": sm.current},
                     summary=f"{len(stages)} stage(s), {open_n} open" if stages else "no stages yet")
    if action == "preview":
        info, images = await sm.preview(stage_id, zoom=zoom)
        warn = [] if images else ["no screenshot available (AutoCAD window occluded or rendering failed): ask the user to look at the bright staging entities directly inside AutoCAD."]
        return Reply(data=info, images=images, warnings=warn,
                     summary=f"stage {info['id']}: {info['pending']} entities pending (on layer {sm.layer}, bright colour); commit to keep them, discard to drop them")
    raise InvalidArgument(f"unknown action {action!r}", hint="status | preview")


# ------------------------------------------------------------------------- undo
async def op_undo(state: GatewayState, scope: str = "last", steps: int = 1, group: str | None = None, confirm: bool = False) -> Reply:
    if scope not in ("last", "group"):
        raise InvalidArgument("scope must be last or group")
    if scope == "group" and not group:
        raise InvalidArgument("scope='group' needs group (the batch id returned by cad_batch)")
    actions, skipped = state.journal.plan_undo(steps=steps, group=group if scope == "group" else None)
    plan = [{"do": a.describe, "tool": a.tool} for a in actions]
    if not actions:
        return Reply(data={"plan": [], "skipped": skipped}, summary="no gateway-recorded operations that can be undone automatically" + (f" ({len(skipped)} cannot be undone automatically)" if skipped else ""))
    if not confirm:
        return Reply(data={"plan": plan, "skipped": skipped, "confirm_required": True},
                     summary=f"will undo {len(actions)} item(s): {'; '.join(p['do'] for p in plan)}. Call again with confirm=true to proceed")
    done, failed = 0, []
    async with state.router.session():
        with state.journal.quiet():  # undoing must not create new "things to undo"
            for a in actions:
                out = await state.router.slacker(a.tool, a.args, confirm=True)
                message = (out["error"] or {}).get("message", "")
                if out["ok"] or "No accessible entity" in message:
                    state.journal.mark_undone(a.seq)
                    done += 1
                else:
                    failed.append({"do": a.describe, "error": message})
    return Reply(data={"undone": done, "failed": failed, "skipped": skipped}, ok=not failed,
                 summary=f"undid {done} item(s)" + (f", {len(failed)} failed" if failed else "") + (f", {len(skipped)} cannot be undone automatically" if skipped else ""),
                 error=None if not failed else {"code": "UNDO_PARTIAL", "message": "some undo actions failed", "hint": "See data.failed; you can handle them manually inside AutoCAD."})


# ------------------------------------------------------------------------ batch
BATCH_OPS: dict[str, Callable[..., Any]] = {
    "line": op_line, "circle": op_circle, "arc": op_arc, "polyline": op_polyline, "rect": op_rect, "wall": op_wall,
    "text": op_text, "dimension": op_dimension, "move": op_move, "copy": op_copy, "layer": op_layer,
}


async def op_batch(state: GatewayState, operations: list[dict[str, Any]], stop_on_error: bool = True, stage: bool = False,
                   verify: bool | None = None, progress: Callable[[int, int, str], Any] | None = None) -> Reply:
    limit = state.router.config.runtime.max_batch_operations
    if not operations:
        raise InvalidArgument("operations must not be empty")
    if len(operations) > limit:
        raise InvalidArgument(f"at most {limit} steps per call (got {len(operations)})", hint="split into several cad_batch calls")
    unknown = sorted({str(o.get("op")) for o in operations if o.get("op") not in BATCH_OPS})
    if unknown:
        raise InvalidArgument(f"unsupported op(s): {unknown}", hint="available: " + ", ".join(BATCH_OPS))
    results: list[dict[str, Any]] = []
    created: list[str] = []
    stopped_at: int | None = None
    async with state.router.session():
        with state.journal.group(state.journal.new_group("b")) as gid:
            for i, spec in enumerate(operations):
                spec = dict(spec)
                name = spec.pop("op")
                fn = BATCH_OPS[name]
                params = inspect.signature(fn).parameters
                bad = sorted(set(spec) - set(params))
                if "stage" in params and "stage" not in spec and stage:
                    spec["stage"] = True
                if "verify" in params and "verify" not in spec and verify is not None:
                    spec["verify"] = verify
                entry: dict[str, Any] = {"i": i, "op": name}
                try:
                    if bad:
                        raise InvalidArgument(f"step {i + 1} ({name}) got unknown parameter(s) {bad}", hint="available: " + ", ".join(p for p in params if p != "state"))
                    reply = await fn(state, **spec)
                    entry.update(ok=reply.ok, summary=reply.summary)
                    if isinstance(reply.data, dict) and reply.data.get("handle"):
                        entry["handle"] = reply.data["handle"]
                        created.append(reply.data["handle"])
                    if not reply.ok:
                        entry["error"] = reply.error
                except GatewayError as exc:
                    entry.update(ok=False, summary=f"failed: {exc.message}", error=exc.to_dict())
                except Exception as exc:  # noqa: BLE001
                    err = to_gateway_error(exc)
                    entry.update(ok=False, summary=f"failed: {err.message}", error=err.to_dict())
                results.append(entry)
                if progress is not None:
                    try:
                        maybe = progress(i + 1, len(operations), entry["summary"])
                        if inspect.isawaitable(maybe):
                            await maybe
                    except Exception:  # noqa: BLE001 - progress is best effort
                        pass
                if not entry["ok"] and stop_on_error:
                    stopped_at = i
                    break
    ok_n = sum(1 for r in results if r["ok"])
    all_ok = ok_n == len(operations)
    summary = f"batch of {len(operations)} steps: {ok_n} succeeded" + (f", stopped at step {stopped_at + 1}" if stopped_at is not None else "") + f" (group {gid}; undo it all with cad_undo scope='group' group='{gid}')"
    return Reply(
        data={"batch": gid, "results": results, "created": created, "stopped_at": stopped_at},
        summary=summary, ok=all_ok,
        error=None if all_ok else {"code": "BATCH_PARTIAL", "message": f"{len(operations) - ok_n} step(s) failed or were not run",
                                   "hint": f"Completed steps are kept; undo the whole group with cad_undo(scope='group', group='{gid}'), or fix and continue from step {(stopped_at or 0) + 1}."},
    )


# ------------------------------------------------------------------------ verify
async def op_verify(state: GatewayState, topology_detail: str = "summary", render: bool = True, deep: bool = False) -> Reply:
    """Closed-loop check after writing: rescan + validate (+ IR/constraints when deep) + a picture."""

    router = state.router
    steps: list[tuple[str, str, dict[str, Any]]] = [("scan", "scan_all_entities", {"topology_detail": topology_detail})]
    if deep:
        steps += [("ir", "build_drawing_ir", {}), ("dimension_binding", "bind_all_dimensions", {}),
                  ("constraints", "extract_drawing_constraints", {}), ("constraint_check", "check_drawing_constraints", {})]
    steps.append(("validation", "validate_geometry", {}))
    data: dict[str, Any] = {}
    failed: list[str] = []
    images: list[dict[str, str]] = []
    async with router.session():  # one atomic look: nothing can interleave between the steps
        for key, tool, args in steps:
            try:
                out = await router.best(tool, args)
                data[key] = out["structured"] if out["ok"] else {"ok": False, "error": out["error"]}
                if not out["ok"]:
                    failed.append(key)
            except GatewayError as exc:
                data[key] = {"ok": False, "error": exc.to_dict()}
                failed.append(key)
        if render:
            try:
                shot = await router.render({})
                data["render"] = {"ok": shot["ok"], "backend": shot["backend"], "fallback_from": shot.get("fallback_from")}
                images = list(shot.get("images") or [])
                if not shot["ok"]:
                    failed.append("render")
            except GatewayError as exc:
                data["render"] = {"ok": False, "error": exc.to_dict()}
                failed.append("render")
    ok = not failed
    return Reply(
        data=data, images=images, ok=ok,
        summary=("re-check done: " + ", ".join(k for k, _, _ in steps) + (" all passed" if ok else f"; failed: {', '.join(failed)}")),
        error=None if ok else {"code": "VERIFY_INCOMPLETE", "message": f"{len(failed)} verification step(s) failed", "hint": "See the error field of the corresponding step in data."},
    )
