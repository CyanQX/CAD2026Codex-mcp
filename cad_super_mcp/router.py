from __future__ import annotations

import asyncio
import contextvars
import hashlib
import re
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, nullcontext
from typing import Any

from . import policy as P
from .audit import AuditLog, new_call_id
from .backend import BackendManager, CallResult
from .config import AppConfig
from .errors import (
    BackendToolError,
    BackendUnavailable,
    ConfirmRequired,
    DrawingChanged,
    LockTimeout,
    NotAllowed,
    RawDisabled,
    classify_text,
    to_gateway_error,
)
from .interlock import CrossProcessLock
from .rawguard import validate_raw_command, validate_raw_lisp

# Names other code (and v0.1 scripts) import from here.
from .policy import (  # noqa: F401
    BEST_READ_TOOLS,
    BEST_WRITE_TOOLS,
    DESTRUCTIVE,
    OFFICIAL_TOOLS,
    PRODUCT_HELP_TOOLS,
    SAVE_TOOLS,
    SLACKER_PREFERRED_ANNOTATION,
    SLACKER_PREFERRED_DRAW,
    SLACKER_PREFERRED_EDIT,
    SLACKER_TOOLS,
)

# Which backends talk to AutoCAD (and therefore must be serialised).  Product Help is pure network.
COM_BACKENDS = frozenset({"best", "slacker", "felix", "official"})
_WRITE_LIKE = frozenset({"write", "destructive", "export", "raw"})
# Tools whose whole purpose is to change which drawing is active: exempt from the pin check,
# and the pin follows the new drawing once they succeed.
_CONTEXT_SWITCH_TOOLS = frozenset({"open_drawing", "create_new_drawing"})

_HELD: contextvars.ContextVar[frozenset[int]] = contextvars.ContextVar("cad_super_lock_held", default=frozenset())


# ------------------------------------------------------------------- result handling
def soft_failure(payload: Any) -> str | None:
    """A backend can report a failure inside a *successful* MCP response (``{"ok": false}``)."""

    if isinstance(payload, dict):
        for key in ("ok", "success"):
            if key in payload and payload[key] is False:
                message = payload.get("message") or payload.get("error") or payload.get("msg") or "backend reported failure"
                return str(message)
    return None


def normalize(result: CallResult) -> dict[str, Any]:
    """Turn a CallResult into the gateway's dict, with a truthful ``ok``."""

    out = result.as_dict()
    out["transport_ok"] = out["ok"]
    out["error"] = None
    message = soft_failure(out["structured"])
    if message is not None:
        out["ok"] = False
        code, hint = classify_text(message) or ("BACKEND_REPORTED_FAILURE", None)
        err: dict[str, Any] = {"code": code, "message": message}
        if hint:
            err["hint"] = hint
        out["error"] = err
    return out


def text_of(out: dict[str, Any]) -> str:
    """The plain text a backend returned (best tools return strings)."""

    structured = out.get("structured")
    if isinstance(structured, dict) and isinstance(structured.get("result"), str):
        return structured["result"]
    if isinstance(structured, str):
        return structured
    for block in out.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            return block["text"]
    return ""


def drawing_identity(payload: Any) -> dict[str, Any] | None:
    """{"id","name","path",...} from a Slacker drawing-info style payload (or None)."""

    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    doc = next((data[k] for k in ("drawing", "active_document") if isinstance(data.get(k), dict)), None)
    if not doc:
        return None
    name, path = str(doc.get("name") or ""), str(doc.get("path") or "")
    if not (name or path):
        return None
    ident = hashlib.sha1((path or name).lower().encode("utf-8")).hexdigest()[:10]
    return {"id": ident, "name": name, "path": path, "insunits": doc.get("insunits"), "saved": doc.get("saved")}


class Router:
    def __init__(
        self,
        config: AppConfig,
        *,
        backends: BackendManager | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self.config = config
        self.backends = backends or BackendManager(config)
        s = config.safety
        self.audit = audit or AuditLog(
            config.audit_log_path,
            enabled=s.audit_log,
            max_bytes=s.audit_max_bytes,
            backups=s.audit_backups,
            max_arg_chars=s.audit_arg_chars,
        )
        self._lock = asyncio.Lock()
        self._xlock = CrossProcessLock(config.lock_path) if config.runtime.cross_process_lock else None
        self._hooks: list[Callable[[dict[str, Any]], None]] = []
        self.pinned: dict[str, Any] | None = None

    # ------------------------------------------------------------------ locking
    @asynccontextmanager
    async def session(self):
        """Hold the AutoCAD interlock for a whole composite operation (re-entrant).

        Lock order is always process-local lock, then the OS-level lock shared by every gateway on
        the machine, so at most one CAD call chain is inside AutoCAD at a time.
        """

        held = _HELD.get()
        if id(self) in held:
            yield
            return
        timeout = self.config.runtime.lock_timeout_seconds
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout)
        except TimeoutError as exc:
            raise LockTimeout(f"Timed out waiting for the in-process AutoCAD call queue after {timeout:g}s") from exc
        try:
            if self._xlock is not None:
                await self._xlock.acquire(timeout)
            try:
                token = _HELD.set(held | {id(self)})
                try:
                    yield
                finally:
                    _HELD.reset(token)
            finally:
                if self._xlock is not None:
                    self._xlock.release()
        finally:
            self._lock.release()

    def add_hook(self, hook: Callable[[dict[str, Any]], None]) -> None:
        """Register a callback ``hook(event)`` invoked after every successful write-like call."""

        self._hooks.append(hook)

    def _notify(self, event: dict[str, Any]) -> None:
        for hook in self._hooks:
            try:
                hook(event)
            except Exception:  # noqa: BLE001 - observers must never break a CAD call
                pass

    # ------------------------------------------------------------- drawing pinning
    def pin(self, identity: dict[str, Any] | None) -> None:
        self.pinned = identity

    async def current_identity(self) -> dict[str, Any] | None:
        """Identity of the drawing that is active in AutoCAD right now (None if it cannot be read)."""

        try:
            res = await self.backends.call("slacker", "get_active_drawing_info", {}, read_only=True)
        except Exception:  # noqa: BLE001 - cannot verify; let the real call surface the problem
            return None
        return drawing_identity(res.structured)

    async def _check_pin(self) -> None:
        pinned = self.pinned
        if not pinned:
            return
        current = await self.current_identity()
        if current is not None and current["id"] != pinned["id"]:
            raise DrawingChanged(
                f"Active drawing changed from {pinned['name'] or pinned['path']} to {current['name'] or current['path']}",
                details={"pinned": pinned, "active": current},
            )

    # -------------------------------------------------------------------- calls
    async def call(
        self,
        backend: str,
        tool: str,
        args: dict[str, Any] | None = None,
        *,
        risk: str = "read",
        read_only: bool | None = None,
    ) -> dict[str, Any]:
        args = dict(args or {})
        read_only = (risk == "read") if read_only is None else read_only
        call_id = new_call_id()
        guard = self.session() if backend in COM_BACKENDS else nullcontext()
        async with guard:
            started = time.perf_counter()
            self.audit.write(
                {"phase": "before", "call_id": call_id, "backend": backend, "tool": tool, "risk": risk,
                 "args": self.audit.summarize(args)}
            )

            def elapsed() -> float:
                return round((time.perf_counter() - started) * 1000, 1)

            try:
                if (
                    self.pinned
                    and risk in _WRITE_LIKE
                    and backend in ("best", "slacker", "felix")
                    and tool not in _CONTEXT_SWITCH_TOOLS
                ):
                    await self._check_pin()
                result = await self.backends.call(backend, tool, args, read_only=read_only)
            except BaseException as exc:
                cancelled = isinstance(exc, asyncio.CancelledError)
                err = None if cancelled else to_gateway_error(exc)
                self.audit.write(
                    {"phase": "after", "call_id": call_id, "backend": backend, "tool": tool, "risk": risk, "ok": False,
                     "outcome": "cancelled_outcome_unknown" if cancelled else "error",
                     "error_code": err.code if err else None, "error": err.message if err else "cancelled",
                     "elapsed_ms": elapsed()}
                )
                raise
            out = normalize(result)
            out["call_id"] = call_id
            out["elapsed_ms"] = elapsed()
            self.audit.write(
                {"phase": "after", "call_id": call_id, "backend": backend, "tool": tool, "risk": risk, "ok": out["ok"],
                 "outcome": "ok" if out["ok"] else "backend_reported_failure",
                 "error": (out["error"] or {}).get("message"), "elapsed_ms": out["elapsed_ms"]}
            )
            if out["ok"] and risk in _WRITE_LIKE:
                if self.pinned and tool in ("open_drawing", "create_new_drawing", "save_drawing_as"):
                    self.pinned = await self.current_identity() or self.pinned  # the gateway itself switched
                self._notify({"backend": backend, "tool": tool, "args": args, "result": out, "risk": risk, "call_id": call_id})
            return out

    async def status(self, include_tools: bool = False, include_remote: bool = True) -> dict[str, Any]:
        """Backend health.  ``include_remote=False`` skips the internet-facing Product Help check (~seconds)."""

        async def one(name: str) -> tuple[str, dict[str, Any]]:
            if name == "product_help" and not include_remote and self.config.product_help.enabled:
                return name, {"backend": name, "ok": None, "skipped": True,
                              "note": "remote service (network required); not checked by default - call cad_health(check_remote=true) to check it"}
            try:
                info = await self.backends.list_tools(name)
                if not include_tools:
                    info["tool_count"] = len(info.pop("tools", []))
                return name, info
            except Exception as exc:  # noqa: BLE001
                err = to_gateway_error(exc)
                entry: dict[str, Any] = {"backend": name, "ok": False, "error": err.message, "code": err.code}
                if err.hint:
                    entry["hint"] = err.hint
                return name, entry

        names = ["best", "slacker", "official", "felix", "product_help"]
        results = await asyncio.gather(*(one(n) for n in names))
        s, r = self.config.safety, self.config.runtime
        return {
            "workspace_root": self.config.workspace_root,
            "backends": dict(results),
            "sessions": self.backends.session_info(),
            "policy": {
                "raw_enabled": s.enable_raw_felix,
                "cross_process_lock": r.cross_process_lock,
                "verify_writes": r.verify_writes,
                "pinned_drawing": self.pinned,
            },
        }

    # --------------------------------------------------------------- policy checks
    def _check_lane(self, backend: str, tool: str, lane: str | None) -> None:
        if lane is None:
            return
        allowed = P.LANES[lane] if backend == "best" else P.SLACKER_LANES.get(lane, frozenset())
        if tool not in allowed:
            raise NotAllowed(
                f"{backend} tool {tool!r} cannot be called through the {lane} entrance",
                details={"tool": tool, "lane": lane, "allowed": sorted(allowed)},
                hint="Use the dedicated tool (plans only via cad_plan, saving only via cad_save), or pick one from details.allowed.",
            )

    def require_confirm(self, tool: str, confirm: bool) -> None:
        """Raise ConfirmRequired if ``tool`` needs confirm=true and it was not given (checked before any I/O)."""

        s = self.config.safety
        if confirm:
            return
        if tool in P.DESTRUCTIVE and s.require_confirm_for_destructive:
            raise ConfirmRequired(f"{tool} requires confirm=true")
        if tool == "execute_cad_plan" and s.require_confirm_for_plan_execute:
            raise ConfirmRequired("execute_cad_plan requires confirm=true")
        if tool in P.SAVE_TOOLS and s.require_confirm_for_save:
            raise ConfirmRequired(f"{tool} requires confirm=true")

    # ------------------------------------------------------------ backend entrances
    async def official(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool not in P.OFFICIAL_TOOLS:
            raise NotAllowed(f"Official tool not allowed by gateway: {tool}", details={"allowed": sorted(P.OFFICIAL_TOOLS)})
        return await self.call("official", tool, args, risk="read")

    async def docs(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool not in P.PRODUCT_HELP_TOOLS:
            raise NotAllowed(f"Product Help tool not allowed by gateway: {tool}", details={"allowed": sorted(P.PRODUCT_HELP_TOOLS)})
        return await self.call("product_help", tool, args, risk="read")

    async def best(self, tool: str, args: dict[str, Any], confirm: bool = False, *, lane: str | None = None) -> dict[str, Any]:
        args = dict(args or {})
        self._check_lane("best", tool, lane)
        if tool in P.BEST_READ_TOOLS:
            return await self.call("best", tool, args, risk="read")
        if tool not in P.BEST_WRITE_TOOLS:
            raise NotAllowed(
                f"best-cad tool is not in the curated gateway allowlist: {tool}",
                details={"tool": tool},
                hint="Use cad_recommend_tools / cad_tool_help to see the available tools.",
            )
        self.require_confirm(tool, confirm)
        if tool == "execute_cad_plan":
            # Enforced HERE, at the single choke point, so no entrance can skip the safeguards.
            args = P.enforce_plan_safety(args)
        if tool in P.BEST_LAYER_SIDE_EFFECT and args.get("layer"):
            return await self._call_preserving_current_layer(tool, args)
        return await self.call("best", tool, args, risk=P.classify_risk("best", tool))

    async def _call_preserving_current_layer(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run a best drawing tool without leaving the human's current layer changed behind them."""

        async with self.session():
            previous = await self._read_current_layer()
            try:
                return await self.call("best", tool, args, risk=P.classify_risk("best", tool))
            finally:
                await self._restore_current_layer(previous, str(args.get("layer")))

    async def _read_current_layer(self) -> str | None:
        try:
            out = await self.call("best", "get_variable", {"variable_name": "CLAYER"}, risk="read")
        except Exception:  # noqa: BLE001 - best effort: never block the drawing on this
            return None
        m = re.match(r"^\s*CLAYER\s*=\s*(.+?)\s*$", text_of(out), re.I | re.S)
        # best-cad-mcp prefixes a failed read with this token; escaped so the codebase stays ASCII-only.
        failure_prefix = "\u83b7\u53d6\u53d8\u91cf\u5931\u8d25"
        return m.group(1) if m and not m.group(1).startswith(failure_prefix) else None

    async def _restore_current_layer(self, previous: str | None, drawn_on: str) -> None:
        if not previous or previous.lower() == drawn_on.lower():
            return
        try:
            await self.slacker("set_current_layer", {"name": previous})
        except Exception:  # noqa: BLE001 - the draw already happened; report via audit, do not mask its result
            pass

    async def slacker(self, tool: str, args: dict[str, Any], confirm: bool = False, *, lane: str | None = None) -> dict[str, Any]:
        args = dict(args or {})
        if tool not in P.SLACKER_TOOLS:
            raise NotAllowed(f"Slacker tool not allowed by gateway: {tool}", details={"allowed": sorted(P.SLACKER_TOOLS)})
        self._check_lane("slacker", tool, lane)
        self.require_confirm(tool, confirm)
        return await self.call("slacker", tool, args, risk=P.classify_risk("slacker", tool))

    async def draw(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        # Do not auto-fallback writes across backends: Slacker and best use different schemas and
        # unit contracts, so a transparent retry could duplicate geometry or scale it wrongly.
        if tool in P.SLACKER_PREFERRED_DRAW:
            return await self.slacker(tool, args, lane="draw")
        if tool in P.LANES["draw"]:
            return await self.best(tool, args, lane="draw")
        raise NotAllowed(
            f"Unsupported curated drawing tool: {tool}",
            details={"allowed": sorted(P.SLACKER_PREFERRED_DRAW | P.LANES["draw"])},
        )

    async def edit(self, tool: str, args: dict[str, Any], confirm: bool = False) -> dict[str, Any]:
        if tool in P.SLACKER_PREFERRED_EDIT:
            return await self.slacker(tool, args, confirm=confirm, lane="edit")
        return await self.best(tool, args, confirm=confirm, lane="edit")

    async def annotate(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool in P.SLACKER_PREFERRED_ANNOTATION:
            return await self.slacker(tool, args, lane="annotate")
        return await self.best(tool, args, lane="annotate")

    async def render(self, args: dict[str, Any]) -> dict[str, Any]:
        """Render a review view with best; fall back to a Slacker screenshot (read-only, safe to fall back)."""

        first_failure: dict[str, Any] | BaseException
        try:
            out = await self.best("render_drawing_view", args)
            if out["ok"]:
                return out
            first_failure = out
        except (BackendUnavailable, BackendToolError) as exc:
            first_failure = exc
        try:
            fallback = await self.slacker("capture_screenshot", {})
            fallback["fallback_from"] = "best.render_drawing_view"
            return fallback
        except Exception:  # noqa: BLE001
            if isinstance(first_failure, BaseException):
                raise first_failure from None  # report the render failure, not the fallback's
            return first_failure

    async def raw_command(self, command: str, confirm: bool) -> dict[str, Any]:
        if not self.config.safety.enable_raw_felix:
            raise RawDisabled("Raw felix backend is disabled in config (safety.enable_raw_felix=false)")
        if not confirm:
            raise ConfirmRequired("Raw AutoCAD command requires confirm=true")
        validate_raw_command(command, self.config.safety.raw_extra_commands)
        return await self.call("felix", "send_command", {"command": command}, risk="raw")

    async def eval_lisp(self, expression: str, confirm: bool) -> dict[str, Any]:
        if not self.config.safety.enable_raw_felix:
            raise RawDisabled("Raw felix backend is disabled in config (safety.enable_raw_felix=false)")
        if not confirm:
            raise ConfirmRequired("AutoLISP evaluation requires confirm=true")
        validate_raw_lisp(expression)
        return await self.call("felix", "eval_lisp", {"expression": expression}, risk="raw")

    async def aclose(self) -> None:
        await self.backends.close()
