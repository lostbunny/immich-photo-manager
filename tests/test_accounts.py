"""Tests for multi-account loading and switching."""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure src/ is on sys.path so tests can run standalone.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from immich_mcp_server.immich_client import ImmichClient  # noqa: E402
from immich_mcp_server import server as srv  # noqa: E402


SAMPLE_KEYRING_OUTPUT = """\
[/org/freedesktop/secrets/collection/login/15]
label = Immich API key (account=nathan)
secret = nathan-secret-key
created = 2026-05-10 23:03:20
modified = 2026-05-10 23:03:20
schema = org.freedesktop.Secret.Generic
attribute.account = nathan
attribute.base_url = http://nathan.example.com:2283
attribute.service = immich-mcp

[/org/freedesktop/secrets/collection/login/16]
label = Immich API key (account=tasha)
secret = tasha-secret-key
attribute.account = tasha
attribute.base_url = http://tasha.example.com:2283
attribute.service = immich-mcp

[/org/freedesktop/secrets/collection/login/17]
label = Some other unrelated entry
secret = bogus
attribute.service = immich-mcp
attribute.base_url = http://nope
"""


class ParseKeyringOutputTests(unittest.TestCase):
    def test_parses_two_accounts_and_skips_block_without_account_attr(self):
        result = ImmichClient._parse_keyring_search_output(SAMPLE_KEYRING_OUTPUT)
        self.assertEqual(set(result.keys()), {"nathan", "tasha"})
        self.assertEqual(
            result["nathan"],
            {"base_url": "http://nathan.example.com:2283",
             "api_key":  "nathan-secret-key"},
        )
        self.assertEqual(
            result["tasha"],
            {"base_url": "http://tasha.example.com:2283",
             "api_key":  "tasha-secret-key"},
        )

    def test_empty_input_returns_empty_dict(self):
        self.assertEqual(ImmichClient._parse_keyring_search_output(""), {})


class LoadAllAccountsTests(unittest.TestCase):
    def test_raises_runtime_error_when_keyring_empty(self):
        with patch.object(ImmichClient, "_secret_tool_search", return_value=""):
            with self.assertRaises(RuntimeError) as ctx:
                ImmichClient.load_all_accounts()
        self.assertIn("No Immich keyring entries", str(ctx.exception))
        self.assertIn("secret-tool store", str(ctx.exception))

    def test_loads_clients_from_keyring_output(self):
        with patch.object(
            ImmichClient, "_secret_tool_search", return_value=SAMPLE_KEYRING_OUTPUT
        ):
            clients = ImmichClient.load_all_accounts()
        self.assertEqual(set(clients.keys()), {"nathan", "tasha"})
        self.assertEqual(clients["nathan"].base_url, "http://nathan.example.com:2283")
        self.assertEqual(clients["tasha"].api_key, "tasha-secret-key")


class _FakeContext:
    """Minimal stand-in for FastMCP's Context with a writable lifespan_context."""

    def __init__(self, state: dict):
        class _Req:
            pass
        self.request_context = _Req()
        self.request_context.lifespan_context = state


def _call_tool(tool, *args, **kwargs):
    """FastMCP tool wrappers expose the underlying coroutine via .fn."""
    fn = getattr(tool, "fn", tool)
    return fn(*args, **kwargs)


class SwitchAccountTests(unittest.TestCase):
    def _run(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_ping_failure_keeps_active_unchanged(self):
        nathan = ImmichClient(base_url="http://nathan", api_key="nk")
        tasha = ImmichClient(base_url="http://tasha", api_key="tk")

        async def boom():
            raise RuntimeError("connection refused")

        tasha.ping = boom  # type: ignore[assignment]

        state = {"accounts": {"nathan": nathan, "tasha": tasha}, "active": "nathan"}
        ctx = _FakeContext(state)

        result_json = self._run(_call_tool(srv.switch_account, ctx, "tasha"))
        result = json.loads(result_json)

        self.assertFalse(result["success"])
        self.assertIn("Could not connect", result["error"])
        self.assertEqual(
            state["active"], "nathan",
            "active account must not change on ping failure",
        )

    def test_unknown_account_returns_error_with_available_list(self):
        nathan = ImmichClient(base_url="http://nathan", api_key="nk")
        state = {"accounts": {"nathan": nathan}, "active": "nathan"}
        ctx = _FakeContext(state)

        result = json.loads(
            self._run(_call_tool(srv.switch_account, ctx, "ghost"))
        )
        self.assertFalse(result["success"])
        self.assertIn("ghost", result["error"])
        self.assertIn("nathan", result["error"])
        self.assertEqual(state["active"], "nathan")


if __name__ == "__main__":
    unittest.main()
