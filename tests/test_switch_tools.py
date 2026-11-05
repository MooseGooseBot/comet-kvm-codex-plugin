"""Comet X (GL-RM4PE) multi-port switch: client contract and tool behaviour.

Firmware facts come from gl-inet/glkvm ``kvmd/apps/kvmd/api/switch.py`` at the
pinned corpus commit: query parameters, 0-based ``port``, and a SwitchApi that
exists only when the hardware model is ``rm4pe``.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional
from unittest.mock import patch

import pytest

from src.kvm_core.comet.client import CometClient, SwitchUnavailable
from src.kvm_core.runtime import TargetRuntime
from src.kvm_core.server import mcp
import src.kvm_core.tools_switch as tools_switch
from tests.bios_test_helpers import ConnectableScriptedClient
from tests.local_services import CometProtocolService

TOOLS_RUNTIME_PATH = "src.kvm_core.tools_core.get_kvm_runtime"


def run(coro):
    return asyncio.run(coro)


def switch_state(active: int = 0, ports: int = 4, synced: bool = True) -> dict:
    return {
        "model": {
            "firmware": {"version": 1},
            "units": [],
            "ports": [
                {"unit": 0, "channel": ch, "name": f"Port {ch + 1}", "id": f"1.{ch + 1}"}
                for ch in range(ports)
            ],
        },
        "summary": {"active_port": active, "active_id": f"1.{active + 1}", "synced": synced},
        "video": {"links": [True, False, True, False][:ports]},
        "usb_otg": {"links": [True, True, False, False][:ports]},
    }


# ---------------------------------------------------------------------------
# Client contract against the loopback Comet service
# ---------------------------------------------------------------------------


def test_client_switch_routes_use_query_params_and_zero_based_port():
    async def scenario() -> tuple[CometProtocolService, list[Any]]:
        async with CometProtocolService() as service:
            service.respond("/api/switch", 200, {"ok": True, "result": switch_state(active=2)})
            client = CometClient(service.base_url, username="admin", password="secret", verify_ssl=False)
            client.ws_ping_period = 0.01
            results = []
            try:
                assert await client.connect() is True
                results.append(await client.switch_state())
                results.append(await client.switch_set_active(2))
                results.append(await client.switch_set_port_params(1, name="pup", dummy=True, edid_id=None))
                results.append(await client.switch_atx_power(3, "reset"))
                results.append(await client.switch_atx_click(0, "power_long"))
                results.append(client.capabilities["features"]["switch"])
            finally:
                await client.disconnect()
            return service, results

    service, results = run(scenario())
    state, set_active, params, power, click, feature = results
    assert state["summary"]["active_port"] == 2
    assert feature is True

    posts = [row for row in service.requests if row["method"] == "POST" and row["path"].startswith("/api/switch")]
    assert [(row["path"], row["query"]) for row in posts] == [
        ("/api/switch/set_active", {"port": "2"}),
        ("/api/switch/set_port_params", {"port": "1", "name": "pup", "dummy": "true"}),
        ("/api/switch/atx/power", {"port": "3", "action": "reset_hard"}),
        ("/api/switch/atx/click", {"port": "0", "button": "power_long"}),
    ]
    assert all(row["body"] == b"" for row in posts)
    assert set_active["status"] == 200
    assert power == {"port": 3, "action": "reset_hard", "status": 200, "result": {}}
    assert click["port"] == 0 and click["button"] == "power_long"
    assert params["status"] == 200


def test_client_reports_switch_unavailable_on_single_port_comet():
    async def scenario() -> tuple[Optional[BaseException], bool]:
        async with CometProtocolService() as service:
            service.respond("/api/switch", 404, {"ok": False, "result": {"error": "not found"}})
            client = CometClient(service.base_url, username="admin", password="secret", verify_ssl=False)
            client.ws_ping_period = 0.01
            try:
                assert await client.connect() is True
                feature = client.capabilities["features"]["switch"]
                try:
                    await client.switch_state()
                except SwitchUnavailable as exc:
                    return exc, feature
                return None, feature
            finally:
                await client.disconnect()

    exc, feature = run(scenario())
    assert feature is False
    assert isinstance(exc, SwitchUnavailable)
    assert "GL-RM4PE" in str(exc)


def test_client_rejects_bad_actions_and_params_before_sending():
    client = CometClient("http://127.0.0.1:1", username="admin", password="x", verify_ssl=False)
    client.http = object()  # never used: validation fails first
    with pytest.raises(ValueError):
        run(client.switch_atx_power(0, "explode"))
    with pytest.raises(ValueError):
        run(client.switch_atx_click(0, "eject"))
    with pytest.raises(ValueError):
        run(client.switch_set_port_params(0, colour="red"))


# ---------------------------------------------------------------------------
# Tools against a scripted client
# ---------------------------------------------------------------------------


class SwitchScriptedClient(ConnectableScriptedClient):
    """Scripted Comet X: records switch calls and flips active_port after N polls."""

    def __init__(self, *, polls_until_switched: int = 2, ports: int = 4, available: bool = True) -> None:
        super().__init__(connected=True)
        self.active = 0
        self.ports = ports
        self.available = available
        self.polls_until_switched = polls_until_switched
        self.pending: Optional[int] = None
        self.calls: list[tuple[str, Any]] = []
        self.state_reads = 0

    async def switch_state(self) -> dict:
        if not self.available:
            raise SwitchUnavailable("This Comet has no multi-port switch API")
        self.state_reads += 1
        if self.pending is not None:
            self.polls_until_switched -= 1
            if self.polls_until_switched <= 0:
                self.active = self.pending
                self.pending = None
        return switch_state(active=self.active, ports=self.ports)

    async def switch_set_active(self, port: int) -> dict:
        self.calls.append(("set_active", port))
        self.pending = port
        return {"status": 200, "result": {}}

    async def switch_set_port_params(self, port: int, **params: Any) -> dict:
        self.calls.append(("set_port_params", (port, params)))
        return {"status": 200, "result": {}}

    async def switch_atx_power(self, port: int, action: str) -> dict:
        self.calls.append(("atx_power", (port, action)))
        return {"port": port, "action": action, "status": 200, "result": {}}

    async def switch_atx_click(self, port: int, button: str) -> dict:
        self.calls.append(("atx_click", (port, button)))
        return {"port": port, "button": button, "status": 200, "result": {}}


class RuntimeStub:
    def __init__(self, client: SwitchScriptedClient) -> None:
        self.client = client
        self.selected_target = "default"
        self.targets = {"default": TargetRuntime("default", client)}
        self.ensure_calls: list[Optional[str]] = []

    async def ensure_connected(self, target: Optional[str] = None):
        self.ensure_calls.append(target)
        return self.client

    def is_connected(self, target: Optional[str] = None) -> bool:
        return True


def install(client: SwitchScriptedClient):
    runtime = RuntimeStub(client)
    return patch(TOOLS_RUNTIME_PATH, return_value=runtime), runtime


def test_switch_tools_are_registered_with_annotations():
    tools = {t.name: t for t in run(mcp.list_tools())}
    assert tools["comet_switch_state"].annotations.readOnlyHint is True
    assert tools["comet_switch_set_active"].annotations.destructiveHint is True
    assert tools["comet_switch_atx_power"].annotations.destructiveHint is True
    assert tools["comet_switch_atx_click"].annotations.destructiveHint is True
    assert tools["comet_switch_set_port_name"].annotations.destructiveHint is False
    assert set(tools["comet_switch_set_active"].inputSchema["required"]) == {"port"}


def test_set_active_posts_then_confirms_with_get(monkeypatch):
    monkeypatch.setattr(tools_switch, "CONFIRM_POLL_S", 0.0)
    client = SwitchScriptedClient(polls_until_switched=2)
    patcher, runtime = install(client)
    with patcher:
        result = run(tools_switch.comet_switch_set_active(2))
    assert client.calls == [("set_active", 2)]
    assert result["confirmed"] is True
    assert result["previous_port"] == 0
    assert result["active_port"] == 2
    assert "comet_media_mount" in result["warning"]
    assert "error" not in result
    assert runtime.ensure_calls == [None]


def test_set_active_reports_unconfirmed_when_firmware_never_syncs(monkeypatch):
    monkeypatch.setattr(tools_switch, "CONFIRM_POLL_S", 0.0)
    client = SwitchScriptedClient(polls_until_switched=10**6)
    patcher, _ = install(client)
    with patcher:
        result = run(tools_switch.comet_switch_set_active(3, confirm_timeout_s=0.05))
    assert client.calls == [("set_active", 3)]
    assert result["confirmed"] is False
    assert result["active_port"] == 0
    assert "did not become 3" in result["error"]


def test_set_active_rejects_out_of_range_port_without_posting():
    client = SwitchScriptedClient(ports=4)
    patcher, _ = install(client)
    with patcher:
        with pytest.raises(ValueError, match="out of range"):
            run(tools_switch.comet_switch_set_active(4))
        with pytest.raises(ValueError):
            run(tools_switch.comet_switch_set_active(-1))
        with pytest.raises(ValueError):
            run(tools_switch.comet_switch_set_active(True))  # type: ignore[arg-type]
    assert client.calls == []


def test_single_port_comet_surfaces_switch_unavailable():
    client = SwitchScriptedClient(available=False)
    patcher, _ = install(client)
    with patcher:
        with pytest.raises(SwitchUnavailable):
            run(tools_switch.comet_switch_state())
        with pytest.raises(SwitchUnavailable):
            run(tools_switch.comet_switch_set_active(1))
    assert client.calls == []


def test_per_port_atx_and_name_tools_forward_port_and_arguments():
    client = SwitchScriptedClient()
    patcher, _ = install(client)
    with patcher:
        power = run(tools_switch.comet_switch_atx_power(3, "reset"))
        click = run(tools_switch.comet_switch_atx_click(1, "power"))
        named = run(tools_switch.comet_switch_set_port_name(2, "  dragon  q8b "))
        with pytest.raises(ValueError):
            run(tools_switch.comet_switch_set_port_name(2, "   "))
    assert client.calls == [
        ("atx_power", (3, "reset")),
        ("atx_click", (1, "power")),
        ("set_port_params", (2, {"name": "dragon  q8b"})),
    ]
    assert power["port"] == 3 and click["port"] == 1
    assert named["port"] == 2 and named["name"] == "dragon  q8b"
