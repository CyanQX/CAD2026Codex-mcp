import json
from pathlib import Path

from cad_super_mcp.doctor import EXPECTED_PARAMS, REQUIRED_TOOLS, check_inventory, render_summary

SNAP = json.loads((Path(__file__).parent / "data" / "upstream_tools.json").read_text(encoding="utf-8"))


def status_from_snapshot(drop_tools=(), drop_params=None):
    """A status dict shaped like Router.status(include_tools=True) for the real upstream tool names."""

    def tools(names, backend):
        out = []
        for n in names:
            if n in drop_tools:
                continue
            props = {p: {} for p in EXPECTED_PARAMS.get(backend, {}).get(n, {"x"})}
            for p in (drop_params or {}).get(n, []):
                props.pop(p, None)
            out.append({"name": n, "description": "", "input_schema": {"properties": props}})
        return out

    return {"backends": {
        "best": {"ok": True, "tools": tools(SNAP["best-cad-mcp"]["tools"], "best")},
        "slacker": {"ok": True, "tools": tools(SNAP["slacker"]["tools"], "slacker")},
    }}


def test_required_tools_and_expected_params_exist_in_the_upstream_snapshot():
    assert REQUIRED_TOOLS["best"] <= set(SNAP["best-cad-mcp"]["tools"])
    assert REQUIRED_TOOLS["slacker"] <= set(SNAP["slacker"]["tools"])
    assert set(EXPECTED_PARAMS["best"]) <= set(SNAP["best-cad-mcp"]["tools"])
    assert set(EXPECTED_PARAMS["slacker"]) <= set(SNAP["slacker"]["tools"])


def test_a_healthy_inventory_passes():
    inv = check_inventory(status_from_snapshot())
    assert inv["best"]["ok"] and inv["slacker"]["ok"] and inv["slacker"]["tool_count"] == 29


def test_removed_upstream_tool_fails_the_doctor():
    inv = check_inventory(status_from_snapshot(drop_tools={"draw_text"}))
    assert not inv["slacker"]["ok"] and "draw_text" in inv["slacker"]["missing_tools"]


def test_renamed_upstream_parameter_fails_the_doctor_even_though_the_tool_still_exists():
    inv = check_inventory(status_from_snapshot(drop_params={"draw_line": ["start_mm"]}))
    assert not inv["slacker"]["ok"] and inv["slacker"]["params_upstream_no_longer_accepts"] == {"draw_line": ["start_mm"]}


def test_unreachable_backend_is_reported_not_crashed():
    inv = check_inventory({"backends": {"best": {"ok": False, "error": "x"}, "slacker": {"ok": False}}})
    assert not inv["best"]["ok"] and not inv["slacker"]["ok"]


def test_summary_is_human_readable_and_explains_the_first_fix():
    report = {
        "status": {"backends": {"best": {"ok": True, "tool_count": 210, "protocol_version": "2026-07-28"},
                                "official": {"ok": False, "error": "official: Backend 'official' is disabled"}}},
        "inventory_checks": check_inventory(status_from_snapshot()),
        "live_checks": {"slacker_autocad": {"ok": False, "error": {"code": "AUTOCAD_NOT_RUNNING", "message": "No running AutoCAD session", "hint": "Start AutoCAD 2026"}}},
        "essential_ready": False,
    }
    text = render_summary(report)
    assert "[OK  ] best" in text and "[--  ] official" in text and "[FAIL] live slacker_autocad" in text
    assert "Start AutoCAD 2026" in text and "essential_ready = false" in text
