"""
Smoke test for glkvm_mcp.py.

Imports the server module without launching the stdio loop and asserts that the
expected MCP tools are registered. Run from the repo root:

    python -m unittest tests.test_smoke

or directly:

    python tests/test_smoke.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
SERVER_PATH = os.path.join(REPO_ROOT, "glkvm_mcp.py")
MCP_CONFIG_PATH = os.path.join(REPO_ROOT, ".mcp.json")
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

_SERVER_MODULE = None

EXPECTED_TOOLS = {
    "kvm_connect",
    "kvm_disconnect",
    "kvm_send_text",
    "kvm_terminal_run",
    "kvm_send_keys",
    "kvm_hold_key",
    "kvm_release_all",
    "kvm_mouse_move",
    "kvm_mouse_move_pct",
    "kvm_mouse_click",
    "kvm_mouse_scroll",
    "kvm_screenshot",
    "kvm_screenshot_to_file",
    "kvm_ocr_status",
    "kvm_ocr_text",
    "kvm_ocr_screenshot",
    "kvm_ocr_click",
    "kvm_status",
    "comet_atx_power",
    "comet_atx_click",
    "comet_power_state",
    "comet_sysinfo",
    "comet_capabilities",
    "comet_media_state",
    "comet_media_upload",
    "comet_media_fetch",
    "comet_media_mount",
    "comet_media_unmount",
    "comet_media_remove",
    "comet_media_reset",
    "comet_wol_list",
    "comet_wol_scan",
    "comet_wol_wake",
    "comet_streamer_state",
    "comet_streamer_set_params",
    "comet_recorder_state",
    "comet_recorder_start",
    "comet_recorder_stop",
    "comet_metrics",
    "comet_tailscale_status",
    "comet_redfish_power",
    "comet_switch_state",
    "comet_switch_set_active",
    "comet_switch_set_port_name",
    "comet_switch_atx_power",
    "comet_switch_atx_click",
    "kvm_select_target",
    # Deprecated raw aliases remain public compatibility API.
    # Tier 1 stateful BIOS tools
    "bios_observe_state",
    "bios_crawl_step",
    "bios_crawl_region",
    "bios_navigate_to",
    "bios_propose_setting_change",
    "bios_apply_setting_change",
    "bios_save_and_reboot",
    "bios_abort_and_recover",
    "bios_export_trace",
    # Tier 3 perception + raw namespace
    "kvm_vlm_parse",
    "kvm_match_screen",
}


def _load_module():
    global _SERVER_MODULE
    if _SERVER_MODULE is not None:
        return _SERVER_MODULE
    spec = importlib.util.spec_from_file_location("glkvm_mcp", SERVER_PATH)
    assert spec is not None and spec.loader is not None, "could not load glkvm_mcp.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["glkvm_mcp"] = mod
    spec.loader.exec_module(mod)
    _SERVER_MODULE = mod
    return mod


class SmokeTest(unittest.TestCase):
    def test_module_imports(self):
        mod = _load_module()
        self.assertTrue(hasattr(mod, "mcp"), "module should expose `mcp` (FastMCP instance)")

    def test_tools_registered(self):
        mod = _load_module()
        tools = asyncio.run(mod.mcp.list_tools())
        names = {t.name for t in tools}
        missing = EXPECTED_TOOLS - names
        self.assertFalse(
            missing,
            msg=f"Missing tools: {sorted(missing)}. Got: {sorted(names)}",
        )

    def test_kvm_connect_signature(self):
        mod = _load_module()
        tools = asyncio.run(mod.mcp.list_tools())
        connect = next((t for t in tools if t.name == "kvm_connect"), None)
        self.assertIsNotNone(connect, "kvm_connect tool not registered")
        schema = connect.inputSchema
        required = set(schema.get("required", []))
        self.assertEqual(
            required, set(),
            msg=f"kvm_connect is an optional override and must require nothing, got {required}",
        )
        properties = schema.get("properties", {})
        password_schema = properties.get("password", {})
        self.assertIsNone(
            password_schema.get("default"),
            msg="kvm_connect should expose no non-null password default in its schema",
        )
        username_default = properties.get("username", {}).get("default")
        self.assertIsNone(
            username_default,
            msg=(
                "kvm_connect username must default to null so the resolved default "
                f"username is used, got {username_default!r}"
            ),
        )
        self.assertIn(
            "force_reconnect", properties,
            msg="kvm_connect must expose force_reconnect to replace a live session",
        )

    def test_kvm_connect_explicit_empty_password_is_rejected(self):
        import src.kvm_core.tools as kvm_tools

        with self.assertRaisesRegex(ValueError, "No Comet password available"):
            asyncio.run(kvm_tools.kvm_connect("192.0.2.1", password=""))

    def test_kvm_connect_fetches_password_from_doppler_when_omitted(self):
        import src.kvm_core.tools as kvm_tools
        import src.kvm_core.doppler_credentials as doppler_credentials

        class FakeClient:
            base_url = "https://192.0.2.1"
            capabilities = {"features": {}}

            def is_connected(self):
                return True

        class FakeTarget:
            def __init__(self, client):
                self.client = client

        class FakeRuntime:
            """Cold managed runtime: no live session until connect() runs."""

            def __init__(self):
                self.client = None
                self.targets = {}
                self.selected_target = "default"
                self.received = None

            async def connect(self, host, username, password, target="default", select=True):
                self.received = (host, username, password, target)
                self.client = FakeClient()
                self.targets[target] = FakeTarget(self.client)
                return True

            async def ensure_connected(self, target=None):
                return self.client

            def get_client(self, target=None):
                return self.client

            def is_connected(self, target=None):
                return self.client is not None

        runtime = FakeRuntime()
        with patch.object(doppler_credentials, "resolve_comet_password", return_value="doppler-secret"):
            with patch("src.kvm_core.tools_core.get_kvm_runtime", return_value=runtime):
                result = asyncio.run(kvm_tools.kvm_connect("192.0.2.1"))

        self.assertTrue(result["connected"])
        self.assertEqual(runtime.received, ("192.0.2.1", "admin", "doppler-secret", "default"))

    def test_bundled_mcp_launcher_uses_uv_not_doppler_env_injection(self):
        import json

        with open(MCP_CONFIG_PATH, encoding="utf-8") as config_file:
            server = json.load(config_file)["mcpServers"]["comet-kvm"]

        self.assertEqual(server["command"], "uv")
        self.assertEqual(
            server["args"],
            ["run", "--locked", "--python", "3.13", "python", "./glkvm_mcp.py"],
        )

    def test_kvm_core_tools_do_not_import_sidecar(self):
        code = """
import asyncio
import sys
import src.kvm_core.tools
from src.kvm_core.server import mcp
tool_names = {tool.name for tool in asyncio.run(mcp.list_tools())}
assert not any(name.startswith('bios_') for name in tool_names), sorted(tool_names)
assert not any(name.startswith('src.bios_sidecar') for name in sys.modules), 'sidecar imported by kvm core'
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)

    def test_sidecar_runtime_reconfigures_existing_kvm_cache(self):
        from src.bios_sidecar.controller.runtime import StatefulBiosRuntime
        import src.kvm_core.runtime as kvm_runtime

        with tempfile.TemporaryDirectory() as temp_dir:
            initial_cache = os.path.join(temp_dir, "initial-screenshots")
            requested_cache = os.path.join(temp_dir, "requested-screenshots")
            core = kvm_runtime.KVMRuntime(screenshot_cache=initial_cache)
            runtime = StatefulBiosRuntime(
                db_path=":memory:",
                screenshot_cache=requested_cache,
                kvm_runtime=core,
            )
            try:
                self.assertEqual(runtime.capture_mgr.cache_dir, requested_cache)
            finally:
                asyncio.run(runtime.vlm_client.close())
                runtime.store.close()

    def test_media_upload_preserves_file_read_error_as_cause(self):
        import src.kvm_core.tools as kvm_tools
        from src.kvm_core.runtime import KVMRuntime, TargetRuntime
        from tests.bios_test_helpers import ScriptedCometClient, installed_kvm_runtime

        with tempfile.TemporaryDirectory() as temp_dir:
            missing_path = os.path.join(temp_dir, "missing.iso")
            runtime = KVMRuntime(screenshot_cache=os.path.join(temp_dir, "shots"))
            client = ScriptedCometClient()
            runtime.targets["default"] = TargetRuntime("default", client)
            runtime._sync_selected_client()
            with installed_kvm_runtime(runtime):
                with self.assertRaises(ValueError) as raised:
                    asyncio.run(kvm_tools.comet_media_upload(missing_path, "missing.iso"))

        self.assertIsInstance(raised.exception.__cause__, FileNotFoundError)


if __name__ == "__main__":
    unittest.main(verbosity=2)
