"""Offline contract tests for the quota backend. Never calls a model or a provider.

Run with the Python that owns your Hermes install (it needs `fastapi`,
`httpx`, `tui_gateway` and `agent.account_usage` on the path):

    python tests/test_backend.py

Every provider response here comes from `httpx.MockTransport`, so the suite is
safe to run with real credentials exported.
"""

import importlib.util
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:  # Hermes runtime — the only import the suite cannot fake
    import fastapi  # noqa: F401
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
    from hermes_constants import get_hermes_home
    from tui_gateway import server
except Exception as exc:  # pragma: no cover - environment problem, not a code path
    HERMES_IMPORT_ERROR = exc
else:
    HERMES_IMPORT_ERROR = None

PLUGIN_API = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
GATEWAY_MEMBERS = ("_sessions", "_sessions_lock", "_session_uses_compute_host")
GATEWAY_API = HERMES_IMPORT_ERROR is None and all(hasattr(server, name) for name in GATEWAY_MEMBERS)


def load_plugin_api():
    """Import the shipped router by path — the plugin is not an installable package."""
    spec = importlib.util.spec_from_file_location("provider_quota_api_under_test", PLUGIN_API)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipIf(HERMES_IMPORT_ERROR, f"Hermes runtime not importable: {HERMES_IMPORT_ERROR}")
class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.api = load_plugin_api()
        self.api._CACHE.clear()

    def test_account_identity_cache_and_honest_failure(self):
        """A cached read is reused per credential; failures degrade to unavailable."""
        snap = AccountUsageSnapshot("openai-codex", "offline-test", datetime.now(timezone.utc),
                                    windows=(AccountUsageWindow("Session", 28),))
        with patch.object(self.api, "account_snapshot", return_value=snap) as fetch:
            first = self.api.read_quota(("openai-codex", "https://example.invalid", "secret-A"))
            self.assertTrue(first["available"])
            self.assertEqual(first["windows"][0]["used_percent"], 28)
            self.assertIs(first, self.api.read_quota(("openai-codex", "https://example.invalid", "secret-A")))
            self.assertEqual(fetch.call_count, 1)
            self.api.read_quota(("openai-codex", "https://example.invalid", "secret-B"))
            self.assertEqual(fetch.call_count, 2)
            self.assertNotIn("secret-", str(first))
            self.assertFalse(self.api.read_quota(("not-supported", "", ""))["available"])
            self.assertEqual(fetch.call_count, 2)
        with patch.object(self.api, "account_snapshot", side_effect=RuntimeError("private credential")):
            failed = self.api.read_quota(("anthropic", "", "different"))
            self.assertFalse(failed["available"])
            self.assertNotIn("private credential", str(failed))

    @unittest.skipUnless(GATEWAY_API, f"Hermes runtime lacks the gateway session registry {GATEWAY_MEMBERS}")
    def test_route_resolves_the_live_sessions_own_credentials(self):
        """The route reads the focused session's agent — never a configured fallback account."""
        app = FastAPI()
        app.include_router(self.api.router, prefix="/api/plugins/provider-quota")
        client = TestClient(app)
        agent = SimpleNamespace(provider="openai-codex", base_url="https://example.invalid", api_key="route-secret")
        session = {"agent": agent, "profile_home": str(get_hermes_home())}
        snapshot = AccountUsageSnapshot("openai-codex", "offline-test", datetime.now(timezone.utc),
                                        windows=(AccountUsageWindow("Weekly", 72, datetime.now(timezone.utc)),))
        with patch.dict(server._sessions, {"quota-offline": session}), \
                patch.object(server, "_session_uses_compute_host", return_value=False), \
                patch.object(self.api, "account_snapshot", return_value=snapshot):
            response = client.get("/api/plugins/provider-quota/quota?session_id=quota-offline")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["available"])
            self.assertIsInstance(response.json()["windows"][0]["reset_at"], str)
            self.assertNotIn("route-secret", response.text)

            self.assertEqual(client.get("/api/plugins/provider-quota/quota?session_id=absent").status_code, 404)

            session["agent"] = None
            idle = client.get("/api/plugins/provider-quota/quota?session_id=quota-offline").json()
            self.assertFalse(idle["available"])

            session["profile_home"] = str(Path(get_hermes_home()) / "profiles/other")
            self.assertEqual(client.get("/api/plugins/provider-quota/quota?session_id=quota-offline").status_code, 409)

        self.assertEqual(client.get("/api/plugins/provider-quota/quota").status_code, 422)

    @unittest.skipUnless(GATEWAY_API, f"Hermes runtime lacks the gateway session registry {GATEWAY_MEMBERS}")
    def test_compute_host_sessions_do_not_borrow_local_quota(self):
        session = {"agent": SimpleNamespace(provider="openai-codex", base_url="", api_key="k"),
                   "profile_home": str(get_hermes_home())}
        with patch.dict(server._sessions, {"quota-remote": session}), \
                patch.object(server, "_session_uses_compute_host", return_value=True):
            self.assertIsNone(self.api.session_credentials("quota-remote"))

    @unittest.skipUnless(GATEWAY_API, f"Hermes runtime lacks the gateway session registry {GATEWAY_MEMBERS}")
    def test_moved_gateway_internals_fail_closed(self):
        """A renamed internal must read as 'unavailable', never as another account's quota."""
        removed = {name: server.__dict__.pop(name) for name in GATEWAY_MEMBERS if name in server.__dict__}
        try:
            with self.assertRaises(RuntimeError):
                self.api.session_credentials("quota-anything")
            self.assertFalse(self.api.read_quota(None)["available"])
        finally:
            server.__dict__.update(removed)

    def test_opencode_go_reads_every_allowance_window(self):
        import httpx
        payload = {"usage": {key: {"status": "ok", "percent": percent,
                                   "resetsAt": "2026-12-01T12:00:00.000Z"}
                             for key, percent in (("rolling", 1), ("weekly", 24), ("monthly", 63))}}

        def serve(request):
            self.assertEqual(str(request.url), "https://opencode.ai/zen/go/v1/usage")
            self.assertEqual(request.headers["authorization"], "Bearer offline-opencode-key")
            return httpx.Response(200, json=payload)

        client = httpx.Client(transport=httpx.MockTransport(serve))
        with patch("httpx.Client", return_value=client):
            result = self.api.read_quota(
                ("opencode-go", "https://opencode.ai/inference/go/openai/v1", "offline-opencode-key"))

        self.assertTrue(result["available"])
        self.assertEqual([w["used_percent"] for w in result["windows"]], [1, 24, 63])
        self.assertEqual([w["label"] for w in result["windows"]], ["Rolling", "Weekly", "Monthly"])
        self.assertNotIn("offline-opencode-key", str(result))

    def test_opencode_go_never_invents_remaining_quota(self):
        """4xx, malformed and partial payloads all read as unavailable — never as 0% used."""
        import httpx
        valid = {"usage": {key: {"percent": 10, "resetsAt": "2026-12-01T12:00:00Z"}
                           for key in ("rolling", "weekly", "monthly")}}
        bad_percent = deepcopy(valid)
        bad_percent["usage"]["rolling"]["percent"] = True
        missing_window = deepcopy(valid)
        del missing_window["usage"]["monthly"]
        no_tz = deepcopy(valid)
        no_tz["usage"]["rolling"]["resetsAt"] = "2026-12-01T12:00:00"

        for status, payload in ((401, {}), (429, {}), (200, bad_percent), (200, missing_window), (200, no_tz)):
            with self.subTest(status=status, payload=payload):
                self.api._CACHE.clear()
                client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, json=payload)))
                with patch("httpx.Client", return_value=client):
                    result = self.api.read_quota(
                        ("opencode-go", "https://opencode.ai/zen/go/v1", "offline-key"))
                self.assertFalse(result["available"])
                self.assertEqual(result["windows"], [])

    def test_opencode_go_refuses_to_send_the_key_anywhere_else(self):
        with patch("httpx.Client") as client:
            for base_url in ("http://opencode.ai/zen/go/v1",
                             "https://opencode.ai.evil.test/zen/go/v1",
                             "https://example.invalid/zen/go/v1",
                             "https://opencode.ai/zen/go/v1?x=1"):
                self.api._CACHE.clear()
                self.assertFalse(self.api.read_quota(("opencode-go", base_url, "offline-key"))["available"])
            client.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
