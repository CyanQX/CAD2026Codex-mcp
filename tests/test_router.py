"""Router: every defect found while auditing v0.1 is pinned down here as a regression test."""

import asyncio
import json

import pytest
from helpers import FakeAutoCAD, FakeBackends, make_config, run

from cad_super_mcp import policy as P
from cad_super_mcp.backend import BackendUnavailable
from cad_super_mcp.errors import (
    ConfirmRequired,
    DrawingChanged,
    LockTimeout,
    NotAllowed,
    RawBlocked,
    RawDisabled,
)
from cad_super_mcp.router import Router, drawing_identity


def make_router(tmp_path, *, script=None, cad=None, safety=None, runtime=None):
    cfg = make_config(tmp_path, safety=safety, runtime=runtime)
    fake = FakeBackends(cad=cad, script=script)
    return Router(cfg, backends=fake), fake


def audit_rows(router):
    path = router.audit.path
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


# ------------------------------------------------------------ safety at the choke point
HOSTILE_PLAN = {"plan": {"steps": []}, "transactional": False, "rollback_on_error": False, "validate_after_plan": False,
                "rollback_on_high_severity_validation": False, "rescan_after_plan": False, "allow_modify": False}


def test_execute_cad_plan_always_gets_the_forced_safety_flags(tmp_path):
    async def go():
        r, fake = make_router(tmp_path)
        await r.best("execute_cad_plan", dict(HOSTILE_PLAN), confirm=True, lane="plan")
        sent = fake.calls[-1][2]
        assert all(sent[k] is True for k in P.PLAN_EXECUTE_FLAGS)          # hostile False values were overridden
        assert sent["plan"] == {"steps": []}                                # ...and the caller's plan is untouched
        # Even a legacy script calling Router.best() directly (no lane) cannot skip them:
        await r.best("execute_cad_plan", dict(HOSTILE_PLAN), confirm=True)
        assert all(fake.calls[-1][2][k] is True for k in P.PLAN_EXECUTE_FLAGS)

    run(go())


def test_execute_cad_plan_is_unreachable_through_edit_draw_annotate(tmp_path):
    # v0.1: cad_edit("execute_cad_plan", {...transactional: False}, confirm=True) reached the backend unprotected.
    async def go():
        r, fake = make_router(tmp_path)
        with pytest.raises(NotAllowed):
            await r.edit("execute_cad_plan", dict(HOSTILE_PLAN), confirm=True)
        with pytest.raises(NotAllowed):
            await r.draw("execute_cad_plan", dict(HOSTILE_PLAN))
        with pytest.raises(NotAllowed):
            await r.annotate("execute_cad_plan", dict(HOSTILE_PLAN))
        assert fake.calls == []                                             # nothing reached a backend

    run(go())


def test_execute_cad_plan_requires_confirm(tmp_path):
    async def go():
        r, fake = make_router(tmp_path)
        with pytest.raises(ConfirmRequired):
            await r.best("execute_cad_plan", {}, lane="plan")
        assert fake.calls == []

    run(go())


def test_destructive_and_save_tools_require_confirm(tmp_path):
    async def go():
        r, fake = make_router(tmp_path)
        with pytest.raises(ConfirmRequired):
            await r.slacker("erase_entity", {"handle": "1"})
        with pytest.raises(ConfirmRequired):
            await r.slacker("save_drawing_as", {"path": "a.dwg"})
        with pytest.raises(ConfirmRequired):
            await r.best("export_pdf", {}, lane="export")
        with pytest.raises(ConfirmRequired):
            await r.best("delete_entity", {"handle": "1"}, lane="edit")
        assert fake.calls == []
        await r.slacker("save_drawing_as", {"path": "a.dwg"}, confirm=True)
        assert fake.calls[-1][1] == "save_drawing_as"

    run(go())


def test_review_image_export_needs_no_confirm(tmp_path):
    async def go():
        r, fake = make_router(tmp_path)
        await r.best("export_view_image", {}, lane="export")
        assert fake.calls[-1][1] == "export_view_image"

    run(go())


def test_image_trace_prepare_is_reachable_now(tmp_path):
    # v0.1: prepare_image_trace was in neither allowlist, so cad_image_trace(action="prepare") always failed.
    async def go():
        r, fake = make_router(tmp_path)
        out = await r.best("prepare_image_trace", {"image_path": "x.png"}, lane="image_trace")
        assert out["ok"] and fake.calls[-1][1] == "prepare_image_trace"

    run(go())


def test_lane_errors_list_what_is_allowed(tmp_path):
    async def go():
        r, _ = make_router(tmp_path)
        with pytest.raises(NotAllowed) as ei:
            await r.edit("draw_rectangle", {})
        assert "draw_rectangle" in ei.value.message and "move_entity" in ei.value.details["allowed"]
        with pytest.raises(NotAllowed) as ei2:
            await r.best("definitely_not_a_tool", {})
        assert ei2.value.hint

    run(go())


def test_generic_routes_prefer_slacker_for_primitives(tmp_path):
    async def go():
        r, fake = make_router(tmp_path)
        await r.draw("draw_line", {"start_mm": [0, 0], "end_mm": [1, 0]})
        await r.edit("move_entity", {"handle": "2A0", "from_mm": [0, 0], "to_mm": [1, 1]})
        await r.annotate("add_linear_dimension", {"start_mm": [0, 0], "end_mm": [1, 0], "dimension_line_point_mm": [0, 1]})
        await r.draw("draw_rectangle", {})
        assert [c[0] for c in fake.calls] == ["slacker", "slacker", "slacker", "best"]

    run(go())


# ------------------------------------------------------------------- honest results
def test_soft_failures_are_reported_as_failures_everywhere(tmp_path):
    # v0.1: payload {"ok": false} was returned as ok=True and audited as ok=True.
    async def go():
        cad = FakeAutoCAD()
        cad.running = False
        r, _ = make_router(tmp_path, cad=cad)
        out = await r.slacker("get_active_drawing_info", {})
        assert out["transport_ok"] is True and out["ok"] is False
        assert out["error"]["code"] == "AUTOCAD_NOT_RUNNING" and "Start AutoCAD" in out["error"]["hint"]
        after = [x for x in audit_rows(r) if x["phase"] == "after"][-1]
        assert after["ok"] is False and after["outcome"] == "backend_reported_failure"

    run(go())


def test_successful_write_stays_successful_even_if_the_audit_log_is_unusable(tmp_path):
    # v0.1: an audit write error after a successful backend write surfaced as a tool error -> duplicate retries.
    async def go():
        r, fake = make_router(tmp_path)
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        r.audit.path = blocker / "sub" / "audit.jsonl"                     # mkdir on a *file*: every write fails
        out = await r.slacker("draw_line", {"start_mm": [0, 0], "end_mm": [1, 0]})
        assert out["ok"] is True and fake.calls[-1][1] == "draw_line"

    run(go())


def test_cancellation_leaves_a_closed_audit_record(tmp_path):
    # v0.1: a cancelled call left a dangling 'before' with no 'after'.
    async def go():
        r, fake = make_router(tmp_path)
        fake.delay = 30
        task = asyncio.create_task(r.slacker("draw_line", {"start_mm": [0, 0], "end_mm": [1, 0]}))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        rows = audit_rows(r)
        assert [x["phase"] for x in rows] == ["before", "after"]
        assert rows[1]["outcome"] == "cancelled_outcome_unknown" and rows[0]["call_id"] == rows[1]["call_id"]

    run(go())


def test_backend_exceptions_are_audited_with_a_code(tmp_path):
    async def go():
        r, _ = make_router(tmp_path, script={"slacker.draw_line": BackendUnavailable("slacker.draw_line: boom", code="TIMEOUT")})
        with pytest.raises(BackendUnavailable):
            await r.slacker("draw_line", {"start_mm": [0, 0], "end_mm": [1, 0]})
        after = audit_rows(r)[-1]
        assert after["outcome"] == "error" and after["error_code"] == "TIMEOUT" and after["elapsed_ms"] >= 0

    run(go())


def test_audit_rows_carry_call_ids_and_summarised_args(tmp_path):
    async def go():
        r, _ = make_router(tmp_path, safety={"audit_arg_chars": 100})
        await r.slacker("draw_polyline", {"vertices_mm": [[i, i] for i in range(200)]})
        before = audit_rows(r)[0]
        assert before["call_id"] and before["risk"] == "write"
        assert before["args"]["_truncated"] is True and before["args"]["chars"] > 100

    run(go())


# ---------------------------------------------------------------------- locking
def test_product_help_does_not_wait_for_the_cad_lock(tmp_path):
    # v0.1: a slow *network-only* docs lookup blocked every CAD call (and vice versa).
    async def go():
        r, _ = make_router(tmp_path)
        release = asyncio.Event()

        async def hold():
            async with r.session():
                await release.wait()

        holder = asyncio.create_task(hold())
        await asyncio.sleep(0.05)
        await asyncio.wait_for(r.docs("search_help_content", {"query": "x"}), 1.0)        # not blocked
        blocked = asyncio.create_task(r.slacker("list_layers", {}))
        await asyncio.sleep(0.2)
        assert not blocked.done()                                                          # CAD calls do wait
        release.set()
        await asyncio.wait_for(blocked, 2.0)
        await holder

    run(go())


def test_session_is_reentrant_for_composite_operations(tmp_path):
    async def go():
        r, _ = make_router(tmp_path)
        async with r.session():
            out = await asyncio.wait_for(r.slacker("list_layers", {}), 1.0)               # would deadlock if not re-entrant
            assert out["ok"]

    run(go())


def test_lock_timeout_is_reported_not_hung(tmp_path):
    async def go():
        r, _ = make_router(tmp_path, runtime={"lock_timeout_seconds": 0.3})
        release = asyncio.Event()

        async def hold():
            async with r.session():
                await release.wait()

        holder = asyncio.create_task(hold())
        await asyncio.sleep(0.05)
        with pytest.raises(LockTimeout):
            await r.slacker("list_layers", {})
        release.set()
        await holder

    run(go())


def test_cross_process_lock_serialises_two_routers(tmp_path):
    async def go():
        cfg = make_config(tmp_path, runtime={"cross_process_lock": True, "lock_timeout_seconds": 0.4})
        a = Router(cfg, backends=FakeBackends())
        b = Router(cfg, backends=FakeBackends())                                            # e.g. another gateway process
        release = asyncio.Event()

        async def hold():
            async with a.session():
                await release.wait()

        holder = asyncio.create_task(hold())
        await asyncio.sleep(0.1)
        with pytest.raises(LockTimeout):
            await b.slacker("list_layers", {})
        release.set()
        await holder
        assert (await b.slacker("list_layers", {}))["ok"]                                   # free again

    run(go())


# ---------------------------------------------------------------- drawing pinning
def test_pinned_drawing_blocks_writes_after_the_user_switches_drawings(tmp_path):
    async def go():
        cad = FakeAutoCAD()
        r, fake = make_router(tmp_path, cad=cad)
        info = await r.slacker("get_active_drawing_info", {})
        r.pin(drawing_identity(info["structured"]))
        assert (await r.slacker("draw_line", {"start_mm": [0, 0], "end_mm": [1, 0]}))["ok"]
        cad.drawing.update(name="other.dwg", path="C:/tmp/other.dwg")                     # the user activates another drawing
        n = len(fake.calls)
        with pytest.raises(DrawingChanged) as ei:
            await r.slacker("draw_line", {"start_mm": [0, 0], "end_mm": [5, 0]})
        assert "other.dwg" in ei.value.message
        assert [c[1] for c in fake.calls[n:]] == ["get_active_drawing_info"]                # the write never ran
        assert (await r.slacker("list_layers", {}))["ok"]                                   # reads are still fine

    run(go())


def test_pin_follows_the_drawing_the_gateway_itself_opens(tmp_path):
    async def go():
        cad = FakeAutoCAD()

        def open_drawing(args):                                     # opening makes the new drawing active, like AutoCAD
            cad.drawing.update(name="opened.dwg", path=args["path"])
            return {"ok": True, "message": "Opened drawing.", "data": {"drawing": dict(cad.drawing)}}

        r, _ = make_router(tmp_path, cad=cad, script={"slacker.open_drawing": open_drawing})
        r.pin(drawing_identity((await r.slacker("get_active_drawing_info", {}))["structured"]))
        await r.slacker("open_drawing", {"path": "C:/tmp/opened.dwg"})                     # explicit, gateway-made switch
        assert r.pinned["name"] == "opened.dwg"                                            # the pin followed it
        assert (await r.slacker("draw_line", {"start_mm": [0, 0], "end_mm": [1, 0]}))["ok"]

    run(go())


def test_open_drawing_is_allowed_even_after_the_user_switched_drawings_by_hand(tmp_path):
    # If the pin blocked open_drawing, the only way out of DRAWING_CHANGED would be a restart.
    async def go():
        cad = FakeAutoCAD()

        def open_drawing(args):
            cad.drawing.update(name="target.dwg", path=args["path"])
            return {"ok": True, "message": "Opened drawing.", "data": {"drawing": dict(cad.drawing)}}

        r, _ = make_router(tmp_path, cad=cad, script={"slacker.open_drawing": open_drawing})
        r.pin(drawing_identity((await r.slacker("get_active_drawing_info", {}))["structured"]))
        cad.drawing.update(name="by-hand.dwg", path="C:/tmp/by-hand.dwg")                  # user clicked another tab
        with pytest.raises(DrawingChanged):
            await r.slacker("draw_line", {"start_mm": [0, 0], "end_mm": [1, 0]})
        await r.slacker("open_drawing", {"path": "C:/tmp/target.dwg"})                     # the way forward
        assert r.pinned["name"] == "target.dwg"
        assert (await r.slacker("draw_line", {"start_mm": [0, 0], "end_mm": [1, 0]}))["ok"]

    run(go())


# ------------------------------------------------------------------------ hooks
def test_hooks_fire_for_successful_writes_only(tmp_path):
    async def go():
        r, _ = make_router(tmp_path)
        seen = []
        r.add_hook(seen.append)
        await r.slacker("list_layers", {})
        assert seen == []
        await r.slacker("draw_line", {"start_mm": [0, 0], "end_mm": [1, 0]})
        assert len(seen) == 1 and seen[0]["tool"] == "draw_line"
        assert seen[0]["result"]["structured"]["data"]["entity"]["handle"]
        r.add_hook(lambda e: 1 / 0)                                                          # a broken observer is harmless
        await r.slacker("draw_line", {"start_mm": [0, 0], "end_mm": [2, 0]})

    run(go())


# ------------------------------------------------------ hidden side effect of best's draw tools
def _layer_rig(tmp_path, *, start="A-FURN", draw_error=None):
    state = {"clayer": start}
    script = {
        "best.get_variable": lambda a: {"result": f"CLAYER = {state['clayer']}"},
        "slacker.set_current_layer": lambda a: state.__setitem__("clayer", a["name"]) or {"ok": True, "message": "set"},
    }

    def draw(a):
        if draw_error:
            raise draw_error
        if a.get("layer"):
            state["clayer"] = a["layer"]                   # what best's draw_* really does: it makes the layer current
        return {"success": True, "handle": "2A9"}

    script["best.draw_rectangle"] = draw
    r, fake = make_router(tmp_path, script=script)
    return r, fake, state


def test_best_drawing_does_not_leave_the_humans_current_layer_changed(tmp_path):
    # best's draw_text/draw_* call set_current_layer(layer): the layer the user works on silently changes.
    async def go():
        r, fake, state = _layer_rig(tmp_path)
        await r.draw("draw_rectangle", {"layer": "A-WALL"})
        assert state["clayer"] == "A-FURN"                                                   # restored
        assert [(c[0], c[1]) for c in fake.calls] == [
            ("best", "get_variable"), ("best", "draw_rectangle"), ("slacker", "set_current_layer")]

    run(go())


def test_current_layer_is_restored_even_when_the_draw_fails(tmp_path):
    async def go():
        r, fake, state = _layer_rig(tmp_path, draw_error=BackendUnavailable("best.draw_rectangle: boom"))
        with pytest.raises(BackendUnavailable):
            await r.draw("draw_rectangle", {"layer": "A-WALL"})
        assert fake.calls[-1][1] == "set_current_layer" or state["clayer"] == "A-FURN"

    run(go())


def test_no_layer_juggling_when_it_is_not_needed(tmp_path):
    async def go():
        r, fake, state = _layer_rig(tmp_path, start="A-WALL")
        await r.draw("draw_rectangle", {"layer": "A-WALL"})                                  # drawing on the current layer anyway
        await r.draw("draw_rectangle", {})                                                   # no layer requested at all
        assert [c[1] for c in fake.calls].count("set_current_layer") == 0
        assert [c[1] for c in fake.calls].count("get_variable") == 1

    run(go())


def test_unreadable_current_layer_never_blocks_drawing(tmp_path):
    async def go():
        # best-cad-mcp prefixes a failed read with its own token; escaped here so the repo stays ASCII-only.
        r, fake = make_router(tmp_path, script={"best.get_variable": {"result": "CLAYER = \u83b7\u53d6\u53d8\u91cf\u5931\u8d25: no doc"}})
        out = await r.draw("draw_rectangle", {"layer": "A-WALL"})
        assert out["ok"] and "set_current_layer" not in [c[1] for c in fake.calls]

    run(go())


# --------------------------------------------------------------------------- raw
def test_raw_channel_is_off_by_default_and_guarded_when_on(tmp_path):
    async def go():
        r, fake = make_router(tmp_path)
        with pytest.raises(RawDisabled):
            await r.raw_command("_LINE 0,0 1,1", confirm=True)
        with pytest.raises(RawDisabled):
            await r.eval_lisp('(getvar "DWGNAME")', confirm=True)

        r2, fake2 = make_router(tmp_path, safety={"enable_raw_felix": True})
        with pytest.raises(ConfirmRequired):
            await r2.raw_command("_LINE 0,0 1,1", confirm=False)
        with pytest.raises(RawBlocked):
            await r2.raw_command("_.NETLOAD", confirm=True)                                  # bypassed the v0.1 regex
        with pytest.raises(RawBlocked):
            await r2.eval_lisp('(command "_.NETLOAD" "x.dll")', confirm=True)
        assert fake2.calls == []
        await r2.raw_command("_LINE 0,0 1,1", confirm=True)
        await r2.eval_lisp('(getvar "DWGNAME")', confirm=True)
        assert [(c[0], c[1]) for c in fake2.calls] == [("felix", "send_command"), ("felix", "eval_lisp")]

    run(go())


def test_extra_raw_commands_are_opt_in_via_config(tmp_path):
    async def go():
        r, fake = make_router(tmp_path, safety={"enable_raw_felix": True, "raw_extra_commands": ["SOLIDEDIT"]})
        await r.raw_command("_.SOLIDEDIT", confirm=True)
        assert fake.calls[-1][1] == "send_command"

    run(go())


# ------------------------------------------------------------ render / status
def test_render_falls_back_to_a_screenshot_when_best_is_down(tmp_path):
    async def go():
        r, fake = make_router(tmp_path, script={"best.render_drawing_view": BackendUnavailable("best: down")})
        out = await r.render({})
        assert out["backend"] == "slacker" and out["fallback_from"] == "best.render_drawing_view"

    run(go())


def test_render_reports_the_first_error_when_both_paths_fail(tmp_path):
    async def go():
        r, _ = make_router(tmp_path, script={"best.render_drawing_view": BackendUnavailable("best: down"),
                                             "slacker.capture_screenshot": BackendUnavailable("slacker: down")})
        with pytest.raises(BackendUnavailable) as ei:
            await r.render({})
        assert "best: down" in ei.value.message

    run(go())


def test_status_reports_backends_sessions_and_policy(tmp_path):
    async def go():
        r, _ = make_router(tmp_path)
        st = await r.status()
        assert set(st["backends"]) == {"best", "slacker", "official", "felix", "product_help"}
        assert st["backends"]["best"]["tool_count"] == 0 and st["policy"]["raw_enabled"] is False

    run(go())
