# ─────────────────────────────────────────────────────────────────────────────
# Every name in the estate, derived from four inputs.
#
# Replaces the ad-hoc `0375` suffix that phases 1-4 accumulated. That suffix was
# collision-avoidance bolted on each time Azure rejected a name, which means it
# was never derivable — you had to look it up, and a second deployment had no way
# to produce a different one.
#
# ── Two properties this module exists to guarantee ───────────────────────────
#
# DETERMINISTIC. The same inputs always produce the same names, so a plan after a
# destroy proposes the same estate rather than a differently-named one. That is
# what makes destroy -> recreate a real test instead of a coincidence.
#
# GLOBALLY UNIQUE. ACR, storage accounts, Key Vaults, Postgres servers, Function
# Apps and App Services share namespaces with every other Azure customer. `uid`
# is what stops `acrsentineldev` — a name someone else certainly owns — from
# being what we ask for.
#
# Prefixes follow Azure's Cloud Adoption Framework abbreviations (rg-, aks-, acr,
# psql-, kv-, st, func-, app-, evgt-) rather than something invented here. A
# published convention means a name is readable by someone who has never seen
# this repo.
# ─────────────────────────────────────────────────────────────────────────────

locals {
  d   = var.deployment
  env = var.environment

  # Region abbreviations. A full region name would blow the 24-char budget on its
  # own (`canadacentral` is 13), so names carry the abbreviation.
  #
  # An unmapped region resolves to "" and trips the precondition below rather
  # than silently producing a name that is legal, wrong, and collides with every
  # other unmapped region.
  #
  # The fallback is "" and NOT null, which looks like a detail and is not: a null
  # blows up inside the string templates below — "Cannot include a null value in
  # a string template", pointing at line 66 — before the precondition gets to
  # run. The guard would exist and never fire. Caught by the test that asserts it
  # fires.
  loc_abbrev = {
    canadacentral = "cc"
    canadaeast    = "ce"
    eastus        = "eus"
    eastus2       = "eus2"
    westus2       = "wus2"
    westeurope    = "weu"
    northeurope   = "neu"
  }
  loc = lookup(local.loc_abbrev, var.location, "")

  # Four hex characters of a hash over subscription + deployment + environment.
  #
  # Hashing the SUBSCRIPTION as well as the names means two people running this
  # config against different subscriptions do not fight over the same global
  # names. Four characters is 65536 values — ample against the handful of names
  # one subscription holds, and short enough to fit the Key Vault budget.
  uid = substr(sha1("${var.subscription_id}-${local.d}-${local.env}"), 0, 4)

  # Hyphenless form for the two resource types that forbid separators outright.
  compact = "${local.d}${local.env}${local.uid}"

  names = {
    # ── Resource groups (1-90 chars, permissive) ──────────────────────────────
    resource_group = "rg-${local.d}-${local.env}-${local.loc}"

    # Linux Consumption (Y1) and Linux Dedicated (F1) plans CANNOT share a
    # resource group — Azure rejects the second with "Requested features
    # 'Dynamic SKU, Linux Worker' not available in resource group". Found live
    # 2026-08-23. This is why a deployment needs two groups, not one.
    functions_resource_group = "rg-${local.d}-${local.env}-func-${local.loc}"

    # ── Global namespace: no hyphens allowed ──────────────────────────────────
    container_registry = "acr${local.compact}" # 5-50, alphanumeric only
    storage_account    = "st${local.compact}"  # 3-24, lowercase alphanumeric only

    # ── Global namespace: hyphens allowed ─────────────────────────────────────
    key_vault       = "kv-${local.d}-${local.env}-${local.uid}"   # 3-24 — the binding limit
    postgres_server = "psql-${local.d}-${local.env}-${local.uid}" # 3-63
    function_app    = "func-${local.d}-${local.env}-${local.uid}" # 2-60, azurewebsites.net
    app_service     = "app-${local.d}-${local.env}-${local.uid}"  # 2-60, same namespace as above

    # ── Scoped names: uniqueness is narrower, so no uid needed ────────────────
    aks_cluster          = "aks-${local.d}-${local.env}"  # unique per resource group
    aks_dns_prefix       = "aks-${local.d}-${local.env}"  # per region; Azure appends its own hash
    app_service_plan     = "asp-${local.d}-${local.env}"  # per resource group
    event_grid_topic     = "evgt-${local.d}-${local.env}" # per region
    kubernetes_namespace = "${local.d}-${local.env}"      # per cluster

    # ── Managed identities (per resource group) ───────────────────────────────
    backend_identity = "id-${local.d}-${local.env}-backend"

    # ── Identity tenant (R4) ──────────────────────────────────────────────────
    # Per-deployment app registrations, so a token minted for one deployment
    # fails AUDIENCE validation at another's backend. Shared apps would leave the
    # API with no isolation at all while the namespaces below it were isolated —
    # the weakest link deciding the property.
    api_app_name    = "sentinel-backend-${local.d}-${local.env}"
    gha_client_name = "sentinel-gha-client-${local.d}-${local.env}"

    # Tenant-qualified, and that is not stylistic: new Entra tenants reject the
    # bare `api://<name>` form with InvalidUniqueTenantIdentifierAsPerAppPolicy
    # (found live 2026-08-23).
    api_identifier_uri = "api://${var.identity_tenant_id}/sentinel-backend-${local.d}-${local.env}"
  }
}

# ── Guards ────────────────────────────────────────────────────────────────────
# Variable `validation` cannot see a computed local, so the length assertions
# that depend on `uid` and `loc` live here. They are cheap and they run at plan
# time, which is the whole point: a name that is one character too long should
# cost two seconds, not eight minutes and a half-built estate.

resource "terraform_data" "name_guards" {
  lifecycle {
    precondition {
      condition     = local.loc != ""
      error_message = "location '${var.location}' has no abbreviation. Add it to local.loc_abbrev in modules/naming/main.tf — an unmapped region would otherwise produce names ending in a bare hyphen."
    }
    precondition {
      condition     = length(local.names.key_vault) <= 24
      error_message = "key vault name '${local.names.key_vault}' is ${length(local.names.key_vault)} chars; Azure allows 24."
    }
    precondition {
      condition     = length(local.names.storage_account) <= 24
      error_message = "storage account name '${local.names.storage_account}' is ${length(local.names.storage_account)} chars; Azure allows 24."
    }
    precondition {
      condition     = length(local.names.container_registry) >= 5 && length(local.names.container_registry) <= 50
      error_message = "container registry name '${local.names.container_registry}' must be 5-50 chars."
    }
    precondition {
      condition     = can(regex("^[a-z0-9]+$", local.names.container_registry))
      error_message = "container registry name '${local.names.container_registry}' must be alphanumeric only — ACR forbids hyphens."
    }
    precondition {
      condition     = can(regex("^[a-z0-9]+$", local.names.storage_account))
      error_message = "storage account name '${local.names.storage_account}' must be lowercase alphanumeric only."
    }
  }
}
