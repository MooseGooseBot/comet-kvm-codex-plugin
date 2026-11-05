"""Multi-port switch tools for the GL.iNet Comet X (GL-RM4PE).

kvmd registers the switch API only when the hardware model is ``rm4pe``. On a
single-port Comet every tool here fails with a clear ``SwitchUnavailable``
message instead of a bare HTTP error. The Comet X controls one channel at a
time: HDMI capture, keyboard, mouse and virtual media all follow the active
port, and virtual media detaches on every switch.
"""
from __future__ import annotations

import asyncio
import logging
import time

from src.kvm_core.server import mcp
from src.kvm_core.tools_core import _managed_client, _operation_fence

LOG = logging.getLogger("kvm_core.tools")

SWITCH_MEDIA_WARNING = (
    "Virtual media detaches when the active port changes. Call comet_media_mount "
    "again after switching if the target still needs the image."
)
DEFAULT_CONFIRM_TIMEOUT_S = 10.0
CONFIRM_POLL_S = 0.25


def _port_count(state: dict) -> int | None:
    model = state.get("model") if isinstance(state, dict) else None
    ports = model.get("ports") if isinstance(model, dict) else None
    if isinstance(ports, list) and ports:
        return len(ports)
    return None


def _summary(state: dict) -> dict:
    summary = state.get("summary") if isinstance(state, dict) else None
    return summary if isinstance(summary, dict) else {}


def _validate_port(port: int, state: dict | None) -> int:
    if isinstance(port, bool) or not isinstance(port, int):
        raise ValueError("port must be an integer channel index (0-based)")
    if port < 0:
        raise ValueError("port must be 0 or greater")
    count = _port_count(state or {})
    if count is not None and port >= count:
        raise ValueError(f"port {port} is out of range; this switch reports {count} ports (0-{count - 1})")
    return port


@mcp.tool(name="comet_switch_state", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True})
async def comet_switch_state(target: str | None = None) -> dict:
    """Read the Comet X multi-port switch via GET /api/switch.

    Returns the firmware state: model.ports[] (channel, name, id), summary
    (active_port 0-based, active_id, synced), video.links[] and usb_otg.links[]
    (one boolean per port), edids and ATX LED arrays. Fails with
    SwitchUnavailable on a single-port Comet.
    """
    client = await _managed_client(target)
    return await client.switch_state()


@mcp.tool(name="comet_switch_set_active", annotations={"readOnlyHint": False, "destructiveHint": True})
async def comet_switch_set_active(
    port: int,
    confirm_timeout_s: float = DEFAULT_CONFIRM_TIMEOUT_S,
    target: str | None = None,
) -> dict:
    """Select which Comet X channel (0-based port) owns video, HID and media.

    POSTs /api/switch/set_active?port=N, then polls GET /api/switch until
    summary.active_port equals the request and synced is true, or
    confirm_timeout_s elapses. The WebSocket switch event is not relied on;
    the GET is the confirmation. Virtual media detaches on every switch.
    Observed firmware switch time is 2-3 s; the first snapshot of the new
    target can lag a further second.
    """
    async with _operation_fence():
        client = await _managed_client(target)
        before = await client.switch_state()
        port = _validate_port(port, before)
        previous = _summary(before).get("active_port")
        started = time.monotonic()
        await client.switch_set_active(port)
        confirmed = False
        state = before
        deadline = started + max(0.0, float(confirm_timeout_s))
        while True:
            state = await client.switch_state()
            summary = _summary(state)
            if summary.get("active_port") == port and summary.get("synced", True):
                confirmed = True
                break
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(CONFIRM_POLL_S)
        elapsed = round(time.monotonic() - started, 3)
    result = {
        "port": port,
        "previous_port": previous,
        "active_port": _summary(state).get("active_port"),
        "synced": _summary(state).get("synced"),
        "confirmed": confirmed,
        "elapsed_s": elapsed,
        "warning": SWITCH_MEDIA_WARNING,
    }
    if not confirmed:
        result["error"] = (
            f"active_port did not become {port} within {confirm_timeout_s}s; "
            "re-read comet_switch_state before sending input"
        )
    return result


@mcp.tool(name="comet_switch_set_port_name", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
async def comet_switch_set_port_name(port: int, name: str, target: str | None = None) -> dict:
    """Name a Comet X channel via POST /api/switch/set_port_params?port=N&name=...

    The firmware trims the name, collapses whitespace and caps it at 255
    printable characters. The name shows in model.ports[].name and on the
    touchscreen.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty string")
    async with _operation_fence():
        client = await _managed_client(target)
        state = await client.switch_state()
        port = _validate_port(port, state)
        result = await client.switch_set_port_params(port, name=name.strip())
    return {"port": port, "name": name.strip(), **result}


@mcp.tool(name="comet_switch_atx_power", annotations={"readOnlyHint": False, "destructiveHint": True})
async def comet_switch_atx_power(port: int, action: str, target: str | None = None) -> dict:
    """Per-port ATX power via POST /api/switch/atx/power?port=N&action=...

    Actions: on, off, off_hard, reset_hard (aliases: reset, force_off). Needs a
    GL.iNet ATX board on that channel's USB accessory port. The route is
    registered in firmware source, but whether it drives a non-active channel
    is not yet qualified on hardware; read comet_power_state or the console
    afterwards instead of trusting the HTTP 200.
    """
    async with _operation_fence():
        client = await _managed_client(target)
        state = await client.switch_state()
        port = _validate_port(port, state)
        return await client.switch_atx_power(port, action)


@mcp.tool(name="comet_switch_atx_click", annotations={"readOnlyHint": False, "destructiveHint": True})
async def comet_switch_atx_click(port: int, button: str, target: str | None = None) -> dict:
    """Per-port momentary ATX button via POST /api/switch/atx/click?port=N&button=...

    Buttons: power, power_long, reset. Same hardware caveat as
    comet_switch_atx_power.
    """
    async with _operation_fence():
        client = await _managed_client(target)
        state = await client.switch_state()
        port = _validate_port(port, state)
        return await client.switch_atx_click(port, button)
