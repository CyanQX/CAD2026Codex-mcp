"""BackendManager: persistent sessions, recycling, retries, image handling, official discovery."""

import asyncio
import time

import pytest
from helpers import make_config, run, stdio_spec

from cad_super_mcp.backend import BackendManager, BackendToolError, BackendUnavailable


def _mgr(tmp_path, **spec):
    return BackendManager(make_config(tmp_path, backends={"best": stdio_spec(**spec)}))


async def _pids(mgr, n, tool="whoami", **kw):
    out = []
    for _ in range(n):
        r = await mgr.call("best", tool, {}, **kw)
        out.append((r.structured["pid"], r.structured["n"]))
    return out


# ------------------------------------------------------------------ session reuse
def test_persistent_session_reuses_one_process(tmp_path):
    async def go():
        mgr = _mgr(tmp_path, session_mode="persistent")
        try:
            calls = await _pids(mgr, 3)
            assert len({pid for pid, _ in calls}) == 1                  # one process...
            assert [n for _, n in calls] == [1, 2, 3]                   # ...that served all three calls
            info = mgr.session_info()["best"]
            assert info["alive"] and info["calls"] == 3
        finally:
            await mgr.close()

    run(go())


def test_per_call_mode_still_spawns_a_process_each_time(tmp_path):
    async def go():
        mgr = _mgr(tmp_path, session_mode="per_call")
        calls = await _pids(mgr, 2)
        assert calls[0][0] != calls[1][0] and [n for _, n in calls] == [1, 1]
        assert mgr.session_info() == {}

    run(go())


def test_persistent_is_much_faster_than_per_call(tmp_path):
    async def timed(mode):
        mgr = _mgr(tmp_path, session_mode=mode)
        try:
            await mgr.call("best", "whoami", {})              # warm-up (starts the process in persistent mode)
            t0 = time.perf_counter()
            for _ in range(4):
                await mgr.call("best", "whoami", {})
            return time.perf_counter() - t0
        finally:
            await mgr.close()

    persistent = run(timed("persistent"))
    per_call = run(timed("per_call"))
    print(f"4 calls: persistent {persistent:.3f}s vs per_call {per_call:.3f}s")
    assert persistent < per_call / 2


def test_auto_mode_is_persistent_for_stdio(tmp_path):
    mgr = _mgr(tmp_path, session_mode="auto")
    assert mgr.is_persistent("best")
    assert not BackendManager(make_config(tmp_path, backends={"best": stdio_spec(session_mode="per_call")})).is_persistent("best")


# ---------------------------------------------------------------------- recycling
def test_recycle_after_n_calls(tmp_path):
    async def go():
        mgr = _mgr(tmp_path, session_mode="persistent", recycle_after_calls=2)
        try:
            calls = await _pids(mgr, 4)
            pids = [pid for pid, _ in calls]
            assert pids[0] == pids[1] and pids[2] == pids[3] and pids[1] != pids[2]
        finally:
            await mgr.close()

    run(go())


def test_idle_session_is_recycled(tmp_path):
    async def go():
        mgr = _mgr(tmp_path, session_mode="persistent", idle_timeout_seconds=0.6)
        try:
            first = (await _pids(mgr, 1))[0][0]
            await asyncio.sleep(1.5)
            second = (await _pids(mgr, 1))[0][0]
            assert first != second
        finally:
            await mgr.close()

    run(go())


# ------------------------------------------------------------------------- errors
def test_tool_error_keeps_the_session_alive(tmp_path):
    async def go():
        mgr = _mgr(tmp_path, session_mode="persistent")
        try:
            before = (await _pids(mgr, 1))[0][0]
            with pytest.raises(BackendToolError):
                await mgr.call("best", "boom", {})
            after = (await _pids(mgr, 1))[0][0]
            assert before == after
        finally:
            await mgr.close()

    run(go())


def test_timeout_kills_the_wedged_backend_and_the_next_call_recovers(tmp_path):
    async def go():
        mgr = _mgr(tmp_path, session_mode="persistent", timeout_seconds=4)
        try:
            before = (await _pids(mgr, 1))[0][0]
            t0 = time.perf_counter()
            with pytest.raises(BackendUnavailable) as ei:
                await mgr.call("best", "sleep_for", {"seconds": 30})
            assert time.perf_counter() - t0 < 10
            assert ei.value.code == "TIMEOUT"
            after = (await _pids(mgr, 1))[0][0]
            assert after != before                                  # fresh process, no queue stuck behind the hang
        finally:
            await mgr.close()

    run(go())


def test_crash_during_a_write_is_reported_as_uncertain_and_not_retried(tmp_path):
    async def go():
        mgr = _mgr(tmp_path, session_mode="persistent")
        try:
            await _pids(mgr, 1)
            marker = str(tmp_path / "marker")
            with pytest.raises(BackendUnavailable) as ei:
                await mgr.call("best", "die_once", {"marker": marker}, read_only=False)
            assert ei.value.code == "SESSION_LOST" and "result unknown" in ei.value.message
            assert (tmp_path / "marker").exists()                   # the call reached the backend exactly once
        finally:
            await mgr.close()

    run(go())


def test_crash_during_a_read_is_retried_once_on_a_fresh_session(tmp_path):
    async def go():
        mgr = _mgr(tmp_path, session_mode="persistent")
        try:
            first = (await _pids(mgr, 1))[0][0]
            r = await mgr.call("best", "die_once", {"marker": str(tmp_path / "m2")}, read_only=True)
            assert r.structured["ok"] and r.structured["pid"] != first
        finally:
            await mgr.close()

    run(go())


def test_startup_failure_reports_the_real_cause_not_an_exception_group(tmp_path):
    async def go():
        cfg = make_config(tmp_path, backends={"best": {"enabled": True, "kind": "stdio", "command": "definitely-not-a-real-binary-xyz",
                                                       "args": [], "timeout_seconds": 5}})
        mgr = BackendManager(cfg)
        with pytest.raises(BackendUnavailable) as ei:
            await mgr.call("best", "whoami", {})
        msg = ei.value.message
        assert "TaskGroup" not in msg and "unhandled errors" not in msg
        assert "best.whoami" in msg
        assert "FileNotFoundError" in msg or "not found" in msg.lower() or "cannot find" in msg.lower()

    run(go())


def test_disabled_backend_gives_an_actionable_hint(tmp_path):
    async def go():
        mgr = BackendManager(make_config(tmp_path))
        with pytest.raises(BackendUnavailable) as ei:
            await mgr.call("felix", "send_command", {})
        assert "disabled" in ei.value.message and "backends.felix.enabled" in (ei.value.hint or "")

    run(go())


# ---------------------------------------------------------------- result shaping
def test_json_string_results_are_unwrapped_and_not_duplicated(tmp_path):
    async def go():
        mgr = _mgr(tmp_path)
        try:
            r = await mgr.call("best", "json_text", {})
            assert r.structured == {"success": True, "handle": "2A3", "layer": "A-WALL"}
            assert r.content == []                                  # the text block only repeated `structured`
        finally:
            await mgr.close()

    run(go())


def test_dicts_wrapped_by_the_sdk_are_unwrapped_so_failures_are_not_hidden(tmp_path):
    # Real defect: best's check_runtime_environment came back as {"result": {"ok": false, ...}} and looked healthy.
    from cad_super_mcp.backend import _unwrap_structured
    from cad_super_mcp.router import normalize

    assert _unwrap_structured({"result": {"ok": False}}) == {"ok": False}
    assert _unwrap_structured({"result": [1, 2]}) == [1, 2]
    assert _unwrap_structured({"result": '{"a": 1}'}) == {"a": 1}
    assert _unwrap_structured({"result": "plain text"}) == {"result": "plain text"}      # text stays wrapped
    assert _unwrap_structured({"result": {"a": 1}, "other": 2}) == {"result": {"a": 1}, "other": 2}

    async def go():
        mgr = _mgr(tmp_path)
        try:
            r = await mgr.call("best", "wrapped_dict_fail", {})
            assert r.structured["ok"] is False and r.structured["data"] == {"ready": False}
            out = normalize(r)
            assert out["transport_ok"] is True and out["ok"] is False and "preflight failed" in out["error"]["message"]
        finally:
            await mgr.close()

    run(go())


def test_images_are_kept_as_images_never_as_base64_text(tmp_path):
    async def go():
        mgr = _mgr(tmp_path)
        try:
            r = await mgr.call("best", "with_image", {})
            assert len(r.images) == 1 and r.images[0]["mime_type"] == "image/png" and r.images[0]["data"].startswith("iVBOR")
            assert any(b.get("omitted") for b in r.content if isinstance(b, dict))
            assert "iVBOR" not in str(r.as_dict()["content"])       # no base64 blob inside the JSON-able content
        finally:
            await mgr.close()

    run(go())


def test_legacy_protocol_mode_still_talks_to_the_backend(tmp_path):
    async def go():
        mgr = _mgr(tmp_path, session_mode="persistent", protocol="legacy")
        try:
            r = await mgr.call("best", "whoami", {})
            assert r.structured["ok"]
            listing = await mgr.list_tools("best")
            assert {"whoami", "boom", "with_image"} <= {t["name"] for t in listing["tools"]}
        finally:
            await mgr.close()

    run(go())


# ------------------------------------------------------------ official discovery
def _official_cfg(tmp_path, lo, hi, **extra):
    spec = {"enabled": True, "kind": "autodiscover_http", "port_range": [lo, hi], "negative_cache_seconds": 30, **extra}
    return make_config(tmp_path, backends={"official": spec})


async def _listen(port=0):
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", port)
    return server, server.sockets[0].getsockname()[1]


def test_official_discovery_scans_ports_in_parallel_and_fast(tmp_path):
    async def go():
        server, port = await _listen()
        try:
            probed = []
            ours = f"http://localhost:{port}/mcp"

            async def probe(url):
                probed.append(url)
                return url == ours                                   # some unrelated listener may sit nearby

            mgr = BackendManager(_official_cfg(tmp_path, port - 25, port + 25), probe=probe)
            t0 = time.perf_counter()
            url = await mgr.discover_official_url()
            elapsed = time.perf_counter() - t0
            assert url == ours
            assert ours in probed and len(probed) <= 3               # only *listening* ports were probed (not all 51)
            assert elapsed < 3.0                                     # v0.1 took 59.5 s for 50 closed ports
            assert await mgr.discover_official_url() == url          # cached
        finally:
            server.close()

    run(go())


def test_official_miss_is_cached_briefly(tmp_path):
    async def go():
        server, port = await _listen()
        server.close()
        await server.wait_closed()                                    # nothing listening any more
        scans = []
        mgr = BackendManager(_official_cfg(tmp_path, port, port))
        original = mgr._listening_ports

        async def counting(ports, timeout=0.25):
            scans.append(1)
            return await original(ports, timeout)

        mgr._listening_ports = counting
        assert await mgr.discover_official_url() is None
        assert await mgr.discover_official_url() is None
        assert len(scans) == 1                                        # the second miss came from the cache
        await mgr.discover_official_url(force=True)
        assert len(scans) == 2

    run(go())


def test_official_non_mcp_listener_is_ignored(tmp_path):
    async def go():
        server, port = await _listen()                                # e.g. Windows CDPSvc on 5040
        try:
            async def probe(url):
                return False

            mgr = BackendManager(_official_cfg(tmp_path, port, port), probe=probe)
            assert await mgr.discover_official_url() is None
        finally:
            server.close()

    run(go())


class _FakeClient:
    """Async-context client whose behaviour is decided by the URL it is pointed at."""

    def __init__(self, url, plan):
        self.url, self.plan = url, plan
        self.protocol_version = "test"
        self.server_info = type("S", (), {"name": "official"})()

    async def __aenter__(self):
        if self.plan.get(self.url) == "down":
            raise ConnectionError("connection refused")
        return self

    async def __aexit__(self, *exc):
        return False

    async def call_tool(self, tool, args):
        return type("R", (), {"content": [], "structured_content": {"ok": True, "url": self.url}, "is_error": False})()


def test_stale_cached_official_url_is_rediscovered_once(tmp_path):
    async def go():
        server, port = await _listen()
        try:
            plan = {}
            mgr = BackendManager(
                _official_cfg(tmp_path, port, port),
                client_factory=lambda target, timeout, protocol: _FakeClient(target, plan),
                probe=lambda url: asyncio.sleep(0, result=True),
            )
            old_url = f"http://localhost:{port}/mcp"
            mgr._official_url = old_url                               # cached from before AutoCAD restarted
            plan[old_url] = "down"                                    # ...and that endpoint is now dead
            # Rediscovery finds the listener again (probe says yes); make the dead URL come back healthy.
            calls = {"n": 0}
            orig_factory = mgr._factory

            def factory(target, timeout, protocol):
                calls["n"] += 1
                if calls["n"] == 2:
                    plan[old_url] = "up"
                return orig_factory(target, timeout=timeout, protocol=protocol)

            mgr._factory = factory
            r = await mgr.call("official", "discoverAutoCADTypes", {}, read_only=True)
            assert r.structured["ok"] and calls["n"] == 2             # first attempt failed, one rediscovery + retry
        finally:
            server.close()

    run(go())
