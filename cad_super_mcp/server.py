from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Literal, Union

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, ToolAnnotations

from . import __version__
from . import ops
from . import policy as P
from .config import ConfigError, load_config
from .ops import ALIGN_CODES, GatewayState
from .results import Reply, build_result, error_result, from_call
from .router import Router

INSTRUCTIONS = """
You are a meticulous AutoCAD 2026 drafter assistant (the CAD Super MCP gateway). Working protocol: Observe → Plan → Validate → Execute → Rescan → Verify.

1. Start with cad_context: confirm AutoCAD is online, the drawing name, units, layers and layouts. When the user may switch drawings mid-task, pin the drawing with cad_context(pin=true).
2. All lengths are written in millimetres, or as unit-suffixed strings ("3.6m", "12in"). Never do coordinate arithmetic yourself - use a "point spec":
   [x,y] | {"from":point,"dx":length,"dy":length} | {"from":point,"angle_deg":angle,"dist":length} | {"mid":[point,point]} |
   {"handle":handle-or-alias,"snap":"start|end|mid|center|vertex:N|top|bottom|left|right|centroid"} | {"intersect":[handle,handle]}.
3. To reference an existing entity, get its handle and geometry first (cad_query_entities / cad_measure kind=entity); give important entities a name with cad_name and use the name afterwards.
4. Writes are verified by read-back by default (data.verification). When verified is false or null you must tell the user honestly - never claim "done".
5. When unsure, when many entities are affected, or when it needs the user's judgement, first draw with stage=true onto the staging layer and show the user cad_stage_view(action="preview");
   commit only after explicit approval (cad_stage(action="commit")), otherwise discard.
6. When a call returns error.code=NEEDS_CLARIFICATION, relay details.options / question to the user; do not guess.
7. Undo your own recent work with cad_undo (preview first; confirm=true after the user agrees). Use cad_batch for multi-step work: it stops on error and returns a group that can be undone as a whole.
8. Delete, save, export and CADPlan execute happen only after the user explicitly asks, and always with confirm=true; never save automatically.
9. Report format: what was done (layer/size/handle) → verification result → suggested next step.
10. Complex drawings: cad_recommend_tools → cad_plan_check (validate, dry_run) → user confirmation → cad_plan(execute) → cad_verify.
""".strip()

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)
READ_OPEN = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)
WRITE_ADDITIVE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
WRITE_MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)
LOCAL_STATE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

# Type aliases live at module level: the MCP SDK resolves tool annotations against this module's globals.
Space = Literal["model", "paper"]
Length = Union[float, str]
PointSpec = Union[list[Union[float, str]], dict[str, Any]]
DrawTool = Literal[tuple(sorted(P.SLACKER_PREFERRED_DRAW | P.LANES["draw"]))]  # type: ignore[valid-type]
EditTool = Literal[tuple(sorted(P.SLACKER_PREFERRED_EDIT | P.LANES["edit"]))]  # type: ignore[valid-type]
AnnotateTool = Literal[tuple(sorted(P.SLACKER_PREFERRED_ANNOTATION | P.LANES["annotate"]))]  # type: ignore[valid-type]
TextAlign = Literal[tuple(ALIGN_CODES)]  # type: ignore[valid-type]
WallJustify = Literal["center", "left", "right"]
DimOrientation = Literal["aligned", "horizontal", "vertical"]
UnderstandAction = Literal["build_ir", "summary", "intent", "semantics", "bind_dimensions", "extract_constraints", "check_constraints"]
PlanAction = Literal["validate", "dry_run", "execute"]
PlanCheckAction = Literal["validate", "dry_run"]
ImageTraceAction = Literal["prepare", "get_source", "validate_spec", "submit_spec", "compile", "validate_fidelity"]
ExportFormat = Literal["pdf", "dxf", "dwf", "image"]
OfficialTool = Literal["discoverAutoCADTypes", "queryAutoCADObjects", "aggregateAutoCADObjects", "manipulateDrawingCanvas", "checkAutoCADObjects"]
DocsTool = Literal["get_available_products", "search_help_content"]
SaveTool = Literal["save_active_drawing", "save_drawing_as"]
LayerAction = Literal["create", "set_current", "set_props"]
DocAction = Literal["open", "new"]
MeasureKind = Literal["distance", "area", "length", "entity"]
NameAction = Literal["set", "get", "list", "remove"]
StageAction = Literal["begin", "commit", "discard", "cleanup"]
StageViewAction = Literal["status", "preview"]
UndoScope = Literal["last", "group"]
TopologyDetail = Literal["summary", "full"]

UNDERSTAND_MAP = {
    "build_ir": "build_drawing_ir", "summary": "summarize_drawing", "intent": "analyze_drawing_intent",
    "semantics": "detect_semantic_objects", "bind_dimensions": "bind_all_dimensions",
    "extract_constraints": "extract_drawing_constraints", "check_constraints": "check_drawing_constraints",
}
PLAN_MAP = {"validate": "validate_cad_plan", "dry_run": "dry_run_cad_plan", "execute": "execute_cad_plan"}
IMAGE_TRACE_MAP = {
    "prepare": "prepare_image_trace", "get_source": "get_trace_source_image", "validate_spec": "validate_image_drawing_spec",
    "submit_spec": "submit_image_drawing_spec", "compile": "compile_image_spec_to_cad_plan",
    "validate_fidelity": "validate_image_fidelity_contract",
}
EXPORT_MAP = {"pdf": "export_pdf", "dxf": "export_dxf", "dwf": "export_dwf", "image": "export_view_image"}


def create_server(router: Router, *, state: GatewayState | None = None) -> MCPServer:
    """Build the MCP server around a Router.  Tests pass a Router with fake backends."""

    state = state or GatewayState(router)
    max_chars = router.config.runtime.max_response_chars

    @asynccontextmanager
    async def lifespan(_app: MCPServer):
        try:
            yield {}
        finally:
            await router.aclose()

    app = MCPServer("CAD Super MCP", instructions=INSTRUCTIONS, version=__version__, lifespan=lifespan)

    async def run(name: str, factory: Callable[[], Awaitable[Reply]]) -> CallToolResult:
        started = time.perf_counter()
        try:
            reply = await factory()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - every failure becomes a structured, actionable envelope
            return error_result(exc, tool=name, max_chars=max_chars)
        reply.meta.setdefault("tool", name)
        reply.meta["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return build_result(reply, max_chars=max_chars)

    async def resolve_handle(ref: str) -> str:
        ident = await router.current_identity()
        return state.names.for_drawing(ident["id"]).get(ref, ref) if ident else ref

    # ------------------------------------------------------------------ observe
    @app.tool(annotations=READ_ONLY)
    async def cad_health(include_tool_inventory: bool = False, check_remote: bool = False) -> CallToolResult:
        """Check the backends (best / Slacker / official MCP / felix): online status, tool counts, sessions and safety policy.
        Product Help is a remote network service (takes seconds) and is not checked by default; pass check_remote=true when needed."""

        async def go() -> Reply:
            st = await router.status(include_tools=include_tool_inventory, include_remote=check_remote)
            online = [n for n, i in st["backends"].items() if i.get("ok")]
            down = [n for n, i in st["backends"].items() if i.get("ok") is False]
            skipped = [n for n, i in st["backends"].items() if i.get("skipped")]
            st["essential_ready"] = all(st["backends"].get(n, {}).get("ok") for n in ("best", "slacker"))
            text = f"online: {', '.join(online) or 'none'}" + (f"; unavailable: {', '.join(down)}" if down else "")
            text += f"; not checked: {', '.join(skipped)}" if skipped else ""
            text += ". Core backends (best+slacker) ready" if st["essential_ready"] else ". Core backends NOT ready - see the hint in data.backends first"
            return Reply(data=st, summary=text)

        return await run("cad_health", go)

    @app.tool(annotations=READ_ONLY)
    async def cad_context(pin: bool = False, max_layers: int = 200) -> CallToolResult:
        """Call this first: is AutoCAD online, current drawing name/path/units, layers, layouts, blocks, open drawings, staging and undo state.
        pin=true pins the current drawing - if the user then switches drawings inside AutoCAD, writes are refused (DRAWING_CHANGED), so "scanning drawing A but writing drawing B" cannot happen."""

        return await run("cad_context", lambda: ops.op_context(state, pin=pin, max_layers=max_layers))

    @app.tool(annotations=READ_ONLY)
    async def cad_drawing_info() -> CallToolResult:
        """Read the active drawing info (name, path, saved flag, INSUNITS units)."""

        async def go() -> Reply:
            out = await router.slacker("get_active_drawing_info", {})
            return from_call(out)

        return await run("cad_drawing_info", go)

    @app.tool(annotations=READ_ONLY)
    async def cad_list_layers() -> CallToolResult:
        """List the layers of the active drawing (colour, locked, frozen, visibility)."""

        return await run("cad_list_layers", lambda: _passthrough(router.slacker("list_layers", {})))

    @app.tool(annotations=READ_ONLY)
    async def cad_query_entities(arguments: dict[str, Any]) -> CallToolResult:
        """Query entities handle-first (Slacker query_entities). Available arguments: space(model|paper), layer, object_name (e.g. AcDbLine), limit(<=1000), max_scan."""

        return await run("cad_query_entities", lambda: _passthrough(router.slacker("query_entities", arguments)))

    @app.tool(annotations=READ_ONLY)
    async def cad_scan(topology_detail: TopologyDetail = "summary", max_entities: int = 2000) -> CallToolResult:
        """Scan the live entities with best-cad-mcp. summary by default; use full only when you need geometric topology. Oversized results are trimmed automatically with a hint to narrow the scope."""

        return await run("cad_scan", lambda: _passthrough(router.best("scan_all_entities", {"topology_detail": topology_detail, "max_entities": max_entities})))

    @app.tool(annotations=READ_ONLY)
    async def cad_explain_entity(handle: str) -> CallToolResult:
        """Explain/confirm one entity before modifying it (handle, or an alias created with cad_name)."""

        async def go() -> Reply:
            return from_call(await router.best("explain_entity", {"handle": await resolve_handle(handle)}))

        return await run("cad_explain_entity", go)

    @app.tool(annotations=READ_ONLY)
    async def cad_measure(kind: MeasureKind, a: PointSpec | None = None, b: PointSpec | None = None,
                          points: list[PointSpec] | None = None, handle: str | None = None) -> CallToolResult:
        """Exact measurement (millimetres, computed locally by the gateway, never estimated). kind=distance: points a, b; area: points (>=3) gives area/perimeter/centroid;
        length: total polyline length of points; entity: read the geometry of handle (line/circle/arc/polyline/text). Points may use point specs (including handle snaps)."""

        return await run("cad_measure", lambda: ops.op_measure(state, kind, a=a, b=b, points=points, handle=handle))

    @app.tool(annotations=READ_ONLY)
    async def cad_understand(action: UnderstandAction, arguments: dict[str, Any] | None = None) -> CallToolResult:
        """Drawing-understanding layer (best-cad-mcp): build_ir / summary / intent / semantics / bind_dimensions / extract_constraints / check_constraints."""

        return await run("cad_understand", lambda: _passthrough(router.best(UNDERSTAND_MAP[action], arguments or {})))

    @app.tool(annotations=READ_ONLY)
    async def cad_recommend_tools(intent: str) -> CallToolResult:
        """Ask best-cad-mcp which dedicated tools/workflow fit a given CAD intent."""

        return await run("cad_recommend_tools", lambda: _passthrough(router.best("recommend_cad_tools", {"intent": intent})))

    @app.tool(annotations=READ_ONLY)
    async def cad_tool_help(tool_name: str = "") -> CallToolResult:
        """Read best-cad-mcp's usage notes for one tool (or all categories)."""

        args = {"tool_name": tool_name} if tool_name else {}
        return await run("cad_tool_help", lambda: _passthrough(router.best("get_tool_help", args)))

    # -------------------------------------------------------- precise drawing
    @app.tool(annotations=WRITE_ADDITIVE)
    async def cad_line(start_mm: PointSpec, end_mm: PointSpec, layer: str | None = None, space: Space = "model",
                       stage: bool = False, verify: bool | None = None) -> CallToolResult:
        """Draw a line (millimetres). Start/end may be [x,y] or a point spec (relative offset, midpoint, snapping to an existing entity's endpoint, ...).
        Read-back verification is on by default; stage=true draws onto the staging layer for preview first."""

        return await run("cad_line", lambda: ops.op_line(state, start_mm, end_mm, layer, space, stage, verify))

    @app.tool(annotations=WRITE_ADDITIVE)
    async def cad_circle(center_mm: PointSpec, radius_mm: Length | None = None, diameter_mm: Length | None = None,
                         layer: str | None = None, space: Space = "model", stage: bool = False,
                         verify: bool | None = None) -> CallToolResult:
        """Draw a circle. Pass exactly one of radius_mm or diameter_mm (values like "600" or "0.6m")."""

        return await run("cad_circle", lambda: ops.op_circle(state, center_mm, radius_mm, diameter_mm, layer, space, stage, verify))

    @app.tool(annotations=WRITE_ADDITIVE)
    async def cad_arc(center_mm: PointSpec, radius_mm: Length, start_angle_deg: float, end_angle_deg: float,
                      layer: str | None = None, space: Space = "model", stage: bool = False,
                      verify: bool | None = None) -> CallToolResult:
        """Draw an arc: counter-clockwise from start_angle_deg to end_angle_deg (degrees, from +X)."""

        return await run("cad_arc", lambda: ops.op_arc(state, center_mm, radius_mm, start_angle_deg, end_angle_deg, layer, space, stage, verify))

    @app.tool(annotations=WRITE_ADDITIVE)
    async def cad_polyline(vertices_mm: list[PointSpec], closed: bool = False, layer: str | None = None,
                           space: Space = "model", stage: bool = False, verify: bool | None = None) -> CallToolResult:
        """Draw a polyline (>=2 vertices). closed=true closes it and returns area/perimeter; every vertex may use a point spec."""

        return await run("cad_polyline", lambda: ops.op_polyline(state, vertices_mm, closed, layer, space, stage, verify))

    @app.tool(annotations=WRITE_ADDITIVE)
    async def cad_rect(width: Length, height: Length, corner: PointSpec | None = None, center: PointSpec | None = None,
                       rotation_deg: float = 0.0, layer: str | None = None, space: Space = "model",
                       stage: bool = False, verify: bool | None = None) -> CallToolResult:
        """Draw a rectangle (a closed polyline). Pass corner (bottom-left) or center - one of the two; width/height accept "3.6m"; rotation is optional. Returns vertices, area (m²) and perimeter."""

        return await run("cad_rect", lambda: ops.op_rect(state, width, height, corner, center, rotation_deg, layer, space, stage, verify))

    @app.tool(annotations=WRITE_ADDITIVE)
    async def cad_wall(centerline: list[PointSpec], thickness: Length, closed: bool = False, justify: WallJustify = "center",
                       layer: str | None = None, space: Space = "model", stage: bool = False,
                       verify: bool | None = None) -> CallToolResult:
        """Draw a wall along a centreline: give the centreline (>=2 points, point specs allowed) and the thickness (e.g. 240 or "0.24m"); the gateway computes the mitred outline exactly and draws it as a closed polyline.
        closed=true (the centreline itself is closed, e.g. a ring of room walls) draws the inner and outer loops; justify picks which side of the centreline the wall sits on (center|left|right, relative to the travel direction)."""

        return await run("cad_wall", lambda: ops.op_wall(state, centerline, thickness, closed, justify, layer, space, stage, verify))

    @app.tool(annotations=WRITE_ADDITIVE)
    async def cad_dimension(start_mm: PointSpec, end_mm: PointSpec, offset: Length = 500, orientation: DimOrientation = "aligned",
                            layer: str | None = None, space: Space = "model", stage: bool = False) -> CallToolResult:
        """Linear dimension: pass the two measured points and how far the dimension line sits from them (offset, default 500 mm, negative flips to the other side); the gateway computes the dimension-line position.
        orientation=aligned (along the two points)/horizontal/vertical."""

        return await run("cad_dimension", lambda: ops.op_dimension(state, start_mm, end_mm, offset, orientation, layer, space, stage))

    @app.tool(annotations=WRITE_ADDITIVE)
    async def cad_text(text: str, at: PointSpec, height_mm: Length, layer: str | None = None, space: Space = "model",
                       rotation_deg: float = 0.0, align: TextAlign | None = None, stage: bool = False,
                       verify: bool | None = None) -> CallToolResult:
        """Write single-line text. at may be a snap point such as a room centroid (e.g. {"handle":"room1","snap":"centroid"}) + align="middle_center" for a centred label.
        Unrotated, left-aligned text goes through Slacker (verified); rotated/aligned text goes through best (not yet verified on a real machine and warned about - stage a preview first)."""

        return await run("cad_text", lambda: ops.op_text(state, text, at, height_mm, layer, space, rotation_deg, align, stage, verify))

    @app.tool(annotations=WRITE_MUTATING)
    async def cad_move(handle: str, from_mm: PointSpec, to_mm: PointSpec) -> CallToolResult:
        """Move one entity by handle (or alias): from from_mm to to_mm (millimetres; both accept point specs). Reversible with cad_undo."""

        return await run("cad_move", lambda: ops.op_move(state, handle, from_mm, to_mm))

    @app.tool(annotations=WRITE_ADDITIVE)
    async def cad_copy(handle: str, displacement_mm: list[Length] | None = None) -> CallToolResult:
        """Copy one entity by handle (or alias); optional displacement [dx,dy] (millimetres). Without a displacement it lands in place, overlapping the original."""

        return await run("cad_copy", lambda: ops.op_copy(state, handle, displacement_mm))

    @app.tool(annotations=WRITE_MUTATING)
    async def cad_erase(handle: str, confirm: bool = False) -> CallToolResult:
        """Erase one entity by handle (or alias). Destructive: requires confirm=true (ask the user first)."""

        return await run("cad_erase", lambda: ops.op_erase(state, handle, confirm))

    @app.tool(annotations=WRITE_ADDITIVE)
    async def cad_layer(action: LayerAction, name: str, color_aci: int | None = None, locked: bool | None = None,
                        frozen: bool | None = None, visible: bool | None = None,
                        make_current: bool | None = None) -> CallToolResult:
        """Layer management: create (returned as-is if it exists) / set_current / set_props (colour ACI, locked, frozen, on/off). Use cad_list_layers to inspect layers."""

        return await run("cad_layer", lambda: ops.op_layer(state, action, name, color_aci, locked, frozen, visible, make_current))

    @app.tool(annotations=WRITE_ADDITIVE)
    async def cad_document(action: DocAction, path: str | None = None, template_path: str | None = None) -> CallToolResult:
        """Documents: open (open a DWG/DXF) / new (create). After a switch the gateway follows the new drawing (if it was pinned)."""

        return await run("cad_document", lambda: ops.op_document(state, action, path, template_path))

    @app.tool(annotations=WRITE_ADDITIVE)
    async def cad_batch(ctx: Context, operations: list[dict[str, Any]], stop_on_error: bool = True, stage: bool = False,
                        verify: bool | None = None) -> CallToolResult:
        """Run several steps back-to-back (one session, one lock, stop on error). Each step is {"op": "line|circle|arc|polyline|rect|wall|text|dimension|move|copy|layer", ...the tool's arguments}.
        Returns each step's result, the created handles and the group id (undo the whole group with cad_undo scope="group"). With stage=true everything is drawn onto the staging layer first."""

        async def progress(i: int, n: int, message: str) -> None:
            await ctx.report_progress(i, n, message)

        return await run("cad_batch", lambda: ops.op_batch(state, operations, stop_on_error, stage, verify, progress))

    # ----------------------------------------------------- human-in-the-loop
    @app.tool(annotations=WRITE_MUTATING)
    async def cad_stage(action: StageAction, stage_id: str | None = None, label: str | None = None,
                        layer: str | None = None) -> CallToolResult:
        """Staging: begin starts a session; commit moves the staged entities to their target layers (layer may provide the default target); discard erases the staged entities; cleanup empties the staging layer.
        Add stage=true to any drawing tool to draw onto the bright staging layer _AI_STAGE, show the user, then commit/discard."""

        return await run("cad_stage", lambda: ops.op_stage(state, action, stage_id, label, layer))

    @app.tool(annotations=READ_ONLY)
    async def cad_stage_view(action: StageViewAction = "status", stage_id: str | None = None, zoom: bool = False) -> CallToolResult:
        """Inspect staging: status lists the sessions; preview returns a screenshot (a viewable image) plus the pending entities. zoom=true zooms to extents first (this changes the user's view)."""

        return await run("cad_stage_view", lambda: ops.op_stage_view(state, action, stage_id, zoom))

    @app.tool(annotations=WRITE_MUTATING)
    async def cad_undo(scope: UndoScope = "last", steps: int = 1, group: str | None = None, confirm: bool = False) -> CallToolResult:
        """Undo what the gateway itself did (create→erase; move→move back). Only touches entities the gateway created/moved, never the user's manual work, and never AutoCAD's own UNDO stack.
        Without confirm it only returns a preview of what would happen; pass confirm=true after the user agrees."""

        return await run("cad_undo", lambda: ops.op_undo(state, scope, steps, group, confirm))

    @app.tool(annotations=LOCAL_STATE)
    async def cad_name(action: NameAction, name: str | None = None, handle: str | None = None) -> CallToolResult:
        """Give an entity an alias (e.g. "west wall" -> handle), remembered per drawing. Afterwards every handle argument and point spec can use the alias directly."""

        return await run("cad_name", lambda: ops.op_name(state, action, name, handle))

    # ------------------------------------------------------- generic doors
    @app.tool(annotations=WRITE_ADDITIVE)
    async def cad_draw(tool: DrawTool, arguments: dict[str, Any]) -> CallToolResult:
        """Generic drawing entrance. Common primitives go through Slacker (millimetres); advanced objects (rectangle/polygon/spline/multiline/MTEXT/hatch/block/3D solids) go through best-cad-mcp (note: best's coordinates are drawing units)."""

        return await run("cad_draw", lambda: _passthrough(router.draw(tool, arguments)))

    @app.tool(annotations=WRITE_MUTATING)
    async def cad_edit(tool: EditTool, arguments: dict[str, Any], confirm: bool = False) -> CallToolResult:
        """Generic edit entrance (by handle). Move/copy/erase/change-layer go through Slacker; the rest (rotate, scale, mirror, offset, trim, fillet, array, properties, text styles...) goes through best. Destructive operations need confirm."""

        return await run("cad_edit", lambda: _passthrough(router.edit(tool, arguments, confirm=confirm)))

    @app.tool(annotations=WRITE_ADDITIVE)
    async def cad_annotate(tool: AnnotateTool, arguments: dict[str, Any]) -> CallToolResult:
        """Dimensions/leaders/tables. Linear dimensions go through Slacker; radius/diameter/angular/baseline/continue/quick dimensions, multileaders and tables go through best."""

        return await run("cad_annotate", lambda: _passthrough(router.annotate(tool, arguments)))

    # ------------------------------------------------------------ plan & verify
    @app.tool(annotations=READ_ONLY)
    async def cad_plan_check(action: PlanCheckAction, arguments: dict[str, Any]) -> CallToolResult:
        """Read-only CADPlan checks: validate / dry_run (dry run, no drawing changes). Use cad_plan(action="execute") to execute."""

        return await run("cad_plan_check", lambda: _passthrough(router.best(PLAN_MAP[action], arguments, lane="plan")))

    @app.tool(annotations=WRITE_MUTATING)
    async def cad_plan(action: PlanAction, arguments: dict[str, Any], confirm: bool = False) -> CallToolResult:
        """CADPlan workflow. execute requires confirm=true, and the gateway enforces transactions, rollback on failure, rollback on high severity, post-execution validation and rescan (the caller cannot turn these off).
        validate / dry_run also work here, but the read-only cad_plan_check is preferred (it does not trigger approvals)."""

        return await run("cad_plan", lambda: _passthrough(router.best(PLAN_MAP[action], arguments, confirm=confirm, lane="plan")))

    @app.tool(annotations=READ_ONLY)
    async def cad_validate(arguments: dict[str, Any] | None = None) -> CallToolResult:
        """Run geometry validation on the live drawing (best-cad-mcp validate_geometry)."""

        return await run("cad_validate", lambda: _passthrough(router.best("validate_geometry", arguments or {})))

    @app.tool(annotations=READ_ONLY)
    async def cad_render(arguments: dict[str, Any] | None = None) -> CallToolResult:
        """Render the current view so you can "see" it: returns a viewable image (best render, falls back to an AutoCAD window screenshot)."""

        return await run("cad_render", lambda: _passthrough(router.render(arguments or {})))

    @app.tool(annotations=READ_ONLY)
    async def cad_verify(topology_detail: TopologyDetail = "summary", render: bool = True, deep: bool = False) -> CallToolResult:
        """Post-write closed-loop check: rescan + geometry validation + (deep=true) rebuild IR / bind dimensions / check constraints + (render=true) a screenshot. The whole check runs under one lock and cannot be interleaved by other writes."""

        return await run("cad_verify", lambda: ops.op_verify(state, topology_detail, render, deep))

    @app.tool(annotations=WRITE_ADDITIVE)
    async def cad_image_trace(action: ImageTraceAction, arguments: dict[str, Any], confirm: bool = False) -> CallToolResult:
        """Image-to-CAD (the controlled phases of best-cad-mcp): prepare → get_source → validate_spec → submit_spec → compile → validate_fidelity."""

        return await run("cad_image_trace", lambda: _passthrough(router.best(IMAGE_TRACE_MAP[action], arguments, confirm=confirm, lane="image_trace")))

    # ---------------------------------------------------- external references
    @app.tool(annotations=READ_ONLY)
    async def cad_official(tool: OfficialTool, arguments: dict[str, Any]) -> CallToolResult:
        """Read-only/analysis tools of Autodesk's official AutoCAD MCP (auto-discovered on localhost:5001-5050)."""

        return await run("cad_official", lambda: _passthrough(router.official(tool, arguments)))

    @app.tool(annotations=READ_OPEN)
    async def cad_docs(tool: DocsTool, arguments: dict[str, Any]) -> CallToolResult:
        """Query Autodesk's official help documentation (online). When an API/command/version is uncertain, check here first - do not guess."""

        return await run("cad_docs", lambda: _passthrough(router.docs(tool, arguments)))

    @app.tool(annotations=WRITE_MUTATING)
    async def cad_export(format: ExportFormat, arguments: dict[str, Any] | None = None, confirm: bool = False) -> CallToolResult:
        """Export pdf / dxf / dwf / image. File-writing exports need confirm=true; the view-only image does not."""

        return await run("cad_export", lambda: _passthrough(router.best(EXPORT_MAP[format], arguments or {}, confirm=confirm, lane="export")))

    @app.tool(annotations=WRITE_MUTATING)
    async def cad_save(tool: SaveTool, arguments: dict[str, Any], confirm: bool = False) -> CallToolResult:
        """Save / save-as (Slacker). Saving is a stand-alone decision and requires confirm=true by default; save-as paths are relative to the output root."""

        return await run("cad_save", lambda: _passthrough(router.slacker(tool, arguments, confirm=confirm)))

    @app.tool(annotations=WRITE_MUTATING)
    async def cad_raw_command(command: str, confirm: bool = False) -> CallToolResult:
        """Last resort: run a native AutoCAD command through felix. Disabled by default; once enabled it still needs confirm=true, and the first command must be on the allow-list (commands that load code / start programs / write files / quit are always blocked). A seat belt, not a sandbox."""

        return await run("cad_raw_command", lambda: _passthrough(router.raw_command(command, confirm=confirm)))

    @app.tool(annotations=WRITE_MUTATING)
    async def cad_eval_lisp(expression: str, confirm: bool = False) -> CallToolResult:
        """Last resort: evaluate AutoLISP through felix. Disabled by default; only allow-listed functions are accepted (command/eval/read/apply/file/COM etc. are not)."""

        return await run("cad_eval_lisp", lambda: _passthrough(router.eval_lisp(expression, confirm=confirm)))

    return app


async def _passthrough(pending: Awaitable[dict[str, Any]]) -> Reply:
    return from_call(await pending)


def main() -> None:
    parser = argparse.ArgumentParser(description="CAD Super MCP gateway")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allow-remote", action="store_true",
                        help="permit binding a non-loopback host (the HTTP endpoint has NO authentication: only behind a trusted tunnel)")
    args = parser.parse_args()
    # httpx logs every request at INFO; on stderr that floods the MCP host's server log.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        config = load_config()
    except (ConfigError, FileNotFoundError) as exc:
        print(f"cad-super-mcp: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    app = create_server(Router(config))
    if args.transport == "stdio":
        app.run("stdio")
        return
    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.allow_remote:
        print("cad-super-mcp: refusing to bind a non-loopback host without --allow-remote "
              "(the HTTP endpoint has no authentication and controls a writable AutoCAD).", file=sys.stderr)
        raise SystemExit(2)
    app.run("streamable-http", host=args.host, port=args.port, streamable_http_path="/mcp")


if __name__ == "__main__":
    main()
