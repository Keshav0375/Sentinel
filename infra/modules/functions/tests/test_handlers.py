"""Unit tests for the bridge and rotate handlers (tasks 3.3 / 3.6).

stdlib-only on purpose: the handlers import `azure.functions` (present in the
Function host, absent on dev boxes and CI runners), so a fake module is
injected before import. Every outbound HTTP call is intercepted at
urllib.request.urlopen — no network, no credentials, no Azure.

Run:  python -m unittest discover modules/functions/tests -v
"""
import importlib.util
import json
import os
import sys
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"


# ── fake azure.functions, just enough for the handlers ────────────────────────
class _FakeEventGridEvent:
    def __init__(self, payload, event_id="evt-42"):
        self._payload = payload
        self.id = event_id

    def get_json(self):
        return self._payload


def _install_fake_azure_functions():
    azure = types.ModuleType("azure")
    azure.__path__ = []
    functions = types.ModuleType("azure.functions")
    functions.EventGridEvent = _FakeEventGridEvent
    sys.modules["azure"] = azure
    sys.modules["azure.functions"] = functions


def _load(name, rel):
    _install_fake_azure_functions()
    spec = importlib.util.spec_from_file_location(name, SRC / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _CapturedRequest:
    """Context-manager stand-in for urlopen's response, recording the request."""

    def __init__(self, store):
        self.store = store
        self.status = 204

    def __call__(self, req, timeout=None):
        self.store.append(req)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b"{}"


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = _load("bridge_handler", "bridge/__init__.py")
        self.env = {
            "GITHUB_EVENT_TYPE": "incident-alert",
            "GITHUB_REPO": "Keshav0375/Sentinel",
            "GITHUB_TOKEN": "test-token",
        }

    def _run(self, payload):
        sent = []
        with mock.patch.dict("os.environ", self.env, clear=False), \
             mock.patch.object(self.bridge.urllib.request, "urlopen", _CapturedRequest(sent)):
            self.bridge.main(_FakeEventGridEvent(payload))
        self.assertEqual(len(sent), 1, "exactly one dispatch per event")
        req = sent[0]
        return req, json.loads(req.data)

    def test_deploy_failure_classified_from_tags_list(self):
        _, body = self._run({"tags": ["deploy_status:failed", "service:sentinel-watchtower"], "title": "x"})
        self.assertEqual(body["client_payload"]["signal_type"], "deploy_failure")

    def test_runtime_error_is_the_default(self):
        _, body = self._run({"tags": ["service:sentinel-watchtower"], "title": "5xx spike"})
        self.assertEqual(body["client_payload"]["signal_type"], "runtime_error")

    def test_classifier_reads_title_when_tags_missing(self):
        # Datadog webhook bodies vary: no tags key at all, marker in the title.
        _, body = self._run({"title": "ALERT deploy_status:failed on sentinel-watchtower"})
        self.assertEqual(body["client_payload"]["signal_type"], "deploy_failure")

    def test_payload_nests_event_under_one_key(self):
        # GitHub caps client_payload at 10 TOP-LEVEL properties (422 beyond) —
        # so the Datadog event nests under "event" and the top level stays at 3.
        data = {f"k{i}": i for i in range(15)} | {"tags": ["t"], "title": "T"}
        _, body = self._run(data)
        cp = body["client_payload"]
        self.assertEqual(sorted(cp), ["correlation_id", "event", "signal_type"])
        self.assertEqual(cp["event"], data, "full event preserved, nested")
        self.assertEqual(body["event_type"], "incident-alert")

    def test_tags_as_comma_joined_string(self):
        # Datadog's webhook template renders $TAGS as ONE comma-joined string —
        # the shape that broke the original " ".join (per-character iteration).
        _, body = self._run({"tags": "deploy_status:failed,service:sentinel-watchtower", "title": "x"})
        self.assertEqual(body["client_payload"]["signal_type"], "deploy_failure")

    def test_tags_null_does_not_crash(self):
        _, body = self._run({"tags": None, "title": "boom"})
        self.assertEqual(body["client_payload"]["signal_type"], "runtime_error")

    def test_correlation_id_is_stable_across_redeliveries(self):
        # Event Grid re-delivers on failure; the SAME event must keep the SAME
        # correlation id or duplicates are undedupable downstream.
        _, b1 = self._run({"title": "x"})
        _, b2 = self._run({"title": "x"})
        self.assertEqual(b1["client_payload"]["correlation_id"], "evt-42")
        self.assertEqual(b2["client_payload"]["correlation_id"], "evt-42")

    def test_dispatch_target_and_auth(self):
        req, _ = self._run({"title": "x"})
        self.assertEqual(req.full_url, "https://api.github.com/repos/Keshav0375/Sentinel/dispatches")
        self.assertEqual(req.get_header("Authorization"), "token test-token")

    def test_flat_datadog_body_as_custom_schema_data(self):
        # The topic takes Datadog's FLAT webhook body under CustomEventSchema and
        # re-delivers it as EventGridSchema with the whole body as `data`
        # (decision 2026-10-05). This is exactly what get_json() returns then.
        data = {
            "title": "[Triggered] sentinel-watchtower deploy failed",
            "tags": "deploy_status:failed,service:sentinel-watchtower,env:dev",
            "alert_transition": "Triggered",
            "link": "https://us5.datadoghq.com/monitors/123",
            "alert_id": "123",
            "date": "1791158400000",
        }
        _, body = self._run(data)
        cp = body["client_payload"]
        self.assertEqual(cp["signal_type"], "deploy_failure")
        self.assertEqual(cp["event"], data, "flat body forwarded unchanged")
        _, body = self._run(data | {"tags": "service:sentinel-watchtower,env:dev",
                                    "title": "[Triggered] 5xx rate high"})
        self.assertEqual(body["client_payload"]["signal_type"], "runtime_error")

    def test_empty_event_still_dispatches_runtime_error(self):
        _, body = self._run({})
        self.assertEqual(body["client_payload"]["signal_type"], "runtime_error")

    _UNSET = object()

    def _run_with_token(self, token, opener):
        """Run the bridge with GITHUB_TOKEN = token, or with it absent if _UNSET."""
        env = self.env | ({} if token is self._UNSET else {"GITHUB_TOKEN": token})
        with mock.patch.dict("os.environ", env, clear=False), \
             mock.patch.object(self.bridge.urllib.request, "urlopen", opener):
            if token is self._UNSET:
                # Inside patch.dict, so the caller's environment is restored after.
                os.environ.pop("GITHUB_TOKEN", None)
            self.bridge.main(_FakeEventGridEvent({"title": "x"}))

    def _assert_skipped(self, token, expect_in_log):
        """The bridge must log an ERROR, send nothing, and never log the value."""
        sent = []
        with self.assertLogs(self.bridge.log, level="DEBUG") as logs:
            self._run_with_token(token, _CapturedRequest(sent))
        self.assertEqual(sent, [], "must not call GitHub without a usable token")
        self.assertTrue(any(r.levelname == "ERROR" for r in logs.records), logs.output)
        text = "\n".join(logs.output)
        self.assertIn(expect_in_log, text)
        self.assertNotIn("@Microsoft.KeyVault", text, "the reference string must not be logged")
        if isinstance(token, str) and token:
            self.assertNotIn(token, text, "the token value must not be logged")

    def test_unresolved_kv_reference_logs_and_does_not_dispatch(self):
        # Unseeded github-pat (live 2026-10-07): App Service hands the app the
        # LITERAL reference. Raising would make Event Grid retry for 24h on a
        # config gap — so log an error, send nothing, return.
        self._assert_skipped("@Microsoft.KeyVault(VaultName=v;SecretName=github-pat)",
                             "unresolved Key Vault reference")

    def test_empty_token_logs_and_does_not_dispatch(self):
        self._assert_skipped("", "empty or unset")

    def test_unset_token_logs_and_does_not_dispatch(self):
        # The app setting missing altogether (not just blank) — a KeyError
        # here would fail every delivery exactly like the literal did.
        self._assert_skipped(self._UNSET, "empty or unset")

    def test_real_http_error_still_raises_so_event_grid_retries(self):
        # A real token that GitHub rejects (401) or a 5xx is not a config gap
        # this code can see — it must raise so the delivery fails and retries.
        def reject(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Bad credentials", {}, None)

        with self.assertRaises(urllib.error.HTTPError):
            self._run_with_token("github_pat_real", reject)


class RotateTests(unittest.TestCase):
    def setUp(self):
        self.rotate = _load("rotate_handler", "rotate/__init__.py")

    def _run(self, payload, env):
        sent = []
        with mock.patch.dict("os.environ", env, clear=False), \
             mock.patch.object(self.rotate.urllib.request, "urlopen", _CapturedRequest(sent)):
            self.rotate.main(_FakeEventGridEvent(payload))
        return sent

    def test_non_llm_secret_is_ignored(self):
        sent = self._run({"ObjectName": "github-pat"}, {"TEAMS_WEBHOOK_URL": "https://t"})
        self.assertEqual(sent, [], "only the two LLM keys are rotatable")

    def test_manual_path_posts_to_teams(self):
        sent = self._run({"ObjectName": "openai-api-key"},
                         {"TEAMS_WEBHOOK_URL": "https://teams.example/hook",
                          "ANTHROPIC_ADMIN_KEY": ""})
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].full_url, "https://teams.example/hook")
        self.assertIn("manual", json.loads(sent[0].data)["text"].lower().replace("cannot be auto-rotated", "manual"))

    def test_missing_webhook_does_not_crash(self):
        # B8 open: no webhook seeded. The event re-fires on schedule; the
        # handler must log-and-return, never raise.
        sent = self._run({"ObjectName": "anthropic-api-key"},
                         {"TEAMS_WEBHOOK_URL": "", "ANTHROPIC_ADMIN_KEY": ""})
        self.assertEqual(sent, [])

    def test_unresolved_kv_reference_does_not_crash(self):
        # App Service hands the app the LITERAL "@Microsoft.KeyVault(...)" when
        # the reference cannot resolve — not an empty string. urlopen on that
        # literal raises "unknown url type" and would kill the escalation path.
        sent = self._run({"ObjectName": "openai-api-key"},
                         {"TEAMS_WEBHOOK_URL": "@Microsoft.KeyVault(VaultName=v;SecretName=s)",
                          "ANTHROPIC_ADMIN_KEY": ""})
        self.assertEqual(sent, [], "must log and return, never call urlopen")

    def test_anthropic_without_admin_key_takes_manual_path(self):
        sent = self._run({"ObjectName": "anthropic-api-key"},
                         {"TEAMS_WEBHOOK_URL": "https://teams.example/hook",
                          "ANTHROPIC_ADMIN_KEY": ""})
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].full_url, "https://teams.example/hook")


if __name__ == "__main__":
    unittest.main()
