"""Structured, model-actionable errors and exception forensics.

Every failure the gateway raises on purpose carries a stable ``code`` (so a model or
a script can branch on it), a human ``message`` and, where useful, a ``hint`` that
tells the caller what to do next.  Exceptions that come from the outside world
(anyio task groups, COM, subprocesses) are unwrapped and classified so the real
cause is never hidden behind ``ExceptionGroup: unhandled errors in a TaskGroup``.
"""

from __future__ import annotations

import re
from typing import Any


class GatewayError(Exception):
    """A failure with a stable code, a message and an optional next-step hint."""

    code = "GATEWAY_ERROR"
    default_hint: str | None = None

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.hint = hint if hint is not None else self.default_hint
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        if self.details:
            out["details"] = self.details
        return out

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


class NotAllowed(GatewayError, ValueError):
    code = "NOT_ALLOWED"


class ConfirmRequired(GatewayError, PermissionError):
    code = "CONFIRM_REQUIRED"
    default_hint = "Explain what you are about to do to the user, get their confirmation, then retry with confirm=true."


class RawDisabled(GatewayError, PermissionError):
    code = "RAW_DISABLED"
    default_hint = "The raw channel is disabled by default (safety.enable_raw_felix=false). Prefer the structured cad_* tools."


class RawBlocked(GatewayError, PermissionError):
    code = "RAW_BLOCKED"
    default_hint = "Use the structured cad_* tools instead; or have the user add that command to safety.raw_extra_commands in config.json."


class InvalidArgument(GatewayError, ValueError):
    code = "INVALID_ARGUMENT"


class NeedsClarification(GatewayError, ValueError):
    """Ambiguity the model must resolve with the user instead of guessing."""

    code = "NEEDS_CLARIFICATION"
    default_hint = "Do not guess: relay the options/question in details.options to the user and continue only after their answer."


class BackendUnavailable(GatewayError, RuntimeError):
    code = "BACKEND_UNAVAILABLE"


class BackendToolError(GatewayError, RuntimeError):
    code = "BACKEND_TOOL_ERROR"


class LockTimeout(GatewayError, TimeoutError):
    code = "LOCK_TIMEOUT"
    default_hint = "Another call (possibly from another host/session) is using AutoCAD; retry later, or close the extra MCP sessions."


class DrawingChanged(GatewayError, RuntimeError):
    code = "DRAWING_CHANGED"
    default_hint = "The active drawing differs from the pinned one: check the current drawing with cad_context, then decide whether to re-pin (cad_context pin=true)."


class GeometryUnavailable(GatewayError, RuntimeError):
    code = "GEOMETRY_UNAVAILABLE"
    default_hint = "Cannot read that entity's geometry (the best backend is unavailable or returned an unknown shape); use explicit millimetre coordinates instead, or inspect it with cad_explain_entity first."


# --------------------------------------------------------------------- forensics


def leaf_exceptions(exc: BaseException) -> list[BaseException]:
    """Flatten (possibly nested) exception groups into their leaf exceptions."""

    if isinstance(exc, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for inner in exc.exceptions:
            leaves.extend(leaf_exceptions(inner))
        return leaves
    return [exc]


def describe_exception(exc: BaseException, limit: int = 3) -> str:
    """One readable line with the *real* causes (groups unwrapped, duplicates removed)."""

    parts: list[str] = []
    for leaf in leaf_exceptions(exc):
        text = f"{type(leaf).__name__}: {leaf}".strip()
        if text.endswith(":"):
            text = text[:-1]
        if text not in parts:
            parts.append(text)
        cause = leaf.__cause__
        if cause is not None and not isinstance(cause, BaseExceptionGroup):
            ctext = f"{type(cause).__name__}: {cause}".strip()
            if ctext not in parts:
                parts.append(f"(caused by {ctext})")
    return "; ".join(parts[:limit]) or type(exc).__name__


_CLASSIFIERS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "AUTOCAD_NOT_RUNNING",
        re.compile(
            r"no running autocad|autocad (?:is )?not running|-2147221021|0?x?800401e3",
            re.I,
        ),
        "Start AutoCAD 2026 first and open a drawing (the gateway will not start AutoCAD for you); "
        "AutoCAD and Codex must run as the same Windows user at the same privilege level.",
    ),
    (
        "AUTOCAD_BUSY",
        re.compile(
            r"-2147418111|-2147417846|0?x?80010001|0?x?8001010a|rejected by callee|RPC_E_CALL_REJECTED|RETRYLATER",
            re.I,
        ),
        "AutoCAD is busy (executing a command or showing a modal dialog). Ask the user to close the dialog / press Esc, then retry; "
        "writes are never retried automatically - confirm the current state with cad_context first.",
    ),
    (
        "NO_ACTIVE_DRAWING",
        re.compile(r"no active drawing|has no active drawing", re.I),
        "AutoCAD is running but there is no active drawing: open or create one (cad_document).",
    ),
    (
        "TIMEOUT",
        re.compile(r"timed out|timeouterror|read timeout", re.I),
        "Backend timed out: AutoCAD may be waiting for input or showing a dialog. The result of a write is unknown; verify the current state before retrying.",
    ),
]


def classify_text(text: str) -> tuple[str, str] | None:
    """Map a raw backend/COM message to a stable (code, hint), if we recognise it."""

    for code, pattern, hint in _CLASSIFIERS:
        if pattern.search(text or ""):
            return code, hint
    return None


def to_gateway_error(exc: BaseException) -> GatewayError:
    """Turn any exception into a GatewayError (keeping GatewayErrors untouched)."""

    if isinstance(exc, GatewayError):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        leaves = leaf_exceptions(exc)
        for leaf in leaves:
            if isinstance(leaf, GatewayError):
                return leaf
    text = describe_exception(exc)
    hit = classify_text(text)
    if hit:
        code, hint = hit
        return GatewayError(text, code=code, hint=hint)
    if isinstance(exc, (TimeoutError,)) or any(isinstance(x, TimeoutError) for x in leaf_exceptions(exc)):
        return GatewayError(
            text,
            code="TIMEOUT",
            hint="Backend timed out: AutoCAD may be waiting for input or showing a dialog. The result of a write is unknown; check the current state first with cad_context / cad_query_entities and do not retry blindly.",
        )
    if isinstance(exc, (ValueError, TypeError)):
        return GatewayError(text, code="INVALID_ARGUMENT")
    return GatewayError(text, code="INTERNAL_ERROR")
