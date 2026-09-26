import json

import pytest
from helpers import FakeAutoCAD, FakeBackends, make_config, run

from cad_super_mcp.backend import CallResult
from cad_super_mcp.errors import ConfirmRequired, NeedsClarification
from cad_super_mcp.journal import Journal, find_handle
from cad_super_mcp.results import Reply, build_result, error_result, from_call, shrink
from cad_super_mcp.router import Router
from cad_super_mcp.staging import StageManager, all_handles, layer_names


# ------------------------------------------------------------------------ results
def test_envelope_shape_and_single_copy_of_the_payload():
    res = build_result(Reply(data={"handle": "2A3"}, summary="line drawn", meta={"tool": "cad_line"}))
    env = res.structured_content
    assert list(env) == ["ok", "summary", "data", "warnings", "error", "meta"]
    assert env["ok"] is True and env["data"] == {"handle": "2A3"} and res.is_error is False
    assert json.loads(res.content[0].text) == env                          # text mirrors structured, once
    assert len(res.content) == 1


def test_images_are_emitted_as_native_image_blocks_not_text():
    img = {"mime_type": "image/png", "data": "iVBORw0KGgo="}
    res = build_result(Reply(data={"ok": True}, images=[img]))
    assert [c.type for c in res.content] == ["text", "image"]
    assert res.content[1].data == "iVBORw0KGgo=" and res.content[1].mime_type == "image/png"
    assert "iVBOR" not in res.content[0].text                               # never base64 inside the JSON text


def test_failures_use_error_flag_code_and_hint():
    res = error_result(ConfirmRequired("erase_entity requires confirm=true"), tool="cad_erase")
    env = res.structured_content
    assert res.is_error and env["ok"] is False and env["error"]["code"] == "CONFIRM_REQUIRED" and env["error"]["hint"]
    assert env["summary"].startswith("Failed [CONFIRM_REQUIRED]")
    generic = error_result(RuntimeError("No running AutoCAD session was found. (-2147221021)"), tool="cad_health")
    assert generic.structured_content["error"]["code"] == "AUTOCAD_NOT_RUNNING"


def test_oversized_payloads_are_trimmed_structurally_with_a_notice():
    data = {"entities": [{"handle": f"H{i}", "layer": "A-WALL", "note": "x" * 40} for i in range(2000)], "count": 2000}
    res = build_result(Reply(data=data, summary="scan finished"), max_chars=8000)
    env = res.structured_content
    assert len(res.content[0].text) <= 8000
    assert env["data"]["count"] == 2000 and 1 <= len(env["data"]["entities"]) < 2000     # structure kept, list halved
    assert any("trimmed" in w and "/entities" in w for w in env["warnings"])
    small = build_result(Reply(data={"a": 1}), max_chars=8000)
    assert small.structured_content["warnings"] == []


def test_shrink_handles_lists_at_the_root_and_unshrinkable_blobs():
    out, info = shrink([{"i": i, "pad": "y" * 50} for i in range(500)], 3000)
    assert isinstance(out, list) and 1 <= len(out) < 500 and info["trimmed"]
    blob, info2 = shrink({"text": "z" * 50000}, 3000)
    assert blob["_truncated"] is True and len(json.dumps(blob)) < 3500 and info2["total_chars"] > 50000


def test_from_call_builds_reply_with_meta_and_failure_summary():
    ok = from_call({"ok": True, "backend": "slacker", "tool": "draw_line", "structured": {"a": 1}, "call_id": "c1", "elapsed_ms": 4.2})
    assert ok.ok and ok.data == {"a": 1} and ok.meta == {"backend": "slacker", "tool": "draw_line", "call_id": "c1", "elapsed_ms": 4.2}
    bad = from_call({"ok": False, "backend": "slacker", "tool": "draw_line", "structured": None, "content": [],
                     "error": {"code": "X", "message": "boom"}})
    assert not bad.ok and "boom" in bad.summary and bad.error["code"] == "X"
    shot = from_call({"ok": True, "backend": "slacker", "tool": "capture_screenshot", "images": [{"mime_type": "image/png", "data": "QQ=="}],
                      "fallback_from": "best.render_drawing_view"})
    assert shot.images and shot.meta["fallback_from"] == "best.render_drawing_view"


# ----------------------------------------------------------------------- journal
def rig(tmp_path, cad=None, script=None):
    cfg = make_config(tmp_path)
    fake = FakeBackends(cad=cad, script=script)
    router = Router(cfg, backends=fake)
    journal = Journal()
    router.add_hook(journal.on_event)
    return router, fake, journal


def test_find_handle_understands_every_backend_shape():
    assert find_handle({"ok": True, "data": {"entity": {"handle": "2A3"}}}) == "2A3"       # Slacker
    assert find_handle({"success": True, "handle": "2B"}) == "2B"                            # best JSON
    assert find_handle('drew text handle="2c", height=3') == "2C"                            # best text
    assert find_handle({"data": {"layers": [1, 2]}}) is None


def test_journal_records_and_plans_exact_undo(tmp_path):
    async def go():
        router, fake, j = rig(tmp_path)
        await router.slacker("create_layer", {"name": "A-WALL"})
        a = (await router.slacker("draw_line", {"start_mm": [0, 0], "end_mm": [100, 0]}))["structured"]["data"]["entity"]["handle"]
        b = (await router.slacker("draw_circle", {"center_mm": [5, 5], "radius_mm": 3}))["structured"]["data"]["entity"]["handle"]
        await router.slacker("move_entity", {"handle": a, "from_mm": [0, 0], "to_mm": [50, 20]})
        assert [e.op for e in j.entries()] == ["create", "create", "move"]
        actions, skipped = j.plan_undo(steps=2)
        assert [(x.tool, x.args) for x in actions] == [
            ("move_entity", {"handle": a, "from_mm": [50, 20], "to_mm": [0, 0]}),          # newest first: move it back...
            ("erase_entity", {"handle": b}),                                                # ...then remove the circle
        ]
        assert skipped == []

    run(go())


def test_irreversible_operations_are_reported_not_silently_skipped(tmp_path):
    async def go():
        router, fake, j = rig(tmp_path)
        await router.slacker("create_layer", {"name": "X"})
        h = (await router.slacker("draw_line", {"start_mm": [0, 0], "end_mm": [1, 0]}))["structured"]["data"]["entity"]["handle"]
        await router.slacker("set_entity_layer", {"handle": h, "layer": "X"})
        await router.slacker("erase_entity", {"handle": h}, confirm=True)
        actions, skipped = j.plan_undo(steps=3)
        assert actions == [] or all(a.tool == "erase_entity" for a in actions)
        assert any("erased" in s for s in skipped) and any("layer change" in s for s in skipped)

    run(go())


def test_groups_tag_and_scope_undo(tmp_path):
    async def go():
        router, fake, j = rig(tmp_path)
        await router.slacker("draw_line", {"start_mm": [0, 0], "end_mm": [1, 0]})               # ungrouped
        with j.group("batch1") as gid:
            for i in range(3):
                await router.slacker("draw_line", {"start_mm": [0, i], "end_mm": [1, i]})
        assert gid == "batch1" and len(j.created_handles("batch1")) == 3 and len(j.created_handles()) == 4
        actions, _ = j.plan_undo(group="batch1")
        assert len(actions) == 3 and all(a.tool == "erase_entity" for a in actions)
        for a in actions:
            j.mark_undone(a.seq)
        assert j.created_handles("batch1") == [] and len(j.created_handles()) == 1

    run(go())


def test_journal_ignores_reads_and_failed_writes(tmp_path):
    async def go():
        cad = FakeAutoCAD()
        router, fake, j = rig(tmp_path, cad=cad)
        await router.slacker("list_layers", {})
        await router.slacker("draw_line", {"start_mm": [0, 0], "end_mm": [1, 0], "layer": "NOPE"})   # backend says ok=false
        assert j.entries() == []

    run(go())


# ------------------------------------------------------------------------ staging
def stage_rig(tmp_path, script=None):
    cad = FakeAutoCAD()
    cad.layers["A-WALL"] = {"name": "A-WALL", "color_aci": 7}
    router, fake, journal = rig(tmp_path, cad=cad, script=script)
    sm = StageManager(router, journal, router.config.runtime)
    return router, fake, journal, sm, cad


async def stage_line(router, sm, stage, target="A-WALL"):
    out = await router.slacker("draw_line", {"start_mm": [0, 0], "end_mm": [100, 0], "layer": sm.layer})
    h = out["structured"]["data"]["entity"]["handle"]
    sm.add(stage, h, "draw_line", target, "line")
    return h


def test_stage_lifecycle_commit_moves_entities_to_their_real_layer(tmp_path):
    async def go():
        router, fake, j, sm, cad = stage_rig(tmp_path)
        stage = await sm.ensure_open("walls")
        assert sm.layer in cad.layers and (await sm.ensure_open()) is stage                    # reused, not duplicated
        h1, h2 = await stage_line(router, sm, stage), await stage_line(router, sm, stage)
        assert cad.entities[h1]["layer"] == sm.layer                                            # drawn on the review layer
        result = await sm.commit()
        assert result["committed"] == 2 and result["failed"] == [] and stage.status == "committed"
        assert cad.entities[h1]["layer"] == "A-WALL" and cad.entities[h2]["layer"] == "A-WALL"

    run(go())


def test_commit_asks_instead_of_guessing_when_a_layer_is_missing_or_unspecified(tmp_path):
    async def go():
        router, fake, j, sm, cad = stage_rig(tmp_path)
        stage = await sm.ensure_open()
        await stage_line(router, sm, stage, target="NO-SUCH-LAYER")
        with pytest.raises(NeedsClarification) as ei:
            await sm.commit()
        assert "NO-SUCH-LAYER" in ei.value.message and ei.value.details["missing"] == ["NO-SUCH-LAYER"]
        stage2 = await sm.begin()
        await stage_line(router, sm, stage2, target=None)
        with pytest.raises(NeedsClarification) as ei2:
            await sm.commit()
        assert ei2.value.details["handles"]
        assert (await sm.commit(default_layer="A-WALL"))["committed"] == 1                     # the human named a layer

    run(go())


def test_target_layer_is_checked_when_staging_starts(tmp_path):
    async def go():
        router, fake, j, sm, cad = stage_rig(tmp_path)
        await sm.check_target_layer("A-WALL")
        await sm.check_target_layer(None)
        with pytest.raises(NeedsClarification) as ei:
            await sm.check_target_layer("A-DOOR")
        assert any("cad_layer" in o for o in ei.value.details["options"])
        assert "A-WALL" in " ".join(ei.value.details["options"])                              # shown as the user wrote it, not lower-cased

    run(go())


def test_discard_erases_staged_entities_and_tolerates_ones_already_gone(tmp_path):
    async def go():
        router, fake, j, sm, cad = stage_rig(tmp_path)
        stage = await sm.ensure_open()
        await stage_line(router, sm, stage)
        h2 = await stage_line(router, sm, stage)
        del cad.entities[h2]                                                                    # the user erased one by hand
        result = await sm.discard()
        assert result["erased"] == 1 and result["already_gone"] == 1 and result["failed"] == []
        assert cad.entities == {} and stage.status == "discarded"
        assert j.created_handles() == []                                                        # nothing left for cad_undo to chase

    run(go())


def test_preview_returns_native_images_and_the_item_list(tmp_path):
    async def go():
        shot = CallResult(backend="best", tool="render_drawing_view", ok=True, structured={"success": True}, content=[],
                          images=[{"mime_type": "image/png", "data": "QUJD"}])
        router, fake, j, sm, cad = stage_rig(tmp_path, script={"best.render_drawing_view": shot})
        stage = await sm.ensure_open()
        await stage_line(router, sm, stage)
        info, images = await sm.preview(zoom=True)
        assert images == [{"mime_type": "image/png", "data": "QUJD"}] and info["pending"] == 1
        assert ("slacker", "zoom_extents") in [(c[0], c[1]) for c in fake.calls]

    run(go())


def test_cleanup_recovers_orphaned_staging_after_a_restart(tmp_path):
    async def go():
        router, fake, j, sm, cad = stage_rig(tmp_path)
        await sm.ensure_layer()
        for _ in range(3):                                                                      # entities left on the stage layer
            await router.slacker("draw_line", {"start_mm": [0, 0], "end_mm": [1, 0], "layer": sm.layer})
        await router.slacker("draw_line", {"start_mm": [0, 0], "end_mm": [1, 0], "layer": "A-WALL"})
        res = await sm.cleanup()
        assert res == {"layer": sm.layer, "found": 3, "erased": 3}
        assert [e["layer"] for e in cad.entities.values()] == ["A-WALL"]                        # real geometry untouched

    run(go())


def test_get_without_a_stage_is_a_clarification_not_a_crash(tmp_path):
    async def go():
        _, _, _, sm, _ = stage_rig(tmp_path)
        with pytest.raises(NeedsClarification):
            sm.get()

    run(go())


def test_payload_helpers():
    assert layer_names({"structured": {"data": {"layers": [{"name": "A-WALL"}, {"name": "0"}]}}}) == ["A-WALL", "0"]   # original case
    assert layer_names({"structured": {"data": {"x": 1}}}) is None
    assert all_handles({"data": {"entities": [{"handle": "1"}, {"handle": "2", "sub": {"handle": "3"}}]}}) == ["1", "2", "3"]
