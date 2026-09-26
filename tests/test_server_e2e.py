"""End-to-end through the real MCP protocol (in-process Client) with a fake AutoCAD behind the gateway."""

import math

import pytest
from helpers import FakeAutoCAD, FakeBackends, make_config, run
from mcp import Client

from cad_super_mcp import geometry as G
from cad_super_mcp.backend import CallResult
from cad_super_mcp.router import Router
from cad_super_mcp.server import INSTRUCTIONS, create_server


def best_props(cad: FakeAutoCAD, shift: float = 0.0):
    """Answer best's get_entity_properties from the fake drawing (optionally shifting the read-back)."""

    def pad(p):
        return [p[0] + shift, p[1], p[2] if len(p) > 2 else 0.0]

    def handler(args):
        e = cad.entities.get(args["handle"])
        if not e:
            return {"result": "get properties failed: entity not found"}
        a, tool = e["args"], e["tool"]
        out = {"handle": e["handle"], "layer": e["layer"], "object_name": e["object_name"]}
        if tool == "draw_line":
            out.update(start_point=pad(a["start_mm"]), end_point=pad(a["end_mm"]))
        elif tool == "draw_circle":
            out.update(center=pad(a["center_mm"]), radius=a["radius_mm"])
        elif tool == "draw_arc":
            pts = G.arc_points(G.Pt.of(a["center_mm"]), a["radius_mm"], a["start_angle_deg"], a["end_angle_deg"])
            out.update(center=pad(a["center_mm"]), radius=a["radius_mm"], start_point=pad(pts["start"].as_list(dims=3)),
                       end_point=pad(pts["end"].as_list(dims=3)), start_angle=math.radians(a["start_angle_deg"]),
                       end_angle=math.radians(a["end_angle_deg"]))
        elif tool == "draw_polyline":
            out.update(vertices=[pad(v) for v in a["vertices_mm"]], closed=a.get("closed", False))
        elif tool == "draw_text":
            out.update(text_string=a["text"], height=a["height_mm"], insertion_point=pad(a["insertion_point_mm"]))
        return out

    return handler


def rig(tmp_path, *, script=None, cad=None, best_enabled=True, shift=0.0, safety=None, backends_extra=False):
    cad = cad or FakeAutoCAD()
    cad.layers["A-WALL"] = {"name": "A-WALL", "color_aci": 7}
    scripts = {"best.get_entity_properties": best_props(cad, shift)}
    scripts.update(script or {})
    fake = FakeBackends(cad=cad, script=scripts)
    backends = {"best": {"enabled": True, "kind": "stdio", "command": "unused"}} if best_enabled else {}
    if backends_extra:
        backends["product_help"] = {"enabled": True, "kind": "http", "url": "https://example.invalid/mcp"}
    router = Router(make_config(tmp_path, backends=backends, safety=safety), backends=fake)
    return create_server(router), router, fake, cad


async def call(app, name, args=None):
    async with Client(app) as client:
        return await client.call_tool(name, args or {})


def env(result):
    return result.structured_content


# ------------------------------------------------------------------ surface
def test_tool_surface_and_schemas(tmp_path):
    async def go():
        app, *_ = rig(tmp_path)
        async with Client(app) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
        assert len(tools) >= 40
        for name in ("cad_context", "cad_line", "cad_rect", "cad_text", "cad_measure", "cad_stage", "cad_stage_view", "cad_undo",
                     "cad_batch", "cad_name", "cad_layer", "cad_document", "cad_plan_check", "cad_render", "cad_verify"):
            assert name in tools, name
        # generic doors advertise their legal tool names instead of a free-form string
        draw_enum = tools["cad_draw"].input_schema["properties"]["tool"]["enum"]
        assert "draw_rectangle" in draw_enum and "execute_cad_plan" not in draw_enum
        assert "execute_cad_plan" not in tools["cad_edit"].input_schema["properties"]["tool"]["enum"]
        # read/write split: validating a plan must not look like a write to the host's approval logic
        assert tools["cad_plan_check"].annotations.read_only_hint is True
        assert tools["cad_plan"].annotations.read_only_hint is False
        assert tools["cad_stage_view"].annotations.read_only_hint is True and tools["cad_stage"].annotations.destructive_hint is True
        assert tools["cad_context"].annotations.read_only_hint is True

    run(go())


def test_instructions_teach_the_drafting_workflow():
    for needle in ("cad_context", "point spec", "verification", "stage=true", "NEEDS_CLARIFICATION", "cad_undo", "confirm=true"):
        assert needle in INSTRUCTIONS


# ------------------------------------------------------------------ context
def test_cad_context_grounds_the_model(tmp_path):
    async def go():
        app, router, _, cad = rig(tmp_path)
        cad.drawing["insunits"] = 1
        res = await call(app, "cad_context", {"pin": True})
        e = env(res)
        assert not res.is_error and e["ok"] and "test.dwg" in e["summary"] and "drawing pinned" in e["summary"]
        assert e["data"]["drawing"]["name"] == "test.dwg" and e["data"]["units"]["insunits"] == 1
        assert any("not millimetres" in w for w in e["warnings"])
        assert router.pinned and router.pinned["name"] == "test.dwg"
        assert "point spec" in e["data"]["point_spec_help"].lower() or "handle" in e["data"]["point_spec_help"]

    run(go())


def test_autocad_not_running_is_an_actionable_error_not_a_crash(tmp_path):
    async def go():
        cad = FakeAutoCAD()
        cad.running = False
        app, *_ = rig(tmp_path, cad=cad)
        res = await call(app, "cad_context")
        e = env(res)
        assert res.is_error and e["ok"] is False and e["error"]["code"] == "AUTOCAD_NOT_RUNNING" and "Start AutoCAD" in e["error"]["hint"]
        res2 = await call(app, "cad_line", {"start_mm": [0, 0], "end_mm": [1, 0]})
        assert env(res2)["error"]["code"] == "AUTOCAD_NOT_RUNNING"

    run(go())


# ---------------------------------------------------------------- precise drawing
def test_cad_line_is_verified_by_reading_it_back(tmp_path):
    async def go():
        app, _, fake, cad = rig(tmp_path)
        res = await call(app, "cad_line", {"start_mm": [0, 0], "end_mm": ["3.6m", 0], "layer": "A-WALL"})
        e = env(res)
        assert not res.is_error and e["ok"]
        d = e["data"]
        assert d["end_mm"] == [3600, 0] and d["length_mm"] == 3600 and d["layer"] == "A-WALL"
        assert d["verification"]["verified"] is True and d["verification"]["max_deviation_mm"] == 0
        assert "length 3600 mm" in e["summary"] and "read-back verification passed" in e["summary"] and d["handle"] in e["summary"]
        assert cad.entities[d["handle"]]["args"]["end_mm"] == [3600.0, 0.0]                  # exact mm reached the backend

    run(go())


def test_readback_mismatch_is_surfaced_loudly(tmp_path):
    async def go():
        app, *_ = rig(tmp_path, shift=0.5)                                                   # AutoCAD "drew" it 0.5 mm off
        e = env(await call(app, "cad_line", {"start_mm": [0, 0], "end_mm": [100, 0]}))
        assert e["ok"] is True                                                               # the entity exists...
        assert e["data"]["verification"]["verified"] is False and e["data"]["verification"]["max_deviation_mm"] == pytest.approx(0.5)
        assert "verification FAILED" in e["summary"] and any("report data.verification.checks" in w for w in e["warnings"])

    run(go())


def test_without_the_readback_backend_the_write_still_succeeds_but_says_it_is_unverified(tmp_path):
    async def go():
        app, *_ = rig(tmp_path, best_enabled=False)
        e = env(await call(app, "cad_line", {"start_mm": [0, 0], "end_mm": [100, 0]}))
        assert e["ok"] and e["data"]["verification"]["verified"] is None and "could not verify by read-back" in e["summary"]
        e2 = env(await call(app, "cad_line", {"start_mm": [0, 0], "end_mm": [100, 0], "verify": False}))
        assert "verification" not in e2["data"]

    run(go())


def test_anchors_let_the_model_avoid_arithmetic(tmp_path):
    async def go():
        app, _, fake, cad = rig(tmp_path)
        wall = env(await call(app, "cad_line", {"start_mm": [0, 0], "end_mm": [3600, 0], "layer": "A-WALL"}))["data"]["handle"]
        # "a 1200 mm circle table 900 above the middle of that wall" - no coordinates computed by the model:
        res = env(await call(app, "cad_circle", {"center_mm": {"handle": wall, "snap": "mid", "dy": 900}, "diameter_mm": "1.2m"}))
        assert res["ok"] and res["data"]["center_mm"] == [1800, 900] and res["data"]["radius_mm"] == 600
        # named entity + polar offset
        await call(app, "cad_name", {"action": "set", "name": "west wall", "handle": wall})
        r2 = env(await call(app, "cad_line", {"start_mm": {"name": "west wall", "snap": "end"}, "end_mm": {"from": {"name": "west wall", "snap": "end"}, "angle_deg": 90, "dist": 2400}}))
        assert r2["data"]["start_mm"] == [3600, 0] and r2["data"]["end_mm"] == [3600, 2400]

    run(go())


def test_ambiguous_references_ask_instead_of_guessing(tmp_path):
    async def go():
        app, *_ = rig(tmp_path)
        wall = env(await call(app, "cad_line", {"start_mm": [0, 0], "end_mm": [3600, 0]}))["data"]["handle"]
        res = await call(app, "cad_line", {"start_mm": {"handle": wall}, "end_mm": [1, 1]})
        e = env(res)
        assert res.is_error and e["error"]["code"] == "NEEDS_CLARIFICATION"
        assert set(e["error"]["details"]["options"]) == {"start", "end", "mid"} and "Do not guess" in e["error"]["hint"]

    run(go())


def test_rect_circle_arc_polyline_text_report_exact_derived_numbers(tmp_path):
    async def go():
        app, *_ = rig(tmp_path)
        r = env(await call(app, "cad_rect", {"corner": [0, 0], "width": "3.6m", "height": 2400, "layer": "A-WALL"}))
        assert r["ok"] and r["data"]["area_m2"] == 8.64 and r["data"]["perimeter_mm"] == 12000 and r["data"]["verification"]["verified"]
        assert r["data"]["vertices_mm"] == [[0, 0], [3600, 0], [3600, 2400], [0, 2400]] and "8.64 m²" in r["summary"]
        c = env(await call(app, "cad_circle", {"center_mm": [1500, 1800], "radius_mm": 600}))
        assert c["data"]["diameter_mm"] == 1200 and c["data"]["verification"]["verified"]
        a = env(await call(app, "cad_arc", {"center_mm": [0, 0], "radius_mm": 100, "start_angle_deg": 0, "end_angle_deg": 180}))
        assert a["data"]["sweep_deg"] == 180 and a["data"]["mid_mm"] == [0, 100] and a["data"]["verification"]["verified"]
        p = env(await call(app, "cad_polyline", {"vertices_mm": [[0, 0], [100, 0], [100, 60], [0, 60]], "closed": True}))
        assert p["data"]["area_mm2"] == 6000 and p["data"]["verification"]["verified"]
        t = env(await call(app, "cad_text", {"text": "EXIT", "at": [100, 200], "height_mm": 250, "layer": "A-WALL"}))
        assert t["ok"] and t["data"]["verification"]["verified"] and "EXIT" in t["summary"]

    run(go())


def test_wall_from_a_centerline_is_computed_exactly_and_verified(tmp_path):
    async def go():
        app, _, fake, cad = rig(tmp_path)
        w = env(await call(app, "cad_wall", {"centerline": [[0, 0], ["3.6m", 0]], "thickness": 240, "layer": "A-WALL"}))
        assert w["ok"] and w["data"]["thickness_mm"] == 240 and w["data"]["centerline_length_mm"] == 3600
        loop = w["data"]["outlines"][0]
        assert loop["vertices_mm"] == [[0, 120], [3600, 120], [3600, -120], [0, -120]] and loop["area_mm2"] == 3600 * 240
        assert loop["verification"]["verified"] is True and "read-back verification passed for all outlines" in w["summary"]
        # a closed centre line (a room) gives an inner and an outer ring
        ring = env(await call(app, "cad_wall", {"centerline": [[0, 0], [4000, 0], [4000, 3000], [0, 3000]], "thickness": "0.24m",
                                                "closed": True, "layer": "A-WALL"}))
        assert ring["ok"] and len(ring["data"]["handles"]) == 2
        inner, outer = ring["data"]["outlines"]
        assert inner["area_mm2"] == pytest.approx((4000 - 240) * (3000 - 240)) and outer["area_mm2"] == pytest.approx((4000 + 240) * (3000 + 240))
        # one-sided wall
        left = env(await call(app, "cad_wall", {"centerline": [[0, 0], [1000, 0]], "thickness": 200, "justify": "left"}))
        assert left["data"]["outlines"][0]["vertices_mm"] == [[0, 200], [1000, 200], [1000, 0], [0, 0]]
        # bad input never reaches AutoCAD
        n = len(fake.calls)
        for args in ({"centerline": [[0, 0]], "thickness": 240}, {"centerline": [[0, 0], [1, 0]], "thickness": 0},
                     {"centerline": [[0, 0], [0, 0]], "thickness": 200}):
            res = await call(app, "cad_wall", args)
            assert res.is_error and env(res)["error"]["code"] == "INVALID_ARGUMENT", args
        assert [c for c in fake.calls[n:] if c[1].startswith("draw_")] == []

    run(go())


def test_wall_can_be_staged_and_undone_as_a_unit(tmp_path):
    async def go():
        app, _, fake, cad = rig(tmp_path)
        async with Client(app) as c:
            r = (await c.call_tool("cad_wall", {"centerline": [[0, 0], [4000, 0], [4000, 3000], [0, 3000]], "thickness": 240,
                                                "closed": True, "layer": "A-WALL", "stage": True})).structured_content
            assert r["ok"] and all(cad.entities[h]["layer"] == "_AI_STAGE" for h in r["data"]["handles"])
            assert (await c.call_tool("cad_stage", {"action": "commit"})).structured_content["data"]["committed"] == 2
            undo = (await c.call_tool("cad_undo", {"steps": 2, "confirm": True})).structured_content
            assert undo["data"]["undone"] == 2 and cad.entities == {}

    run(go())


def test_dimension_line_position_is_computed_by_the_gateway(tmp_path):
    async def go():
        app, _, fake, cad = rig(tmp_path)
        d = env(await call(app, "cad_dimension", {"start_mm": [0, 0], "end_mm": [3600, 0], "offset": "0.5m", "layer": "A-WALL"}))
        assert d["ok"] and d["data"]["measured_mm"] == 3600 and d["data"]["dimension_line_point_mm"] == [1800, 500]
        sent = fake.calls[-1][2]
        assert sent["dimension_line_point_mm"] == [1800.0, 500.0] and sent["orientation"] == "aligned"
        below = env(await call(app, "cad_dimension", {"start_mm": [0, 0], "end_mm": [3600, 0], "offset": -300}))
        assert below["data"]["dimension_line_point_mm"] == [1800, -300]                          # negative offset = other side
        diag = env(await call(app, "cad_dimension", {"start_mm": [0, 0], "end_mm": [3000, 4000], "offset": 1000}))
        # midpoint (1500, 2000) + 1000 mm along the left normal (-0.8, 0.6) = (700, 2600); (-800, 600) . (3000, 4000) == 0
        assert diag["data"]["measured_mm"] == 5000 and diag["data"]["dimension_line_point_mm"] == [700, 2600]
        h = env(await call(app, "cad_dimension", {"start_mm": [0, 0], "end_mm": [3000, 4000], "orientation": "horizontal", "offset": 200}))
        assert h["data"]["measured_mm"] == 3000 and h["data"]["dimension_line_point_mm"] == [1500, 4200]
        v = env(await call(app, "cad_dimension", {"start_mm": [0, 0], "end_mm": [3000, 4000], "orientation": "vertical", "offset": 200}))
        assert v["data"]["measured_mm"] == 4000 and v["data"]["dimension_line_point_mm"] == [3200, 2000]
        zero = await call(app, "cad_dimension", {"start_mm": [0, 0], "end_mm": [0, 500], "orientation": "horizontal"})
        assert zero.is_error and env(zero)["error"]["code"] == "INVALID_ARGUMENT"

    run(go())


def test_wall_and_dimension_work_inside_a_batch(tmp_path):
    async def go():
        app, *_ = rig(tmp_path)
        ops = [{"op": "wall", "centerline": [[0, 0], [3600, 0]], "thickness": 240, "layer": "A-WALL"},
               {"op": "dimension", "start_mm": [0, 0], "end_mm": [3600, 0], "offset": 600, "layer": "A-WALL"}]
        e = env(await call(app, "cad_batch", {"operations": ops}))
        assert e["ok"] and len(e["data"]["results"]) == 2 and e["data"]["stopped_at"] is None

    run(go())


def test_invalid_geometry_is_rejected_before_touching_autocad(tmp_path):
    async def go():
        app, _, fake, _ = rig(tmp_path)
        n = len(fake.calls)
        cases = [
            ("cad_line", {"start_mm": [0, 0], "end_mm": [0, 0]}),
            ("cad_circle", {"center_mm": [0, 0]}),                                            # neither radius nor diameter
            ("cad_circle", {"center_mm": [0, 0], "radius_mm": -5}),
            ("cad_rect", {"width": 100, "height": 50}),                                       # no anchor
            ("cad_rect", {"width": 100, "height": 50, "corner": [0, 0], "center": [1, 1]}),   # both anchors
            ("cad_polyline", {"vertices_mm": [[0, 0]]}),
            ("cad_line", {"start_mm": [0, "abc"], "end_mm": [1, 1]}),
            ("cad_text", {"text": "  ", "at": [0, 0], "height_mm": 10}),
        ]
        for name, args in cases:
            res = await call(app, name, args)
            assert res.is_error, (name, args)
            assert env(res)["error"]["code"] == "INVALID_ARGUMENT", (name, args, env(res)["error"])
        assert [c for c in fake.calls[n:] if c[1].startswith("draw_")] == []                  # nothing was drawn

    run(go())


def test_missing_layer_gets_a_hint(tmp_path):
    async def go():
        app, *_ = rig(tmp_path)
        res = await call(app, "cad_line", {"start_mm": [0, 0], "end_mm": [1, 0], "layer": "NOPE"})
        e = env(res)
        assert res.is_error and "cad_layer" in e["error"]["hint"]

    run(go())


# ---------------------------------------------------------- safety through the wire
def test_confirm_is_checked_before_anything_touches_autocad(tmp_path):
    async def go():
        cad = FakeAutoCAD()
        cad.running = False                                                  # AutoCAD is down...
        app, _, fake, _ = rig(tmp_path, cad=cad)
        res = await call(app, "cad_erase", {"handle": "2A3"})
        assert env(res)["error"]["code"] == "CONFIRM_REQUIRED"               # ...but the refusal is about confirm, not AutoCAD
        assert fake.calls == []                                              # and no backend was contacted at all

    run(go())


def test_health_skips_the_slow_remote_check_unless_asked(tmp_path):
    async def go():
        app, *_ = rig(tmp_path, backends_extra=True)
        e = env(await call(app, "cad_health"))
        assert e["data"]["backends"]["product_help"]["skipped"] is True and "not checked: product_help" in e["summary"]
        e2 = env(await call(app, "cad_health", {"check_remote": True}))
        assert e2["data"]["backends"]["product_help"].get("skipped") is None

    run(go())


def test_erase_needs_confirm_and_says_how(tmp_path):
    async def go():
        app, *_ = rig(tmp_path)
        h = env(await call(app, "cad_line", {"start_mm": [0, 0], "end_mm": [1, 0]}))["data"]["handle"]
        res = await call(app, "cad_erase", {"handle": h})
        e = env(res)
        assert res.is_error and e["error"]["code"] == "CONFIRM_REQUIRED" and "confirm=true" in e["error"]["hint"]
        assert env(await call(app, "cad_erase", {"handle": h, "confirm": True}))["ok"]

    run(go())


def test_plan_execution_always_carries_the_safety_flags_and_the_generic_doors_cannot_reach_it(tmp_path):
    async def go():
        app, _, fake, _ = rig(tmp_path)
        res = await call(app, "cad_plan", {"action": "execute", "arguments": {"transactional": False, "plan": {}}, "confirm": True})
        assert env(res)["ok"] and fake.calls[-1][1] == "execute_cad_plan" and fake.calls[-1][2]["transactional"] is True
        bad = await call(app, "cad_edit", {"tool": "execute_cad_plan", "arguments": {"transactional": False}, "confirm": True})
        assert bad.is_error                                                                    # rejected by the schema itself
        n = len(fake.calls)
        chk = await call(app, "cad_plan_check", {"action": "validate", "arguments": {}})
        assert env(chk)["ok"] and fake.calls[n][1] == "validate_cad_plan"

    run(go())


def test_raw_channel_disabled_by_default_through_the_wire(tmp_path):
    async def go():
        app, *_ = rig(tmp_path)
        e = env(await call(app, "cad_raw_command", {"command": "_LINE 0,0 1,1", "confirm": True}))
        assert e["error"]["code"] == "RAW_DISABLED"
        app2, *_ = rig(tmp_path, safety={"enable_raw_felix": True})
        e2 = env(await call(app2, "cad_raw_command", {"command": "_.NETLOAD", "confirm": True}))
        assert e2["error"]["code"] == "RAW_BLOCKED"

    run(go())


# --------------------------------------------------------- stage / undo / batch
def test_stage_preview_commit_and_undo_flow(tmp_path):
    async def go():
        shot = CallResult(backend="best", tool="render_drawing_view", ok=True, structured={"success": True}, content=[],
                          images=[{"mime_type": "image/png", "data": "QUJD"}])
        app, _, fake, cad = rig(tmp_path, script={"best.render_drawing_view": shot})
        async with Client(app) as c:
            w = (await c.call_tool("cad_line", {"start_mm": [0, 0], "end_mm": [3600, 0], "layer": "A-WALL", "stage": True})).structured_content
            assert w["data"]["staged"] and w["data"]["layer"] == "_AI_STAGE" and w["data"]["target_layer"] == "A-WALL"
            assert "staging layer" in w["summary"] and cad.entities[w["data"]["handle"]]["layer"] == "_AI_STAGE"
            prev = await c.call_tool("cad_stage_view", {"action": "preview"})
            assert [b.type for b in prev.content] == ["text", "image"]                          # the human/model can SEE it
            assert prev.content[1].data == "QUJD" and prev.structured_content["data"]["pending"] == 1
            done = (await c.call_tool("cad_stage", {"action": "commit"})).structured_content
            assert done["ok"] and done["data"]["committed"] == 1
            assert cad.entities[w["data"]["handle"]]["layer"] == "A-WALL"
            plan = (await c.call_tool("cad_undo", {})).structured_content                          # preview only
            assert plan["data"]["confirm_required"] and w["data"]["handle"] in plan["summary"] and w["data"]["handle"] in cad.entities
            undone = (await c.call_tool("cad_undo", {"confirm": True})).structured_content
            assert undone["ok"] and undone["data"]["undone"] == 1 and cad.entities == {}

    run(go())


def test_discard_removes_staged_geometry(tmp_path):
    async def go():
        app, _, fake, cad = rig(tmp_path)
        async with Client(app) as c:
            await c.call_tool("cad_rect", {"corner": [0, 0], "width": 1000, "height": 500, "layer": "A-WALL", "stage": True})
            await c.call_tool("cad_circle", {"center_mm": [5, 5], "radius_mm": 3, "layer": "A-WALL", "stage": True})
            assert len(cad.entities) == 2
            res = (await c.call_tool("cad_stage", {"action": "discard"})).structured_content
            assert res["ok"] and res["data"]["erased"] == 2 and cad.entities == {}

    run(go())


def test_batch_runs_in_one_group_stops_on_error_and_undoes_as_a_unit(tmp_path):
    async def go():
        app, _, fake, cad = rig(tmp_path)
        ops = [
            {"op": "rect", "corner": [0, 0], "width": 4000, "height": 3000, "layer": "A-WALL"},
            {"op": "circle", "center_mm": [2000, 1500], "diameter_mm": 1200, "layer": "A-WALL"},
            {"op": "line", "start_mm": [0, 0], "end_mm": [0, 0]},                                   # invalid: stops here
            {"op": "text", "text": "never runs", "at": [0, 0], "height_mm": 100},
        ]
        async with Client(app) as c:
            res = await c.call_tool("cad_batch", {"operations": ops})
            e = res.structured_content
            assert res.is_error and e["error"]["code"] == "BATCH_PARTIAL" and e["data"]["stopped_at"] == 2
            assert [r["ok"] for r in e["data"]["results"]] == [True, True, False] and len(e["data"]["created"]) == 2
            group = e["data"]["batch"]
            assert len(cad.entities) == 2
            undone = (await c.call_tool("cad_undo", {"scope": "group", "group": group, "confirm": True})).structured_content
            assert undone["data"]["undone"] == 2 and cad.entities == {}

    run(go())


def test_batch_rejects_unknown_ops_and_parameters(tmp_path):
    async def go():
        app, *_ = rig(tmp_path)
        res = await call(app, "cad_batch", {"operations": [{"op": "explode_everything"}]})
        assert res.is_error and "explode_everything" in env(res)["error"]["message"]
        res2 = await call(app, "cad_batch", {"operations": [{"op": "line", "start_mm": [0, 0], "end_mm": [1, 1], "colour": "red"}]})
        e = env(res2)
        assert res2.is_error and e["data"]["results"][0]["error"]["code"] == "INVALID_ARGUMENT" and "colour" in e["data"]["results"][0]["error"]["message"]

    run(go())


# ------------------------------------------------------------ measure / layer / doc
def test_measure_is_exact_local_math(tmp_path):
    async def go():
        app, *_ = rig(tmp_path)
        d = env(await call(app, "cad_measure", {"kind": "distance", "a": [0, 0], "b": ["3m", "4m"]}))["data"]
        assert d["distance_mm"] == 5000 and d["angle_deg"] == pytest.approx(53.130102, abs=1e-5) and d["midpoint_mm"] == [1500, 2000]
        a = env(await call(app, "cad_measure", {"kind": "area", "points": [[0, 0], ["4m", 0], ["4m", "3m"], [0, "3m"]]}))["data"]
        assert a["area_m2"] == 12 and a["perimeter_mm"] == 14000 and a["centroid_mm"] == [2000, 1500]
        wall = env(await call(app, "cad_line", {"start_mm": [0, 0], "end_mm": [3600, 0]}))["data"]["handle"]
        ent = env(await call(app, "cad_measure", {"kind": "entity", "handle": wall}))["data"]
        assert ent["kind"] == "line" and ent["end_mm"] == [3600, 0]

    run(go())


def test_pure_math_measuring_works_even_when_autocad_is_down(tmp_path):
    async def go():
        cad = FakeAutoCAD()
        cad.running = False
        app, *_ = rig(tmp_path, cad=cad)
        d = await call(app, "cad_measure", {"kind": "distance", "a": [0, 0], "b": ["3m", "4m"]})
        assert not d.is_error and env(d)["data"]["distance_mm"] == 5000 and any("pure-math" in w for w in env(d)["warnings"])
        ar = await call(app, "cad_measure", {"kind": "area", "points": [[0, 0], [1000, 0], [1000, 1000]]})
        assert not ar.is_error and env(ar)["data"]["area_mm2"] == 500000
        # ...but anything that must *read* the drawing still explains why it cannot
        ref = await call(app, "cad_measure", {"kind": "distance", "a": {"handle": "2A3", "snap": "end"}, "b": [0, 0]})
        assert ref.is_error and env(ref)["error"]["code"] == "AUTOCAD_NOT_RUNNING"
        ent = await call(app, "cad_measure", {"kind": "entity", "handle": "2A3"})
        assert ent.is_error and env(ent)["error"]["code"] == "AUTOCAD_NOT_RUNNING"

    run(go())


def test_layer_and_document_tools(tmp_path):
    async def go():
        app, _, fake, cad = rig(tmp_path)
        assert env(await call(app, "cad_layer", {"action": "create", "name": "A-DOOR", "color_aci": 3}))["ok"]
        assert "A-DOOR" in cad.layers
        bad = await call(app, "cad_layer", {"action": "set_props", "name": "A-DOOR"})
        assert bad.is_error and env(bad)["error"]["code"] == "INVALID_ARGUMENT"
        restricted, *_ = rig(tmp_path, safety={"allowed_open_roots": [str(tmp_path / "ok")]})
        denied = await call(restricted, "cad_document", {"action": "open", "path": str(tmp_path / "elsewhere" / "x.dwg")})
        assert denied.is_error and env(denied)["error"]["code"] == "NOT_ALLOWED"

    run(go())


# ----------------------------------------------------------- envelope / images
def test_render_returns_a_native_image_and_no_base64_text(tmp_path):
    async def go():
        shot = CallResult(backend="best", tool="render_drawing_view", ok=True, structured={"success": True, "path": "out.png"}, content=[],
                          images=[{"mime_type": "image/png", "data": "iVBORw0KGgo="}])
        app, *_ = rig(tmp_path, script={"best.render_drawing_view": shot})
        res = await call(app, "cad_render")
        assert [b.type for b in res.content] == ["text", "image"] and res.content[1].mime_type == "image/png"
        assert "iVBOR" not in res.content[0].text

    run(go())


def test_verify_is_one_atomic_look_with_a_picture(tmp_path):
    async def go():
        shot = CallResult(backend="best", tool="render_drawing_view", ok=True, structured={"success": True}, content=[],
                          images=[{"mime_type": "image/png", "data": "QUJD"}])
        app, _, fake, _ = rig(tmp_path, script={"best.render_drawing_view": shot})
        res = await call(app, "cad_verify", {"deep": True})
        e = env(res)
        assert e["ok"] and {"scan", "ir", "dimension_binding", "constraints", "constraint_check", "validation", "render"} <= set(e["data"])
        assert [b.type for b in res.content] == ["text", "image"]

    run(go())


def test_large_scans_are_trimmed_instead_of_flooding_the_context(tmp_path):
    async def go():
        big = {"entities": [{"handle": f"H{i}", "layer": "A-WALL", "pad": "x" * 60} for i in range(5000)], "count": 5000}
        app, *_ = rig(tmp_path, script={"best.scan_all_entities": big})
        res = await call(app, "cad_scan")
        e = env(res)
        assert len(res.content[0].text) <= 60000 and e["data"]["count"] == 5000 and len(e["data"]["entities"]) < 5000
        assert any("trimmed" in w for w in e["warnings"])

    run(go())


def test_legacy_passthrough_tools_use_the_same_envelope(tmp_path):
    async def go():
        app, *_ = rig(tmp_path)
        e = env(await call(app, "cad_list_layers"))
        assert list(e) == ["ok", "summary", "data", "warnings", "error", "meta"] and e["ok"]
        e2 = env(await call(app, "cad_health"))
        assert e2["ok"] and e2["data"]["essential_ready"] in (True, False) and "online" in e2["summary"]

    run(go())
