from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

DEFAULT_PRODUCT_HELP_URL = "https://developer.api.autodesk.com/knowledge/public/v1/mcp"

SESSION_MODES = ("auto", "persistent", "per_call")
PROTOCOLS = ("auto", "legacy")


class ConfigError(ValueError):
    """The config file is unreadable or contains keys the gateway does not know."""


@dataclass(slots=True)
class BackendConfig:
    enabled: bool = True
    kind: str = "stdio"  # stdio | http | autodiscover_http
    command: str | None = None
    args: list[str] = field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    timeout_seconds: float = 120.0
    # auto = persistent for stdio backends, per_call for http backends.  A persistent session keeps
    # one backend process alive (the way these servers are designed to run) instead of paying a
    # ~0.6 s process start + COM lookup on every single call.  It is recycled on any error/timeout,
    # after `idle_timeout_seconds` idle, or after `recycle_after_calls` calls.
    session_mode: str = "auto"
    # legacy = skip the MCP 2.x `server/discover` probe (right for SDK 1.x backends such as Slacker).
    protocol: str = "auto"
    idle_timeout_seconds: float = 900.0
    recycle_after_calls: int = 500
    # autodiscover_http only: where to look for Autodesk's official MCP, and how long a miss is cached.
    port_range: list[int] = field(default_factory=lambda: [5001, 5050])
    negative_cache_seconds: float = 10.0


@dataclass(slots=True)
class SafetyConfig:
    enable_raw_felix: bool = False
    require_confirm_for_destructive: bool = True
    require_confirm_for_save: bool = True
    require_confirm_for_plan_execute: bool = True
    audit_log: bool = True
    audit_max_bytes: int = 5_000_000
    audit_backups: int = 3
    audit_arg_chars: int = 2000
    # Extra first-command verbs the raw channel may use (never overrides the built-in deny list).
    raw_extra_commands: list[str] = field(default_factory=list)
    # If non-empty, cad_document(open) only accepts files below one of these folders.
    allowed_open_roots: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RuntimeConfig:
    # OS-level lock shared by every gateway process on the machine (Codex + Claude + ...).
    cross_process_lock: bool = True
    lock_timeout_seconds: float = 180.0
    # Cap for one tool response (characters); larger payloads are truncated with a hint.
    max_response_chars: int = 60000
    # Read the entity back after drawing it and compare with what was asked for.
    verify_writes: bool = True
    verify_tolerance_mm: float = 0.01
    # Staging (preview -> commit/discard) layer used by cad_stage.
    stage_layer: str = "_AI_STAGE"
    stage_color_aci: int = 6
    max_batch_operations: int = 200


@dataclass(slots=True)
class AppConfig:
    workspace_root: str
    audit_log_path: str
    best: BackendConfig
    slacker: BackendConfig
    official: BackendConfig
    felix: BackendConfig
    product_help: BackendConfig
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)

    @property
    def lock_path(self) -> str:
        return str(Path(self.workspace_root) / ".cad_super" / "cad.lock")


def _build(cls: type, data: dict[str, Any] | None, where: str, **defaults: Any) -> Any:
    merged = dict(defaults)
    if data:
        if not isinstance(data, dict):
            raise ConfigError(f"'{where}' must be a JSON object")
        merged.update(data)
    allowed = {f.name for f in fields(cls)}
    unknown = sorted(set(merged) - allowed)
    if unknown:
        raise ConfigError(
            f"Unknown key(s) {unknown} in '{where}'. Allowed keys: {sorted(allowed)}. "
            "Fix or remove them in config.json."
        )
    obj = cls(**merged)
    if isinstance(obj, BackendConfig):
        if obj.session_mode not in SESSION_MODES:
            raise ConfigError(f"'{where}.session_mode' must be one of {SESSION_MODES}, got {obj.session_mode!r}")
        if obj.protocol not in PROTOCOLS:
            raise ConfigError(f"'{where}.protocol' must be one of {PROTOCOLS}, got {obj.protocol!r}")
    return obj


def default_config_path() -> Path:
    raw = os.environ.get("CAD_SUPER_CONFIG")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".cad-super-mcp" / "config.json"


def parse_config(data: dict[str, Any]) -> AppConfig:
    """Build an AppConfig from an already-parsed JSON object (used by tests and load_config)."""

    if not isinstance(data, dict):
        raise ConfigError("config.json must contain a JSON object")
    for key in data:
        if key not in {"workspace_root", "audit_log_path", "backends", "safety", "runtime", "version"} and not key.startswith(("_", "$")):
            raise ConfigError(f"Unknown top-level key {key!r} in config.json")
    if "workspace_root" not in data:
        raise ConfigError("config.json must define 'workspace_root'")
    workspace = str(Path(data["workspace_root"]).expanduser())
    audit = data.get("audit_log_path") or str(Path(workspace) / ".cad_super" / "audit.jsonl")
    backends = data.get("backends", {}) or {}
    for key in backends:
        if key not in {"best", "slacker", "official", "felix", "product_help"}:
            raise ConfigError(f"Unknown backend {key!r} in 'backends'")
    return AppConfig(
        workspace_root=workspace,
        audit_log_path=audit,
        best=_build(BackendConfig, backends.get("best"), "backends.best", kind="stdio"),
        # Slacker is an MCP SDK 1.x server: skip the failed 2.x `server/discover` probe.
        slacker=_build(BackendConfig, backends.get("slacker"), "backends.slacker", kind="stdio", protocol="legacy"),
        official=_build(
            BackendConfig, backends.get("official"), "backends.official", kind="autodiscover_http", timeout_seconds=30.0
        ),
        felix=_build(
            BackendConfig, backends.get("felix"), "backends.felix",
            kind="http", url="http://localhost:7410/", timeout_seconds=90.0,
        ),
        # Autodesk's Product Help server does not speak the 2.x `server/discover` probe (it answers 400):
        # skipping it saves one failed network round trip on every call.
        product_help=_build(
            BackendConfig, backends.get("product_help"), "backends.product_help",
            kind="http", url=DEFAULT_PRODUCT_HELP_URL, timeout_seconds=60.0, protocol="legacy",
        ),
        safety=_build(SafetyConfig, data.get("safety"), "safety"),
        runtime=_build(RuntimeConfig, data.get("runtime"), "runtime"),
    )


def load_config(path: str | Path | None = None) -> AppConfig:
    cfg_path = Path(path).expanduser() if path else default_config_path()
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"CAD Super MCP config not found: {cfg_path}. Run scripts\\install.ps1 first or set CAD_SUPER_CONFIG."
        )
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{cfg_path} is not valid JSON: {exc}") from exc
    return parse_config(data)
