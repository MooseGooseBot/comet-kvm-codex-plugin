# Comet KVM API & Software Surface Reference

> **Repo:** `Coldaine/comet-kvm-codex-plugin` (fork of `kennypeh85/glkvm-mcp`)
> **Status:** Project-facing summary of upstream contracts this MCP cares about, plus what tools this repo exposes. Curated upstream detail: [`docs/research/glkvm-api-surface.md`](../research/glkvm-api-surface.md). Complete pinned corpus: [`docs/reference/glkvm-api/`](glkvm-api/README.md).
> **Compiled:** 2026-07-07 · **Revised:** 2026-07-27 (auth/discovery/ATX/MSD/OCR/verification)
> **Purpose:** Auth, discovery, important request shapes, live-probe status, and MCP tool tables. Not ops advice (see [`docs/research/oob-proxmox-tailscale-vision.md`](../research/oob-proxmox-tailscale-vision.md)). Not a client-bug tracker (see [`docs/research/glkvm-client-audit-2026-07-15.md`](../research/glkvm-client-audit-2026-07-15.md)).

## Architecture Overview

```
┌──────────────┐     MCP stdio      ┌─────────────────┐     HTTPS/WSS     ┌──────────┐
│  AI Agent    │ ◄─────────────────► │  glkvm_mcp.py   │ ◄───────────────► │  Comet   │
│ (Codex/LLM)  │    tool calls       │  (MCP server)   │   (PiKVM API)     │  (family)│
└──────────────┘                     └─────────────────┘                   └──────────┘
                                             │
                                      kvm_ocr_text
                                             │
                                             ▼
                                      Host Tesseract
                                       (pytesseract)
```

The MCP server uses `glkvm_mcp.py` as a PEP 723 composition entry point and keeps implementation under `src/kvm_core/` and `src/bios_sidecar/`. It is launched via `uv run --locked --python 3.13 python ./glkvm_mcp.py` and runs over stdio. The KVM core maintains a persistent WebSocket connection to the Comet for low-latency input and uses HTTP for screenshots and authentication.

> **Source:** `glkvm_mcp.py`, `src/kvm_core/server.py`, `src/kvm_core/runtime.py`. Verified 2026-07-10.

**Versioning note:** There is no single native “API version.” Prefer connect-time discovery (`/api/upgrade/version`, `/api/info`, `/api/system/capability`, subsystem GETs) over pinning firmware/KVMD/Redfish triples in clients. There is no official OpenAPI for `/api`; the documentary evidence snapshot is pinned to [`gl-inet/glkvm@9bd8ad11`](https://github.com/gl-inet/glkvm/tree/9bd8ad11ba03d220401b0b6a4208bbfd84ed6107), while the connected appliance remains authoritative at runtime. See the [research catalog](../research/glkvm-api-surface.md) and [generated corpus](glkvm-api/README.md).

**JSON envelope:** Most native JSON routes return `{ "ok": true, "result": { ... } }`. Exceptions: JPEG/binary downloads, NDJSON progress streams, and Redfish (`wrap_result=False`).

## Upstream contracts (firmware)

The Comet runs a PiKVM-fork firmware. Paths below are the public `/api/...` forms.

### Authentication

```
POST /api/auth/login
Form body: user=admin&passwd=<password>&expire=0
```

- Default username: `admin`
- Password is passed per-session via `kvm_connect`, or fetched from Doppler CLI (`GLCOMET_ADMIN_PASSWORD` per `doppler.yaml`; legacy alias `COMET_PASSWORD` if present) — never from process environment, never stored server-side
- Success returns `result.token` and sets `auth_token` cookie; may return `two_step_required` / `two_step_token` when two-step login is enabled
- Firmware accepts (among others): **`Token` header** (preferred), `auth_token` cookie, HTTP Basic, `X-KVMD-User` / `X-KVMD-Passwd`, `auth_token` query (avoid — leaks into logs)
- This repo's client stores the cookie token and sends the HTTP `Token` header on subsequent requests; WebSocket auth uses `Cookie: auth_token=...` and `Token` headers (not a query-string token)
- Clean disconnect calls `POST /api/auth/logout`; also `GET /api/auth/check`

> **Source:** pinned [`auth.py`](https://github.com/gl-inet/glkvm/blob/9bd8ad11ba03d220401b0b6a4208bbfd84ed6107/kvmd/apps/kvmd/api/auth.py); `src/kvm_core/comet/client.py` (`CometClient.connect` / `disconnect`). Confidence: **High**.

**Agent traps (observed):** Prefer this MCP or `curl -k` over driving the web UI — Chrome's self-signed-cert interstitial is not automation-attachable (`Cannot attach to this target`; `screenshot`/`read_page` fail with `Frame … showing error page`; `thisisunsafe` needs a focused page and cannot run when attach fails). Guessed login paths (`/api/login`, `/api/auth`, `/api/session`, `/api/user/login`, `/api/v1/login`, `/rpc`, `/cgi-bin/api`, `/api/system`) return **404**; the real route is `POST /api/auth/login` above. Pre-auth, `/api/hid`, `/api/streamer`, and `/api/info` return **401** (they exist). For video, open `WSS /api/ws?stream=true` before snapshots — `stream=false` leaves `streamer` null and can produce snapshot HTTP 503. `stream=true` is necessary in the observed mode, not a guarantee: the RM10 later returned HTTP 503 after earlier successful JPEG reads on 2026-07-27.

### Discovery

Useful GETs before assuming features:

| Path | Role |
|------|------|
| `GET /api/upgrade/version` | Firmware / model reporting |
| `GET /api/info` (optional `fields=`) | System / meta / extras |
| `GET /api/system/capability` | Model capability files |
| `GET /api/hid`, `/api/atx`, `/api/msd`, `/api/streamer`, `/api/recorder` | Subsystem state |
| `GET /api/streamer/ocr` | Inherited PiKVM server-OCR state; discovery-only here, not GL.iNet's browser Text Recognition engine |
| `GET /api/tailscale/status`, `/api/tailscale/config` | Overlay status when present |

MCP today exposes `GET /api/info` via `comet_sysinfo` / `comet_capabilities`. Connect-time discovery also probes `/api/upgrade/version`, `/api/system/capability`, and subsystem GETs. Fuller route inventory remains in the research catalog.

### Keyboard/Mouse WebSocket: `WSS /api/ws`

- Typical query: `stream=true` (requests a live HDMI streamer; binary frames are drained on the socket). `stream=false` is HID-only and leaves `result.streamer` null → snapshot HTTP 503 on Comet/RM10. A `stream=true` session is not itself a successful-frame guarantee; later RM10 snapshot reads also returned HTTP 503.
- Auth: cookie / `Token` header; query `auth_token` also accepted by firmware (prefer header/cookie — this client uses header/cookie)
- Keyboard events: keydown, keyup (with `finish=true` flag)
- Mouse events: button press/release, absolute move (int16 coordinates), wheel scroll
- Application ping must include **both** fields: `{"event_type":"ping","event":{}}`
- A receiver task drains server events (`*_state`, `pong`, `kickout`) and caches the latest subsystem state
- Intentional `kvm_hold_key` holds are watchdog-protected until their release deadline

> **Source:** PiKVM handbook + GLKVM WS handling; `src/kvm_core/comet/client.py`; research catalog. Confidence: **Medium–High**.

### Screenshot / OCR

```
GET /api/streamer/snapshot?preview=<bool>&preview_max_width=<int>&preview_quality=<int>
→ JPEG bytes

GET /api/streamer/ocr
→ inherited PiKVM server-OCR state (legacy observation)
```

Snapshot JPEGs are used by `kvm_screenshot`, `kvm_screenshot_to_file`, and
the host-backed `kvm_ocr_*` tools. **Streamer lifecycle (RM10):** kvmd only keeps
`result.streamer` non-null while at least one WebSocket client is connected with
`stream=true`. With `stream=false`, snapshot returns HTTP 503 even when HDMI
in/out LEDs show a live signal. This client connects with `stream=true`, but a
later 2026-07-27 read still received HTTP 503 after earlier successful JPEGs;
do not treat the connection mode as a full capture qualification.

The MCP now retries snapshot HTTP 503 internally on schedule 0.1/0.2/0.4/0.8s
(five attempts total) before raising a capture-specific error; the session and
its credentials remain valid throughout — this is a streamer-warmup condition,
not an auth failure. Any other HTTP status is raised immediately without
retry. If the session dies mid-retry, capture aborts rather than polling a
dead session. `kvm_status` reports the last capture outcome and retry count.

The PiKVM fork still contains server-side OCR parameters, but GL.iNet firmware
1.9's product UI Text Recognition code crops its canvas and runs bundled
Tesseract.js/WASM in the controlling browser. A live browser recognition
succeeded with the server OCR route disabled and no device OCR socket/process,
proving these are separate paths. The MCP does not call the legacy OCR snapshot
mode.

> **Source:** GLKVM `streamer.py`, `src/kvm_core/comet/client.py`, live probes 2026-07-16. Confidence: **High**.

### ATX power control

**Requires the ATX add-on board.** Without it, calls fail even when authenticated.

Firmware uses **query parameters**, not JSON bodies (**High** confidence — `kvmd/apps/kvmd/api/atx.py`):

| Method | Path | Query |
|--------|------|-------|
| GET | `/api/atx` | — state / LEDs |
| POST | `/api/atx/power` | `action=on\|off\|off_hard\|reset_hard`, optional `wait=true\|false` |
| POST | `/api/atx/click` | `button=power\|power_long\|reset`, optional `wait` |

There is no `action=reset` on `/atx/power` — use `reset_hard`.

```
POST /api/atx/power?action=reset_hard&wait=true
POST /api/atx/click?button=power_long&wait=true
```

MCP aliases: `reset` → `reset_hard`, `force_off` → `off_hard`.

MCP tools: `comet_power_state`, `comet_atx_power`, `comet_atx_click` (see tool table). Treat this section as the **firmware** contract; historical client/doc mismatches are recorded in the [2026-07-15 audit snapshot](../research/glkvm-client-audit-2026-07-15.md).

### Multi-port switch (Comet X / GL-RM4PE only)

**Confidence: High** for routes and query shapes (`kvmd/apps/kvmd/api/switch.py`);
**not live-qualified** by this project. `kvmd/apps/kvmd/__init__.py` constructs
`SwitchApi` only when the hardware model is `rm4pe`, so on a single-port Comet
`GET /api/switch` fails and the client raises `SwitchUnavailable`. Ports are
**0-based** channel indexes; the firmware ignores an out-of-range port silently,
so the MCP validates against `model.ports` first.

| Method | Path | Query |
|--------|------|-------|
| GET | `/api/switch` | — `model.ports[]`, `summary.active_port` / `active_id` / `synced`, `video.links[]`, `usb_otg.links[]`, `edids`, `atx` |
| POST | `/api/switch/set_active` | `port=N` |
| POST | `/api/switch/set_active_prev` / `set_active_next` | — |
| POST | `/api/switch/set_port_params` | `port=N`, any of `name`, `edid_id`, `dummy`, `atx_click_power_delay`, `atx_click_power_long_delay`, `atx_click_reset_delay` |
| POST | `/api/switch/atx/power` | `port=N`, `action=on\|off\|off_hard\|reset_hard` |
| POST | `/api/switch/atx/click` | `port=N`, `button=power\|power_long\|reset` |
| POST | `/api/switch/edids/*`, `/api/switch/set_beacon`, `/api/switch/set_colors`, `/api/switch/reset` | not wrapped by this project |

The WebSocket pushes a `switch` event on change, but third-party hardware
checks saw it arrive late or not at all; `comet_switch_set_active` confirms
with `GET /api/switch` instead. One channel owns HDMI, HID and virtual media at
a time, and **virtual media detaches on every switch**. In the published 1.10.0
source the per-port ATX routes reach a sysfs chain whose `click_power` /
`click_reset` are empty, so whether `/api/switch/atx/*` drives a non-active
channel on shipping firmware is unconfirmed.

MCP tools: `comet_switch_state`, `comet_switch_set_active`,
`comet_switch_set_port_name`, `comet_switch_atx_power`, `comet_switch_atx_click`.

### Mass storage (MSD)

Virtual media lifecycle (**High** confidence — `msd.py`):

1. `POST /api/msd/write?image=<name>` — **raw image body** + `Content-Length` (not multipart)
2. Optional: `POST /api/msd/write_remote?url=...&image=...` — Comet downloads (may stream NDJSON progress)
3. `POST /api/msd/set_params?image=...&cdrom=true&rw=false`
4. `POST /api/msd/set_connected?connected=true|false`
5. `GET /api/msd` — state / images / storage

Also: `remove`, `reset`, partition show/connect/disconnect/format (some are **mutating GETs** — do not prefetch).

MCP tools: `comet_media_state`, `comet_media_upload`, `comet_media_fetch`, `comet_media_mount`, `comet_media_unmount`, `comet_media_remove`, `comet_media_reset` (plus legacy `comet_msd_upload`). Catalog has the complete upstream route list.

### System info

`GET /api/info` — device metadata. Connect-time discovery also probes `/api/upgrade/version`, `/api/system/capability`, and subsystem GETs.

MCP tools: `comet_sysinfo`, `comet_capabilities`.

### GPIO

`GET/POST /api/gpio/*` — low-level GPIO; ATX API usually preferred. No dedicated MCP tool.

### Broader surface

WOL, Redfish (narrow power facade), recorder, Prometheus export, Tailscale config/status, Fingerbot, upgrade/diagnostics, and server-owned streamer controls are curated in [`docs/research/glkvm-api-surface.md`](../research/glkvm-api-surface.md). The [generated corpus](glkvm-api/README.md) contains every source registration; not all are wrapped by this MCP.

## Verification status

What this project has actually exercised against the deployed GL-RM10. The
2026-07-27 evidence was authenticated and read-only. Destructive ATX actions
and MSD uploads were **not** invoked.

The durable per-endpoint record is
[`project-endpoint-coverage.csv`](glkvm-api/project-endpoint-coverage.csv). It
keeps source handler presence, registration, live discovery, live exercise,
hardware requirements, contract tests, and live qualification separate; none of
those fields should be inferred from another.

| Surface | Live tested? | When / notes |
|---------|--------------|--------------|
| `POST /api/auth/login` | Yes, read-only | Authenticated session succeeded 2026-07-27 |
| `GET /api/info`, `/api/upgrade/version`, `/api/system/capability` | Yes, read-only | Successful capability discovery and metadata reads 2026-07-27 |
| `GET /api/hid`, `/api/atx`, `/api/msd`, `/api/streamer` | Yes, read-only | Successful subsystem reads 2026-07-27; HID was not sent and ATX/media were not changed |
| `GET /api/streamer/snapshot` | Mixed | Successful JPEGs in the live smoke; a later read returned HTTP 503 on 2026-07-27 |
| `GET /api/streamer/ocr` | Yes | HTTP 200; `enabled: false` on unit (legacy server OCR; MCP uses host Tesseract) |
| Snapshot OCR (`ocr=true`) | N/A for MCP | Product Text Recognition is browser Tesseract.js; MCP does not use legacy OCR snapshot |
| WebSocket HID + stream | Yes, read-only | MCP uses `?stream=true`; pong/receiver healthy on 2026-07-27; no physical HID effect was tested |
| `GET /api/recorder`, `/api/tailscale/status`, `/api/wol/list` | Yes, read-only | Recorder idle, Tailscale running, and saved WOL list empty on 2026-07-27; no recorder, tailnet, or WOL mutation was sent |
| `GET /api/export/prometheus/metrics` | Failed read | Returned generic HTTP 500 on the initial and repeat 2026-07-27 calls, including through the locked stdio MCP `comet_metrics` tool. The source exporter reads ATX, health/fan, and GPIO state; the RM10 returned an ATX state with LEDs and GPIO HTTP 200, so the server-side cause remains unconfirmed. |
| `POST /api/atx/power` / `click` | **Not live-tested** | No destructive power tests in smoke |
| `POST /api/msd/write` + mount lifecycle | **Not live-tested** | Upload not invoked in 2026-07-10 verification |
| WOL wake, Redfish power, recorder start/stop, Tailscale configuration | **Not live-tested** | Source-derived operations; no mutation was sent |
| `/api/switch/*` (Comet X) | **Not live-tested** | Source-derived; the deployed unit is a single-port GL-RM10 where the route set is absent. Loopback contract tests only |
| Offline CI | Yes | stdio MCP list-tools smoke, executable loopback HTTP/WebSocket contracts, and real Tesseract OCR (see CI) |

Manual live smoke: `.github/workflows/live-smoke.yml` (Doppler + self-hosted
`comet-lan` runner). Direct execution requires explicit
`RUN_LIVE_COMET_SMOKE=1`; otherwise collection skips before credentials or
network access. Preflight without KVM actions: `scripts/comet_preflight.py`.

## This repo’s MCP tools

This section is **this repo’s tool surface**, not the full firmware catalog. Keep it separate from the upstream-contract sections above.

### Connection
| Tool | Signature | Annotations | Description |
|------|-----------|-------------|-------------|
| `kvm_connect` | `(host?, password?, username?, target="default", force_reconnect?)` | write, non-destructive, idempotent | Optional override — device tools auto-connect the default target on demand. Omitted host/username resolve to the managed default; a live session matching the requested host **and** username is reused (`reused: true`, no Doppler call) — a same-host request under a different username replaces the session; `force_reconnect=true` replaces it unconditionally. Omitted password fetches `GLCOMET_ADMIN_PASSWORD` from Doppler CLI |
| `kvm_disconnect` | `(target?)` | write, non-destructive, idempotent | Close one target or all sessions. Non-sticky: not required cleanup, the next device operation reconnects the default automatically |
| `kvm_select_target` | `(target)` | write, non-destructive, idempotent | Select the active multi-Comet target for subsequent tools |
| `kvm_status` | `(target?)` | read-only, non-destructive, idempotent | Report managed defaults, auto-connect state, capture diagnostics, held keys, and target list. Never connects |

### Keyboard
| Tool | Signature | Annotations | Description |
|------|-----------|-------------|-------------|
| `kvm_send_text` | `(text, wpm?)` | write, destructive | Type a string (atomic press pattern) |
| `kvm_terminal_run` | `(command, timeout_seconds=30, poll_interval_seconds=0.5)` | write, destructive | POSIX-only, isolated `sh -c` command with OCR-confirmed submission and bounded visual observation. `timeout_seconds` is capped at 300 and `poll_interval_seconds` must be at least 0.1. It refuses submission if HID skips input characters. Returns `completed`, `timeout`, or `not_submitted`; output is best-effort visible-console text, never exact stdout/stderr. |
| `kvm_send_keys` | `(combo)` | write, destructive | Send key chord (e.g. "Ctrl+Alt+Delete") |
| `kvm_hold_key` | `(key, duration_ms)` | write, destructive | Press and hold (for auto-repeat scrolling) |
| `kvm_release_all` | `()` | write, destructive, idempotent | Force-release all held keys |

### Mouse
| Tool | Signature | Annotations | Description |
|------|-----------|-------------|-------------|
| `kvm_mouse_move` | `(x, y)` | write, destructive | Absolute int16 coordinates |
| `kvm_mouse_move_pct` | `(x_pct, y_pct)` | write, destructive | Percentage of screen (0,0 = top-left) |
| `kvm_mouse_click` | `(button?, count?)` | write, destructive | Click at current position |
| `kvm_mouse_scroll` | `(dx?, dy?)` | write, destructive | Scroll wheel |

### Screenshot / OCR
| Tool | Signature | Annotations | Description |
|------|-----------|-------------|-------------|
| `kvm_screenshot` | `(preview?, max_width?, quality?)` | read-only, non-destructive, idempotent | JPEG as MCP image content |
| `kvm_screenshot_to_file` | `(path, preview?, ...)` | read-only, non-destructive, idempotent | Save JPEG to disk |
| `kvm_ocr_status` | `()` | read-only, non-destructive, idempotent | Host Tesseract status plus browser-only product UI engine metadata |
| `kvm_ocr_text` | `(psm?, languages?, left?, top?, right?, bottom?)` | read-only, non-destructive, idempotent | Host Tesseract text OCR with optional language and crop |
| `kvm_ocr_screenshot` | `(search_text?, preview?, psm?)` | read-only, non-destructive, idempotent | Capture + Tesseract OCR → ordered text/lines plus word coordinates; `psm=6` suits terminals |
| `kvm_ocr_click` | `(text, button?, count?, search_area?)` | write, destructive | OCR-find text → click it (all-in-one) |

> **Source:** `src/kvm_core/tools.py` (`@mcp.tool` registrations and annotations). Verified 2026-07-10.

### Comet Hardware
| Tool | Signature | Annotations | Description |
|------|-----------|-------------|-------------|
| `comet_power_state` | `(target?)` | read-only | GET `/api/atx` |
| `comet_atx_power` | `(action, wait?, target?)` | write, destructive | Query-param ATX power (`reset`→`reset_hard`) |
| `comet_atx_click` | `(button, wait?, target?)` | write, destructive | `power` / `power_long` / `reset` |
| `comet_sysinfo` | `(target?)` | read-only | Device metadata |
| `comet_capabilities` | `(refresh?, target?)` | read-only | Connect-time capability profile |
| `comet_msd_upload` | `(local_path, image_name?, target?)` | write, destructive | Raw streaming MSD upload |
| `comet_media_*` | — | — | state/upload/fetch/mount/unmount/remove/reset |
| `comet_wol_*` | — | — | list/scan (`GET`) / wake (`POST /api/wol/wake`) |
| `comet_streamer_*` / `comet_recorder_*` | — | — | stream + recording controls |
| `comet_metrics` | `(target?)` | read-only | Prometheus metrics text |
| `comet_tailscale_status` | `(target?)` | read-only | Tailscale status |
| `comet_redfish_power` | `(reset_type, target?)` | write, destructive | Redfish ComputerSystem.Reset |
| `comet_switch_state` | `(target?)` | read-only | GET `/api/switch` (Comet X only) |
| `comet_switch_set_active` | `(port, confirm_timeout_s?, target?)` | write, destructive | Select the active channel, confirm by re-reading state |
| `comet_switch_set_port_name` | `(port, name, target?)` | write | `set_port_params?name=` |
| `comet_switch_atx_power` | `(port, action, target?)` | write, destructive | Per-port ATX power (`reset`→`reset_hard`) |
| `comet_switch_atx_click` | `(port, button, target?)` | write, destructive | Per-port `power` / `power_long` / `reset` |

This table is **MCP tool names only**. Curated upstream facts stay in the [research catalog](../research/glkvm-api-surface.md); the full route set stays in the [generated corpus](glkvm-api/README.md). Historical client mismatches (if any) are snapshotted in the [2026-07-15 audit](../research/glkvm-client-audit-2026-07-15.md) — do not treat audit blockers as current API facts.

### BIOS Workflow (sidecar)
| Tool | Signature | Description |
|------|-----------|-------------|
| `bios_observe_state` | `()` | Capture, parse, and sync current BIOS position |
| `bios_crawl_step` | `()` | Single safe crawl transition (debug) |
| `bios_crawl_region` | `(max_depth?)` | DFS region crawl with cycle detection |
| `bios_navigate_to` | `(target_node_id)` | Replay graph path to target node |
| `bios_propose_setting_change` | `(capability_id, desired_value)` | Plan a setting change |
| `bios_apply_setting_change` | `(capability_id, desired_value)` | Apply mutation with verification |
| `bios_save_and_reboot` | `()` | F10 save with dialog verification, reboot |
| `bios_abort_and_recover` | `()` | Release keys and Escape back-out |
| `bios_export_trace` | `()` | Export replayable run trace JSON |

### Perception (sidecar)
| Tool | Signature | Description |
|------|-----------|-------------|
| `kvm_vlm_parse` | `(screenshot_ref, previous_state_id?, last_action?)` | VLM structured parse of cached screenshot |
| `kvm_match_screen` | `(screenshot_ref, expected_node_id?)` | Local phash + OCR fingerprint graph match |

### MCP Resources (sidecar)
| URI | Returns | Description |
|-----|---------|-------------|
| `bios://state/current` | JSON string | Latest normalized BIOS state |
| `bios://screen/current` | bytes | Current screenshot bytes (prefer `kvm_screenshot` / `kvm_screenshot_to_file` for agent-facing capture) |
| `bios://graph/current` | JSON string | Navigation graph summary |
| `bios://capabilities/current` | JSON string | Discovered settings index |

See [`docs/kvm-core.md`](../kvm-core.md) for the BIOS interaction lifecycle.

## External References

| Source | What it covers |
|--------|----------------|
| [docs/research/README.md](../research/README.md) | Research index + homes map |
| [docs/reference/glkvm-api/](glkvm-api/README.md) | Complete generated endpoint/event inventory, immutable sources, and project coverage status |
| [docs/research/glkvm-api-surface.md](../research/glkvm-api-surface.md) | Curated request shapes, auth detail, and exact source permalinks |
| [docs/research/oob-proxmox-tailscale-vision.md](../research/oob-proxmox-tailscale-vision.md) | Ops vision (judgment; not API facts) |
| [docs/research/glkvm-client-audit-2026-07-15.md](../research/glkvm-client-audit-2026-07-15.md) | Dated client-vs-firmware audit snapshot |
| [PiKVM API docs](https://docs.pikvm.org/api/) | Canonical PiKVM HTTP/WebSocket API (Comet firmware is a fork) |
| [GL.iNet KVM docs](https://docs.gl-inet.com/kvm/) | Comet product documentation and user guides |
| [gl-inet/glkvm@9bd8ad11](https://github.com/gl-inet/glkvm/tree/9bd8ad11ba03d220401b0b6a4208bbfd84ed6107) | Pinned firmware-source evidence; handlers live in `api/*.py` and `server.py` |
| [kennypeh85/glkvm-mcp](https://github.com/kennypeh85/glkvm-mcp) | Upstream MCP server this repo forked from |

## Internal Background Tasks (Asyncio)

The MCP process runs **two background asyncio loops** for transport reliability:

### `_watchdog_loop` (40ms period)
- Monitors held keys
- Force-releases any key still tracked as held after `STALE_S` (250ms)
- Prevents stuck keys from input sequences that were interrupted or failed

### `_pinger_loop` (1s period)
- Sends WebSocket application pings to keep the connection alive
- Detects dropped connections

> **Source:** `src/kvm_core/comet/client.py` (`_watchdog_loop` and `_pinger_loop`). Verified 2026-07-10.

**Design implication:** These loops are transport reliability mechanisms. The BIOS state tracker remains on demand; it is not an always-on third screenshot/OCR loop. `kvm_terminal_run` likewise polls only for its active tool call and discards its transcript when it returns. See `docs/decisions.md` D7 and D-K7.

## Known Firmware Bugs & Workarounds

### Stuck Key / Double-Typing (Firmware ≤ 1.9.0)
- **Bug:** Characters sent rapidly can double-type or get stuck in the down state
- **Fix in client:** Every character is sent as an atomic `keydown → 25ms → keyup(finish=true)` pattern
- **Tunables:** `MIN_DOWN_UP_GAP_S = 0.025`, `INTER_CHAR_GAP_S = 0.010`

### Modifier Release Order Bug (gl-inet/glkvm #22)
- **Bug:** Modifiers released in wrong order relative to the main key
- **Fix:** Modifiers wrap strictly outside the main key: `mods down → key down → key up → mods up`

### Stale Key Watchdog
- **Prevention:** Any key held >250ms is auto-released by the watchdog loop
- **Recovery tool:** `kvm_release_all` force-releases everything — should be called after any failed or interrupted input sequence

> **Sources:**
> - `src/kvm_core/comet/client.py` (HID timing fields, atomic press, modifier wrapping, watchdog). Verified 2026-07-10.
> - `README.md#firmware-bug-fixes`. Verified 2026-07-07.

## OCR Integration

All MCP OCR runs in the host process:

- Tesseract binary is located via `TESSERACT_PATH`/`TESSERACT_CMD` env vars, then `PATH`, then Windows default paths
- `kvm_ocr_status` reports host availability and explicitly marks GL.iNet's Tesseract.js UI engine as browser-only/unavailable to MCP
- `kvm_ocr_text` captures a JPEG and uses host `image_to_string` with preserved inter-word spacing
- `kvm_ocr_screenshot` captures a frame, passes it to Tesseract, and returns structured JSON with word coordinates
- `kvm_ocr_click` finds text by name and clicks its exact coordinates
- Pillow supplies decoded image dimensions; pytesseract is bounded to 15 seconds and runs off the MCP asyncio loop

### Browser Text Recognition versus inherited server OCR

The fork exposes inherited PiKVM server-OCR state at `/api/streamer/ocr`, which
may mention engines such as `tesseract` or `rknn`. That is not the implementation
behind GL.iNet firmware 1.9's web UI Text Recognition feature. Inspection of the
served product bundle shows `Tesseract.createWorker(...)` and
`worker.recognize(...)` operating on a browser canvas crop. Live recognition
also succeeds while server OCR is disabled. Therefore RKNN enablement would be
a separate firmware experiment, not a way for this Python process to invoke the
existing product UI OCR.

> **Source:** `src/kvm_core/ocr.py`, `src/kvm_core/tools.py`, and live probes. Verified 2026-07-10.

## Security Model

- **LAN only** — designed for trusted local networks
- **TLS verification disabled** — device ships with self-signed certificate; `verify=False` in httpx client
- **No credentials in repo** — secrets are never committed, logged, or stored in files. The Comet admin password is fetched at connect time from Doppler CLI as `GLCOMET_ADMIN_PASSWORD` (`doppler.yaml` → `homelab`/`dev`). Process-env injection is not used for that secret. Agent browser/login traps are under [Authentication](#authentication) above.
- **stdio exposure warning** — do not expose the MCP server's stdio to a remote agent without confirming the target host is on a trusted network
- **Remote access options:** Tailscale (supported on Comet models that ship the integration), GL.iNet cloud service (`glkvm.com`), or VPN — topology judgment is **not** documented here; see the [OOB vision](../research/oob-proxmox-tailscale-vision.md) if needed

### Credentials and environment

Device tools auto-connect the default target on demand, so most sessions never call `kvm_connect` explicitly. When `kvm_connect` (or the auto-connect path) does need a password and none is passed explicitly, it calls the Doppler CLI once per process — the resolved value is cached in-process, so subsequent connects (auto or explicit) reuse the cached password instead of re-shelling to Doppler. A `kvm_connect` call whose requested host matches an already-live session for that target skips Doppler entirely (`reused: true`). The blocker for a first resolution is: Doppler installed + authenticated to the project/config in `doppler.yaml`. Optional non-secret overrides:

| Variable | Secret? | Required | Default | Description |
|---|---|---|---|---|
| `COMET_HOST` | no | no | `192.168.0.126` | LAN IP of the Comet (live tests / scripts) |
| `COMET_USERNAME` | no | no | `admin` | Comet login username |
| `COMET_DISABLE_BIOS_SIDECAR` | no | no | unset | Set to `1` to skip loading `bios_sidecar` |
| `VLM_API_KEY` | **yes** | for `openrouter`/`openai` only | — | OpenAI-compatible API key; falls back to Doppler secret `OPENROUTER_API_KEY` when unset. `codex` and `aperture` need no key |
| `VLM_PROVIDER` | no | **yes for BIOS perception** | — | `codex` \| `aperture` \| `openrouter` \| `ollama` \| `vllm` \| `openai`; missing/unsupported values fail closed. `codex` shells out to the local Codex CLI (`codex exec`, ChatGPT-subscription auth); `aperture` targets the tailnet Aperture gateway (tailnet-identity auth) |
| `VLM_MODEL` | no | no | provider default; codex → `gpt-5.6-sol`; aperture → `gemini-oai/gemini-3.5-flash`; openrouter → `openrouter/free` (Free Models Router) | Model string for the OpenAI-compatible endpoint (or `codex exec -m`) |
| `VLM_CODEX_EFFORT` | no | no | `medium` | Reasoning effort for the `codex` sub-agent (`codex exec -c model_reasoning_effort=...`) |
| `VLM_BASE_URL` | no | no | provider default | Override API endpoint |

The `codex` sub-agent runs **hermetically** (`--ephemeral --ignore-user-config -s read-only` in a
throwaway working directory): the operator's `~/.codex/config.toml` is deliberately not loaded, so a
personal `notify` hook cannot be spawned once per screen parse and personal plugins/MCP clients do not
load per parse. Auth is unaffected — it resolves from `$CODEX_HOME/auth.json` (`auth_mode: chatgpt`),
never from an API key. `OPENAI_API_KEY`/`OPENAI_BASE_URL` are stripped from the child environment so an
ambient key can never silently move parses onto metered API billing. Because the config is ignored, the
model and reasoning effort are pinned by this repo (table above).

Doppler secret name: **`GLCOMET_ADMIN_PASSWORD`** (legacy alias `COMET_PASSWORD` only if you add it later).

#### MCP Client Config Example

```json
{
  "mcpServers": {
    "comet-kvm": {
      "command": "uv",
      "args": ["run", "--locked", "--python", "3.13", "python", "./glkvm_mcp.py"]
    }
  }
}
```

The host must have Doppler CLI logged in (`doppler login`). The bundled [`.mcp.json`](../../.mcp.json) launches with `uv run --locked --python 3.13 python ./glkvm_mcp.py` and does **not** wrap the process in `doppler run` for env injection.

> **Source:** `src/kvm_core/doppler_credentials.py`, `src/kvm_core/tools.py` (`kvm_connect`), `.mcp.json`, `doppler.yaml`. Verified 2026-07-16.

## Runtime Composition Assessment

`glkvm_mcp.py` contains PEP 723 dependency metadata and imports the two registration layers. `src/kvm_core/` owns the shared FastMCP instance, Comet HTTP/WebSocket client, HID workarounds, capture, OCR, logging, runtime, and universal tools. `src/bios_sidecar/` owns BIOS state, graph, perception, trace, controller, resources, and `bios_*` tool registration.

This is one MCP process with managed physical Comet sessions, not separate KVM and BIOS servers. The runtime owns one session per connected target; the modular boundary keeps universal KVM behavior usable without introducing BIOS semantics into transport code. See `docs/decisions.md` D6.

> **Source:** `glkvm_mcp.py`, `src/kvm_core/`, and `src/bios_sidecar/`. Verified 2026-07-10.
