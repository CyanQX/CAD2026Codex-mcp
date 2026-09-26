"""What the gateway may call, through which door, and how risky each call is.

This module is the single source of truth: the Router enforces it, the server derives tool
annotations from it, the doctor and the contract tests check it against the real backends.
Adding a capability means adding its name here - never in more than one place.
"""

from __future__ import annotations

from typing import Any

# ------------------------------------------------------------------ non-COM backends
OFFICIAL_TOOLS = frozenset(
    {"discoverAutoCADTypes", "queryAutoCADObjects", "aggregateAutoCADObjects", "manipulateDrawingCanvas", "checkAutoCADObjects"}
)
PRODUCT_HELP_TOOLS = frozenset({"get_available_products", "search_help_content"})

# --------------------------------------------------------------------------- Slacker
SLACKER_READ_TOOLS = frozenset(
    {
        "autocad_status", "list_open_drawings", "get_active_drawing_info", "list_layers", "list_blocks",
        "list_layouts", "query_entities", "capture_screenshot", "zoom_extents",
    }
)
SLACKER_WRITE_TOOLS = frozenset(
    {
        "create_new_drawing", "open_drawing", "save_active_drawing", "save_drawing_as",
        "create_layer", "set_current_layer", "set_layer_properties", "set_entity_layer",
        "draw_line", "draw_circle", "draw_arc", "draw_ellipse", "draw_polyline", "draw_point", "draw_text",
        "insert_block", "add_linear_dimension", "erase_entity", "move_entity", "copy_entity",
    }
)
SLACKER_TOOLS = SLACKER_READ_TOOLS | SLACKER_WRITE_TOOLS

SLACKER_PREFERRED_DRAW = frozenset(
    {"draw_line", "draw_circle", "draw_arc", "draw_ellipse", "draw_polyline", "draw_point", "draw_text", "insert_block"}
)
SLACKER_PREFERRED_EDIT = frozenset({"move_entity", "copy_entity", "erase_entity", "set_entity_layer"})
SLACKER_PREFERRED_ANNOTATION = frozenset({"add_linear_dimension"})
SLACKER_LANES: dict[str, frozenset[str]] = {
    "draw": SLACKER_PREFERRED_DRAW,
    "edit": SLACKER_PREFERRED_EDIT,
    "annotate": SLACKER_PREFERRED_ANNOTATION,
}

# ---------------------------------------------------------------------- best-cad-mcp
BEST_READ_TOOLS = frozenset(
    {
        # environment / scan / understand
        "check_runtime_environment", "scan_all_entities", "build_drawing_ir", "summarize_drawing",
        "analyze_drawing_intent", "detect_semantic_objects", "bind_all_dimensions", "extract_drawing_constraints",
        "check_drawing_constraints", "validate_geometry", "explain_entity", "recommend_cad_tools", "get_tool_help",
        "get_semantic_graph", "get_drawing_constraints", "get_validation_report", "find_semantic_objects",
        "find_entities_by_description", "propose_repair_plan", "propose_constraint_repair_plan",
        # vision
        "render_drawing_view", "get_snapshot_image", "get_vision_capabilities", "prepare_visual_semantic_context",
        "view_image", "list_spatial_annotations", "export_view_image_with_mapping", "get_current_view",
        "get_visible_entities_in_view", "map_pixel_to_world", "map_world_to_pixel", "map_pixel_region_to_world_bbox",
        # exact geometry / measurement / lookups (used by anchors, measure and write-verification)
        "get_entity_properties", "get_bounding_box", "get_entity_topology", "get_entity_statistics",
        "get_topology_summary", "scan_entities_in_area", "find_text", "measure_distance",
        "get_all_layers", "get_layouts", "get_text_styles", "get_dimension_styles", "get_all_blocks",
        "get_block_attributes", "get_variable", "get_document_info",
        # planning / image-trace reads
        "validate_cad_plan", "dry_run_cad_plan", "get_trace_source_image", "validate_image_drawing_spec",
        "validate_image_fidelity_contract",
    }
)

_DRAW_LANE = frozenset(
    {
        "draw_line", "draw_circle", "draw_arc", "draw_ellipse", "draw_polyline", "draw_text", "insert_block",
        "draw_rectangle", "draw_polygon", "draw_spline", "draw_mline", "draw_mtext", "create_block", "add_hatch",
        "hatch_add_boundary", "draw_box", "draw_cylinder", "insert_block_with_attributes",
    }
)
_EDIT_LANE = frozenset(
    {
        "move_entity", "copy_entity", "delete_entity", "rotate_entity", "scale_entity", "mirror_entity",
        "offset_entity", "trim_entity", "extend_entity", "fillet_entities", "chamfer_entities", "fillet_polyline",
        "chamfer_polyline", "array_rectangular", "array_polar", "solid_boolean", "set_entity_properties",
        "set_text_alignment", "set_text_properties", "set_block_attribute", "join_entities", "explode_entity",
        "create_text_style", "set_current_text_style", "set_current_dimension_style",
    }
)
_ANNOTATE_LANE = frozenset(
    {
        "add_linear_dimension", "add_qdim", "add_mleader", "add_table", "add_radial_dimension",
        "add_diametric_dimension", "add_angular_dimension", "add_baseline_dimension", "add_continue_dimension",
        "add_rotated_dimension", "add_leader", "edit_table_cell",
    }
)
_EXPORT_LANE = frozenset({"export_pdf", "export_dxf", "export_dwf", "export_view_image"})
_PLAN_LANE = frozenset({"validate_cad_plan", "dry_run_cad_plan", "execute_cad_plan"})
_IMAGE_TRACE_LANE = frozenset(
    {
        "prepare_image_trace", "get_trace_source_image", "validate_image_drawing_spec",
        "submit_image_drawing_spec", "compile_image_spec_to_cad_plan", "validate_image_fidelity_contract",
    }
)

# A tool may only be reached through the door(s) listed here.  In particular ``execute_cad_plan``
# is reachable ONLY through the plan lane: v0.1 let ``cad_edit("execute_cad_plan")`` skip the
# forced transaction/rollback flags that ``cad_plan`` applied.
LANES: dict[str, frozenset[str]] = {
    "draw": _DRAW_LANE,
    "edit": _EDIT_LANE,
    "annotate": _ANNOTATE_LANE,
    "export": _EXPORT_LANE,
    "plan": _PLAN_LANE,
    "image_trace": _IMAGE_TRACE_LANE,
}

BEST_WRITE_TOOLS = frozenset(
    _DRAW_LANE | _EDIT_LANE | _ANNOTATE_LANE | _EXPORT_LANE | {"execute_cad_plan"}
    | {"prepare_image_trace", "submit_image_drawing_spec", "compile_image_spec_to_cad_plan"}
)

DESTRUCTIVE = frozenset({"erase_entity", "delete_entity", "execute_cad_plan", "solid_boolean", "explode_entity"})
SAVE_TOOLS = frozenset({"save_active_drawing", "save_drawing_as", "export_pdf", "export_dxf", "export_dwf"})

# best's drawing/annotation tools create the requested layer *and make it the current layer*
# (e.g. draw_text: ``create_layer(layer); set_current_layer(layer)``).  That silently changes the
# layer the human is working on, so the Router snapshots CLAYER before such a call and restores it.
BEST_LAYER_SIDE_EFFECT = frozenset(_DRAW_LANE | _ANNOTATE_LANE)

# Every tool call the gateway itself sends to a backend (server.py / precision / staging).
# The contract tests check that each is allow-listed and exists upstream.
INTERNAL_BEST_TOOLS = frozenset({"get_entity_properties", "get_bounding_box"})

PLAN_EXECUTE_FLAGS: dict[str, Any] = {
    "allow_modify": True,
    "transactional": True,
    "rollback_on_error": True,
    "rollback_on_high_severity_validation": True,
    "validate_after_plan": True,
    "rescan_after_plan": True,
}


def enforce_plan_safety(args: dict[str, Any]) -> dict[str, Any]:
    """Force the transactional safeguards on a CADPlan execution, whatever the caller asked for."""

    safe = dict(args)
    safe.update(PLAN_EXECUTE_FLAGS)
    return safe


def classify_risk(backend: str, tool: str) -> str:
    """read | write | destructive | export | raw"""

    if backend in ("official", "product_help"):
        return "read"
    if backend == "felix":
        return "raw"
    if tool in DESTRUCTIVE:
        return "destructive"
    if tool in SAVE_TOOLS:
        return "export"
    if backend == "slacker":
        return "read" if tool in SLACKER_READ_TOOLS else "write"
    if backend == "best":
        return "read" if tool in BEST_READ_TOOLS else "write"
    return "write"
