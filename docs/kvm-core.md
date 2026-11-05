# KVM MCP Server Architecture

> **Repo:** `Coldaine/comet-kvm-codex-plugin`
> **Status:** Current product framing for the universal KVM MCP core.

The KVM MCP server is the universal physical-control substrate and the product's
primary path. The BIOS sidecar is a separate specialist lane (loaded in the
same process for compatibility; set `COMET_DISABLE_BIOS_SIDECAR=1` to skip) for
explicit firmware work. Dependency direction is one-way: sidecar may depend on
KVM core, never the reverse.

## 1. Overview

The KVM MCP server is a hardened fork of `kennypeh85/glkvm-mcp` that exposes a
GL.iNet Comet-family KVM device's keyboard, mouse, screenshot, OCR, and
hardware-control capabilities as MCP tools, plus host-process OCR
(`kvm_ocr_*`) over captured frames.

It is a stdio MCP server intended to run from `glkvm_mcp.py` with `uv run --locked --python 3.13 python ./glkvm_mcp.py`. The entry point composes universal tools from `src/kvm_core/` and BIOS-aware tools from `src/bios_sidecar/` (loaded by default; `COMET_DISABLE_BIOS_SIDECAR=1` skips sidecar registration) against one shared MCP server. The KVM core owns the physical session; the sidecar delegates to it rather than duplicating transport state.

## 2. Connection Model

The server maintains one physical I/O session per target and manages the
default target's lifecycle automatically.

| Channel | Purpose |
|---------|---------|
| HTTP(S) | Authentication, screenshots, sysinfo, ATX, MSD upload |
| WebSocket | Keyboard, mouse, and ping frames |

Any device tool call auto-establishes the default session on first use
(`KVMRuntime.ensure_connected`, single-flight under a connection lock) — an
agent does not need to call `kvm_connect` before an ordinary screenshot, OCR,
or HID action. Only the default target is auto-managed; named (non-default)
targets fail closed and must be connected explicitly.

`kvm_connect(host=None, password?, username=None, target="default", force_reconnect=False)` is an optional override, not a precondition. Omitted `host`/`username` resolve to the managed default (`COMET_HOST`/`COMET_USERNAME` env overrides, else `192.168.0.126`/`admin`). A live session already matching the requested host is reused (`reused: true`) without touching Doppler or the network; `force_reconnect=True` replaces it unconditionally. Password comes from an explicit argument or, when omitted, `GLCOMET_ADMIN_PASSWORD` fetched from the Doppler CLI via `doppler.yaml` (`homelab`/`dev`) and cached in-process for the life of the server so repeated connects don't re-shell to Doppler. The process environment is not used for the Comet password. The bundled launcher is plain `uv run --locked --python 3.13 python ./glkvm_mcp.py`; Doppler must be installed and authenticated on the host.

`kvm_status` never connects; `kvm_disconnect` is optional and non-sticky — the
next device operation reconnects the default automatically.

TLS verification is disabled because the Comet ships with a self-signed certificate. The expected operating model is trusted LAN access, or remote access through Tailscale/VPN rather than direct public exposure.

## 3. Background Loops

Two background loops are part of the core KVM reliability model.

| Loop | Cadence | Purpose |
|------|---------|---------|
| Stale key watchdog | 40ms | Force-releases keys held longer than 250ms to recover from interrupted input sequences. |
| WebSocket pinger | 1s | Keeps the kvmd WebSocket alive so the Comet does not drop the input channel. |

These loops are firmware-workaround infrastructure, not optional BIOS policy.

## 4. Input Protocol

Keyboard input uses W3C KeyboardEvent codes over the Comet/PiKVM WebSocket API.

Patterns to preserve:

- Atomic key press: `keydown -> 25ms -> keyup(finish=true)`. This mitigates the firmware <= 1.9.0 stuck-key / double-typing bug.
- Modifier wrapping: `mods down -> key down -> key up -> mods up`. This preserves proper modifier release order and addresses `gl-inet/glkvm#22`.
- US keymap and aliases: human-readable key names are resolved to W3C codes before transmission.
- Mouse movement supports PiKVM normalized absolute coordinates and percentage coordinates.
- Mouse clicks and wheel scrolls are raw physical input primitives; they do not know what UI is under the pointer.

## 5. Screenshot and OCR Pipeline

The KVM core exposes frame capture and OCR as general-purpose primitives.

| Tool | Purpose |
|------|---------|
| `kvm_screenshot` | Captures a JPEG frame and returns MCP `Image` content. Supports preview/max-width/quality controls. |
| `kvm_screenshot_to_file` | Captures a frame and stores it under the screenshot cache directory. |
| `kvm_ocr_status` | Reports host Tesseract availability and identifies GL.iNet's browser-only Tesseract.js UI engine. |
| `kvm_ocr_text` | Captures a frame and runs host Tesseract; returns text/lines and supports crop/language parameters. |
| `kvm_ocr_screenshot` | Runs host Tesseract, returning ordered `text`/`lines` plus word coordinates and confidence. |
| `kvm_ocr_click` | Finds text with OCR and clicks the highest-confidence match. Supports quadrant filtering with `top-left`, `top-right`, `bottom-left`, and `bottom-right`. |

`kvm_screenshot_to_file` uses path safety validation: only filenames or relative paths under the screenshot cache are accepted. Absolute paths and `..` escapes are rejected.

**Capture retry:** `/api/streamer/snapshot` returning HTTP 503 means the
streamer hasn't come up yet, not an error. `get_screenshot` retries internally
on schedule 0.1/0.2/0.4/0.8s (five attempts total) before raising a
capture-specific error; the session and credentials are unaffected either way.
Any other HTTP status is raised immediately without retry. If the session dies
mid-retry, capture aborts rather than continuing to poll a dead session.
`kvm_status` surfaces the last capture outcome and retry count.

The KVM core has no screen semantics. It sends input, captures frames, runs OCR, and exposes Comet hardware APIs. It does not know whether the screen is BIOS, Windows, an installer, a shell, a crash screen, POST, recovery UI, or anything else.

Both text and coordinate OCR capture a frame and run in the MCP host process. Pillow decodes the frame; pytesseract's spacing-preserving text output backs `kvm_ocr_text`, while its TSV/dictionary output supplies boxes and confidence for structured/click OCR. Calls have a 15-second timeout and run in a worker thread so OCR cannot block the asyncio watchdog and pinger.

GL.iNet firmware 1.9's web UI **Text Recognition** feature is different: the
product JavaScript crops the canvas and runs bundled Tesseract.js/WASM in the
controlling browser. It works with the inherited PiKVM server OCR route disabled
and does not expose its result to this Python MCP process. `/api/streamer/ocr`
may still be observed during capability discovery as `legacy_server_ocr`, but it
is not used as this product's OCR execution path.

## 6. Comet Hardware Tools

The server exposes Comet-specific hardware APIs in addition to HID and screenshots.

| Tool | Purpose | Caution |
|------|---------|---------|
| `comet_power_state` | Read ATX power/LED state. | Read-only; ATX board required for useful LEDs. |
| `comet_atx_power(action)` | Power on/off/reset through the ATX add-on board. | Requires the ATX add-on board. Destructive. |
| `comet_atx_click(button)` | Momentary power/reset button pulse. | Requires the ATX add-on board. Destructive. |
| `comet_switch_state()` | Comet X (GL-RM4PE) channel inventory and active port. | Read-only. Fails with `SwitchUnavailable` on single-port Comets. |
| `comet_switch_set_active(port)` | Make one Comet X channel own video, keyboard, mouse and media. | Destructive to the session: virtual media detaches; remount after. Confirms by re-reading state. |
| `comet_switch_set_port_name(port, name)` | Name a Comet X channel. | Write, idempotent. |
| `comet_switch_atx_power(port, action)` / `comet_switch_atx_click(port, button)` | Per-port ATX board actions on a Comet X. | Requires an ATX board on that channel. Not hardware-qualified for non-active channels. Destructive. |
| `comet_sysinfo()` | Reads device metadata. | Read-only. |
| `comet_capabilities()` | Discover supported subsystems on the connected unit. | Read-only. |
| `comet_media_*` | Virtual media inventory, upload/fetch, mount/unmount, remove, reset. | Writes to device storage / changes boot media. |
| `comet_msd_upload` | Legacy alias for media upload. Prefer `comet_media_upload`. | Writes to device storage. |

ATX endpoints being exposed does not guarantee the target machine is wired for ATX control. The hardware board and cable path still need to exist. Full tool tables (WOL, streamer, recorder, Tailscale, Redfish) live in `docs/reference/comet-api.md` and the README.

## 7. Tool Annotations

MCP tool annotations are metadata, not an approval system.

Use and keep these annotations:

- `readOnlyHint`
- `destructiveHint`
- `idempotentHint`

These hints tell the client and operator what a tool can do. They do not grant or deny authority, do not create approval tokens, and do not replace operator judgment.

Read-only examples: `kvm_screenshot`, `kvm_screenshot_to_file`, `kvm_ocr_screenshot`, `kvm_status`, `comet_sysinfo`.

Destructive or physical-input examples: `kvm_send_text`, `kvm_terminal_run`, `kvm_send_keys`, `kvm_hold_key`, mouse tools, `kvm_ocr_click`, `comet_atx_power`, `comet_atx_click`, `comet_switch_set_active`, `comet_switch_atx_power`, `comet_switch_atx_click`, `comet_media_upload`, `comet_media_mount`.

## 8. Security Model

The security model is intentionally narrow.

- LAN-first operation.
- Per-session password supplied through `kvm_connect`, or fetched from Doppler CLI (`GLCOMET_ADMIN_PASSWORD` secret) and cached in process memory for the life of the server.
- No stored Comet password in the repository.
- TLS verification disabled for the Comet's self-signed certificate.
- Remote operation should use Tailscale or a VPN.
- Do not expose MCP stdio or the Comet HTTP/WebSocket APIs directly to untrusted networks.

Host, username, and LAN IP are non-sensitive in this repo. `GLCOMET_ADMIN_PASSWORD` is the secret and is managed through Doppler.

## 9. Command Output Delivery

An agent receives ordinary MCP tool results directly. It does not need to inspect the runtime log or manually interpret an image when text OCR is sufficient.

### Current pixel-console flow

1. Call `kvm_ocr_status()` once, then `kvm_ocr_text()` to establish the current prompt — the default Comet session is established automatically on the first device call.
2. Call `kvm_send_text(command)` and `kvm_send_keys("Enter")`.
3. Call `kvm_ocr_text()` and read its returned `text` or `lines` fields.
4. Repeat the OCR read only if the command is still updating the visible screen.

This is appropriate for BIOS, recovery, network-down hosts, and other pixel-only states. It cannot recover bytes that scrolled off the HDMI viewport before a frame was captured, and OCR cannot provide a trustworthy process exit status by itself.

### Bounded POSIX command observer

`kvm_terminal_run(command, timeout_seconds?, poll_interval_seconds?)` is one bounded call for a POSIX-visible shell. It creates unique start/end/typed markers, types an isolated `sh -c` wrapper, and OCR-confirms the shell plus all three markers before it presses Enter. The timeout is capped at 300 seconds and the poll interval must be at least 0.1 seconds, keeping the operation fence bounded. If Comet HID reports skipped characters or OCR confirmation fails, it returns `status: "not_submitted"` and does not submit the command.

After submission it polls screenshots only for that call, skips OCR for identical frames, and merges only exact text overlap. Its result has `status` (`completed` or `timeout` after submission), a best-effort visible `transcript`, marker evidence, poll/duration counts, and explicit uncertainty/truncation flags. `exit_code` is populated only when the exact end marker and numeric code are visibly OCR-observed. On timeout it releases HID state without sending Ctrl+C, so the result explicitly warns that the remote command may still be running.

This is not exact stdout/stderr: viewport scrollback, whitespace, stream separation, fast changes, and OCR mistakes remain uncertain. It has no background transcript and does not persist command output.

An always-on transcript buffer is **Deferred**. Persistent background OCR would add cost, retain potentially sensitive shell text, and still fail to guarantee capture of fast scrollback.

### Candidate exact-output transport

Direct target SSH is a **Candidate** companion component for hosts reachable over the network. It should use AsyncSSH to return exact stdout, stderr, exit status, and timeout state; enforce known-host verification and host allowlisting; and keep target credentials separate from the Comet credential. It is not part of the universal KVM core because it disappears precisely when BIOS, recovery, or network failure makes KVM necessary.

`kvm_ocr_text` always uses host Tesseract. The MCP process cannot reuse the
browser's Tesseract.js worker, so Tesseract must be installed where the MCP runs.
The inherited server OCR route is not treated as a fallback or as evidence that
GL.iNet's product UI OCR runs on the device.

MCP resources or resource-updated notifications may mirror a current transcript for clients that subscribe, but the portable primary interface remains an explicit tool result. Logging is diagnostic only and must not capture commands or OCR text.

## 10. KVM and Sidecar Boundary

The product boundary is engine vs. steering.

| Analogy | Tools | Meaning |
|---------|-------|---------|
| Engine / tires | `kvm_*`, `comet_*` | Universal physical I/O. Sends signals without knowing screen meaning. |
| Steering / navigation | `bios_*` | BIOS-specific orchestration, graph state, and verification. |
| Camera / eyes | Screenshot, OCR, VLM | Perception inputs that can be used by downstream workflows. |

The KVM core does not know about VLMs. It exposes screenshots, OCR, HID, and Comet hardware APIs. A downstream sidecar may call those tools and may use a VLM to interpret screenshots.

### Interaction Lifecycle

| Phase | Tool Call | Layer | Position Tracker Role |
|:---|:---|:---|:---|
| **I. KVM session** | any device tool (auto-connect) | Universal KVM | Idle. Opens physical I/O session on first use; `kvm_connect()` is an optional override, not required. |
| **II. General triage** | `kvm_ocr_text()` | Universal KVM | Host Tesseract visible text for shells, POST, recovery, and other text screens. |
| | `comet_atx_power("reset")` | Universal KVM | No BIOS semantics. Physical power action. |
| **III. BIOS entry** | `kvm_hold_key("Delete")` or repeated `kvm_send_keys("Delete")` | Universal KVM | Still mostly passive. Getting into setup. |
| **IV. BIOS alignment** | `bios_observe_state()` | BIOS sidecar | Wakes up. Uses screenshot/OCR/VLM to set `current_state`. |
| **V. BIOS cartography** | `bios_crawl_region(...)` | BIOS sidecar | Takes the wheel. Enumerates safe BIOS tree. |
| **VI. BIOS navigation** | `bios_navigate_to(target_node_id="...")` | BIOS sidecar | Replays a graph path and verifies each transition. |
| **VII. BIOS mutation** | `bios_apply_setting_change(capability_id=..., desired_value=...)` | BIOS sidecar | Verifies row, opens selector, uses VLM to read options, changes visible value. |
| **VIII. Save/reboot** | `bios_save_and_reboot()` | BIOS sidecar | **Visually verifies** save dialog before confirming. Verification, not approval. |
| **IX. Evidence** | `bios_export_trace()` | BIOS sidecar | Packages screenshots, parses, transitions, and actions. |
| **X. Close** | `kvm_disconnect()` — optional, non-sticky | Universal KVM | Frees the session/streamer now; the next device operation reconnects automatically. |

Current design: `kvm_*` remains raw; `bios_*` wraps and verifies. The driver chooses the correct layer.

Future optional design: a deliberate BIOS-active middleware could warn or block raw input during sidecar sessions. That interception does not exist today, so docs should not imply raw `kvm_*` calls are automatically state-checked.

Visual verification stays. Approval-gating is cut. For example, `bios_save_and_reboot` verifying a confirmation dialog before pressing Enter is screen-state verification, not human approval-token policy.

## 11. Known Gaps and Improvement Opportunities

1. **Bounded terminal command observation:** The current OCR primitive returns visible text, but no composite call yet captures screen changes for the duration of a command or reports truncation/uncertainty.
2. **Exact target shell:** No optional AsyncSSH companion exists, so exact stdout/stderr/exit status is unavailable through this project even when the controlled OS is network-reachable.
3. **`comet_raw_*` aliases:** The 10 aliases duplicate `kvm_*` tools. They remain for compatibility and are deprecated in documentation.
4. **Non-OCR operation timeouts:** HTTP requests have a client timeout and OCR has a Tesseract timeout, but some multi-step WebSocket and BIOS operations still lack an overall tool deadline.
5. **Remaining live coverage:** Loopback contracts cover login, HTTP/WebSocket auth, capability discovery, HID, watchdog release, ATX error mapping, MSD streaming/mount, multi-target sessions, OCR, VLM routing, and BIOS safety behavior. Live Lane A is read-only; reversible Lane B and destructive Lane C still require disposable hardware sign-off.
