"""Key Vault SecretNearExpiry -> rotate or escalate (architecture/infra.md §3.8).

Fires ~30 days before an LLM key expires. Two paths:

  1. AUTOMATED — only if an Anthropic *admin* key is configured
     (ANTHROPIC_ADMIN_KEY): mint a replacement via the Admin API, write it as a
     NEW VERSION of the same secret with a fresh 90-day expiry. Consumers read
     "latest", so no redeploy is needed.
  2. MANUAL — everything else (including openai-api-key: OpenAI has no key-mint
     API): post "manual rotation required" to Teams and write NOTHING. A human
     rotates at the provider and re-seeds with --expires; the rotator never
     fabricates a value it cannot obtain.

Writing happens through this function's system-assigned identity, which holds
Key Vault Secrets Officer via the keyvault module's enable_rotator_officer
toggle — the only WRITE principal besides the human operator.
"""
import datetime
import json
import logging
import os
import urllib.request

import azure.functions as func

# A named logger rather than the root one: it still propagates to the root
# handler the Functions worker installs, and the record carries its origin.
log = logging.getLogger(__name__)

ROTATABLE = {"anthropic-api-key", "openai-api-key"}


def _teams(message: str) -> None:
    url = os.environ.get("TEAMS_WEBHOOK_URL", "")
    # An UNRESOLVED Key Vault reference is not empty — App Service hands the app
    # the literal "@Microsoft.KeyVault(...)" string (grant missing, secret not
    # seeded (B8), or the app not yet restarted after the grant). urlopen would
    # die on it with "unknown url type" and kill the escalation path.
    if url.startswith("@Microsoft.KeyVault"):
        log.warning("rotate: TEAMS_WEBHOOK_URL is an unresolved KV reference — grant/seed/restart missing. Wanted to say: %s", message)
        return
    if not url:
        # B8 open: no webhook seeded yet. Log loudly rather than crash — the
        # SecretNearExpiry event will re-fire on its schedule.
        log.warning("rotate: TEAMS_WEBHOOK_URL empty (B8?) — wanted to say: %s", message)
        return
    req = urllib.request.Request(
        url,
        data=json.dumps({"text": message}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        log.info("rotate: teams notified (HTTP %s)", resp.status)


def _mint_anthropic(admin_key: str) -> str:
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/organizations/api_keys",
        data=json.dumps({"name": f"sentinel-rotated-{datetime.datetime.now(datetime.timezone.utc).date().isoformat()}"}).encode(),
        headers={
            "x-api-key": admin_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())["api_key"]


def main(event: func.EventGridEvent) -> None:
    data = event.get_json() or {}
    secret_name = data.get("ObjectName", "")

    if secret_name not in ROTATABLE:
        log.info("rotate: ignoring SecretNearExpiry for %r (not an LLM key)", secret_name)
        return

    admin_key = os.environ.get("ANTHROPIC_ADMIN_KEY", "")
    if secret_name == "anthropic-api-key" and admin_key:
        # Imports deferred: the SDK only loads on the automated path, so the
        # manual path works even if a bad deploy broke the azure-* packages.
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient

        new_value = _mint_anthropic(admin_key)
        client = SecretClient(os.environ["KEY_VAULT_URI"], DefaultAzureCredential())
        client.set_secret(
            secret_name,
            new_value,
            expires_on=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=90),
        )
        log.info("rotate: %s rotated, new version set with +90d expiry", secret_name)
        # Best-effort AFTER the write: if Teams fails here and the exception
        # escaped, Event Grid would re-deliver and the handler would MINT AGAIN
        # — up to 30 extra key versions for one notification hiccup. The secret
        # is already rotated; a lost courtesy ping must not raise.
        try:
            _teams(f"Sentinel: `{secret_name}` rotated automatically (new version, +90d).")
        except Exception:
            log.exception("rotate: rotated OK but the Teams notification failed (ignored)")
    else:
        _teams(
            f"Sentinel: `{secret_name}` expires in ~30 days and cannot be auto-rotated. "
            f"Rotate at the provider and re-seed with: az keyvault secret set "
            f"--vault-name {os.environ.get('KEY_VAULT_NAME', 'sentinel-kv-0375')} "
            f"--name {secret_name} --value <new> --expires <+90d>"
        )
