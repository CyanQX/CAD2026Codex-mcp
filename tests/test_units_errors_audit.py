import asyncio
import json
import subprocess
import sys
import textwrap

import pytest

from cad_super_mcp.audit import AuditLog
from cad_super_mcp.errors import (
    ConfirmRequired,
    GatewayError,
    InvalidArgument,
    NotAllowed,
    classify_text,
    describe_exception,
    leaf_exceptions,
    to_gateway_error,
)
from cad_super_mcp.interlock import CrossProcessLock
from cad_super_mcp.units import fmt, parse_length_mm, parse_number, round_mm, to_drawing_units, to_mm, units_per_mm


# ---------------------------------------------------------------------------- units
@pytest.mark.parametrize(
    ("raw", "mm"),
    [
        (3600, 3600.0),
        (3600.5, 3600.5),
        ("3600", 3600.0),
        ("3600mm", 3600.0),
        ("3.6m", 3600.0),
        ("3.6 M", 3600.0),
        ("360cm", 3600.0),
        ("12in", 304.8),
        ('12"', 304.8),
        ("2ft", 609.6),
        ("2'6\"", 762.0),
        ("2' 6\"", 762.0),
        ("-1.5m", -1500.0),
        ("1e3", 1000.0),
    ],
)
def test_parse_length_mm(raw, mm):
    assert parse_length_mm(raw) == pytest.approx(mm)


@pytest.mark.parametrize("bad", [True, None, "abc", "3 parsecs-ish", "1,200", float("nan"), float("inf"), "", []])
def test_parse_length_rejects_garbage(bad):
    with pytest.raises(InvalidArgument):
        parse_length_mm(bad)


def test_parse_number_and_formatting():
    assert parse_number("45.5") == 45.5
    with pytest.raises(InvalidArgument):
        parse_number(True)
    assert fmt(3600.0) == "3600"
    assert fmt(3600.50) == "3600.5"
    assert fmt(-0.00000001) == "0"
    assert round_mm(-0.0) == 0.0


def test_unit_tables_match_known_values():
    assert units_per_mm(4) == 1.0                      # millimetres
    assert units_per_mm(1) == pytest.approx(1 / 25.4)  # inches
    assert units_per_mm(6) == pytest.approx(0.001)     # metres
    assert units_per_mm(0) is None                     # unitless
    assert to_drawing_units(25.4, units_per_mm(1)) == pytest.approx(1.0)
    assert to_mm(1.0, units_per_mm(6)) == pytest.approx(1000.0)
    assert to_mm(5.0, None) == 5.0                     # unitless passes through 1:1


# --------------------------------------------------------------------------- errors
def test_gateway_error_shapes_and_compat_bases():
    err = ConfirmRequired("erase_entity requires confirm=true")
    assert isinstance(err, PermissionError) and isinstance(err, GatewayError)
    d = err.to_dict()
    assert d["code"] == "CONFIRM_REQUIRED" and "confirm=true" in d["message"] and d["hint"]
    assert str(err).startswith("[CONFIRM_REQUIRED]")
    assert isinstance(NotAllowed("x"), ValueError)


def test_exception_groups_are_unwrapped_to_the_real_cause():
    inner = ExceptionGroup("unhandled errors in a TaskGroup", [ConnectionResetError("pipe closed")])
    outer = ExceptionGroup("unhandled errors in a TaskGroup", [inner])
    assert [type(e).__name__ for e in leaf_exceptions(outer)] == ["ConnectionResetError"]
    text = describe_exception(outer)
    assert "ConnectionResetError: pipe closed" in text and "TaskGroup" not in text


def test_autocad_hresults_are_classified():
    msg = "No running AutoCAD session was found. AutoCAD.Application: (-2147221021, 'The operation is unavailable', None, None)"
    code, hint = classify_text(msg)
    assert code == "AUTOCAD_NOT_RUNNING" and "Start AutoCAD" in hint
    assert classify_text("(-2147418111, 'Call was rejected by callee.')")[0] == "AUTOCAD_BUSY"
    assert classify_text("AutoCAD has no active drawing")[0] == "NO_ACTIVE_DRAWING"
    assert classify_text("something else entirely") is None
    assert to_gateway_error(RuntimeError(msg)).code == "AUTOCAD_NOT_RUNNING"
    assert to_gateway_error(TimeoutError("x")).code == "TIMEOUT"


# ---------------------------------------------------------------------------- audit
def test_audit_writes_pairs_rotates_and_summarises(tmp_path):
    log = AuditLog(tmp_path / "a" / "audit.jsonl", max_bytes=600, backups=2, max_arg_chars=200)
    big = {"blob": "x" * 5000}
    summary = log.summarize(big)
    assert summary["_truncated"] and summary["chars"] > 5000 and len(summary["sha256"]) == 16
    assert log.summarize({"a": 1}) == {"a": 1}
    for i in range(30):
        assert log.write({"phase": "before", "call_id": f"c{i}", "tool": "draw_line", "args": {"i": i}})
    files = sorted(p.name for p in (tmp_path / "a").iterdir())
    assert "audit.jsonl" in files and "audit.jsonl.1" in files          # rotated
    row = json.loads((tmp_path / "a" / "audit.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert row["call_id"] and row["pid"] and row["ts"]


def test_audit_write_never_raises_even_when_the_path_is_unusable(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    log = AuditLog(blocker / "sub" / "audit.jsonl")          # parent is a *file*: mkdir must fail
    assert log.write({"phase": "after"}) is False           # reported, not raised


def test_audit_disabled_is_a_noop(tmp_path):
    log = AuditLog(tmp_path / "x.jsonl", enabled=False)
    assert log.write({"a": 1}) is True and not (tmp_path / "x.jsonl").exists()


# -------------------------------------------------------------------------- interlock
def _run(coro):
    return asyncio.run(coro)


def test_cross_process_lock_excludes_another_process(tmp_path):
    lock_path = tmp_path / "cad.lock"
    holder_code = textwrap.dedent(
        f"""
        import asyncio, os, sys, time
        sys.path.insert(0, {str(__import__('pathlib').Path(__file__).resolve().parents[1])!r})
        from cad_super_mcp.interlock import CrossProcessLock
        async def main():
            lock = CrossProcessLock({str(lock_path)!r})
            await lock.acquire(timeout=5)
            print("HELD", os.getpid(), flush=True)
            time.sleep(1.5)
            lock.release()
        asyncio.run(main())
        """
    )
    proc = subprocess.Popen([sys.executable, "-c", holder_code], stdout=subprocess.PIPE, text=True)
    try:
        tag, real_pid = proc.stdout.readline().split()
        assert tag == "HELD"

        async def contend():
            mine = CrossProcessLock(lock_path)
            with pytest.raises(GatewayError) as ei:
                await mine.acquire(timeout=0.3)
            assert ei.value.code == "LOCK_TIMEOUT"
            # diagnostics name the holder (the real interpreter pid; a venv launcher's Popen.pid differs)
            assert ei.value.details["holder"]["pid"] == int(real_pid)
            await mine.acquire(timeout=5)                              # succeeds once the holder releases
            assert mine.held
            mine.release()
            assert not mine.held

        _run(contend())
    finally:
        proc.wait(timeout=10)


def test_lock_is_released_when_the_holder_process_dies(tmp_path):
    lock_path = tmp_path / "cad.lock"
    code = textwrap.dedent(
        f"""
        import asyncio, os, sys
        sys.path.insert(0, {str(__import__('pathlib').Path(__file__).resolve().parents[1])!r})
        from cad_super_mcp.interlock import CrossProcessLock
        async def main():
            await CrossProcessLock({str(lock_path)!r}).acquire(timeout=5)
            print("HELD", flush=True)
            os._exit(0)                       # die without releasing
        asyncio.run(main())
        """
    )
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "HELD"
    p.wait(timeout=10)

    async def after_crash():
        lock = CrossProcessLock(lock_path)
        await lock.acquire(timeout=2)      # the OS freed it: a crashed gateway can't wedge the others
        lock.release()

    _run(after_crash())
