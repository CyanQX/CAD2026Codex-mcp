"""The README is the only documentation: keep it honest and in sync with the code."""

import asyncio
import re
from dataclasses import fields
from pathlib import Path

from helpers import FakeBackends, make_config
from mcp import Client

from cad_super_mcp.config import BackendConfig, RuntimeConfig, SafetyConfig
from cad_super_mcp.router import Router
from cad_super_mcp.server import create_server

README = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
NOT_TOOLS = {"cad_super", "cad_super_mcp"}                      # package / folder names that merely look like tools


def registered_tools(tmp_path):
    async def go():
        app = create_server(Router(make_config(tmp_path), backends=FakeBackends()))
        async with Client(app) as client:
            return (await client.list_tools()).tools

    return asyncio.run(go())


def inline_code_spans(text):
    text = re.sub(r"```.*?```", "", text, flags=re.S)             # drop fenced blocks
    return re.findall(r"`([^`\n]+)`", text)


def test_every_tool_is_documented_and_every_documented_tool_exists(tmp_path):
    tools = {t.name for t in registered_tools(tmp_path)}
    mentioned = set()
    for span in inline_code_spans(README):
        mentioned.update(re.findall(r"\bcad_[a-z_]+\b", span))
    mentioned -= NOT_TOOLS
    assert tools - mentioned == set(), f"tools missing from the README: {sorted(tools - mentioned)}"
    assert mentioned - tools == set(), f"README mentions tools that do not exist: {sorted(mentioned - tools)}"


def test_readme_tool_counts_match_reality(tmp_path):
    tools = registered_tools(tmp_path)
    read_only = sum(1 for t in tools if t.annotations and t.annotations.read_only_hint)
    m = re.search(r"(\d+) tools: (\d+) read-only tools, (\d+) write tools", README)
    assert m, "the README must state the tool counts"
    assert (int(m.group(1)), int(m.group(2)), int(m.group(3))) == (len(tools), read_only, len(tools) - read_only)
    assert f"{read_only} read-only tools" in README


def test_every_config_key_is_documented():
    documented = set()
    for span in inline_code_spans(README):
        documented.update(re.findall(r"[a-z_]+", span))
    for cls in (BackendConfig, SafetyConfig, RuntimeConfig):
        for f in fields(cls):
            assert f.name in documented, f"config key {cls.__name__}.{f.name} is not documented in the README"


def slug(heading):
    h = re.sub(r"`", "", heading.strip().lower())
    return "".join(c for c in h if c.isalnum() or c in " -_").replace(" ", "-")


def test_all_internal_links_resolve_to_a_heading():
    body = re.sub(r"```.*?```", "", README, flags=re.S)
    slugs = {slug(m.group(2)) for m in re.finditer(r"^(#{2,6})\s+(.+?)\s*$", body, flags=re.M)}
    broken = [a for a in re.findall(r"\]\(#([^)]+)\)", body) if a not in slugs]
    assert broken == [], f"broken anchors: {broken}"


def test_headings_are_unique():
    body = re.sub(r"```.*?```", "", README, flags=re.S)
    slugs = [slug(m.group(2)) for m in re.finditer(r"^(#{2,6})\s+(.+?)\s*$", body, flags=re.M)]
    assert len(slugs) == len(set(slugs))


def test_the_single_readme_is_the_only_markdown_file():
    root = Path(__file__).resolve().parents[1]
    others = [p.name for p in root.rglob("*.md") if ".venv" not in str(p) and p.name != "README.md"]
    assert others == []
