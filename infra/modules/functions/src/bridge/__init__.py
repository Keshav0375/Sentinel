"""Event Grid -> GitHub repository_dispatch bridge (architecture/infra.md §3.5).

The ONLY thing this function does is classify and forward. It never mutates
Azure, GitHub state, or the incident itself — HITL demands that GitHub Actions
is the executor and this stays a messenger (fire-and-forget).

Classification is the two-signal contract (§3.4): a Datadog event tagged
`deploy_status:failed` is a failed deploy (Case 2 — revert PR path); anything
else on this topic is a runtime error (Case 3 — full incident response). The
backend branches on `signal_type`, so this field is load-bearing.

stdlib-only HTTP (urllib) — keeps the remote build trivial and the dependency
surface at exactly one package (azure-functions, for the trigger binding).
"""
import json
import logging
import os
import urllib.request
import uuid

import azure.functions as func

# A named logger rather than the root one: it still propagates to the root
# handler the Functions worker installs, and the record carries its origin.
log = logging.getLogger(__name__)


def _tags_blob(data: dict) -> str:
    """Normalize Datadog's tags to one searchable string.

    The webhook template's $TAGS renders a COMMA-JOINED STRING; hand-crafted
    tests and some integrations send a list; the key can be absent or null.
    A naive " ".join() on the string form iterates it per CHARACTER — which
    silently misclassified every deploy failure as runtime_error (review
    blocker, 2026-08-23).
    """
    raw = data.get("tags") or []
    if isinstance(raw, str):
        blob = raw
    else:
        blob = " ".join(str(x) for x in raw)
    return blob + " " + str(data.get("title", ""))


def main(event: func.EventGridEvent) -> None:
    # GITHUB_TOKEN is a Key Vault *reference* resolved by App Service via the
    # function's system-assigned identity. When it cannot resolve (github-pat
    # not seeded, enable_bridge_reader grant missing, or the app not restarted
    # since), App Service hands over the LITERAL "@Microsoft.KeyVault(...)"
    # string. Sent as a token it 401s, the raise fails the delivery, and Event
    # Grid retries for 24h on what is a config gap, not a transient fault — so
    # log it and return. A real token that 401s/5xxs still raises and retries.
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token or token.startswith("@Microsoft.KeyVault"):
        log.error(
            "bridge: GITHUB_TOKEN is %s — github-pat unseeded, bridge KV grant "
            "missing, or app not restarted; event %s NOT dispatched",
            "an unresolved Key Vault reference" if token else "empty or unset",
            getattr(event, "id", None),
        )
        return

    data = event.get_json() or {}

    signal_type = "deploy_failure" if "deploy_status:failed" in _tags_blob(data) else "runtime_error"

    # Stable across Event Grid RE-DELIVERIES: the same event keeps the same
    # correlation id, so duplicate dispatches are dedupable downstream. A fresh
    # uuid4 per attempt would defeat exactly that.
    correlation_id = getattr(event, "id", None) or str(uuid.uuid4())

    payload = {
        "event_type": os.environ["GITHUB_EVENT_TYPE"],
        # GitHub HARD-CAPS client_payload at 10 top-level properties (422 above
        # it), and a real Datadog event easily exceeds that — so the event is
        # NESTED under one key instead of splatted (**data would 422 on every
        # live alert; review blocker, 2026-08-23). Consumers read
        # client_payload.event.*; signal_type stays top-level because the
        # backend branches on it before parsing anything else.
        "client_payload": {
            "signal_type": signal_type,
            "correlation_id": correlation_id,
            "event": data,
        },
    }

    req = urllib.request.Request(
        f"https://api.github.com/repos/{os.environ['GITHUB_REPO']}/dispatches",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        # 204 is the documented success for repository_dispatch.
        log.info("bridge: %s dispatched (HTTP %s)", signal_type, resp.status)
