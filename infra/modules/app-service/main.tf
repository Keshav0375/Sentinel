# ─────────────────────────────────────────────────────────────────────────────
# App Service — the TARGET app (architecture/infra.md §3.6).
#
# Hosts sentinel-watchtower, the deliberately-breakable FastAPI app from the
# Sentinel-deployment repo. Its deploys and failures are what generate the real
# Datadog signal the whole incident pipeline runs on. Terraform provisions the
# plan + empty web app; code arrives via ci_app_deployment.yml (deployment
# phase 2), never from here.
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

resource "azurerm_service_plan" "deployment" {
  name                = "sentinel-deploy-plan"
  location            = var.location
  resource_group_name = var.resource_group_name
  os_type             = "Linux"
  sku_name            = "F1" # always-free tier: 60 CPU-min/day, 1 GB — the real ceiling for the 30-scenario runs
}

# The `dummy_api` address is historical: renaming it would destroy and recreate
# the web app in every deployment's state for a cosmetic gain. The app it hosts
# is sentinel-watchtower; the resource is named from var.app_name.
resource "azurerm_linux_web_app" "dummy_api" {
  name                = var.app_name
  location            = var.location
  resource_group_name = var.resource_group_name
  service_plan_id     = azurerm_service_plan.deployment.id

  # No username/password deployment credentials. The app pipeline deploys as
  # gha-app over OIDC: `az webapp deploy` (CLI >= 2.48.1) falls back to Entra
  # auth against Kudu when SCM basic auth is off (Microsoft Learn, "Disable
  # basic authentication for deployment"), authorised by gha-app's Website
  # Contributor grant. With basic auth on, anyone holding the publish profile
  # could deploy around that grant, and around the main-only environment.
  #
  # ACCEPTED, recorded (PR #16 review): Website Contributor includes
  # Microsoft.Web/sites/basicPublishingCredentialsPolicies/write, so a stolen
  # gha-app token could turn basic auth back on. Accepted because:
  #   - the token can only be minted by a workflow run on main (sentinel-dev's
  #     main-only branch policy, plus main's PR-required protection);
  #   - the next apply reverts the drift, since these two flags are managed here;
  #   - a custom role without that action needs a role definition, which only
  #     an Owner can create, and the pipeline identities hold no Owner.
  # Revisit if gha-app is ever granted on a second deployment.
  ftp_publish_basic_authentication_enabled       = false
  webdeploy_publish_basic_authentication_enabled = false

  site_config {
    # Hard F1 constraints, not preferences: the tier rejects Always On (and
    # azurerm defaults it to true → apply fails without this) and only offers a
    # 32-bit worker.
    always_on         = false
    use_32_bit_worker = true

    application_stack {
      python_version = "3.12"
    }

    # Deployment §2.5's startup command — gunicorn wrapping uvicorn workers.
    app_command_line = "gunicorn --bind=0.0.0.0 --timeout 600 -k uvicorn.workers.UvicornWorker app.main:app"
  }

  app_settings = {
    "APP_VERSION"                    = "initial"
    "DD_SERVICE"                     = "sentinel-watchtower" # logical service name (decision 2026-10-04 D1), NOT the resource name
    "DD_ENV"                         = "dev"
    "SCM_DO_BUILD_DURING_DEPLOYMENT" = "true" # Oryx builds requirements.txt on zip deploy
  }

  lifecycle {
    # ci_app_deployment.yml rewrites APP_VERSION on every deploy — that is the
    # pipeline's job, and Terraform must not revert it on the next apply.
    ignore_changes = [app_settings["APP_VERSION"]]
  }
}
