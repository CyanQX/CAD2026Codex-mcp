"""Contract tests: the gateway's allow-lists must match what the upstream backends really expose.

`tests/data/upstream_tools.json` is a snapshot of the tool names of best-cad-mcp 1.7.0 (profile
`core`) and the Slacker backend.  A drift between policy.py and upstream fails here instead of at
the moment a model calls a tool that does not exist.
"""

import json
from pathlib import Path

from cad_super_mcp import policy as P

SNAP = json.loads((Path(__file__).parent / "data" / "upstream_tools.json").read_text(encoding="utf-8"))
BEST = set(SNAP["best-cad-mcp"]["tools"])
SLACKER = set(SNAP["slacker"]["tools"])


def test_slacker_allowlist_is_exactly_the_upstream_tool_set():
    assert P.SLACKER_TOOLS == SLACKER
    assert not (P.SLACKER_READ_TOOLS & P.SLACKER_WRITE_TOOLS)


def test_every_best_name_in_the_policy_exists_upstream():
    names = P.BEST_READ_TOOLS | P.BEST_WRITE_TOOLS
    assert names <= BEST, f"not in best-cad-mcp core: {sorted(names - BEST)}"


def test_read_and_write_sets_do_not_overlap():
    assert not (P.BEST_READ_TOOLS & P.BEST_WRITE_TOOLS), sorted(P.BEST_READ_TOOLS & P.BEST_WRITE_TOOLS)


def test_every_lane_tool_is_allowlisted():
    allowed = P.BEST_READ_TOOLS | P.BEST_WRITE_TOOLS
    for lane, tools in P.LANES.items():
        assert tools <= allowed, (lane, sorted(tools - allowed))


def test_execute_cad_plan_lives_only_in_the_plan_lane():
    homes = [lane for lane, tools in P.LANES.items() if "execute_cad_plan" in tools]
    assert homes == ["plan"]


def test_destructive_and_save_tools_are_known_tools():
    known = P.BEST_READ_TOOLS | P.BEST_WRITE_TOOLS | P.SLACKER_TOOLS
    assert P.DESTRUCTIVE <= known and P.SAVE_TOOLS <= known


def test_slacker_lanes_are_subsets_of_slacker_tools():
    for lane, tools in P.SLACKER_LANES.items():
        assert tools <= P.SLACKER_TOOLS, lane


def test_internal_tools_used_by_precision_features_are_readable():
    assert P.INTERNAL_BEST_TOOLS <= P.BEST_READ_TOOLS <= BEST


def test_risk_classification():
    assert P.classify_risk("slacker", "list_layers") == "read"
    assert P.classify_risk("slacker", "draw_line") == "write"
    assert P.classify_risk("slacker", "erase_entity") == "destructive"
    assert P.classify_risk("slacker", "save_drawing_as") == "export"
    assert P.classify_risk("best", "scan_all_entities") == "read"
    assert P.classify_risk("best", "execute_cad_plan") == "destructive"
    assert P.classify_risk("felix", "send_command") == "raw"
    assert P.classify_risk("product_help", "search_help_content") == "read"


def test_plan_safety_flags_override_everything():
    forced = P.enforce_plan_safety({"transactional": False, "plan": 1})
    assert forced["transactional"] is True and forced["plan"] == 1
    assert set(P.PLAN_EXECUTE_FLAGS) <= set(forced)
