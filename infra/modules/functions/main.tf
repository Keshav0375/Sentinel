# ─────────────────────────────────────────────────────────────────────────────
# Functions — the Event Grid → GitHub Actions bridge (architecture/infra.md §3.5).
#
# The bridge is the seam between "Azure noticed something" and "GitHub Actions
# acts on it": Event Grid delivers the Datadog event, the function classifies it
# into one of the two signals and fires repository_dispatch. It is deliberately
# a messenger — HITL means GHA executes, never this function.
#
# The rotator (task 3.6) shares this module and this Y1 plan.
# ─────────────────────────────────────────────────────────────────────────────

terraform {
  required_version = ">= 1.9"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

# Mandatory backing storage for the Consumption plan. Globally unique →
# 0375 convention (the architecture referenced this account but never named it).
resource "azurerm_storage_account" "func" {
  name                     = var.storage_account_name
  location                 = var.location
  resource_group_name      = var.resource_group_name
  account_tier             = "Standard"
  account_replication_type = "LRS"
  min_tls_version          = "TLS1_2"
}

resource "azurerm_service_plan" "functions" {
  name                = "sentinel-func-plan"
  location            = var.location
  resource_group_name = var.resource_group_name
  os_type             = "Linux"
  sku_name            = "Y1" # Consumption — 1M executions/mo always free
}

# The function code ships with the infrastructure. There is no CI workflow for
# function code (infra ships three workflows, none of them deploys functions —
# R6 review), and an app with no code would fail Event Grid's endpoint
# validation when task 3.2 creates the subscription. Zip + remote Oryx build
# keeps the module self-contained; the zip hash retriggers deploy on src change.
# ── Packaging determinism ─────────────────────────────────────────────────────
# DEPLOY_HASH below is output_base64sha256, so the zip's BYTES decide whether
# Terraform thinks the function changed. Three separate things made those bytes
# differ for byte-identical source, and every one of them had to be closed:
#
#   1. __pycache__. src/ is the source_dir, so running the unit tests compiled
#      bytecode INTO the package. The hash moved on every gate run — a perpetual
#      diff redeploying both apps forever — and shipped CPython 3.13 Windows
#      bytecode to a Linux 3.11 host. Closed by `excludes`.
#   2. Line endings. src/*.py|json|txt were uncovered by .gitattributes: Windows
#      checked out CRLF, a Linux runner LF. Closed there.
#   3. FILE MODE. archive_file records a Unix mode per entry, taken from the
#      filesystem — 0666 on Windows, 0644 on Linux. Found at the phase-4 review;
#      on its own it would have kept local apply and CI apply redeploying past
#      each other forever, with a byte-identical tree and nothing to see in git.
#      Closed by `output_file_mode`.
#
# Mtimes were never a problem: the provider stamps every entry with a constant
# sentinel date (1 Jan 2049), which is why touching a file changes nothing.
#
# A `dynamic "source"` block built from a fileset was tried instead and rejected.
# It is deterministic too, but it writes entries with NO Unix mode at all
# (verified: mode 0o0), leaving extraction behaviour on the Functions host to a
# default this repo cannot see. source_dir + output_file_mode keeps the packaging
# shape that has already deployed successfully and normalises the one field that
# actually varied.
data "archive_file" "bridge" {
  type             = "zip"
  source_dir       = "${path.module}/src"
  output_path      = "${path.module}/dist/bridge.zip"
  output_file_mode = "0644"
  excludes         = ["rotate", "bridge/__pycache__", "__pycache__"]
}

resource "azurerm_linux_function_app" "bridge" {
  name                = var.bridge_name
  location            = var.location
  resource_group_name = var.resource_group_name
  service_plan_id     = azurerm_service_plan.functions.id

  storage_account_name       = azurerm_storage_account.func.name
  storage_account_access_key = azurerm_storage_account.func.primary_access_key

  # REQUIRED: GITHUB_TOKEN below is a Key Vault *reference*. Without an identity
  # holding Secrets User on the vault it resolves EMPTY — silently — and the
  # bridge 401s at runtime with nothing pointing at the cause. The keyvault
  # module's enable_bridge_reader grant targets this principal.
  identity {
    type = "SystemAssigned"
  }

  site_config {
    application_stack {
      python_version = "3.12"
    }
  }

  app_settings = {
    "GITHUB_TOKEN"      = "@Microsoft.KeyVault(VaultName=${var.key_vault_name};SecretName=github-pat)"
    "GITHUB_REPO"       = "Keshav0375/Sentinel"
    "GITHUB_EVENT_TYPE" = "incident-alert"

    # Remote build: Oryx installs requirements.txt during zip deploy. Do NOT set
    # WEBSITE_RUN_FROM_PACKAGE alongside these — they are mutually exclusive.
    "SCM_DO_BUILD_DURING_DEPLOYMENT" = "true"
    "ENABLE_ORYX_BUILD"              = "true"

    # zip_deploy_file is a constant PATH — the provider diffs the string, so a
    # src/ edit alone never redeploys (review blocker, 2026-08-23). The hash
    # changes with the content, forces an app update, and the provider re-runs
    # the zip deploy on any update.
    "DEPLOY_HASH" = data.archive_file.bridge.output_base64sha256
  }

  zip_deploy_file = data.archive_file.bridge.output_path
}

# ── Rotator (task 3.6) ────────────────────────────────────────────────────────
data "archive_file" "rotator" {
  type             = "zip"
  source_dir       = "${path.module}/src"
  output_path      = "${path.module}/dist/rotator.zip"
  output_file_mode = "0644"
  excludes         = ["bridge", "rotate/__pycache__", "__pycache__"]
}

# Second app on the same Y1 plan. SystemAssigned: this identity is the only
# WRITE principal on the vault besides the human operator (Secrets Officer via
# enable_rotator_officer). TEAMS_WEBHOOK_URL is a Key Vault reference — Officer
# includes read, so it resolves through the same grant.
resource "azurerm_linux_function_app" "rotator" {
  name                = var.rotator_name
  location            = var.location
  resource_group_name = var.resource_group_name
  service_plan_id     = azurerm_service_plan.functions.id

  storage_account_name       = azurerm_storage_account.func.name
  storage_account_access_key = azurerm_storage_account.func.primary_access_key

  identity {
    type = "SystemAssigned"
  }

  site_config {
    application_stack {
      python_version = "3.12"
    }
  }

  app_settings = {
    "KEY_VAULT_URI"     = "https://${var.key_vault_name}.vault.azure.net/"
    "KEY_VAULT_NAME"    = var.key_vault_name
    "TEAMS_WEBHOOK_URL" = "@Microsoft.KeyVault(VaultName=${var.key_vault_name};SecretName=teams-webhook-url)"
    # ANTHROPIC_ADMIN_KEY is deliberately NOT configured: no admin key exists
    # among the 9 seeded secrets, so the rotator escalates to Teams (its manual
    # path) rather than pretending it can mint. Set it later to enable
    # auto-rotation without any code change.

    "SCM_DO_BUILD_DURING_DEPLOYMENT" = "true"
    "ENABLE_ORYX_BUILD"              = "true"

    # Same content-hash redeploy trigger as the bridge.
    "DEPLOY_HASH" = data.archive_file.rotator.output_base64sha256
  }

  zip_deploy_file = data.archive_file.rotator.output_path
}
