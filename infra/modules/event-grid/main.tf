# ─────────────────────────────────────────────────────────────────────────────
# Event Grid — the Datadog → bridge conduit (architecture/infra.md §3.4).
#
# ONE topic, ONE subscription. Both Datadog monitors post to the same topic and
# the bridge classifies — routing lives in code, not in subscription filters,
# so adding a third signal type is a code change rather than infra surgery.
# ─────────────────────────────────────────────────────────────────────────────

terraform {
  required_version = ">= 1.9"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}

# ── Why CustomEventSchema (decision 2026-10-05 "Deploy phase 2", amended) ────
# The default EventGridSchema rejects any event without an ISO-8601 `eventTime`,
# and Datadog's webhook template variables cannot produce one ($DATE is epoch
# milliseconds). So the topic accepts Datadog's FLAT body as-is and maps it:
# Event Grid stamps `id` and `eventTime` itself, `subject` comes from the body's
# `alert_id`, `eventType`/`dataVersion` are fixed defaults, and the whole flat
# body becomes `data`. The subscription below still DELIVERS EventGridSchema,
# so the bridge's `event.get_json()` sees the flat body unchanged.
# Changing input_schema forces a new topic (and a new key): re-run
# Sentinel-deployment datadog/apply.sh after any such apply.
resource "azurerm_eventgrid_topic" "sentinel" {
  name                = var.topic_name
  location            = var.location
  resource_group_name = var.resource_group_name

  input_schema = "CustomEventSchema"

  input_mapping_fields {
    subject = "alert_id"
  }

  input_mapping_default_values {
    event_type   = "datadog.monitor"
    data_version = "1"
  }
}

# Event Grid VALIDATES the endpoint at creation, so the bridge function must
# already exist with its code deployed — which is why the root wires
# function_app_id from module.functions and why 3.3 builds before 3.2.
resource "azurerm_eventgrid_event_subscription" "to_function" {
  name  = "sentinel-to-function"
  scope = azurerm_eventgrid_topic.sentinel.id

  # The provider default, declared: the mapped custom input is re-emitted as a
  # standard Event Grid event, which is what the bridge's EventGridEvent
  # trigger binding parses. CustomInputSchema here would break that binding.
  event_delivery_schema = "EventGridSchema"

  azure_function_endpoint {
    function_id = "${var.function_app_id}/functions/bridge"

    # Azure populates both server-side; omitting them is a PERPETUAL DIFF
    # proposing to null them on every plan — same class as the Postgres
    # authentication.tenant_id (task 2.2). These are the service defaults,
    # declared, not choices.
    max_events_per_batch              = 1
    preferred_batch_size_in_kilobytes = 64
  }
}
