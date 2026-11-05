# Comet KVM Plugin

| | |
|---|---|
| **This repo** | [`Coldaine/comet-kvm-codex-plugin`](https://github.com/Coldaine/comet-kvm-codex-plugin) |
| **Forked from** | [`kennypeh85/glkvm-mcp`](https://github.com/kennypeh85/glkvm-mcp) (upstream MCP server) |
| **Relationship** | Selective fork — occasionally review upstream for bug fixes, but this repo diverges strongly and is its own project |

This repository develops and ships a **Comet KVM MCP server** for physical-machine operation and recovery, packaged for Codex as a plugin. Its primary path is console/HID, screenshots and OCR, power/WOL where available, virtual media, appliance diagnostics, and private remote access. BIOS-aware tools remain available as a separate specialist lane; they are not a prerequisite for ordinary Comet work. Not VM orchestration or general-purpose remote desktop.

**Primary distribution target: Codex.** The MCP server itself is usable from any MCP client; Codex packaging is first. See [`docs/NORTH_STAR.md`](docs/NORTH_STAR.md) for goals.

---

## What ships where

A Codex plugin is an installable bundle. For this project that bundle is:

| Plugin payload | Role |
|---|---|
| `.codex-plugin/plugin.json` | Manifest — identity + pointers |
| `.mcp.json` | How Codex launches **this repo's** MCP server |
| `.claude/skills/` | General Comet operations and specialized BIOS driver playbooks |
| `glkvm_mcp.py` + `src/` | The MCP server implementation the launcher runs |

That is the whole plugin shape: **your MCP + skill(s)**. It is not a thin wrapper around upstream. Upstream (`kennypeh85/glkvm-mcp`) was the starting fork; this tree owns and augments the server.

### Not part of the plugin

These live in the repo for development and local agent work. They are **not** Codex plugin components:

| Repo surface | Role |
|---|---|
| `AGENTS.md` | Thin router into `docs/` for developer agents working *in* this repo |
| `docs/` | Project authority and design docs |
| `scripts/`, `tests/`, `extras/` | Local tooling, tests, preserved upstream helpers |

`AGENTS.md` is a thin router into `docs/` (Codex loads it from the repo). Skills are workflows. MCP is tools. The plugin packages skills + MCP — not `AGENTS.md`.

### Repo layout

```
comet-kvm-codex-plugin/
├── .codex-plugin/
│   └── plugin.json          # Codex plugin manifest → skills + MCP
├── .mcp.json                # Launches this repo's MCP server
├── glkvm_mcp.py             # PEP 723 MCP entry point
├── src/
│   ├── kvm_core/            # Universal KVM transport, OCR, tools, runtime
│   └── bios_sidecar/        # BIOS-aware tools (default on; one-way dep on kvm_core)
├── .claude/
│   └── skills/              # Bundled driver skills (plugin payload; repo-scoped for Claude Code)
│       ├── comet-kvm-operations/
│       └── comet-bios-triage/
├── AGENTS.md                # Thin router into docs/ (not plugin payload)
├── docs/                    # Design / authority docs (not plugin payload)
├── scripts/                 # Local tooling (preflight, run ledger)
├── extras/                  # Upstream helpers (calibration, click helper, userscript)
├── runs/                    # Experiment records (gitignored)
├── state/                   # Runtime state (gitignored)
└── tests/
```

### Manifest

`.codex-plugin/plugin.json` points at the plugin payload only:

```json
{
  "skills": "./.claude/skills/",
  "mcpServers": "./.mcp.json"
}
```

`.mcp.json` starts `glkvm_mcp.py` via `uv run --locked --python 3.13 python ./glkvm_mcp.py`. The server code that runs is this project's — `kvm_core` plus `bios_sidecar` (loaded by default; set `COMET_DISABLE_BIOS_SIDECAR=1` to skip) on one `FastMCP("comet-kvm")` process. `kvm_connect` resolves an omitted password through the Doppler CLI; the launcher does not inject secret environment variables. Dependency direction is one-way: sidecar may depend on KVM core, not vice versa.

---

## Current Scope

The **core** is the universal KVM MCP server: transport, session/auth, HID,
screenshots/OCR, media, appliance control, and plugin packaging. That is the
normal route for a Comet task.

The **firmware specialist lane** is the BIOS sidecar: cartography, navigation,
mutation, and optional HWiNFO-backed validation for a named machine. It is
entered only for an explicit firmware request and does not gate the core
product. Board-specific procedure lives in `.claude/skills/comet-bios-triage/`.

See:
- [`docs/NORTH_STAR.md`](docs/NORTH_STAR.md) — durable goals and anti-goals
- [`docs/architecture.md`](docs/architecture.md) — system shape, maturity, cartography spike
- [`docs/kvm-core.md`](docs/kvm-core.md) — KVM MCP server architecture, tool surface, and KVM/BIOS sidecar boundary
- [`docs/decisions.md`](docs/decisions.md) — implementation decisions
- [`docs/vlm-prompt-contract.md`](docs/vlm-prompt-contract.md) — VLM prompt draft + justification
- [`docs/reference/comet-hardware.md`](docs/reference/comet-hardware.md) — verified Comet hardware/platform facts
- [`docs/reference/comet-api.md`](docs/reference/comet-api.md) — verified Comet API/software surface
- [`docs/reference/glkvm-api/`](docs/reference/glkvm-api/README.md) — pinned 200-route source corpus and project coverage map
- [`docs/workflows/live-hardware-qualification.md`](docs/workflows/live-hardware-qualification.md) — disposable-node / MSI proof runbook
---

## Installation

### Prerequisites

- Python >= 3.10
- [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki) installed on the host
- A GL.iNet Comet Pro (GL-RM10) or PiKVM-compatible device on your LAN (firmware 1.9.0+)
- [uv](https://docs.astral.sh/uv/) for running the MCP server
- [Doppler CLI](https://docs.doppler.com/docs/install-cli) configured for `homelab/dev` when using the bundled plugin launcher

### Install Tesseract

```bash
# Windows
choco install tesseract-ocr

# macOS
brew install tesseract

# Linux (Debian/Ubuntu)
sudo apt-get install tesseract-ocr
```

### Use in Codex

The plugin is auto-discovered when the repo is installed as a Codex plugin. Its bundled launcher runs `uv run --locked --python 3.13 python ./glkvm_mcp.py`. `kvm_connect` fetches `GLCOMET_ADMIN_PASSWORD` from the Doppler CLI (`doppler.yaml`); the host must have Doppler installed and authenticated.

**Launcher note:** The bundled [`.mcp.json`](.mcp.json) does not wrap the process in `doppler run` for env injection. Doppler CLI auth on the host is required for password resolution. A future portable credential-elicitation path remains a candidate in [`docs/plans/02-mcp-v2-migration-evaluation.md`](docs/plans/02-mcp-v2-migration-evaluation.md).

### Use as a standalone MCP server

Add to any MCP client config:

```json
{
  "mcpServers": {
    "comet-kvm": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/repo", "--locked", "--python", "3.13", "python", "./glkvm_mcp.py"]
    }
  }
}
```

---

## MCP Tools

### Connection
| Tool | Description |
|------|-------------|
| `kvm_connect(host?, password?, username?, target?, force_reconnect?)` | Optional override — device tools auto-connect to the managed default; a matching live session returns `reused: true`. Omitted password is fetched from Doppler CLI (`GLCOMET_ADMIN_PASSWORD`) and cached in-process |
| `kvm_disconnect(target?)` | Close one target or all sessions (non-sticky: the next device tool reconnects the default) |
| `kvm_select_target(target)` | Select the active multi-Comet target |
| `kvm_status()` | Report connection state, managed defaults, capture diagnostics, held keys, and targets — never connects |

### Keyboard
| Tool | Description |
|------|-------------|
| `kvm_send_text(text, wpm?)` | Type a string (atomic press pattern fixes stuck-key bug) |
| `kvm_terminal_run(command, timeout_seconds?, poll_interval_seconds?)` | Run one POSIX command through the visible console with bounded, best-effort OCR output; not exact stdout/stderr |
| `kvm_send_keys(combo)` | Send a key chord (e.g. "Ctrl+Alt+Delete", "F5", "Win+L") |
| `kvm_hold_key(key, duration_ms)` | Press and hold a key (for auto-repeat scrolling) |
| `kvm_release_all()` | Force-release all held keys |

### Mouse
| Tool | Description |
|------|-------------|
| `kvm_mouse_move(x, y)` | Move to absolute int16 coordinates |
| `kvm_mouse_move_pct(x_pct, y_pct)` | Move to percentage of screen (0,0 = top-left) |
| `kvm_mouse_click(button?, count?)` | Click at current position |
| `kvm_mouse_scroll(dx?, dy?)` | Scroll the mouse wheel |

### Screenshot / OCR
| Tool | Description |
|------|-------------|
| `kvm_screenshot(preview?, max_width?, quality?)` | Capture JPEG frame as MCP image content |
| `kvm_screenshot_to_file(path, ...)` | Capture and save to disk |
| `kvm_ocr_status()` | Report MCP host Tesseract status and the browser-only product UI engine |
| `kvm_ocr_text(psm?, languages?, left?, top?, right?, bottom?)` | Host Tesseract visible-text OCR with optional crop |
| `kvm_ocr_screenshot(search_text?, preview?, psm?)` | Host Tesseract OCR with ordered text/lines plus word coordinates |
| `kvm_ocr_click(text, button?, count?, search_area?)` | Find text via OCR and click it |

### Comet appliance and power
| Tool | Description |
|------|-------------|
| `comet_power_state(target?)` | Read ATX power/LED state |
| `comet_atx_power(action, wait?, target?)` | Power on/off/reset through the ATX add-on board |
| `comet_atx_click(button, wait?, target?)` | Momentary power/reset button pulse through the ATX add-on board |
| `comet_switch_state(target?)` | Comet X (GL-RM4PE) only: ports, active channel, per-port video and USB link state |
| `comet_switch_set_active(port, confirm_timeout_s?, target?)` | Comet X only: make a 0-based channel own video, HID and media, confirmed by re-reading state |
| `comet_switch_set_port_name(port, name, target?)` | Comet X only: name a channel |
| `comet_switch_atx_power(port, action, target?)` / `comet_switch_atx_click(port, button, target?)` | Comet X only: per-port ATX board actions |
| `comet_sysinfo(target?)` | Retrieve device metadata |
| `comet_capabilities(refresh?, target?)` | Discover and cache the connected unit's supported subsystems |
| `comet_streamer_state(target?)` | Read capture/stream state |
| `comet_streamer_set_params(..., target?)` | Change supported stream parameters |
| `comet_metrics(target?)` | Read Prometheus appliance metrics |
| `comet_tailscale_status(target?)` | Read the Comet's Tailscale status |
| `comet_redfish_power(reset_type, target?)` | Invoke the narrow Redfish power facade |

### Virtual media, WOL, and recording

| Tool | Description |
|------|-------------|
| `comet_media_state(target?)` | Read virtual-media inventory and connection state |
| `comet_media_upload(local_path, image_name?, target?)` | Stream a host file to the Comet image store |
| `comet_media_fetch(url, image_name, target?)` | Ask the Comet to fetch an image from an approved URL |
| `comet_media_mount(image_name, cdrom?, rw?, target?)` | Select and connect an image |
| `comet_media_unmount(target?)` | Disconnect virtual media |
| `comet_media_remove(image_name, target?)` | Delete a selected image |
| `comet_media_reset(target?)` | Recover a stuck media subsystem |
| `comet_wol_list` / `comet_wol_scan` / `comet_wol_wake` | Discover saved targets and send Wake-on-LAN |
| `comet_recorder_state` / `start` / `stop` | Capture a bounded console recording |

`comet_msd_upload` remains as the legacy upload alias. Exact schemas and the
complete tool table live in [`docs/reference/comet-api.md`](docs/reference/comet-api.md).

### BIOS Workflow (sidecar)

| Tool | Description |
|------|-------------|
| `bios_observe_state()` | Capture and parse current BIOS screen; sync position tracker |
| `bios_crawl_step()` | Execute one safe crawl transition (debug single-step) |
| `bios_crawl_region(max_depth?)` | DFS crawl of current BIOS region with cycle detection |
| `bios_navigate_to(target_node_id)` | Replay stored graph path to a target node |
| `bios_propose_setting_change(capability_id, desired_value)` | Plan and validate a setting change |
| `bios_apply_setting_change(capability_id, desired_value)` | Apply mutation with visual verification |
| `bios_save_and_reboot()` | F10 save with dialog verification, then reboot |
| `bios_abort_and_recover()` | Release keys and Escape back-out of modals |
| `bios_export_trace()` | Export replayable run trace JSON |

### Perception (sidecar)

| Tool | Description |
|------|-------------|
| `kvm_vlm_parse(screenshot_ref, previous_state_id?, last_action?)` | VLM structured parse of a cached screenshot |
| `kvm_match_screen(screenshot_ref, expected_node_id?)` | Local phash + OCR fingerprint match against graph |

### MCP Resources (sidecar)

| URI | Description |
|-----|-------------|
| `bios://state/current` | Latest normalized BIOS state (JSON) |
| `bios://screen/current` | Current screenshot bytes (MCP resource returns raw bytes; prefer `kvm_screenshot` / `kvm_screenshot_to_file` for agent-facing capture) |
| `bios://graph/current` | Navigation graph summary (nodes + edges) |
| `bios://capabilities/current` | Discovered settings capability index |

See [`docs/kvm-core.md`](docs/kvm-core.md) for the BIOS interaction lifecycle.

### Deprecated Aliases

The `comet_raw_*` aliases currently duplicate `kvm_*` tools. They are deprecated in documentation only; the tools still exist and removal is a future code task.

---

## Architecture

```
┌──────────────┐     MCP stdio      ┌─────────────────┐     HTTPS/WSS     ┌──────────┐
│  AI Agent    │ ◄─────────────────► │  glkvm_mcp.py   │ ◄───────────────► │  Comet   │
│ (Codex)      │    tool calls       │  (MCP server)   │   (PiKVM API)     │  (family)│
└──────────────┘                     └─────────────────┘                   └──────────┘
                                             │
                                      kvm_ocr_text
                                             │
                                             ▼
                                      Host Tesseract
                                       (pytesseract)
```

The GL.iNet 1.9 web UI's **Text Recognition** button is a separate
Tesseract.js/WASM worker running in the controlling browser. It is not executed
by the Comet and is not callable by this Python MCP process. The inherited
PiKVM `/api/streamer/ocr` route is retained only as a discovery observation;
the MCP does not treat it as the product UI OCR backend.

The MCP server maintains a persistent WebSocket connection to the Comet for low-latency keyboard/mouse input, and uses HTTP for screenshots, authentication, ATX, sysinfo, and MSD upload. It runs background key-watchdog and WebSocket-pinger loops.

Runtime logs go to stderr and to the rotating file `state/logs/comet-kvm.log`. Set `COMET_LOG_LEVEL` or `COMET_LOG_DIR` to override the default level or directory.

See [`docs/kvm-core.md`](docs/kvm-core.md) for the KVM core architecture and [`docs/reference/comet-api.md`](docs/reference/comet-api.md) for verified Comet API details.

---

## Security

- **LAN only** — designed for trusted local networks
- **TLS verification disabled** — the Comet ships with a self-signed certificate
- **No credentials stored** — password is passed per-session or fetched from Doppler CLI (`GLCOMET_ADMIN_PASSWORD` only)
- **Remote access** — use Tailscale (native on Comet) or VPN; do not expose the MCP server's stdio to an untrusted network

---

## Firmware Bug Fixes

This server includes fixes for known GLKVM/PiKVM firmware bugs:
- **Stuck key / double-typing** (firmware <= 1.9.0): every character sent as atomic keydown -> 25ms -> keyup(finish=true)
- **Modifier release order bug** (gl-inet/glkvm #22): modifiers wrap strictly outside the main key
- **Stale key watchdog**: auto-releases any key held >250ms

---

## Upstream Relationship

This repo is a selective fork of [`kennypeh85/glkvm-mcp`](https://github.com/kennypeh85/glkvm-mcp):

- **Upstream** is a standalone MCP server for GLKVM/Comet keyboard/mouse/screenshot/OCR.
- **This repo** ships its **own** MCP server (forked code under `src/`, composed by `glkvm_mcp.py`), augments it (BIOS sidecar, host OCR, Comet hardware tools, etc.), and packages that server for Codex with skills.

We are not wrapping upstream as an external dependency. We keep a fetch-only `upstream` remote to cherry-pick bug fixes or API improvements when useful. This repo is not a mirror and does not track upstream releases.

Helpers that lived at upstream's repo root (calibration, click helper, stuck-key userscript) are preserved in [`extras/`](extras/) — useful, not plugin payload.

## License

MIT
