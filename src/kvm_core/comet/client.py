from __future__ import annotations

import asyncio
import logging
import time
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse
import httpx
import websockets
import ssl as _ssl

LOG = logging.getLogger("kvm_core.comet")

# US key mapping
CHAR_TO_KEY: dict[str, tuple[str, bool]] = {}


def _build_keymap() -> dict[str, tuple[str, bool]]:
    m: dict[str, tuple[str, bool]] = {}
    for c in "abcdefghijklmnopqrstuvwxyz":
        m[c] = (f"Key{c.upper()}", False)
        m[c.upper()] = (f"Key{c.upper()}", True)
    digits = "0123456789"
    shift_digits = ")!@#$%^&*("
    for i, d in enumerate(digits):
        m[d] = (f"Digit{d}", False)
        m[shift_digits[i]] = (f"Digit{d}", True)
    extras = {
        " ": ("Space", False),
        "\t": ("Tab", False),
        "\n": ("Enter", False),
        "\r": ("Enter", False),
        "-": ("Minus", False), "_": ("Minus", True),
        "=": ("Equal", False), "+": ("Equal", True),
        "[": ("BracketLeft", False), "{": ("BracketLeft", True),
        "]": ("BracketRight", False), "}": ("BracketRight", True),
        "\\": ("Backslash", False), "|": ("Backslash", True),
        ";": ("Semicolon", False), ":": ("Semicolon", True),
        "'": ("Quote", False), '"': ("Quote", True),
        ",": ("Comma", False), "<": ("Comma", True),
        ".": ("Period", False), ">": ("Period", True),
        "/": ("Slash", False), "?": ("Slash", True),
        "`": ("Backquote", False), "~": ("Backquote", True),
    }
    m.update(extras)
    return m


CHAR_TO_KEY.update(_build_keymap())

KEY_ALIASES: dict[str, str] = {
    "ctrl": "ControlLeft", "control": "ControlLeft",
    "lctrl": "ControlLeft", "rctrl": "ControlRight",
    "shift": "ShiftLeft", "lshift": "ShiftLeft", "rshift": "ShiftRight",
    "alt": "AltLeft", "lalt": "AltLeft", "ralt": "AltRight",
    "altgr": "AltRight", "option": "AltLeft", "opt": "AltLeft",
    "meta": "MetaLeft", "lmeta": "MetaLeft", "rmeta": "MetaRight",
    "win": "MetaLeft", "windows": "MetaLeft",
    "cmd": "MetaLeft", "command": "MetaLeft",
    "super": "MetaLeft",
    "esc": "Escape", "escape": "Escape",
    "enter": "Enter", "return": "Enter",
    "tab": "Tab", "space": "Space", " ": "Space",
    "backspace": "Backspace", "bs": "Backspace",
    "delete": "Delete", "del": "Delete",
    "insert": "Insert", "ins": "Insert",
    "home": "Home", "end": "End",
    "pageup": "PageUp", "pgup": "PageUp",
    "pagedown": "PageDown", "pgdn": "PageDown", "pgdown": "PageDown",
    "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
    "capslock": "CapsLock", "caps": "CapsLock",
    "numlock": "NumLock", "scrolllock": "ScrollLock", "scroll": "ScrollLock",
    "printscreen": "PrintScreen", "prtsc": "PrintScreen", "prtscn": "PrintScreen",
    "pause": "Pause", "break": "Pause",
    "menu": "ContextMenu", "contextmenu": "ContextMenu",
}
for i in range(1, 13):
    KEY_ALIASES[f"f{i}"] = f"F{i}"

MOUSE_BUTTONS = {"left", "right", "middle", "up", "down"}
MODIFIER_KEYS = {
    "ControlLeft", "ControlRight",
    "ShiftLeft", "ShiftRight",
    "AltLeft", "AltRight",
    "MetaLeft", "MetaRight",
}

ATX_POWER_ALIASES = {
    "reset": "reset_hard",
    "force_off": "off_hard",
    "hard_off": "off_hard",
    "off_hard": "off_hard",
    "reset_hard": "reset_hard",
    "on": "on",
    "off": "off",
}
ATX_POWER_ACTIONS = set(ATX_POWER_ALIASES.values())
ATX_CLICK_BUTTONS = {"power", "power_long", "reset"}
SWITCH_PORT_PARAMS = {
    "name",
    "edid_id",
    "dummy",
    "atx_click_power_delay",
    "atx_click_power_long_delay",
    "atx_click_reset_delay",
}


class SwitchUnavailable(RuntimeError):
    """The connected Comet does not register the multi-port switch API."""

MSD_UPLOAD_TIMEOUT = httpx.Timeout(600.0, connect=30.0)

# GET /api/streamer/snapshot answers 503 while kvmd's streamer process is still
# coming up. That is the ONLY retryable capture failure — every other status is
# a real error and is raised on the first attempt.
CAPTURE_BACKOFF_SCHEDULE = (0.1, 0.2, 0.4, 0.8)


class CometCaptureError(RuntimeError):
    """Screen capture failed. Session and auth state are left intact."""


@dataclass
class HeldKey:
    pressed_at: float
    release_deadline: float | None = None
    watchdog_protected: bool = False


def resolve_key_name(name: str) -> str:
    if not name:
        raise ValueError("empty key name")
    if name in CHAR_TO_KEY.values() or name.startswith(("Key", "Digit", "Numpad", "Arrow", "F")):
        return name
    return KEY_ALIASES.get(name.lower(), name)


def _unwrap_ok_json(response: httpx.Response) -> dict[str, Any]:
    """Parse PiKVM/GLKVM JSON envelope when present."""
    content_type = response.headers.get("content-type", "").lower()
    if "json" not in content_type:
        return {"status": response.status_code, "raw": response.text}
    payload = response.json()
    if isinstance(payload, dict) and "result" in payload:
        return payload.get("result") if payload.get("ok", True) else payload
    return payload if isinstance(payload, dict) else {"result": payload}


class CometClient:
    def __init__(
        self,
        host: str,
        username: str = "admin",
        password: str = "",
        verify_ssl: bool = False,
        target_id: str = "default",
    ):
        self.host = host
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.target_id = target_id
        self.base_url = f"https://{host}" if "://" not in host else host.rstrip("/")
        self.http: Optional[httpx.AsyncClient] = None
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.auth_token: Optional[str] = None
        self.held: dict[str, HeldKey] = {}
        self.send_lock = asyncio.Lock()
        self.watchdog_task: Optional[asyncio.Task] = None
        self.pinger_task: Optional[asyncio.Task] = None
        self.receiver_task: Optional[asyncio.Task] = None
        self.server_state: dict[str, Any] = {}
        self.last_server_event_at: float | None = None
        self.last_pong_at: float | None = None
        self.capabilities: dict[str, Any] = {}
        self._ws_healthy = False

        # Capture diagnostics for the most recent get_screenshot (no image bytes).
        self.last_capture_ok: Optional[bool] = None
        self.last_capture_error: Optional[str] = None
        self.last_capture_at: Optional[float] = None
        self.capture_retry_count = 0

        self.min_down_up_gap = 0.025
        self.inter_char_gap = 0.010
        self.mod_gap = 0.005
        self.stale_s = 0.250
        self.watchdog_period = 0.040
        self.ws_ping_period = 1.0

    def _auth_headers(self) -> dict[str, str]:
        if not self.auth_token:
            return {}
        return {"Token": self.auth_token}

    async def connect(self) -> bool:
        if self.http is not None:
            await self.disconnect()

        self.http = httpx.AsyncClient(
            verify=self.verify_ssl,
            timeout=10.0,
            follow_redirects=True,
            headers={},
        )
        try:
            login = await self.http.post(
                f"{self.base_url}/api/auth/login",
                data={"user": self.username, "passwd": self.password, "expire": "0"},
            )
            login.raise_for_status()
            token = self.http.cookies.get("auth_token")
            if not token:
                body = {}
                try:
                    body = login.json()
                except Exception:
                    pass
                if isinstance(body, dict) and body.get("result", {}).get("two_step_required"):
                    raise RuntimeError("two_step_required: complete 2FA before connecting")
                raise RuntimeError("Login succeeded but no auth_token cookie returned.")
            self.auth_token = token
            self.http.headers["Token"] = token
        except Exception as e:
            if self.http:
                await self.http.aclose()
            self.http = None
            self.auth_token = None
            raise RuntimeError(f"login failed against {self.base_url}: {e}") from e

        parsed = urlparse(self.base_url)
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        # Prefer cookie/header Token auth (no token in the URL). Use stream=true so
        # kvmd keeps the HDMI streamer process up for the session — on Comet/RM10,
        # GET /api/streamer/snapshot returns 503 while streamer is null, and the
        # streamer tears down as soon as the last stream=true client disconnects.
        # Binary video frames are drained (ignored) in _receiver_loop.
        ws_url = f"{ws_scheme}://{parsed.netloc}/api/ws?stream=true"
        additional_headers = {
            "Cookie": f"auth_token={token}",
            "Token": token,
        }

        sslctx = None
        if ws_scheme == "wss" and not self.verify_ssl:
            sslctx = _ssl.create_default_context()
            sslctx.check_hostname = False
            sslctx.verify_mode = _ssl.CERT_NONE

        try:
            self.ws = await websockets.connect(
                ws_url,
                ssl=sslctx,
                max_size=8 * 1024 * 1024,
                open_timeout=10.0,
                ping_interval=None,
                additional_headers=additional_headers,
            )
        except TypeError:
            # Older websockets used extra_headers
            try:
                self.ws = await websockets.connect(
                    ws_url,
                    ssl=sslctx,
                    max_size=8 * 1024 * 1024,
                    open_timeout=10.0,
                    ping_interval=None,
                    extra_headers=additional_headers,
                )
            except Exception as e:
                await self._cleanup_failed_connect()
                raise RuntimeError(f"WebSocket connection failed: {e}") from e
        except Exception as e:
            await self._cleanup_failed_connect()
            raise RuntimeError(f"WebSocket connection failed: {e}") from e

        self._ws_healthy = True
        self.watchdog_task = asyncio.create_task(self._watchdog_loop(), name="comet-watchdog")
        self.pinger_task = asyncio.create_task(self._pinger_loop(), name="comet-pinger")
        self.receiver_task = asyncio.create_task(self._receiver_loop(), name="comet-receiver")
        try:
            self.capabilities = await asyncio.wait_for(
                self.discover_capabilities(),
                timeout=20.0,
            )
        except asyncio.CancelledError:
            await self.disconnect()
            raise
        except Exception as exc:
            LOG.warning("Capability discovery failed: %s", type(exc).__name__)
            self.capabilities = {"error": type(exc).__name__}
        LOG.info("Connected to Comet KVM at %s (target=%s)", self.base_url, self.target_id)
        return True

    async def _cleanup_failed_connect(self) -> None:
        for t in (self.watchdog_task, self.pinger_task, self.receiver_task):
            if t:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self.watchdog_task = None
        self.pinger_task = None
        self.receiver_task = None
        self._ws_healthy = False
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
        if self.http:
            try:
                await self.http.aclose()
            except Exception:
                pass
        self.http = None
        self.auth_token = None

    async def disconnect(self):
        try:
            async with self.send_lock:
                for k in list(self.held.keys()):
                    try:
                        await self._ws_send_key(k, state=False, finish=True)
                    except Exception:
                        pass
        except Exception:
            pass

        self.held.clear()

        for t in (self.watchdog_task, self.pinger_task, self.receiver_task):
            if t:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

        self.watchdog_task = None
        self.pinger_task = None
        self.receiver_task = None
        self._ws_healthy = False

        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None

        if self.http and self.auth_token:
            try:
                await self.http.post(f"{self.base_url}/api/auth/logout")
            except Exception:
                pass

        if self.http:
            try:
                await self.http.aclose()
            except Exception:
                pass
            self.http = None
        self.auth_token = None
        LOG.info("Disconnected from Comet KVM (target=%s)", self.target_id)

    def _record_capture(self, ok: bool, error: Optional[str] = None) -> None:
        self.last_capture_ok = ok
        self.last_capture_error = error
        self.last_capture_at = time.time()

    async def get_screenshot(self, preview: bool = True, max_width: int = 1024, quality: int = 60) -> bytes:
        if not self.http:
            raise RuntimeError("Not connected")
        params = {"allow_offline": "true"}
        if preview:
            params["preview"] = "true"
            params["preview_max_width"] = str(int(max_width))
            params["preview_quality"] = str(max(1, min(100, int(quality))))
        url = f"{self.base_url}/api/streamer/snapshot"
        self.capture_retry_count = 0

        for index in range(len(CAPTURE_BACKOFF_SCHEDULE) + 1):
            try:
                r = await self.http.get(url, params=params)
            except httpx.HTTPError as exc:
                self._record_capture(False, f"{type(exc).__name__}: {exc}")
                raise
            if r.status_code != 503:
                if r.is_error:
                    self._record_capture(False, f"HTTP {r.status_code} from {url}")
                r.raise_for_status()
                self._record_capture(True)
                return r.content
            if index >= len(CAPTURE_BACKOFF_SCHEDULE):
                break
            self.capture_retry_count += 1
            await asyncio.sleep(CAPTURE_BACKOFF_SCHEDULE[index])
            # The streamer only comes back on a live session; if the session died
            # during the backoff there is nothing left to retry against.
            if not self.is_connected():
                message = (
                    "capture aborted: the Comet session died while waiting for the "
                    "streamer to become available"
                )
                self._record_capture(False, message)
                raise CometCaptureError(message)

        message = (
            f"capture failed: streamer unavailable (HTTP 503) after "
            f"{len(CAPTURE_BACKOFF_SCHEDULE) + 1} attempts over "
            f"{sum(CAPTURE_BACKOFF_SCHEDULE):.1f}s. The session and its "
            "credentials are still valid — this is a streamer fault, not auth."
        )
        self._record_capture(False, message)
        raise CometCaptureError(message)

    async def send_key_event(self, key: str, state: bool, finish: bool = False):
        async with self.send_lock:
            await self._ws_send_key(key, state, finish)

    async def _ws_send_key(
        self,
        key: str,
        state: bool,
        finish: bool = False,
        *,
        release_deadline: float | None = None,
        watchdog_protected: bool = False,
    ):
        if not self.ws:
            raise RuntimeError("WS disconnected")
        payload = json.dumps({
            "event_type": "key",
            "event": {"key": key, "state": bool(state), "finish": bool(finish)},
        })
        await self.ws.send(payload)
        if state:
            self.held[key] = HeldKey(
                pressed_at=time.monotonic(),
                release_deadline=release_deadline,
                watchdog_protected=watchdog_protected,
            )
        else:
            self.held.pop(key, None)

    async def send_text(self, text: str, wpm: int = 200) -> dict:
        inter = max(0.0, 60.0 / max(1, wpm) / 5.0) if wpm > 0 else 0.0
        start = time.monotonic()
        sent = 0
        skipped: list[str] = []
        async with self.send_lock:
            for ch in text:
                mapping = CHAR_TO_KEY.get(ch)
                if mapping is None:
                    skipped.append(ch)
                    continue
                key, needs_shift = mapping
                mods = ["ShiftLeft"] if needs_shift else []
                await self._press_with_modifiers(key, mods)
                sent += 1
                await asyncio.sleep(inter + self.inter_char_gap)
        return {"chars": sent, "skipped": skipped, "elapsed_s": round(time.monotonic() - start, 3)}

    async def send_combo(self, combo: str) -> dict:
        parts = [p.strip() for p in combo.split("+") if p.strip()]
        if not parts:
            raise ValueError("empty combo")

        if len(parts) == 1:
            key = resolve_key_name(parts[0])
            async with self.send_lock:
                await self._atomic_press(key)
            return {"sent": combo, "modifiers": [], "key": key}

        *mod_tokens, main_token = parts
        modifiers = [resolve_key_name(m) for m in mod_tokens]
        key = resolve_key_name(main_token)
        async with self.send_lock:
            await self._press_with_modifiers(key, modifiers)
        return {"sent": combo, "modifiers": modifiers, "key": key}

    async def hold_key(self, key: str, duration_ms: int) -> dict:
        duration_ms = max(1, min(5000, int(duration_ms)))
        canonical = resolve_key_name(key)
        deadline = time.monotonic() + (duration_ms / 1000.0)
        async with self.send_lock:
            await self._ws_send_key(
                canonical,
                state=True,
                finish=False,
                release_deadline=deadline,
                watchdog_protected=True,
            )
        try:
            await asyncio.sleep(duration_ms / 1000.0)
        finally:
            async with self.send_lock:
                await self._ws_send_key(canonical, state=False, finish=True)
        return {"key": canonical, "duration_ms": duration_ms}

    async def release_all(self) -> dict:
        released: list[str] = []
        async with self.send_lock:
            for k in list(self.held.keys()):
                try:
                    await self._ws_send_key(k, state=False, finish=True)
                    released.append(k)
                except Exception as e:
                    LOG.error("Failed to release key %s: %s", k, e)
        return {"released": released}

    async def mouse_move_pct(self, x_pct: float, y_pct: float) -> dict:
        x = int(round((max(0.0, min(100.0, x_pct)) / 100.0) * 65535 - 32768))
        y = int(round((max(0.0, min(100.0, y_pct)) / 100.0) * 65535 - 32768))
        async with self.send_lock:
            await self._ws_send_mouse_move(x, y)
        return {"x": x, "y": y, "x_pct": x_pct, "y_pct": y_pct}

    async def mouse_click(self, button: str = "left", count: int = 1) -> dict:
        count = max(1, min(5, int(count)))
        async with self.send_lock:
            for _ in range(count):
                await self._ws_send_mouse_button(button, True)
                await asyncio.sleep(self.min_down_up_gap)
                await self._ws_send_mouse_button(button, False)
                await asyncio.sleep(0.030)
        return {"button": button, "count": count}

    async def mouse_scroll(self, dx: int = 0, dy: int = 0) -> dict:
        async with self.send_lock:
            await self._ws_send_mouse_wheel(dx, dy)
        return {"dx": dx, "dy": dy}

    async def _ws_send_mouse_button(self, button: str, state: bool):
        if not self.ws:
            raise RuntimeError("WS disconnected")
        if button not in MOUSE_BUTTONS:
            raise ValueError(f"unknown mouse button: {button}")
        await self.ws.send(json.dumps({
            "event_type": "mouse_button",
            "event": {"button": button, "state": bool(state)},
        }))

    async def _ws_send_mouse_move(self, x: int, y: int):
        if not self.ws:
            raise RuntimeError("WS disconnected")
        x = max(-32768, min(32767, int(x)))
        y = max(-32768, min(32767, int(y)))
        await self.ws.send(json.dumps({
            "event_type": "mouse_move",
            "event": {"to": {"x": x, "y": y}},
        }))

    async def _ws_send_mouse_wheel(self, dx: int, dy: int):
        if not self.ws:
            raise RuntimeError("WS disconnected")
        dx = max(-127, min(127, int(dx)))
        dy = max(-127, min(127, int(dy)))
        await self.ws.send(json.dumps({
            "event_type": "mouse_wheel",
            "event": {"delta": {"x": dx, "y": dy}, "squash": False},
        }))

    async def _atomic_press(self, key: str, hold_s: float = 0.025):
        await self._ws_send_key(key, state=True, finish=False)
        await asyncio.sleep(max(hold_s, self.min_down_up_gap))
        await self._ws_send_key(key, state=False, finish=True)

    async def _press_with_modifiers(self, key: str, modifiers: list[str]):
        try:
            for m in modifiers:
                await self._ws_send_key(m, state=True, finish=False)
                await asyncio.sleep(self.mod_gap)
            await self._atomic_press(key)
        finally:
            for m in reversed(modifiers):
                if m in self.held:
                    try:
                        await self._ws_send_key(m, state=False, finish=True)
                    except Exception:
                        pass
                    await asyncio.sleep(self.mod_gap)

    def _is_stale_hold(self, key: str, info: HeldKey, now: float) -> bool:
        if info.watchdog_protected:
            if info.release_deadline is None:
                return False
            # Only force-release well past the intentional deadline.
            return now > (info.release_deadline + self.stale_s)
        return (now - info.pressed_at) > self.stale_s

    async def _watchdog_loop(self):
        try:
            while True:
                await asyncio.sleep(self.watchdog_period)
                now = time.monotonic()
                stale = [k for k, info in self.held.items() if self._is_stale_hold(k, info, now)]
                for k in stale:
                    LOG.warning("watchdog releasing stale key %s", k)
                    try:
                        async with self.send_lock:
                            await self._ws_send_key(k, state=False, finish=True)
                    except Exception as e:
                        LOG.error("watchdog send failed for %s: %s", k, e)
                        self.held.pop(k, None)
        except asyncio.CancelledError:
            return

    async def _pinger_loop(self):
        try:
            while True:
                await asyncio.sleep(self.ws_ping_period)
                if self.ws:
                    try:
                        await self.ws.send(json.dumps({"event_type": "ping", "event": {}}))
                    except Exception:
                        self._ws_healthy = False
                        return
        except asyncio.CancelledError:
            return

    async def _receiver_loop(self):
        try:
            while self.ws is not None:
                try:
                    message = await self.ws.recv()
                except Exception:
                    self._ws_healthy = False
                    return
                self.last_server_event_at = time.monotonic()
                if isinstance(message, (bytes, bytearray)):
                    continue
                try:
                    data = json.loads(message)
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                event_type = data.get("event_type")
                event = data.get("event")
                if event_type == "pong":
                    self.last_pong_at = time.monotonic()
                    continue
                if event_type == "kickout":
                    LOG.warning("Comet WebSocket kickout received")
                    self._ws_healthy = False
                    return
                if isinstance(event_type, str) and event_type.endswith("_state"):
                    self.server_state[event_type] = event
                elif isinstance(event_type, str):
                    self.server_state[event_type] = event
        except asyncio.CancelledError:
            return

    def is_connected(self) -> bool:
        if self.http is None or self.ws is None:
            return False
        if hasattr(self.ws, "state"):
            return self.ws.state.name == "OPEN" and self._ws_healthy
        return (not self.ws.closed) and self._ws_healthy

    # ── ATX Power Control (query-param contract) ───────────────────

    async def atx_state(self) -> dict:
        if not self.http:
            raise RuntimeError("Not connected")
        r = await self.http.get(f"{self.base_url}/api/atx")
        r.raise_for_status()
        return _unwrap_ok_json(r)

    async def atx_power(self, action: str, wait: bool = True) -> dict:
        """Power on/off/reset via ATX. Uses query params per PiKVM/GLKVM."""
        if not self.http:
            raise RuntimeError("Not connected")
        action = ATX_POWER_ALIASES.get(action, action)
        if action not in ATX_POWER_ACTIONS:
            raise ValueError(f"Invalid ATX action '{action}'. Use: {sorted(ATX_POWER_ACTIONS)}")
        r = await self.http.post(
            f"{self.base_url}/api/atx/power",
            params={"action": action, "wait": str(wait).lower()},
        )
        if not r.is_success:
            body = r.text
            if "ATX" in body or "not connected" in body or "not available" in body:
                raise RuntimeError(f"ATX board not detected or not connected: {body}")
            r.raise_for_status()
        return {"action": action, "wait": wait, "status": r.status_code, "result": _unwrap_ok_json(r)}

    async def atx_click(self, button: str, wait: bool = True) -> dict:
        """Momentary ATX button press via query params."""
        if not self.http:
            raise RuntimeError("Not connected")
        if button not in ATX_CLICK_BUTTONS:
            raise ValueError(f"Invalid ATX button '{button}'. Use: {sorted(ATX_CLICK_BUTTONS)}")
        r = await self.http.post(
            f"{self.base_url}/api/atx/click",
            params={"button": button, "wait": str(wait).lower()},
        )
        if not r.is_success:
            r.raise_for_status()
        return {"button": button, "wait": wait, "status": r.status_code, "result": _unwrap_ok_json(r)}

    # ── Multi-port switch (Comet X / rm4pe) ────────────────────────
    #
    # kvmd registers SwitchApi only when the hardware model is "rm4pe"
    # (kvmd/apps/kvmd/__init__.py). On a single-port Comet GET /api/switch
    # fails, and every method here raises SwitchUnavailable instead of a bare
    # HTTP error so tools can say why.

    async def switch_state(self) -> dict:
        """Read GET /api/switch (model.ports, summary.active_port, video and USB links)."""
        if not self.http:
            raise RuntimeError("Not connected")
        r = await self.http.get(f"{self.base_url}/api/switch")
        if not r.is_success:
            raise SwitchUnavailable(
                "This Comet has no multi-port switch API: "
                f"GET /api/switch returned HTTP {r.status_code}. "
                "kvmd enables it only on the Comet X (GL-RM4PE)."
            )
        return _unwrap_ok_json(r)

    async def _switch_post(self, path: str, params: dict[str, Any]) -> dict:
        if not self.http:
            raise RuntimeError("Not connected")
        r = await self.http.post(f"{self.base_url}{path}", params=params)
        if r.status_code == 404:
            raise SwitchUnavailable(
                f"POST {path} is not registered on this Comet; "
                "kvmd enables the switch API only on the Comet X (GL-RM4PE)."
            )
        r.raise_for_status()
        return {"status": r.status_code, "result": _unwrap_ok_json(r)}

    async def switch_set_active(self, port: int) -> dict:
        """POST /api/switch/set_active?port=N (0-based channel)."""
        return await self._switch_post("/api/switch/set_active", {"port": int(port)})

    async def switch_set_active_prev(self) -> dict:
        return await self._switch_post("/api/switch/set_active_prev", {})

    async def switch_set_active_next(self) -> dict:
        return await self._switch_post("/api/switch/set_active_next", {})

    async def switch_set_port_params(self, port: int, **params: Any) -> dict:
        """POST /api/switch/set_port_params?port=N&name=...&edid_id=...&dummy=...

        Only parameters that are not None are sent. Firmware accepts name,
        edid_id, dummy, atx_click_power_delay, atx_click_power_long_delay and
        atx_click_reset_delay.
        """
        unknown = set(params) - SWITCH_PORT_PARAMS
        if unknown:
            raise ValueError(f"Unknown switch port params {sorted(unknown)}. Use: {sorted(SWITCH_PORT_PARAMS)}")
        query: dict[str, Any] = {"port": int(port)}
        for key, value in params.items():
            if value is None:
                continue
            query[key] = str(value).lower() if isinstance(value, bool) else value
        return await self._switch_post("/api/switch/set_port_params", query)

    async def switch_atx_power(self, port: int, action: str) -> dict:
        """POST /api/switch/atx/power?port=N&action=on|off|off_hard|reset_hard."""
        action = ATX_POWER_ALIASES.get(action, action)
        if action not in ATX_POWER_ACTIONS:
            raise ValueError(f"Invalid ATX action '{action}'. Use: {sorted(ATX_POWER_ACTIONS)}")
        result = await self._switch_post("/api/switch/atx/power", {"port": int(port), "action": action})
        return {"port": int(port), "action": action, **result}

    async def switch_atx_click(self, port: int, button: str) -> dict:
        """POST /api/switch/atx/click?port=N&button=power|power_long|reset."""
        if button not in ATX_CLICK_BUTTONS:
            raise ValueError(f"Invalid ATX button '{button}'. Use: {sorted(ATX_CLICK_BUTTONS)}")
        result = await self._switch_post("/api/switch/atx/click", {"port": int(port), "button": button})
        return {"port": int(port), "button": button, **result}

    # ── System Info / Capabilities ─────────────────────────────────

    async def get_sysinfo(self, fields: str = "system,meta,extras") -> dict:
        if not self.http:
            raise RuntimeError("Not connected")
        r = await self.http.get(f"{self.base_url}/api/info", params={"fields": fields})
        r.raise_for_status()
        return _unwrap_ok_json(r)

    async def get_version(self) -> dict:
        if not self.http:
            raise RuntimeError("Not connected")
        r = await self.http.get(f"{self.base_url}/api/upgrade/version")
        r.raise_for_status()
        return _unwrap_ok_json(r)

    async def get_system_capability(self) -> dict:
        if not self.http:
            raise RuntimeError("Not connected")
        r = await self.http.get(f"{self.base_url}/api/system/capability")
        if r.status_code == 404:
            return {"available": False}
        r.raise_for_status()
        return _unwrap_ok_json(r)

    async def discover_capabilities(self) -> dict:
        """Connect-time capability profile for this Comet."""
        if not self.http:
            raise RuntimeError("Not connected")

        async def _safe_get(path: str, params: dict | None = None) -> dict:
            try:
                r = await self.http.get(f"{self.base_url}{path}", params=params or {})
                if not r.is_success:
                    return {"ok": False, "status": r.status_code}
                return {"ok": True, "result": _unwrap_ok_json(r)}
            except Exception as exc:
                return {"ok": False, "error": type(exc).__name__}

        version, info, capability, *subsystem_results = await asyncio.gather(
            _safe_get("/api/upgrade/version"),
            _safe_get("/api/info", {"fields": "system,meta,extras"}),
            _safe_get("/api/system/capability"),
            _safe_get("/api/hid"),
            _safe_get("/api/atx"),
            _safe_get("/api/msd"),
            _safe_get("/api/streamer"),
            _safe_get("/api/streamer/ocr"),
            _safe_get("/api/recorder"),
            _safe_get("/api/tailscale/status"),
            _safe_get("/api/wol/list"),
            _safe_get("/api/switch"),
        )
        subsystem_names = (
            "hid",
            "atx",
            "msd",
            "streamer",
            "legacy_server_ocr",
            "recorder",
            "tailscale",
            "wol",
            "switch",
        )
        subsystems = dict(zip(subsystem_names, subsystem_results))

        features = {name: bool(payload.get("ok")) for name, payload in subsystems.items()}
        kvmd = None
        model = None
        firmware = None
        if info.get("ok") and isinstance(info.get("result"), dict):
            system = info["result"].get("system") or info["result"]
            if isinstance(system, dict):
                kvmd = (system.get("kvmd") or {}).get("version") if isinstance(system.get("kvmd"), dict) else system.get("kvmd")
        if version.get("ok") and isinstance(version.get("result"), dict):
            firmware = version["result"].get("version") or version["result"].get("firmware")
            model = version["result"].get("model") or version["result"].get("board")

        profile = {
            "target_id": self.target_id,
            "host": self.base_url,
            "model": model,
            "firmware": firmware,
            "kvmd": kvmd,
            "features": features,
            "version": version,
            "info": info,
            "capability": capability,
            "subsystems": subsystems,
        }
        self.capabilities = profile
        return profile

    # ── Mass Storage lifecycle ─────────────────────────────────────

    async def msd_state(self) -> dict:
        if not self.http:
            raise RuntimeError("Not connected")
        r = await self.http.get(f"{self.base_url}/api/msd")
        r.raise_for_status()
        return _unwrap_ok_json(r)

    async def msd_upload_file(self, local_path: str, image_name: str | None = None) -> dict:
        """Stream a local file as raw body to POST /api/msd/write?image=..."""
        if not self.http:
            raise RuntimeError("Not connected")
        path = Path(local_path)
        if not path.is_file():
            raise FileNotFoundError(f"Local media file not found: {local_path}")
        image = image_name or path.name
        size = path.stat().st_size

        headers = {
            **self._auth_headers(),
            "Content-Type": "application/octet-stream",
            "Content-Length": str(size),
        }

        async def _body():
            with path.open("rb") as fh:
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk

        r = await self.http.post(
            f"{self.base_url}/api/msd/write",
            params={"image": image},
            content=_body(),
            headers=headers,
            timeout=MSD_UPLOAD_TIMEOUT,
        )
        if not r.is_success:
            r.raise_for_status()
        return {
            "image": image,
            "bytes": size,
            "status": r.status_code,
            "result": _unwrap_ok_json(r),
        }

    async def msd_fetch_remote(self, url: str, image_name: str) -> dict:
        if not self.http:
            raise RuntimeError("Not connected")
        r = await self.http.post(
            f"{self.base_url}/api/msd/write_remote",
            params={"url": url, "image": image_name},
            timeout=MSD_UPLOAD_TIMEOUT,
        )
        if not r.is_success:
            r.raise_for_status()
        # May be NDJSON progress; return text when not JSON.
        try:
            return {"image": image_name, "url": url, "result": _unwrap_ok_json(r)}
        except Exception:
            return {"image": image_name, "url": url, "raw": r.text, "status": r.status_code}

    async def msd_set_params(self, image: str, cdrom: bool = True, rw: bool = False) -> dict:
        if not self.http:
            raise RuntimeError("Not connected")
        r = await self.http.post(
            f"{self.base_url}/api/msd/set_params",
            params={
                "image": image,
                "cdrom": str(cdrom).lower(),
                "rw": str(rw).lower(),
            },
        )
        r.raise_for_status()
        return _unwrap_ok_json(r)

    async def msd_set_connected(self, connected: bool) -> dict:
        if not self.http:
            raise RuntimeError("Not connected")
        r = await self.http.post(
            f"{self.base_url}/api/msd/set_connected",
            params={"connected": str(connected).lower()},
        )
        r.raise_for_status()
        return _unwrap_ok_json(r)

    async def msd_mount(self, image_name: str, mode: str = "cdrom", read_only: bool = True) -> dict:
        mode_l = mode.lower().strip()
        cdrom_modes = {"cdrom", "cd", "iso"}
        disk_modes = {"disk", "hdd", "flash", "usb"}
        if mode_l in cdrom_modes:
            cdrom = True
        elif mode_l in disk_modes:
            cdrom = False
        else:
            raise ValueError(
                f"Unsupported MSD mode '{mode}'. Use one of: "
                f"{sorted(cdrom_modes | disk_modes)}"
            )
        params = await self.msd_set_params(image_name, cdrom=cdrom, rw=not read_only)
        connected = await self.msd_set_connected(True)
        return {"image": image_name, "mode": mode_l, "read_only": read_only, "params": params, "connected": connected}

    async def msd_unmount(self) -> dict:
        return await self.msd_set_connected(False)

    async def msd_remove(self, image_name: str) -> dict:
        if not self.http:
            raise RuntimeError("Not connected")
        r = await self.http.post(
            f"{self.base_url}/api/msd/remove",
            params={"image": image_name},
        )
        r.raise_for_status()
        return _unwrap_ok_json(r)

    async def msd_reset(self) -> dict:
        if not self.http:
            raise RuntimeError("Not connected")
        r = await self.http.post(f"{self.base_url}/api/msd/reset")
        r.raise_for_status()
        return _unwrap_ok_json(r)

    # Backward-compatible name used by older tools/tests.
    async def msd_upload(self, remote_path: str, data: bytes) -> dict:
        """Deprecated: prefer msd_upload_file. Writes bytes via raw MSD protocol."""
        if not self.http:
            raise RuntimeError("Not connected")
        image = Path(remote_path).name or "upload.bin"
        headers = {
            **self._auth_headers(),
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(data)),
        }
        r = await self.http.post(
            f"{self.base_url}/api/msd/write",
            params={"image": image},
            content=data,
            headers=headers,
            timeout=MSD_UPLOAD_TIMEOUT,
        )
        if not r.is_success:
            r.raise_for_status()
        return {"image": image, "path": remote_path, "size": len(data), "status": r.status_code}

    # ── WOL / recorder / metrics / stream / tailscale ──────────────

    async def wol_list(self) -> dict:
        if not self.http:
            raise RuntimeError("Not connected")
        r = await self.http.get(f"{self.base_url}/api/wol/list")
        r.raise_for_status()
        return _unwrap_ok_json(r)

    async def wol_scan(self) -> dict:
        if not self.http:
            raise RuntimeError("Not connected")
        r = await self.http.get(f"{self.base_url}/api/wol/scan")
        r.raise_for_status()
        return _unwrap_ok_json(r)

    async def wol_wake(self, mac: str) -> dict:
        if not self.http:
            raise RuntimeError("Not connected")
        r = await self.http.post(f"{self.base_url}/api/wol/wake", params={"mac": mac})
        r.raise_for_status()
        return _unwrap_ok_json(r)

    async def streamer_state(self) -> dict:
        if not self.http:
            raise RuntimeError("Not connected")
        r = await self.http.get(f"{self.base_url}/api/streamer")
        r.raise_for_status()
        return _unwrap_ok_json(r)

    async def streamer_set_params(self, **params: Any) -> dict:
        if not self.http:
            raise RuntimeError("Not connected")
        query = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in params.items() if v is not None}
        r = await self.http.post(f"{self.base_url}/api/streamer/set_params", params=query)
        r.raise_for_status()
        return _unwrap_ok_json(r)

    async def recorder_state(self) -> dict:
        if not self.http:
            raise RuntimeError("Not connected")
        r = await self.http.get(f"{self.base_url}/api/recorder")
        r.raise_for_status()
        return _unwrap_ok_json(r)

    async def recorder_start(self) -> dict:
        if not self.http:
            raise RuntimeError("Not connected")
        r = await self.http.post(f"{self.base_url}/api/recorder/start")
        r.raise_for_status()
        return _unwrap_ok_json(r)

    async def recorder_stop(self) -> dict:
        if not self.http:
            raise RuntimeError("Not connected")
        r = await self.http.post(f"{self.base_url}/api/recorder/stop")
        r.raise_for_status()
        return _unwrap_ok_json(r)

    async def prometheus_metrics(self) -> str:
        if not self.http:
            raise RuntimeError("Not connected")
        r = await self.http.get(f"{self.base_url}/api/export/prometheus/metrics")
        r.raise_for_status()
        return r.text

    async def tailscale_status(self) -> dict:
        if not self.http:
            raise RuntimeError("Not connected")
        r = await self.http.get(f"{self.base_url}/api/tailscale/status")
        r.raise_for_status()
        return _unwrap_ok_json(r)

    async def tailscale_config(self) -> dict:
        if not self.http:
            raise RuntimeError("Not connected")
        r = await self.http.get(f"{self.base_url}/api/tailscale/config")
        r.raise_for_status()
        return _unwrap_ok_json(r)

    async def redfish_system(self) -> dict:
        if not self.http:
            raise RuntimeError("Not connected")
        r = await self.http.get(f"{self.base_url}/redfish/v1/Systems/0")
        r.raise_for_status()
        return r.json()

    async def redfish_reset(self, reset_type: str) -> dict:
        if not self.http:
            raise RuntimeError("Not connected")
        r = await self.http.post(
            f"{self.base_url}/redfish/v1/Systems/0/Actions/ComputerSystem.Reset",
            json={"ResetType": reset_type},
        )
        r.raise_for_status()
        return {"reset_type": reset_type, "status": r.status_code}
