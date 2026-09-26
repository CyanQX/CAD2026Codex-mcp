"""One response envelope for every tool.

Every tool answers with the same shape so a model never has to guess::

    {"ok": bool, "summary": "one sentence a human could read out loud",
     "data": {...}, "warnings": [...], "error": null | {"code","message","hint"}, "meta": {...}}

Improvements over v0.1, which returned each backend's payload *twice* (``structured`` and
``content``) and pushed screenshots through JSON as base64 *text*:

* the payload appears once; images are emitted as native MCP ``ImageContent`` (the model can see them);
* oversized payloads are trimmed structurally (longest lists first) with an explicit notice,
  instead of blowing up the model's context window;
* failures carry a stable error ``code`` and a ``hint`` for the next step.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any

from mcp.types import CallToolResult, ImageContent, TextContent

from .errors import to_gateway_error


@dataclass
class Reply:
    data: Any = None
    summary: str = ""
    ok: bool = True
    warnings: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None
    images: list[dict[str, str]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def _longest_list(node: Any, path: str = "", depth: int = 0) -> tuple[Any, Any, str, list] | None:
    """(container, key, path, list) of the largest list reachable within a few levels."""

    best: tuple[int, Any, Any, str, list] | None = None
    items = node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else ()
    for key, value in items:
        sub = f"{path}/{key}"
        if isinstance(value, list) and len(value) > 1:
            size = len(_dump(value))
            if best is None or size > best[0]:
                best = (size, node, key, sub, value)
        if depth < 5 and isinstance(value, (dict, list)):
            found = _longest_list(value, sub, depth + 1)
            if found is not None:
                size = len(_dump(found[3]))
                if best is None or size > best[0]:
                    best = (size, found[0], found[1], found[2], found[3])
    return None if best is None else best[1:]


def shrink(data: Any, budget: int) -> tuple[Any, dict[str, Any] | None]:
    """Fit ``data`` into ``budget`` characters of JSON by halving its longest lists."""

    text = _dump(data)
    if len(text) <= budget:
        return data, None
    total = len(text)
    root = {"root": copy.deepcopy(data)}
    trimmed: dict[str, dict[str, int]] = {}
    for _ in range(80):
        if len(_dump(root)) <= budget:
            break
        found = _longest_list(root)
        if found is None:
            break
        container, key, path, lst = found
        keep = max(1, len(lst) // 2)
        if keep >= len(lst):
            break
        trimmed.setdefault(path, {"total": len(lst)})["kept"] = keep
        container[key] = lst[:keep]
    out = root["root"]
    if len(_dump(out)) > budget:
        return {"_truncated": True, "total_chars": total, "preview": text[: max(0, budget - 200)]}, {"total_chars": total, "trimmed": {}}
    return out, {"total_chars": total, "trimmed": trimmed}


def build_result(reply: Reply, *, max_chars: int = 60000) -> CallToolResult:
    data, trim = shrink(reply.data, max(2000, max_chars - 3000))
    warnings = list(reply.warnings)
    if trim:
        where = ", ".join(f"{p} (kept {v['kept']}/{v['total']})" for p, v in trim["trimmed"].items()) or "the whole payload"
        warnings.append(f"Output too large ({trim['total_chars']} chars); trimmed: {where}. Narrow the scope (layer/type/area) or re-read in pages.")
    envelope = {
        "ok": reply.ok,
        "summary": reply.summary,
        "data": data,
        "warnings": warnings,
        "error": reply.error,
        "meta": reply.meta,
    }
    content: list[Any] = [TextContent(type="text", text=_dump(envelope))]
    for image in reply.images:
        content.append(ImageContent(type="image", data=image["data"], mime_type=image.get("mime_type", "image/png")))
    return CallToolResult(content=content, structured_content=envelope, is_error=not reply.ok)


def error_result(exc: BaseException, *, tool: str, max_chars: int = 60000) -> CallToolResult:
    err = to_gateway_error(exc)
    reply = Reply(ok=False, summary=f"Failed [{err.code}]: {err.message}", error=err.to_dict(), meta={"tool": tool})
    return build_result(reply, max_chars=max_chars)


def from_call(out: dict[str, Any], summary: str | None = None, *, data: Any = None) -> Reply:
    """A Reply for one Router call.  ``data`` overrides the backend payload when given."""

    payload = data if data is not None else (out.get("structured") if out.get("structured") is not None else (out.get("content") or None))
    ok = bool(out.get("ok"))
    error = out.get("error")
    label = f"{out.get('backend')}.{out.get('tool')}"
    if summary is None:
        summary = f"{label} completed" if ok else f"{label} failed: {(error or {}).get('message', 'unknown reason')}"
    meta = {k: out[k] for k in ("backend", "tool", "call_id", "elapsed_ms") if out.get(k) is not None}
    if out.get("fallback_from"):
        meta["fallback_from"] = out["fallback_from"]
    return Reply(data=payload, summary=summary, ok=ok, error=error, images=list(out.get("images") or []), meta=meta)
