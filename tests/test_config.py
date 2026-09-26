import json
from pathlib import Path

import pytest

from cad_super_mcp.config import ConfigError, load_config, parse_config

ROOT = Path(__file__).resolve().parents[1]

# The exact shape the v0.1 installer wrote (see the user's real config): must keep loading unchanged.
V01_CONFIG = {
    "workspace_root": "C:\\Users\\me\\Documents\\CAD-Super-Workspace",
    "audit_log_path": "C:\\Users\\me\\Documents\\CAD-Super-Workspace\\.cad_super\\audit.jsonl",
    "backends": {
        "best": {"enabled": True, "kind": "stdio", "command": "C:\\x\\cad-mcp.exe", "args": [], "cwd": "C:\\ws",
                 "env": {"CAD_MCP_TOOL_PROFILE": "core"}, "timeout_seconds": 180},
        "slacker": {"enabled": True, "kind": "stdio", "command": "C:\\x\\autocad-mcp.exe", "args": [], "cwd": "C:\\ws",
                    "env": {"ACAD_MCP_OUTPUT_ROOT": "C:\\ws\\output"}, "timeout_seconds": 90},
        "official": {"enabled": False, "kind": "autodiscover_http", "timeout_seconds": 30},
        "felix": {"enabled": False, "kind": "http", "url": "http://localhost:7410/", "timeout_seconds": 90},
        "product_help": {"enabled": True, "kind": "http", "url": "https://developer.api.autodesk.com/knowledge/public/v1/mcp", "timeout_seconds": 60},
    },
    "safety": {"enable_raw_felix": False, "require_confirm_for_destructive": True, "require_confirm_for_save": True,
               "require_confirm_for_plan_execute": True, "audit_log": True},
}


def test_v01_config_loads_unchanged_and_gets_the_new_defaults():
    cfg = parse_config(V01_CONFIG)
    assert cfg.best.session_mode == "auto" and cfg.best.protocol == "auto" and cfg.best.timeout_seconds == 180
    assert cfg.slacker.protocol == "legacy"                              # skip the failed 2.x probe against the SDK 1.x server
    assert cfg.product_help.protocol == "legacy"
    assert cfg.runtime.cross_process_lock and cfg.runtime.verify_writes and cfg.runtime.stage_layer == "_AI_STAGE"
    assert cfg.safety.raw_extra_commands == [] and cfg.safety.enable_raw_felix is False
    assert cfg.lock_path.endswith(".cad_super\\cad.lock") or cfg.lock_path.endswith(".cad_super/cad.lock")


def test_example_config_is_valid_and_matches_documented_defaults():
    data = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    cfg = parse_config(data)
    assert cfg.slacker.protocol == "legacy" and cfg.best.session_mode == "auto"
    assert cfg.safety.enable_raw_felix is False and cfg.felix.enabled is False


def test_user_values_override_defaults():
    data = json.loads(json.dumps(V01_CONFIG))
    data["backends"]["slacker"]["protocol"] = "auto"
    data["backends"]["best"]["session_mode"] = "per_call"
    data["runtime"] = {"verify_writes": False, "max_response_chars": 1000}
    cfg = parse_config(data)
    assert cfg.slacker.protocol == "auto" and cfg.best.session_mode == "per_call"
    assert cfg.runtime.verify_writes is False and cfg.runtime.max_response_chars == 1000


def test_typos_are_reported_with_the_allowed_keys_instead_of_a_typeerror():
    bad = json.loads(json.dumps(V01_CONFIG))
    bad["backends"]["best"]["timeout_second"] = 5
    with pytest.raises(ConfigError) as ei:
        parse_config(bad)
    assert "timeout_second" in str(ei.value) and "timeout_seconds" in str(ei.value) and "backends.best" in str(ei.value)
    bad2 = json.loads(json.dumps(V01_CONFIG))
    bad2["safety"]["enable_raw"] = True
    with pytest.raises(ConfigError, match="enable_raw"):
        parse_config(bad2)
    with pytest.raises(ConfigError, match="Unknown backend"):
        parse_config({**V01_CONFIG, "backends": {**V01_CONFIG["backends"], "autocad": {}}})
    with pytest.raises(ConfigError, match="top-level"):
        parse_config({**V01_CONFIG, "safty": {}})
    with pytest.raises(ConfigError, match="workspace_root"):
        parse_config({"backends": {}})


def test_enum_values_are_validated():
    bad = json.loads(json.dumps(V01_CONFIG))
    bad["backends"]["best"]["session_mode"] = "forever"
    with pytest.raises(ConfigError, match="session_mode"):
        parse_config(bad)
    bad["backends"]["best"]["session_mode"] = "auto"
    bad["backends"]["best"]["protocol"] = "v9"
    with pytest.raises(ConfigError, match="protocol"):
        parse_config(bad)


def test_metadata_keys_are_tolerated_for_forward_compatibility():
    cfg = parse_config({**V01_CONFIG, "$schema": "x", "_comment": "hello", "version": 2})
    assert cfg.workspace_root


def test_load_config_handles_bom_missing_file_and_bad_json(tmp_path):
    p = tmp_path / "config.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps(V01_CONFIG).encode("utf-8"))            # PowerShell 5.1 writes a BOM
    assert load_config(p).slacker.protocol == "legacy"
    with pytest.raises(FileNotFoundError, match="install.ps1"):
        load_config(tmp_path / "nope.json")
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_config(p)
