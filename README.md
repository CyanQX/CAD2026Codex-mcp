# Codex-mcp — a stability-first MCP gateway for AutoCAD 2026

> Stability-first MCP gateway: lets **Codex** (or any MCP host) drive a **local, writable AutoCAD 2026** through a small set of curated, policy-guarded, *precision-first* tools.

`Windows 10/11 x64` · `AutoCAD 2026 (full version)` · `Python 3.11 / 3.12` · `MCP Gateway` · `MIT License`

This repository publishes the **CAD Super MCP** gateway so that users can plug a local, writable AutoCAD 2026 straight into **Codex**. Codex registration name: `codex-CADmcp`.

This is an **MCP gateway**, not several open-source projects crudely merged into one process. The goal is to keep the strongest, most stable capability of each backend while isolating dependencies, reducing tool conflicts and keeping high-risk operations under control — so the model **draws accurately and collaborates like a human**.

## What this release (0.2) brings

| | How | What you actually feel |
|---|---|---|
| **More accurate** | Lengths unified as millimetres (accepts `"3.6m"`, `"12in"`); point specs mean the model never does coordinate arithmetic; the gateway computes rectangles, wall outlines and dimension-line positions locally; writes are **read back and verified** | The model says "900 from the wall" and it *is* 900, with a `verified` receipt |
| **More human** | `cad_context` sizes up the scene in one call; draw onto the staging layer and show me a picture before committing; `cad_undo` takes things back precisely; entity aliases; ambiguity gets a question instead of a guess; drawing pinning | It feels like talking to a drafter, not calling an API |
| **Faster** | Persistent backend sessions (measured: ~0.7 s per call → single-digit milliseconds); `cad_batch` runs many steps in one go; `cad_health` returns within a second | Drawing a room no longer takes half a minute |
| **More robust** | Transaction protection enforced at a single choke point; raw channel allow-listed; backend "soft failures" reported as failures; no dangling audit rows; cross-process mutual exclusion between hosts | When something fails you know **where and why** |

> Measured on the author's machine with AutoCAD not running (process/handshake/protocol overhead only, no real COM work): single call p50 0.74 s → 3 ms; `cad_health` cold start 8.4 s → 1.0 s, warm call 5.6 s → 15 ms; discovery when the official MCP is absent 59.5 s → under 1 s. See [Known limitations and what still needs a real-machine pass](#known-limitations-and-what-still-needs-a-real-machine-pass).

## Table of contents

- [Quick start](#quick-start)
- [Fusion principles and architecture](#fusion-principles-and-architecture)
- [System requirements](#system-requirements)
- [Installation](#installation)
- [AutoCAD-side setup](#autocad-side-setup)
- [Run the doctor first](#run-the-doctor-first)
- [Connect Codex](#connect-codex)
- [Usage guide](#usage-guide)
- [Tools exposed by the gateway](#tools-exposed-by-the-gateway)
- [Stability and safety policy](#stability-and-safety-policy)
- [Configuration reference](#configuration-reference)
- [Smoke test](#smoke-test)
- [Upstream versions and compatibility](#upstream-versions-and-compatibility)
- [ChatGPT desktop and the HTTP mode](#chatgpt-desktop-and-the-http-mode)
- [Known limitations and what still needs a real-machine pass](#known-limitations-and-what-still-needs-a-real-machine-pass)
- [Changelog](#changelog)
- [Uninstall](#uninstall)
- [Upstream projects and licenses](#upstream-projects-and-licenses)
- [Verification status and disclaimer](#verification-status-and-disclaimer)

---

## Quick start

Target environment: Windows 10/11 + AutoCAD 2026 (full version) + Python 3.11/3.12 + Codex.

1. Download the ZIP from this repository (or `git clone`) into a simple path, e.g. `C:\Tools\CAD-Super-MCP` (**do not** put it inside a OneDrive-synced folder).
2. Double-click `INSTALL.cmd`: it creates 3 isolated virtual environments and generates the config (options in [Installation](#installation)).
3. Start AutoCAD 2026 and create/save a **test DWG copy** from `acadiso.dwt` or your own template.
4. Optional: if the Autodesk Assistant offers the AutoCAD MCP Tech Preview, run `MCPHTTPSTART` on the AutoCAD command line.
5. Optional: if you need the raw fallback, build the felix plugin with `INSTALL.cmd -WithFelix` and `NETLOAD` it inside AutoCAD; raw stays disabled by default.
6. Double-click `DOCTOR.cmd`. **Do not continue until `essential_ready` is `true`.**
7. Double-click `REGISTER-CODEX.cmd`: it first backs up your existing `%USERPROFILE%\.codex\config.toml`, then appends `codex-CADmcp`.
8. Fully quit and reopen Codex, type `/mcp`, and confirm `codex-CADmcp` is connected.
9. The first run should be the [Smoke test](#smoke-test) on the test DWG only.
10. Drop the [working rules for Codex](#working-rules-for-codex-agentsmd-template) into your project's `AGENTS.md` and Codex behaves much more like a meticulous drafter.

> **Important**
>
> - Geometry writes are never silently retried across backends — this is a deliberate stability decision.
> - `cad_raw_command` / `cad_eval_lisp` are disabled by default.
> - Delete, CADPlan Execute, Save/Export and other high-risk actions are re-checked and policy-controlled by the gateway.
> - AutoCAD and MCP/Codex must run as the same Windows user at the same privilege level.
> - Copy important drawings before working on them.

---

## Fusion principles and architecture

| Backend | Its job in this package | Why it is used this way |
|---|---|---|
| `best-cad-mcp 1.7.0` | Drawing understanding, CAD-IR, semantics/constraints, CADPlan, dry run, validation, vision, image-to-CAD, advanced CAD; entity geometry read-back | Its agent workflow is the most complete, but it exposes a huge tool surface, so the gateway only exposes curated entrances |
| `Slacker-LLC/autocad-mcp` | Common 2D / layer / handle queries and structured writes (millimetres) | Small, explicit tools built on ActiveX/COM — the "hands" |
| Autodesk official AutoCAD/Civil 3D MCP | Schema, object queries, aggregation, canvas/selection, template compliance | The authoritative read/analysis source; AutoCAD currently exposes read/analysis mostly |
| `felixalmesberger/AUTOCAD-MCP` | `send_command` / `eval_lisp` last-resort fallback | Extremely capable and equally dangerous, so raw is disabled by default |
| Autodesk Product Help MCP | Live official Autodesk help documentation | Check the official docs when an API/version/command is uncertain |

### Why the environments must be isolated

`best-cad-mcp` currently depends on MCP Python SDK **2.x**, while Slacker is explicitly pinned to SDK **1.x (<2)**. Installing them into one Python environment creates dependency conflicts. This package therefore creates:

- `.venv-gateway`: the gateway / MCP SDK 2.x
- `.venv-best`: best-cad-mcp
- `.venv-slacker`: Slacker

The gateway uses the MCP SDK 2.x client, which can negotiate with both old and new protocol backends, so it can call all of them at runtime. For the SDK 1.x Slacker, `protocol=legacy` is used by default to skip the doomed `server/discover` probe.

### Architecture

```text
Codex / MCP Host
       |
       v
CAD Super MCP (MCP SDK 2.x)
   tool layer (server) -> ops layer (ops: point specs / read-back verification / staging / undo / batch) -> Router (policy choke point)
       |                                                            allow-lists · confirm gates · interlock · audit
       +-- best-cad-mcp (own venv, SDK 2.x, persistent session)
       |      drawing understanding / CADPlan / validation / vision / image-to-CAD / advanced CAD / geometry read-back
       |
       +-- Slacker AutoCAD MCP (own venv, SDK 1.x, persistent session)
       |      structured COM queries and common writes / precise handle edits
       |
       +-- Autodesk Official AutoCAD MCP (localhost:5001-5050/mcp)
       |      official Schema / Query / Aggregate / Canvas / Compliance
       |
       +-- felix .NET MCP (localhost:7410)
       |      raw command / AutoLISP fallback, disabled by default
       |
       +-- Autodesk Product Help MCP (remote HTTPS)
              official documentation search
```

### Routing policy

- Common structured writes → Slacker only (a stable mm/degree boundary).
- Advanced / semantic / planning / vision → best-cad-mcp; best coordinates are **drawing units** (the gateway converts automatically for `cad_text` rotation/alignment).
- Official reads / analysis → the Autodesk official MCP.
- Documentation → Product Help.
- Raw commands / LISP → felix, only when explicitly enabled and confirmed.
- Each entrance can only reach its own tool set (e.g. `cad_edit` cannot reach `execute_cad_plan`); this mapping lives in `cad_super_mcp/policy.py` and a test compares it against the real upstream tool inventories.

### Failure policy

- Backends use **persistent sessions** by default (`session_mode: auto`): they are recycled and rebuilt on error, timeout, 15 minutes of idleness or after 500 calls; a read-only call is retried once when a session breaks, **a write is never retried automatically** (an uncertain result is reported as uncertain). Set `session_mode: per_call` for the old behaviour.
- All CAD-related calls are serialised, and mutually excluded across every gateway process on the machine (see [Stability and safety policy](#stability-and-safety-policy)).
- Fallback happens only for clearly safe read-only/visual capabilities; geometry writes are never silently retried across backends, avoiding duplicated or mis-scaled geometry when schemas/units differ.

---

## System requirements

Required:

1. Windows 10/11 x64
2. **AutoCAD 2026 (full version, not LT)**, properly licensed
3. Python **3.11 or 3.12 x64** (the installer tries, in order: the `-Python` argument, the `CAD_SUPER_PYTHON` environment variable, the `py` launcher, `python` on PATH, a `uv`-managed Python; the Microsoft Store `python.exe` stub is skipped automatically)
4. Access to PyPI / GitHub during the first install
5. AutoCAD and Codex/the gateway using the **same Windows user at the same privilege level**; do not run one "as administrator" and the other normally

Optional:

- .NET 8 SDK: to build the felix AutoCAD 2026 plugin (`-WithFelix`)
- Autodesk Assistant Tech Preview: for the Autodesk official AutoCAD MCP

---

## Installation

### 1. Get the source

Download the ZIP from this repository (or `git clone`) into a simple path, e.g.:

```powershell
C:\Tools\CAD-Super-MCP
```

Do not put it inside a OneDrive-synced folder.

### 2. Open PowerShell

A normal PowerShell is fine; administrator is not required. Enter the directory:

```powershell
cd C:\Tools\CAD-Super-MCP
```

If your PowerShell does not allow scripts, unblock it for the current window only:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

### 3. One-command install

Easiest: double-click in the repository root:

```text
INSTALL.cmd
```

Or run in PowerShell:

```powershell
.\scripts\install.ps1
```

The default CAD workspace is:

```text
%USERPROFILE%\Documents\CAD-Super-Workspace
```

Common options:

```powershell
.\scripts\install.ps1 -Workspace "D:\CAD\Workspace"      # custom workspace
.\scripts\install.ps1 -Python "C:\Python312\python.exe"  # pick an interpreter (uv-managed Pythons are found automatically, usually unnecessary)
.\scripts\install.ps1 -SlackerRef "<tag or commit>"      # pin the Slacker version (defaults to main)
.\scripts\install.ps1 -WithFelix                         # also build the felix raw plugin (not installed by default)
```

The installer creates the 3 isolated environments, installs the gateway, best-cad-mcp and Slacker, and generates:

```text
%USERPROFILE%\.cad-super-mcp\config.json       # configuration
%USERPROFILE%\.cad-super-mcp\install-lock.txt  # the exact version of every package actually installed (reproducible, traceable)
```

---

## AutoCAD-side setup

### A. Basics

Start AutoCAD 2026 first and open a **test DWG copy**. On the very first run, do not work on your only production drawing.

### B. Autodesk official MCP (recommended, but not a hard dependency)

If your AutoCAD 2026 / Autodesk Assistant offers the MCP Tech Preview:

1. Open the Autodesk Assistant
2. Open the Tech Preview
3. On the AutoCAD command line, run:

```text
MCPHTTPSTART
```

Autodesk picks a local port in `5001-5050`, for example:

```text
http://localhost:5001/mcp
```

**You never fill in the port by hand. CAD Super MCP first runs a millisecond-level TCP pre-scan and only handshakes with ports that are actually listening, to confirm it is the Autodesk AutoCAD MCP.** (Windows' own CDPSvc occupies port 5040; it is detected and ignored automatically.)

Inspect the status:

```text
MCPCONFIG
```

Stop it:

```text
MCPHTTPSTOP
```

If your Autodesk Assistant does not offer the Tech Preview, you can skip it (set `backends.official.enabled` to `false`). The gateway's best + Slacker core still works.

### C. felix .NET plugin (optional last resort)

felix is **not installed** by default. Loading it leaves a localhost command endpoint (`http://localhost:7410/`) resident inside AutoCAD, and that endpoint is **independent of the gateway's raw switch** — any program on the machine may reach it once the DLL is loaded. Install and load it only when you genuinely need it.

When you do:

```powershell
.\scripts\install.ps1 -WithFelix
```

If a .NET 8 SDK is found and the build succeeds, it prints the full path of `Infomatik.AutoCAD.Mcp.dll`. On the AutoCAD command line:

```text
NETLOAD
```

Choose that file:

```text
Infomatik.AutoCAD.Mcp.dll
```

After it loads, run:

```text
MCPSTATUS
```

Note: **this package ships `enable_raw_felix=false` by default**. Even with the DLL loaded, the gateway will not use `send_command` / `eval_lisp` on its own.

Only when you explicitly want the last resort, enable it (this flips both the raw switch and the felix backend):

```powershell
.\scripts\enable-felix-raw.ps1
```

Disable it:

```powershell
.\scripts\enable-felix-raw.ps1 -Disable
```

Once enabled, raw calls still require `confirm=true` and must pass the gateway's checks:

- **Commands**: the first command must be on the allow-list (common draw/edit/layer/view commands), and every token is scanned against a deny-list of commands that load code, start programs, write files, close drawings or quit AutoCAD (`NETLOAD`, `APPLOAD`, `SHELL`, `SCRIPT`, `SAVEAS`, `QUIT`, ... no matter whether they are written `_.NETLOAD`, `'_.SHELL` or with another prefix); add extra commands explicitly in `safety.raw_extra_commands` (which can never override the dangerous-command list).
- **AutoLISP**: the expression is lexically parsed; the symbol in every function position must be allow-listed; `command`, `eval`, `read`, `apply`, `startapp`, file/registry/COM (`vla-*`/`vlax-*`) and friends are rejected, and `setvar` only accepts a handful of safe variables.

> **Note**: this is still a "seat belt", not a sandbox. Enabling raw is the same as letting the model run AutoCAD commands / AutoLISP: use it on a test DWG only and review every call by hand. Editing `config.json` requires fully restarting Codex to take effect (the gateway reads config only at startup).

---

## Run the doctor first

Make sure AutoCAD is running with an active drawing, then the simplest is to double-click:

```text
DOCTOR.cmd
```

Or in PowerShell:

```powershell
.\scripts\doctor.ps1
```

The doctor will:

1. list every backend (online, tool count, protocol version);
2. **check the contract**: whether the key tools the gateway depends on still exist, **and whether upstream still accepts every parameter the gateway sends** (Slacker's schema is strict — a renamed parameter fails even if the tool name survives);
3. **live check**: actually call `best.check_runtime_environment(check_autocad=true)` and Slacker `autocad_status`; on failure it prints the upstream remediation directly.

Output looks like:

```text
  [OK  ] best          tools=210 protocol=2026-07-28
  [OK  ] slacker       tools=29 protocol=2025-11-25
  [--  ] official      Backend 'official' is disabled
  [OK  ] contract best tool & parameter contracts match upstream
  [FAIL] live slacker_autocad  AUTOCAD_NOT_RUNNING ...
         -> Start AutoCAD 2026 first and open a drawing

  essential_ready = false   (must be true to proceed; fix the FAIL items above first)
```

The important line is `essential_ready = true`. `official` and `felix` showing `--` only means not enabled / not running and does not affect the core; `product_help` needs the network.

Additional options: `--json` (also print the full JSON report), `--tools` (include the full tool inventories in the JSON), `--snapshot PATH` (write the upstream tool-name snapshot used by the contract tests).

---

## Connect Codex

This is currently the smoothest entrance for a local, writable AutoCAD.

Easiest: double-click in the repository root:

```text
REGISTER-CODEX.cmd
```

It backs up your existing `%USERPROFILE%\.codex\config.toml` before touching it, then only appends the `codex-CADmcp` entry without overwriting your other Codex configuration. Paths containing single quotes (e.g. `C:\Users\O'Brien`) are escaped correctly.

- If an entry with the same name already exists it is left alone; after moving the installation, use `REGISTER-CODEX.cmd -Force` to regenerate (only the section this script creates is replaced).
- If the `CODEX_HOME` environment variable is set, the script writes the `config.toml` under that directory.

If you only want to look at the config without modifying anything:

```powershell
.\scripts\print-codex-config.ps1
```

Sample output (see also `examples/codex-config.toml`):

```toml
[mcp_servers.codex-CADmcp]
command = 'C:\Tools\CAD-Super-MCP\.venv-gateway\Scripts\cad-super-mcp.exe'
enabled = true
startup_timeout_sec = 30
tool_timeout_sec = 240
default_tools_approval_mode = "writes"

[mcp_servers.codex-CADmcp.env]
CAD_SUPER_CONFIG = 'C:\Users\your-user\.cad-super-mcp\config.json'
```

`default_tools_approval_mode = "writes"` means Codex asks for approval whenever a tool is not marked read-only. The gateway marks the 18 read-only tools (`cad_context`, `cad_measure`, `cad_plan_check`, `cad_stage_view`, `cad_verify`, ...) as read-only, so "looking" and "checking" never interrupt you — only real drawing calls need your nod.

Paste it into:

```text
%USERPROFILE%\.codex\config.toml
```

Then fully restart Codex.

Inside Codex, type:

```text
/mcp
```

and you should see:

```text
codex-CADmcp
```

### Other MCP clients (generic stdio config)

The gateway is a standard stdio MCP server and can also plug into Claude Desktop / Claude Code / Cursor etc. Generic form:

```json
{
  "mcpServers": {
    "codex-CADmcp": {
      "command": "C:\\Tools\\CAD-Super-MCP\\.venv-gateway\\Scripts\\cad-super-mcp.exe",
      "env": { "CAD_SUPER_CONFIG": "C:\\Users\\your-user\\.cad-super-mcp\\config.json" }
    }
  }
}
```

> Every host/session starts its own gateway process; they mutually exclude each other through the workspace's `.cad_super\cad.lock` and will not drive AutoCAD simultaneously. But do not let several **different** MCP services (e.g. another AutoCAD MCP) write to the same AutoCAD at the same time — they do not honour this lock.

---

## Usage guide

### Recommended order of use

**Read-only inspection**

```text
cad_health
→ cad_context (confirm the drawing, units, layers; add pin=true if the user may switch drawings)
→ cad_scan / cad_query_entities
→ cad_explain_entity / cad_measure
```

**Regular safe writes (recommended)**

```text
cad_context
→ cad_line / cad_rect / cad_circle / cad_wall / cad_text / cad_dimension (use point specs; read-back verification is on by default)
→ look at data.verification.verified and the summary
→ cad_verify
```

**Show me first, then commit (staging flow)**

```text
any drawing tool with stage=true      → draws onto the bright staging layer _AI_STAGE
→ cad_stage_view(action="preview")    → a screenshot right in front of you
→ you say "good": cad_stage(action="commit")   /   "no": cad_stage(action="discard")
```

**The regret pill**

```text
cad_undo                  → previews exactly what would be undone
→ after you agree, cad_undo(confirm=true)
a whole cad_batch         → cad_undo(scope="group", group="b1", confirm=true)
```

**Complex mechanical drawings / bulk edits**

```text
cad_recommend_tools
→ cad_scan
→ CAD-IR / semantic analysis (cad_understand)
→ cad_plan_check(action="validate")
→ cad_plan_check(action="dry_run")
→ user confirmation
→ cad_plan(action="execute", confirm=true)
→ cad_verify
```

**Cross-checking with official data**

```text
cad_official(discoverAutoCADTypes)
→ cad_official(queryAutoCADObjects)
→ make the change
→ cad_official(queryAutoCADObjects) to confirm again
```

**When an API/command is uncertain**

```text
cad_docs(search_help_content)
```

Prefer the current official Autodesk help over letting the model guess the 2026 API.

### Point specs (no coordinate arithmetic)

Every parameter that accepts a "point" (`start_mm`, `center_mm`, `vertices_mm`, `corner`, `at`, each point of `centerline`, ...) can use any form below. Lengths may be plain numbers (millimetres) or unit-suffixed strings: `"3600"`, `"3.6m"`, `"360cm"`, `"12in"`, `"2'6\""`.

| Form | Example | Meaning |
|---|---|---|
| Coordinates | `[0, 0]`, `["3.6m", 900]` | millimetres (2D or 3D) |
| Relative offset | `{"from": [0,0], "dx": "3.6m", "dy": 900}` | offset from a point (`from` may nest) |
| Polar | `{"from": P, "angle_deg": 90, "dist": "0.9m"}` | degrees counter-clockwise from +X |
| Midpoint | `{"mid": [P, Q]}` | midpoint of two specs |
| Snap to an entity | `{"handle": "2A3", "snap": "end"}` | line `start/end/mid`; circle `center/top/bottom/left/right/quadrant:<deg>`; arc `start/end/mid/center`; polyline `start/end/mid/vertex:N/segment:N/centroid`; text `insertion` |
| Alias | `{"name": "west wall", "snap": "mid", "dy": 900}` | an entity named with `cad_name` |
| Intersection | `{"intersect": ["2A3", "2A4"]}` | where two lines cross (`"infinite": true` for the extended lines) |
| Perpendicular foot | `{"foot": {"point": P, "onto": "2A3"}}` | the foot of a perpendicular from a point onto a line |

For example, "draw a 1.2 m round table 900 above the middle of the west wall":

```json
{"tool": "cad_circle", "center_mm": {"name": "west wall", "snap": "mid", "dy": 900}, "diameter_mm": "1.2m", "layer": "A-FURN"}
```

Entity geometry is **read fresh from AutoCAD** (the cache is never trusted) and units are converted automatically; after a write, the entity is read back and compared with what was requested (default tolerance 0.01 mm).

### Working rules for Codex (AGENTS.md template)

The MCP server ships its own working rules (the `instructions`), but hosts display them inconsistently. Putting the block below into your project's `AGENTS.md` (which Codex reads natively) is the most reliable way:

```markdown
# CAD drafting rules (codex-CADmcp)

You are a meticulous interior/architectural drafter working on AutoCAD 2026 through codex-CADmcp.
1. Start with `cad_context`: confirm AutoCAD is online, the drawing name, units, layers. If the units are not millimetres, or are unset, tell me before doing anything.
2. Lengths are always millimetres (or unit-suffixed strings); never compute coordinates yourself - use point specs (relative / polar / midpoint / snaps / intersections).
3. Draw only onto existing, standard layers; if a layer is missing, ask me first - do not create it on your own.
4. After every write, look at `data.verification`: if `verified` is false or null, tell me honestly - never say "done".
5. When unsure, or when many entities are affected, draw with `stage=true` onto the staging layer first, show me `cad_stage_view(preview)`, and only `commit` after I agree.
6. On `NEEDS_CLARIFICATION`: relay `details.options` / `question` to me; do not guess.
7. Use `cad_batch` for multi-step work; undo with `cad_undo` (preview first, then `confirm=true` after I agree).
8. Delete, save, export and CADPlan execute only after I explicitly ask, and with `confirm=true`; never save automatically.
9. Report format: what was done (layer/size/handle) → verification result → suggested next step.
```

### Suggested prompts

| Scenario | Prompt |
|---|---|
| Read-only inspection | Call cad_context first, then scan the entities in the current drawing and tell me the units, layers and entity count. Do not modify the drawing. |
| Precise drawing | On layer A-WALL draw a 3.6 m × 2.4 m room with its bottom-left corner at [0,0] and 240 mm walls (centreline). Draw it with stage=true onto the staging layer first so I can look at it. |
| Relative positioning | On the west wall (handle 2A3, name it "west wall") draw a 1.2 m diameter circle 900 above its midpoint, then dimension its distance to the wall. |
| Undo | That last step is wrong; take it back (tell me what you would undo first). |
| Complex mechanical work | Use cad_recommend_tools first and build a CADPlan; it must validate + dry_run. Show me the plan and only execute after I confirm. |
| Official cross-check | Use cad_official's discoverAutoCADTypes/queryAutoCADObjects to read the target objects; after modifying, confirm the properties with the official query again. |
| Official documentation | Use cad_docs to search the official Autodesk help for the target API/command in AutoCAD 2026 before deciding how to proceed. |

---

## Tools exposed by the gateway

44 tools: 18 read-only tools, 26 write tools. All tools answer with the same envelope (`ok` / `summary` / `data` / `warnings` / `error` / `meta`); screenshots come back as native images; oversized output is trimmed structurally with a hint to narrow the scope.

| Category | Tools | Notes |
|---|---|---|
| **Observe** | `cad_health` | backend online/session/policy; the networked Product Help check is skipped by default (pass `check_remote=true`) |
| | `cad_context` | call it first: AutoCAD/drawing/units/layers/layouts/blocks/staging status; `pin=true` pins the drawing |
| | `cad_drawing_info` `cad_list_layers` | active drawing info; layer list |
| | `cad_query_entities` `cad_scan` `cad_explain_entity` | query entities; live scan (summary by default, trimmed automatically); explain one entity |
| | `cad_measure` | exact measurement: `distance` / `area` / `length` / `entity`; pure-math measurements work even when AutoCAD is offline |
| | `cad_understand` `cad_recommend_tools` `cad_tool_help` | drawing-understanding layer; tool recommendation; tool help |
| **Precise drawing** | `cad_line` `cad_circle` `cad_arc` `cad_polyline` | millimetres + point specs + read-back verification + `stage` |
| | `cad_rect` | rectangle (bottom-left corner or centre + width/height + rotation), returns area/perimeter |
| | `cad_wall` | mitred wall outline from a centreline + thickness (a closed centreline yields inner + outer loops) |
| | `cad_text` | single-line text; centred/aligned/rotated (the latter two go through best, see [Known limitations](#known-limitations-and-what-still-needs-a-real-machine-pass)) |
| | `cad_dimension` | linear dimension; the dimension-line position is computed from the offset |
| **Edit** | `cad_move` `cad_copy` `cad_erase` | by handle or alias; moves are undoable; erase needs `confirm` |
| | `cad_layer` | layers: create / set current / change colour·lock·freeze·visibility |
| | `cad_document` | open / create a drawing (restrict directories with `safety.allowed_open_roots`) |
| **Collaboration** | `cad_stage` `cad_stage_view` | staging: `begin` / `commit` / `discard` / `cleanup`; `status` / `preview` (returns images) |
| | `cad_undo` | undo entities the gateway itself created/moved; preview first, then `confirm` |
| | `cad_name` | name an entity; remembered per drawing |
| | `cad_batch` | run many steps in one go (`line/circle/arc/polyline/rect/wall/text/dimension/move/copy/layer`), stop on error, undo as a unit |
| **Generic entrances** | `cad_draw` `cad_edit` `cad_annotate` | best-cad-mcp's advanced objects; `tool` is an enum listing every legal tool name |
| **Plan and verify** | `cad_plan_check` | read-only: CADPlan `validate` / `dry_run` |
| | `cad_plan` | `execute` forces transaction/rollback/validation/rescan; needs `confirm` |
| | `cad_validate` `cad_render` `cad_verify` | geometry validation; render (returns an image); a post-write check inside one lock |
| | `cad_image_trace` | image-to-CAD phases (`prepare` … `validate_fidelity`) |
| **External and output** | `cad_official` `cad_docs` | Autodesk official MCP; official help docs |
| | `cad_export` `cad_save` | export / save (file writes need `confirm`) |
| **Last resort (off by default)** | `cad_raw_command` `cad_eval_lisp` | felix raw; allow-list + `confirm` |

---

## Stability and safety policy

1. **Every CAD call enters AutoCAD serially and is mutually excluded across processes**: an async lock inside the process plus a file lock at `.cad_super\cad.lock` in the workspace — when Codex, Claude and other hosts each start a gateway, they still never drive AutoCAD at the same time; if the holder crashes the OS releases the lock automatically, so a dead gateway can never wedge the others. The networked-only Product Help does not take this lock. A lock timeout reports the holder's PID.
2. **Handle-first**: scan and confirm handles on the existing drawing before editing anything.
3. **Read-back verification after writes**: every drawing write reads the entity back and compares it by default (`data.verification`: `verified` / `max_deviation_mm` / per-item `checks`); when read-back is unavailable it is reported as "unverified" rather than pretending success. `cad_verify` = rescan + geometry validation + screenshot; `deep=true` also rebuilds CAD-IR, dimension bindings and constraint checks — the whole check runs under one lock.
4. **CADPlan Execute is transaction-protected and has exactly one entrance**: `execute_cad_plan` can only be reached through `cad_plan(action="execute")`; whatever the caller sends, the Router forces `allow_modify=true + transactional=true + rollback_on_error=true + rollback_on_high_severity_validation=true + validate_after_plan=true + rescan_after_plan=true` at the single choke point. The `tool` enums of `cad_edit` / `cad_draw` / `cad_annotate` do not contain it at all.
5. **Deletes need confirmation by default**: `erase_entity` requires it, and the confirm check happens before any backend I/O.
6. **Save is a stand-alone action**: `confirm=true` by default, so an agent can never silently save after editing.
7. **raw is disabled by default**: felix exists only as the last resort; once enabled it is allow-list + `confirm` (see [the felix section](#c-felix-net-plugin-optional-last-resort)).
8. **Drawing pinning**: after `cad_context(pin=true)`, if the active drawing is swapped, writes are refused (`DRAWING_CHANGED`) — "scanning drawing A but writing drawing B" cannot happen; switching deliberately via `cad_document(open/new)` updates the pin automatically.
9. **It does not quietly change your state**: best's drawing/annotation tools like to make the target layer "current"; the Router reads `CLAYER` before the call and restores it afterwards (even on failure).
10. **Honest results**: a backend reporting failure inside a "successful" response (`{"ok": false}`, including the SDK-wrapped `{"result": {...}}` shape) is recognised as a failure, with a stable error code (`AUTOCAD_NOT_RUNNING`, `AUTOCAD_BUSY`, `TIMEOUT`, `CONFIRM_REQUIRED`, `NEEDS_CLARIFICATION`, `DRAWING_CHANGED`, ...) and a next-step hint; a backend crash/timeout no longer surfaces as `ExceptionGroup: unhandled errors in a TaskGroup` but as the real cause.
11. **Audit log**: written by default to:

    ```text
    <workspace>\.cad_super\audit.jsonl
    ```

    Every call carries `call_id`, `risk`, `elapsed_ms`, `outcome` (including "cancelled, outcome unknown"); large arguments are recorded as size + hash only; the file rotates by size; and **a failed audit write never turns a successful write into an error** (which would invite duplicate retries).

12. **Upstream dependency isolation**: best and Slacker do not share a Python environment.
13. **Only safe degradations**: read-only feedback such as vision/screenshots may fall back from best to a Slacker screenshot; **geometry writes are never silently retried across backends**. Slacker and best have different parameter shapes and unit contracts — an automatic retry could duplicate entities or mis-scale them — so when the write backend is unavailable the gateway errors out instead.
14. **HTTP mode binds to localhost by default**: binding a non-loopback address is refused (unless `--allow-remote` is given explicitly, and that endpoint has no authentication).

> **Note**: `confirm=true` is a call parameter that prevents accidental activation; it is **not** the same as human approval. Real human approval is performed by the MCP host (e.g. Codex's `default_tools_approval_mode = "writes"`).

---

## Configuration reference

Config file: `%USERPROFILE%\.cad-super-mcp\config.json` (the `CAD_SUPER_CONFIG` environment variable may point elsewhere). **A config generated by v0.1 keeps working unchanged**; a misspelled key produces a clear error listing the available keys. A full example lives in `config.example.json`.

**`backends.<best|slacker|official|felix|product_help>`**

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | whether this backend is on |
| `kind` | `stdio` / `http` / `autodiscover_http` | transport |
| `command` `args` `cwd` `env` | — | stdio backend launch command, arguments, working directory, environment |
| `url` | — | http backend address |
| `timeout_seconds` | 120 (per-backend defaults apply) | timeout of a single call; a timeout recycles that backend |
| `session_mode` | `auto` | `auto` (stdio uses a persistent session, http a fresh client per call) / `persistent` / `per_call` |
| `protocol` | `auto` (Slacker and Product Help default to `legacy`) | `legacy` skips the MCP 2.x `server/discover` probe |
| `idle_timeout_seconds` | 900 | recycle a persistent session after this much idleness |
| `recycle_after_calls` | 500 | recycle a persistent session after this many calls |
| `port_range` | `[5001, 5050]` | official-MCP autodiscovery only: port range to scan |
| `negative_cache_seconds` | 10 | official MCP only: how long a miss is cached |

**`safety`**

| Key | Default | Meaning |
|---|---|---|
| `enable_raw_felix` | `false` | whether the raw channel is allowed |
| `require_confirm_for_destructive` `require_confirm_for_save` `require_confirm_for_plan_execute` | `true` | whether each class of high-risk action needs `confirm=true` |
| `audit_log` | `true` | whether the audit log is written |
| `audit_max_bytes` `audit_backups` `audit_arg_chars` | 5000000 / 3 / 2000 | audit rotation size, files kept, max characters per argument |
| `raw_extra_commands` | `[]` | extra first commands allowed on the raw channel (never overrides the dangerous-command list) |
| `allowed_open_roots` | `[]` | when non-empty, `cad_document(open)` only accepts files under these directories |

**`runtime`**

| Key | Default | Meaning |
|---|---|---|
| `cross_process_lock` | `true` | the cross-process AutoCAD interlock |
| `lock_timeout_seconds` | 180 | how long to wait for the lock |
| `max_response_chars` | 60000 | per-response cap; larger payloads are trimmed structurally |
| `verify_writes` | `true` | whether drawing writes are read back and verified |
| `verify_tolerance_mm` | 0.01 | verification tolerance (millimetres) |
| `stage_layer` `stage_color_aci` | `_AI_STAGE` / 6 | staging layer name and colour |
| `max_batch_operations` | 200 | maximum steps per `cad_batch` call |

---

## Smoke test

This test must run on your own Windows + AutoCAD 2026; an environment without AutoCAD cannot simulate a real Autodesk COM session. **Do it on a test DWG copy only.**

### 1. Basic connectivity

- AutoCAD 2026 is running.
- An active test DWG is open.
- `DOCTOR.cmd` has been run.
- `essential_ready` should be `true`.

### 2. Codex read-only loop

After `/mcp` shows `codex-CADmcp` in Codex, test in order:

1. `cad_health`
2. `cad_context`
3. `cad_list_layers`
4. `cad_scan`
5. `cad_verify(render=true, deep=false)` (you should see an image directly)

### 3. Exact mm writes and read-back verification

In an empty test drawing (first `cad_layer(action="create", name="TEST")`):

- `cad_line(start_mm=[0,0], end_mm=[100,0], layer="TEST")`
- `cad_circle(center_mm=[50,30], radius_mm=10, layer="TEST")`
- `cad_arc(center_mm=[50,30], radius_mm=20, start_angle_deg=0, end_angle_deg=180, layer="TEST")`
- `cad_polyline(vertices_mm=[[0,0],[100,0],[100,60],[0,60]], closed=true, layer="TEST")`
- `cad_rect(corner=[0,0], width="3.6m", height=2400, layer="TEST")`

**Every one of them should return `data.verification.verified = true`.** Then:

- use `cad_query_entities` to get handles;
- run `cad_copy` on one test entity;
- run `cad_verify(deep=true)`.

### 4. Point specs and measurement

- `cad_circle(center_mm={"handle": "<line handle>", "snap": "mid", "dy": 900}, diameter_mm="1.2m", layer="TEST")`: the centre should sit exactly 900 above the midpoint of that line.
- `cad_measure(kind="entity", handle="<line handle>")`: the returned endpoints should match what you drew.

### 5. Stage → preview → commit / discard / undo

- Any drawing tool with `stage=true`: the entity should appear on the bright layer `_AI_STAGE`.
- `cad_stage_view(action="preview")`: should return a screenshot.
- `cad_stage(action="commit")`: the entities move to layer `TEST`; or `discard`: they are erased.
- `cad_undo` (preview first, then `confirm=true`): only undoes entities the gateway itself drew.
- Set the current layer to `0` in AutoCAD, then have Codex draw a rectangle onto layer `TEST` with `cad_draw(tool="draw_rectangle", ...)`: afterwards your current layer should **still be `0`** (current-layer protection).

### 6. Delete and save protection

- `cad_erase(handle=..., confirm=false)` should be refused (`CONFIRM_REQUIRED`).
- `cad_erase(handle=..., confirm=true)` goes through.
- `cad_save(..., confirm=false)` should be refused.

### 7. CADPlan protection

For a small plan:

1. `cad_plan_check(action="validate")`
2. `cad_plan_check(action="dry_run")`
3. `cad_plan(action="execute", confirm=true)`
4. `cad_verify(deep=true)`

The gateway forces the transaction, rollback-on-error, rollback-on-high-severity-validation, validate-after and rescan-after parameters to true.

### 8. Official MCP (optional)

After running `MCPHTTPSTART` inside AutoCAD:

- `cad_health` should show the official backend online;
- call `cad_official(tool="discoverAutoCADTypes", ...)` or the official query tool for cross-validation.

### 9. felix (optional)

Enable it only when neither the structured tools nor best can cover the need. Do not enable raw by default.

### Acceptance by prompt

If you prefer to run through it in natural language inside Codex, send these in order:

Read-only first:

> Using codex-CADmcp, check the backend health, read the current drawing info and scan the current test DWG; do not modify anything. Tell me the active drawing, INSUNITS, layers and entity count.

Then a structured write:

> Only in the current test DWG, create a line from [0,0] to [100,0] mm with cad_line, then a circle at [50,30] mm with radius 10 mm using cad_circle. Afterwards run cad_verify(deep=true) and tell me the read-back verification result for both. Do not save the drawing; wait for my confirmation.

Finally, the save boundary:

> Tell me how you would save, but do not call cad_save until I explicitly confirm.

---

## Upstream versions and compatibility

- `best-cad-mcp` is pinned to `1.7.0` by the installer, so a single upgrade cannot silently change a large tool surface.
- Slacker currently has no release tag this package relies on, so the installer pulls from its GitHub `main` by default (pin it with `-SlackerRef <tag or commit>`); the exact version of every package is recorded into `install-lock.txt`. **The doctor verifies the key tool inventory this gateway depends on, and the parameter names it sends** — if upstream ever makes a breaking change, the doctor fails instead of letting you believe it is "ready"; the test suite additionally keeps a snapshot of upstream tool names (`tests/data/upstream_tools.json`), and any allow-list drift fails CI immediately.
- The gateway itself uses MCP Python SDK `>=2,<3`; best and Slacker live in separate venvs so MCP 2.x / 1.x dependencies cannot shadow each other. The MCP SDK 2.x client negotiates with older-protocol backends.
- felix is an optional fallback; even after a successful source build `enable_raw_felix=false` stays the default.

**This package does not claim to replace human engineering review of AutoCAD output.** After the first install, verify units, layers, dimension styles, fonts and plot settings on a test DWG copy before deciding to use it on important drawings.

---

## ChatGPT desktop and the HTTP mode

If you are on an individual Plus plan with the ChatGPT desktop app: **do not treat "the ChatGPT desktop app itself" as a local writable MCP host.** The full writable MCP beta in ChatGPT currently targets Business / Enterprise / Edu, and custom MCP connections point at remote endpoints; local MCP requires a Secure MCP Tunnel. For direct execution on a local AutoCAD 2026 today, the smoothest entrance is still **Codex → CAD Super MCP → AutoCAD**.

This gateway also speaks local Streamable HTTP:

```powershell
.\scripts\start-http.ps1 -Port 8765
```

at:

```text
http://127.0.0.1:8765/mcp
```

It binds to loopback by default (the SDK automatically enables Host/Origin validation for loopback to prevent DNS rebinding); binding another address is refused unless `--allow-remote` is given explicitly — and that endpoint has **no authentication**, so it should only sit behind a trusted tunnel. This exists so a supported secure tunnel/remote access can be added later; **never expose this port to the open internet.**

---

## Known limitations and what still needs a real-machine pass

The 0.2 features were verified against "a fake AutoCAD + the real best/Slacker backend processes (AutoCAD not running)" and have **not yet been checked item by item against a live AutoCAD 2026**. Run the [Smoke test](#smoke-test) on a test DWG; the following deserve particular attention:

- **Read-back verification** depends on the shape of best's `get_entity_properties` response (implemented from its source, parsed per entity type). If your best version answers differently, the result is shown as "could not verify by read-back" (`verified: null`) — it never falsely reports success.
- **`cad_text` rotation/alignment** goes through best (Slacker has neither), using `draw_text` + `set_text_alignment`; the tool adds a warning and suggests a `stage=true` preview first. Unrotated, left-aligned text goes through Slacker and is verified.
- **Polylines** are drawn by Slacker as lightweight 2D polylines; `z` is ignored and read-back verification compares 2D vertices.
- **Layer lineweight/linetype**: neither backend offers layer-level lineweight settings; `cad_layer` can change colour/locked/frozen/on-off only. Use `cad_edit(tool="set_entity_properties")` for per-entity properties.
- **`cad_undo` only undoes entities the gateway itself created/moved**; erases, layer changes and best edits cannot be undone automatically (each is spelled out in the result), and AutoCAD's own UNDO stack is never touched.
- **Persistent sessions**: on error/timeout/idleness/N calls they rebuild automatically; if you ever see strange connection behaviour, switch that backend's `session_mode` to `per_call` for the v0.1 behaviour.
- **Staging preview screenshots**: prefer the best render, fall back to a Slacker window screenshot (the AutoCAD window must not be occluded).
- **Not yet provided**: human confirmation via MCP `elicit` (`confirm` is still a parameter), one-shot plan tokens after `dry_run`, and JSON-Schema pre-validation of arguments. These are on the roadmap.
- One AutoCAD instance: with several AutoCAD instances running, Slacker attaches to "the running one" and the official MCP takes the lowest port — they may not be the same instance.

---

## Changelog

**0.2.0**

- Precision: millimetres/unit strings; point specs; gateway-local geometry (rectangles, wall outlines, dimension-line positions); read-back verification after writes; `cad_measure`; `cad_wall` `cad_rect` `cad_dimension` `cad_text`.
- Collaboration: `cad_context`, stage/preview/commit/discard, `cad_undo`, `cad_name`, `cad_batch`, drawing pinning, narrative summaries, `NEEDS_CLARIFICATION`.
- Performance: persistent backend sessions; fast official-MCP discovery (60 s → <1 s); Slacker/Product Help skip the `server/discover` probe; `cad_health` no longer goes online by default.
- Safety and correctness: `execute_cad_plan` has a single entrance with forced transaction protection; fixed `cad_image_trace(prepare)` failing; raw commands/LISP moved to allow-lists; soft failures reported honestly (including SDK-wrapped dicts); audit with `call_id`, complete cancellation records, and audit-write failures no longer turning into errors; cross-process interlock; best drawing no longer quietly changes the current layer; HTTP refuses non-loopback binds.
- Responses: one envelope; native images; structural trimming of oversized output; stable error codes with advice.
- Tool surface: added `cad_layer`, `cad_document`, `cad_plan_check`, `cad_stage`, `cad_stage_view`, `cad_undo`, `cad_name`, `cad_batch`; `cad_draw`/`cad_edit`/`cad_annotate` `tool` parameters became enums.
- Engineering: installer supports uv/`-Python`, checks every step's exit code, felix became an explicit install, records `install-lock.txt`; `register-codex.ps1` supports `-Force` and TOML-safe escaping; the doctor gained parameter-contract checks and a human-readable summary; 260+ automated tests.

**0.1.0**: first public packaging.

---

## Uninstall

Deleting this directory removes the 3 virtual environments and the gateway. Also delete:

```text
%USERPROFILE%\.cad-super-mcp
```

and remove the `[mcp_servers.codex-CADmcp]` section (plus its `.env` section) from `%USERPROFILE%\.codex\config.toml`.

The default workspace lives at:

```text
%USERPROFILE%\Documents\CAD-Super-Workspace
```

Whether to delete the workspace is your call — it may hold output files, screenshots, SQLite state, entity aliases (`.cad_super\names.json`) and the audit log.

---

## Upstream projects and licenses

This package **does not copy** upstream source and bundles no third-party code; the installer installs/builds those projects on your machine:

| Component | Source | License |
|---|---|---|
| Autodesk AutoCAD/Civil 3D MCP | Autodesk official Tech Preview | governed by Autodesk product/service terms |
| Autodesk Product Help MCP | Autodesk official remote service | governed by Autodesk product/service terms |
| best-cad-mcp | <https://github.com/LokmenoWer/best-cad-mcp> | MIT |
| Slacker-LLC/autocad-mcp | <https://github.com/Slacker-LLC/autocad-mcp> | Apache-2.0 |
| felixalmesberger/AUTOCAD-MCP | <https://github.com/felixalmesberger/AUTOCAD-MCP> | MIT |
| Model Context Protocol Python SDK | <https://github.com/modelcontextprotocol/python-sdk> | MIT |

Respect the upstream licenses and Autodesk's product/service terms individually. This project itself is released under the MIT license — see [LICENSE](LICENSE).

---

## Verification status and disclaimer

Performed so far:

- 260+ automated tests (`pytest`): policy and safety regressions (including every raw probe that historically slipped past the old regexes), Router, point specs and the geometry kernel, read-back verification, staging/undo/batch, config, the doctor, plus **session-pool tests with real MCP subprocesses** and **end-to-end tests over the full MCP protocol** (with a fake AutoCAD); `ruff` static checks pass.
- **Contract checks** against the real `best-cad-mcp 1.7.0` (210 tools in its core profile) and the real Slacker (29 tools): the allow-lists and the parameter names the gateway sends match the real upstream schemas.
- **Smoke runs with the real backend processes** (AutoCAD not running): protocol negotiation, persistent-session reuse, error envelopes and hints, audit fields, process recycling; the PowerShell install/register scripts were exercised for real in a sandbox HOME (including a TOML round-trip).
- Key upstream tool names/protocol behaviour were cross-checked against Autodesk, best-cad-mcp, Slacker, felix and the MCP Python SDK as of 2026-09-15.

Not covered: **on-site testing against real COM / DWG writes** (version 0.2.0, build date 2026-09-26 — see `BUILD_INFO.json`). After installing, `DOCTOR.cmd` and the smoke test on a test DWG are the final must-do steps.
