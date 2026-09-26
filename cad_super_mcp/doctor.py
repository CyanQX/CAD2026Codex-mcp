from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .config import ConfigError, load_config
from .errors import to_gateway_error
from .router import Router

# Tools the gateway cannot work without (a rename/removal upstream must fail the doctor, not a model call).
REQUIRED_TOOLS: dict[str, set[str]] = {
    "best": {
        "check_runtime_environment", "scan_all_entities", "build_drawing_ir", "validate_geometry", "validate_cad_plan",
        "dry_run_cad_plan", "execute_cad_plan", "render_drawing_view", "get_entity_properties", "get_variable",
    },
    "slacker": {
        "autocad_status", "get_active_drawing_info", "query_entities", "draw_line", "draw_circle", "draw_arc", "draw_polyline",
        "draw_text", "move_entity", "copy_entity", "erase_entity", "set_entity_layer", "create_layer", "set_current_layer",
        "list_layers", "capture_screenshot",
    },
}

# Parameter names the gateway *sends* to each upstream tool.  Slacker's schemas are strict
# (additionalProperties=false), so a renamed parameter breaks calls even if the tool name still exists.
EXPECTED_PARAMS: dict[str, dict[str, set[str]]] = {
    "slacker": {
        "draw_line": {"start_mm", "end_mm", "layer", "space"},
        "draw_circle": {"center_mm", "radius_mm", "layer", "space"},
        "draw_arc": {"center_mm", "radius_mm", "start_angle_deg", "end_angle_deg", "layer", "space"},
        "draw_polyline": {"vertices_mm", "closed", "layer", "space"},
        "draw_text": {"text", "insertion_point_mm", "height_mm", "layer", "space"},
        "move_entity": {"handle", "from_mm", "to_mm"},
        "copy_entity": {"handle", "displacement_mm"},
        "erase_entity": {"handle"},
        "set_entity_layer": {"handle", "layer"},
        "create_layer": {"name", "color_aci", "make_current"},
        "set_current_layer": {"name"},
        "set_layer_properties": {"name", "color_aci", "locked", "frozen", "visible"},
        "query_entities": {"space", "limit", "object_name", "layer", "max_scan"},
        "open_drawing": {"path"},
        "save_drawing_as": {"path", "allow_overwrite"},
    },
    "best": {
        "get_entity_properties": {"handle"},
        "get_variable": {"variable_name"},
        "scan_all_entities": {"topology_detail", "max_entities"},
        "draw_text": {"text", "insert_x", "insert_y", "z", "height", "rotation"},
        "set_text_alignment": {"handle", "alignment", "align_x", "align_y", "align_z"},
        "check_runtime_environment": {"check_autocad"},
    },
}


def check_inventory(status: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for backend, required in REQUIRED_TOOLS.items():
        info = status["backends"].get(backend, {})
        tools = {t.get("name"): t for t in info.get("tools", []) if isinstance(t, dict)}
        missing = sorted(required - set(tools))
        bad_params: dict[str, list[str]] = {}
        for tool, expected in EXPECTED_PARAMS.get(backend, {}).items():
            schema = (tools.get(tool) or {}).get("input_schema") or {}
            props = set((schema.get("properties") or {}).keys())
            if tool in tools and props:
                unknown = sorted(expected - props)
                if unknown:
                    bad_params[tool] = unknown
        out[backend] = {
            "ok": bool(info.get("ok")) and not missing and not bad_params,
            "tool_count": len(tools),
            "required_count": len(required),
            "missing_tools": missing,
            "params_upstream_no_longer_accepts": bad_params,
        }
    return out


async def _live(router: Router) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    for label, factory in (
        ("best_autocad", lambda: router.best("check_runtime_environment", {"check_autocad": True})),
        ("slacker_autocad", lambda: router.slacker("autocad_status", {})),
    ):
        started = time.perf_counter()
        try:
            out = await factory()
            entry: dict[str, Any] = {"ok": bool(out["ok"]), "elapsed_ms": round((time.perf_counter() - started) * 1000)}
            if not out["ok"]:
                entry["error"] = dict(out["error"] or {})
                # best's preflight lists every failed check with a remediation: surface the first one.
                payload = out.get("structured") if isinstance(out.get("structured"), dict) else {}
                inner = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                failed = [c for c in inner.get("checks", []) if isinstance(c, dict) and c.get("ok") is False and c.get("required", True)]
                if failed:
                    entry["failed_checks"] = [{"name": c.get("name"), "detail": c.get("detail"), "remediation": c.get("remediation")} for c in failed]
                    entry["error"].setdefault("hint", failed[0].get("remediation"))
                    entry["error"]["message"] = f"{entry['error'].get('message', '')} [{', '.join(str(c.get('name')) for c in failed)}]"
            checks[label] = entry
        except Exception as exc:  # noqa: BLE001
            err = to_gateway_error(exc)
            checks[label] = {"ok": False, "error": err.to_dict()}
    return checks


def render_summary(report: dict[str, Any]) -> str:
    def mark(ok: Any) -> str:
        return "OK  " if ok is True else ("FAIL" if ok is False else "--  ")

    lines = [f"CAD Super MCP {__version__} doctor", ""]
    for name, info in report["status"]["backends"].items():
        extra = f"tools={info.get('tool_count')} protocol={info.get('protocol_version')}" if info.get("ok") else str(info.get("error", ""))[:90]
        lines.append(f"  [{mark(info.get('ok') if info.get('ok') else (None if 'disabled' in str(info.get('error', '')) else False))}] {name:13s} {extra}")
    lines.append("")
    for backend, inv in report["inventory_checks"].items():
        detail = "tool & parameter contracts match upstream" if inv["ok"] else (
            (f"missing tools {inv['missing_tools']}" if inv["missing_tools"] else "")
            + (f" params no longer accepted {inv['params_upstream_no_longer_accepts']}" if inv["params_upstream_no_longer_accepts"] else "")
            + ("" if inv["tool_count"] else " backend unavailable"))
        lines.append(f"  [{mark(inv['ok'])}] contract {backend:8s} {detail}")
    for label, chk in report["live_checks"].items():
        err = chk.get("error") or {}
        detail = f"{chk.get('elapsed_ms', '?')} ms" if chk["ok"] else f"{err.get('code', '')} {err.get('message', '')[:70]}"
        lines.append(f"  [{mark(chk['ok'])}] live {label:16s} {detail}")
        if chk.get("failed_checks"):
            lines.append("         failed checks: " + ", ".join(str(c.get("name")) for c in chk["failed_checks"]))
        if not chk["ok"] and err.get("hint"):
            lines.append(f"         -> {err['hint']}")
    lines.append("")
    lines.append(f"  essential_ready = {str(report['essential_ready']).lower()}" + ("" if report["essential_ready"] else "   (must be true to proceed; fix the FAIL items above first)"))
    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> int:
    config = load_config()
    router = Router(config)
    try:
        status = await router.status(include_tools=True, include_remote=True)
        inventory = check_inventory(status)
        live = await _live(router)
    finally:
        await router.aclose()
    if args.snapshot:
        snap = {
            "_comment": "Tool names exposed by the upstream backends this gateway targets.",
            "best-cad-mcp": {"tools": sorted(t["name"] for t in status["backends"].get("best", {}).get("tools", []))},
            "slacker": {"tools": sorted(t["name"] for t in status["backends"].get("slacker", {}).get("tools", []))},
        }
        Path(args.snapshot).write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
    shown = status
    if not args.tools:
        shown = {"workspace_root": status["workspace_root"], "sessions": status.get("sessions"), "policy": status.get("policy"), "backends": {}}
        for name, info in status["backends"].items():
            info = dict(info)
            info["tool_count"] = len(info.pop("tools", [])) if "tools" in info else info.get("tool_count")
            shown["backends"][name] = info
    report = {
        "version": __version__,
        "status": shown,
        "inventory_checks": inventory,
        "live_checks": live,
        "essential_ready": all(x["ok"] for x in inventory.values()) and all(x["ok"] for x in live.values()),
    }
    print(render_summary(report))
    if args.json:
        print()
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report["essential_ready"] else 2


def main() -> None:
    parser = argparse.ArgumentParser(description="CAD Super MCP backend doctor")
    parser.add_argument("--tools", action="store_true", help="include full tool inventories in the JSON")
    parser.add_argument("--json", action="store_true", help="also print the full JSON report")
    parser.add_argument("--snapshot", metavar="PATH", help="write the upstream tool-name snapshot used by the contract tests")
    args = parser.parse_args()
    try:
        code = asyncio.run(_run(args))
    except (ConfigError, FileNotFoundError) as exc:
        print(f"doctor failed: {exc}", file=sys.stderr)
        code = 3
    except Exception as exc:  # noqa: BLE001
        print(f"doctor failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        code = 3
    raise SystemExit(code)


if __name__ == "__main__":
    main()
