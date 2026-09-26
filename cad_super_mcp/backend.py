from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from mcp import Client, MCPError, StdioServerParameters
from mcp_types import CONNECTION_CLOSED, REQUEST_TIMEOUT

from .config import AppConfig, BackendConfig
from .errors import (
    BackendToolError,
    BackendUnavailable,
    GatewayError,
    classify_text,
    describe_exception,
    leaf_exceptions,
)

logger = logging.getLogger("cad_super_mcp.backend")

__all__ = ["BackendManager", "BackendToolError", "BackendUnavailable", "CallResult", "SessionLost"]

OFFICIAL_MARKER_TOOLS = frozenset({"discoverAutoCADTypes", "queryAutoCADObjects"})


@dataclass(slots=True)
class CallResult:
    backend: str
    tool: str
    ok: bool
    structured: Any = None
    content: list[Any] | None = None
    protocol_version: str | None = None
    server_name: str | None = None
    # Image blocks the backend returned (kept separate so they never end up as base64 *text*).
    images: list[dict[str, str]] = field(default_factory=list)
    elapsed_ms: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "tool": self.tool,
            "ok": self.ok,
            "structured": self.structured,
            "content": self.content,
            "images": list(self.images),
            "protocol_version": self.protocol_version,
            "server_name": self.server_name,
        }


def _dump_block(block: Any) -> Any:
    if hasattr(block, "model_dump"):
        return block.model_dump(mode="json")
    if hasattr(block, "dict"):
        return block.dict()
    return str(block)


def _unwrap_structured(structured: Any) -> Any:
    """FastMCP-style servers wrap non-object returns as ``{"result": <value>}``; unwrap what is really the payload.

    * ``{"result": "<json text>"}`` (a tool that returns a JSON *string*)  -> the parsed JSON
    * ``{"result": {...}}`` / ``{"result": [...]}`` (a tool that returns a dict/list typed loosely)
      -> the dict/list itself.  Without this, ``{"result": {"ok": false}}`` hid a failure from the
      router (measured: best's check_runtime_environment looked healthy with AutoCAD not running).
    * ``{"result": "plain text"}`` is left as is (the router reads such text via ``text_of``).
    """

    if isinstance(structured, dict) and set(structured) == {"result"}:
        inner = structured["result"]
        if isinstance(inner, (dict, list)):
            return inner
        if isinstance(inner, str):
            text = inner.strip()
            if text[:1] in "{[":
                try:
                    parsed = json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    return structured
                if isinstance(parsed, (dict, list)):
                    return parsed
    return structured


def shape_result(name: str, tool: str, result: Any, protocol_version: str | None, server_name: str | None) -> CallResult:
    """Normalise an MCP ``CallToolResult`` into a :class:`CallResult`."""

    structured = getattr(result, "structured_content", None)
    content: list[Any] = []
    images: list[dict[str, str]] = []
    text_blocks: list[Any] = []
    for block in result.content or []:
        dumped = _dump_block(block)
        if isinstance(dumped, dict) and dumped.get("type") == "image":
            data = dumped.get("data")
            mime = dumped.get("mimeType") or dumped.get("mime_type") or "image/png"
            if isinstance(data, str):
                images.append({"mime_type": str(mime), "data": data})
            content.append({"type": "image", "mime_type": str(mime), "omitted": True, "base64_chars": len(data or "")})
        elif isinstance(dumped, dict) and dumped.get("type") == "text":
            text_blocks.append(dumped)
        else:
            content.append(dumped)
    # SDK 1.x servers often return JSON only inside TextContent; lift it into `structured`.
    if structured is None:
        for block in text_blocks:
            text = block.get("text")
            if isinstance(text, str):
                try:
                    parsed = json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(parsed, (dict, list)):
                    structured = parsed
                    text_blocks = [b for b in text_blocks if b is not block]
                    break
    structured = _unwrap_structured(structured)
    if structured is not None:
        # The text blocks only repeat what `structured` already says; keep non-duplicated text.
        text_blocks = [b for b in text_blocks if not _duplicates(b.get("text"), structured)]
    content = text_blocks + content
    ok = not bool(getattr(result, "is_error", False))
    out = CallResult(
        backend=name,
        tool=tool,
        ok=ok,
        structured=structured,
        content=content,
        protocol_version=protocol_version,
        server_name=server_name,
        images=images,
    )
    if not ok:
        raise BackendToolError(
            f"{name}.{tool} returned an MCP tool error: {_text_of(content) or content}",
            details={"backend": name, "tool": tool},
        )
    return out


def _text_of(content: Iterable[Any]) -> str:
    return " ".join(str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text").strip()


def _duplicates(text: Any, structured: Any) -> bool:
    """True when a text block merely repeats what ``structured`` already carries."""

    if not isinstance(text, str):
        return False
    if isinstance(structured, dict) and set(structured) == {"result"} and structured["result"] == text:
        return True
    try:
        return json.loads(text) == structured
    except (json.JSONDecodeError, TypeError):
        return False


class SessionLost(Exception):
    """The persistent backend session ended.  ``started`` tells whether the call had been sent."""

    def __init__(self, message: str, *, started: bool) -> None:
        super().__init__(message)
        self.started = started


class _Job:
    __slots__ = ("kind", "tool", "args", "future", "started")

    def __init__(self, kind: str, tool: str | None, args: dict[str, Any] | None, future: asyncio.Future) -> None:
        self.kind = kind
        self.tool = tool
        self.args = args
        self.future = future
        self.started = False


def default_client_factory(target: Any, *, timeout: float, protocol: str) -> Any:
    # The SDK's own read timeout is only a backstop: the gateway's timeout must always fire first so
    # a timeout is reported (and the wedged backend recycled) deterministically.
    kwargs: dict[str, Any] = {"read_timeout_seconds": timeout + 5.0}
    if protocol == "legacy":
        kwargs["mode"] = "legacy"  # skip the MCP 2.x `server/discover` probe (SDK 1.x backends)
    return Client(target, **kwargs)


class _Session:
    """One long-lived backend process, owned by a single background task.

    The task enters ``Client(...)`` and leaves it in the *same* task (required by anyio), and
    serves jobs one at a time - which matches AutoCAD's single-threaded COM model.
    """

    def __init__(self, name: str, target: Any, spec: BackendConfig, factory: Callable[..., Any]) -> None:
        self.name = name
        self._target = target
        self._spec = spec
        self._factory = factory
        self._queue: asyncio.Queue[_Job | None] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._ready: asyncio.Future | None = None
        self._start_lock = asyncio.Lock()
        self.closed = False
        self.calls = 0
        self.created = time.monotonic()
        self.protocol_version: str | None = None
        self.server_name: str | None = None

    @property
    def alive(self) -> bool:
        return not self.closed and self._task is not None and not self._task.done()

    async def ensure_started(self) -> None:
        async with self._start_lock:
            if self._task is not None:
                assert self._ready is not None
                await asyncio.shield(self._ready)
                return
            loop = asyncio.get_running_loop()
            self._ready = loop.create_future()
            self._task = loop.create_task(self._run(), name=f"cad-super-session-{self.name}")
            try:
                await asyncio.wait_for(asyncio.shield(self._ready), self._spec.timeout_seconds)
            except BaseException:
                await self.aclose()
                raise

    async def _run(self) -> None:
        assert self._ready is not None
        try:
            async with self._factory(self._target, timeout=self._spec.timeout_seconds, protocol=self._spec.protocol) as client:
                self.protocol_version = str(client.protocol_version)
                self.server_name = getattr(getattr(client, "server_info", None), "name", None)
                if not self._ready.done():
                    self._ready.set_result(None)
                idle = self._spec.idle_timeout_seconds if self._spec.idle_timeout_seconds > 0 else None
                while True:
                    try:
                        job = await asyncio.wait_for(self._queue.get(), idle)
                    except TimeoutError:
                        return  # idle recycle: the next call starts a fresh process
                    if job is None:
                        return
                    if job.future.done():
                        continue  # the caller gave up (timeout / cancel) before we started
                    job.started = True
                    try:
                        if job.kind == "call":
                            raw = await client.call_tool(job.tool, job.args or {})
                        else:
                            raw = await client.list_tools()
                    except asyncio.CancelledError:
                        if not job.future.done():
                            job.future.set_exception(SessionLost(f"{self.name} session cancelled", started=True))
                        raise
                    except BaseException as exc:  # noqa: BLE001 - any failure ends (and rebuilds) the session
                        if not job.future.done():
                            job.future.set_exception(exc)
                        return
                    if not job.future.done():
                        job.future.set_result(raw)
                    self.calls += 1
                    limit = self._spec.recycle_after_calls
                    if limit and self.calls >= limit:
                        return
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            if not self._ready.done():
                self._ready.set_exception(exc if isinstance(exc, Exception) else SessionLost(str(exc), started=False))
            else:
                logger.debug("backend session %s ended with %s", self.name, describe_exception(exc))
        finally:
            self.closed = True
            if not self._ready.done():
                self._ready.set_exception(SessionLost(f"{self.name} session ended during start", started=False))
            if not self._ready.cancelled():
                self._ready.exception()  # mark as retrieved: nobody may be awaiting it any more
            lost = SessionLost(f"{self.name} session ended", started=False)
            while True:
                try:
                    pending = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if pending is not None and not pending.future.done():
                    pending.future.set_exception(lost)

    async def request(self, kind: str, tool: str | None, args: dict[str, Any] | None, timeout: float) -> Any:
        if not self.alive:
            raise SessionLost(f"{self.name} session is not running", started=False)
        loop = asyncio.get_running_loop()
        job = _Job(kind, tool, args, loop.create_future())
        await self._queue.put(job)
        if self.closed and not job.future.done():  # ended between the check and the put
            job.future.set_exception(SessionLost(f"{self.name} session ended", started=False))
        try:
            return await asyncio.wait_for(job.future, timeout)
        except TimeoutError:
            # The COM call may be wedged: kill the process rather than queue more work behind it.
            await self.aclose()
            raise

    async def aclose(self) -> None:
        self.closed = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=15)


class BackendManager:
    """Talks to the configured backends over MCP.

    ``stdio`` backends use a persistent session by default (see :class:`BackendConfig`); http
    backends and ``session_mode="per_call"`` use a short-lived client per call.
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        client_factory: Callable[..., Any] | None = None,
        probe: Callable[[str], Any] | None = None,
    ) -> None:
        self.config = config
        self._factory = client_factory or default_client_factory
        self._probe = probe or self._probe_official_url
        self._sessions: dict[str, _Session] = {}
        self._session_lock = asyncio.Lock()
        self._official_url: str | None = None
        self._official_negative_until = 0.0
        self._official_lock = asyncio.Lock()

    # ------------------------------------------------------------------ config
    def spec(self, name: str) -> BackendConfig:
        try:
            return getattr(self.config, name)
        except AttributeError as exc:
            raise KeyError(f"Unknown backend: {name}") from exc

    def is_persistent(self, name: str) -> bool:
        spec = self.spec(name)
        return spec.kind == "stdio" and spec.session_mode in ("auto", "persistent")

    def session_info(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            n: {"alive": s.alive, "calls": s.calls, "age_s": round(now - s.created, 1)}
            for n, s in self._sessions.items()
        }

    async def _target(self, name: str) -> Any:
        spec = self.spec(name)
        if not spec.enabled:
            raise BackendUnavailable(
                f"Backend '{name}' is disabled",
                hint=f"Set backends.{name}.enabled=true in config.json if you want to use it.",
            )
        if spec.kind == "stdio":
            if not spec.command:
                raise BackendUnavailable(f"Backend '{name}' has no command configured")
            env = copy.deepcopy(spec.env)
            env.setdefault("CAD_SUPER_PARENT", "1")
            return StdioServerParameters(command=spec.command, args=list(spec.args), env=env, cwd=spec.cwd or None)
        if spec.kind == "http":
            if not spec.url:
                raise BackendUnavailable(f"Backend '{name}' has no URL configured")
            return spec.url
        if spec.kind == "autodiscover_http":
            url = await self.discover_official_url()
            if not url:
                raise BackendUnavailable(
                    "Autodesk official MCP was not found on localhost:5001-5050.",
                    hint="Enable the Autodesk Assistant Tech Preview inside AutoCAD and run MCPHTTPSTART; "
                         "if you do not use the official MCP, set backends.official.enabled=false.",
                )
            return url
        raise BackendUnavailable(f"Unsupported backend kind for '{name}': {spec.kind}")

    # ---------------------------------------------------- official MCP discovery
    async def _listening_ports(self, ports: Iterable[int], timeout: float = 0.25) -> list[int]:
        async def one(port: int) -> int | None:
            for host in ("127.0.0.1", "::1"):
                try:
                    _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
                except Exception:  # noqa: BLE001 - refused / unreachable / timed out
                    continue
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:  # noqa: BLE001
                    pass
                return port
            return None

        found = await asyncio.gather(*(one(p) for p in ports))
        return sorted(p for p in found if p is not None)

    async def _probe_official_url(self, url: str) -> bool:
        try:
            async with asyncio.timeout(3.0):
                async with self._factory(url, timeout=2.5, protocol="auto") as client:
                    tools = await client.list_tools()
                    return bool({t.name for t in tools.tools} & OFFICIAL_MARKER_TOOLS)
        except Exception:  # noqa: BLE001
            return False

    async def discover_official_url(self, force: bool = False) -> str | None:
        """Find Autodesk's official AutoCAD MCP on localhost.

        v0.1 probed 50 ports one by one with an MCP handshake each (measured: 59.5 s when the
        server is not running).  Now: a 250 ms TCP pre-scan finds the few listening ports, only
        those are probed (in parallel), and a miss is cached briefly.
        """

        if self._official_url and not force:
            return self._official_url
        if not force and time.monotonic() < self._official_negative_until:
            return None
        async with self._official_lock:
            if self._official_url and not force:
                return self._official_url
            spec = self.config.official
            bounds = list(spec.port_range) if len(spec.port_range) >= 2 else [5001, 5050]
            ports = await self._listening_ports(range(int(bounds[0]), int(bounds[1]) + 1))
            urls = [f"http://localhost:{p}/mcp" for p in ports]
            verdicts = await asyncio.gather(*(self._probe(u) for u in urls), return_exceptions=True)
            for url, verdict in zip(urls, verdicts, strict=True):
                if verdict is True:
                    self._official_url = url
                    return url
            self._official_url = None
            self._official_negative_until = time.monotonic() + max(0.0, spec.negative_cache_seconds)
            return None

    # --------------------------------------------------------------- invocation
    def _wrap(self, name: str, tool: str | None, exc: BaseException) -> GatewayError:
        if isinstance(exc, GatewayError):
            return exc
        label = f"{name}.{tool}" if tool else name
        text = describe_exception(exc)
        hit = classify_text(text)
        code = "BACKEND_UNAVAILABLE"
        hint = None
        if hit:
            code, hint = hit
        elif any(isinstance(leaf, TimeoutError) for leaf in leaf_exceptions(exc)):
            code = "TIMEOUT"
            hint = "Backend timed out: AutoCAD may be waiting for input or showing a dialog. "\
                   "The result of a write is unknown; verify the current state before retrying."
        return BackendUnavailable(f"{label}: {text}", code=code, hint=hint)

    async def _get_session(self, name: str, target: Any, spec: BackendConfig) -> _Session:
        async with self._session_lock:
            session = self._sessions.get(name)
            if session is not None and not session.alive:
                self._sessions.pop(name, None)
                session = None
            if session is None:
                session = _Session(name, target, spec, self._factory)
                self._sessions[name] = session
        await session.ensure_started()
        return session

    async def _drop(self, name: str, session: _Session) -> None:
        async with self._session_lock:
            if self._sessions.get(name) is session:
                del self._sessions[name]
        await session.aclose()

    async def _invoke(
        self, name: str, kind: str, tool: str | None, args: dict[str, Any] | None, read_only: bool
    ) -> tuple[Any, str | None, str | None]:
        spec = self.spec(name)
        target = await self._target(name)
        timeout = spec.timeout_seconds
        label = tool or kind
        if self.is_persistent(name):
            attempt = 0
            while True:
                attempt += 1
                try:
                    session = await self._get_session(name, target, spec)
                except Exception as exc:  # noqa: BLE001 - startup failure
                    async with self._session_lock:
                        self._sessions.pop(name, None)
                    raise self._wrap(name, tool, exc) from exc
                try:
                    raw = await session.request(kind, tool, args, timeout)
                    return raw, session.protocol_version, session.server_name
                except SessionLost as lost:
                    await self._drop(name, session)
                    if attempt < 2 and (not lost.started or read_only):
                        continue  # nothing was sent, or it was a read: safe to retry once
                    raise BackendUnavailable(
                        f"{name}.{label}: backend session lost ({'call was sent, result unknown' if lost.started else 'call was not sent'})",
                        code="SESSION_LOST",
                        hint="The result of a write is unknown: check the current state with cad_context / cad_query_entities before running it again." if lost.started else None,
                    ) from lost
                except Exception as exc:  # noqa: BLE001
                    await self._drop(name, session)
                    timed_out = isinstance(exc, TimeoutError) or (isinstance(exc, MCPError) and exc.code == REQUEST_TIMEOUT)
                    if timed_out or (isinstance(exc, MCPError) and exc.code != CONNECTION_CLOSED):
                        # Timeout, or the backend *answered* with a protocol error (it did not execute the call).
                        raise self._wrap(name, tool, exc) from exc
                    # Otherwise the connection broke while the call was in flight (process died, pipe closed).
                    if attempt < 2 and read_only:
                        continue  # a read is safe to repeat on a fresh session
                    raise BackendUnavailable(
                        f"{name}.{label}: backend connection broke while the call was in flight ({describe_exception(exc)}); result unknown",
                        code="SESSION_LOST",
                        hint="The result of a write is unknown: check the current state with cad_context / cad_query_entities before running it again.",
                    ) from exc
        try:
            async with asyncio.timeout(timeout):
                async with self._factory(target, timeout=timeout, protocol=spec.protocol) as client:
                    if kind == "call":
                        raw = await client.call_tool(tool, args or {})
                    else:
                        raw = await client.list_tools()
                    server = getattr(getattr(client, "server_info", None), "name", None)
                    return raw, str(client.protocol_version), server
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(name, tool, exc) from exc

    async def list_tools(self, name: str) -> dict[str, Any]:
        try:
            raw, proto, server = await self._invoke(name, "list_tools", None, None, True)
        except GatewayError as exc:
            message = exc.message if exc.message.startswith((f"{name}.", f"{name}:")) else f"{name}: {exc.message}"
            raise BackendUnavailable(message, code=exc.code, hint=exc.hint) from exc
        return {
            "backend": name,
            "ok": True,
            "protocol_version": proto,
            "server_name": server,
            "tools": [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in raw.tools
            ],
        }

    async def call(
        self, name: str, tool: str, arguments: dict[str, Any] | None = None, *, read_only: bool = False
    ) -> CallResult:
        started = time.perf_counter()
        had_cached_official = name == "official" and self._official_url is not None
        try:
            raw, proto, server = await self._invoke(name, "call", tool, dict(arguments or {}), read_only)
        except BackendUnavailable:
            if not had_cached_official:
                raise
            # The cached official-MCP port may be stale (AutoCAD restarted): rediscover once.
            self._official_url = None
            self._official_negative_until = 0.0
            raw, proto, server = await self._invoke(name, "call", tool, dict(arguments or {}), read_only)
        result = shape_result(name, tool, raw, proto, server)
        result.elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        return result

    async def close(self) -> None:
        async with self._session_lock:
            sessions, self._sessions = list(self._sessions.values()), {}
        for session in sessions:
            await session.aclose()
