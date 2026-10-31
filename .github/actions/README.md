# Sentinel composite actions

Nine composite actions that GitHub Actions workflows use to run the backend on the shared AKS cluster
(backend task 7.3; decisions 2026-10-24). Every step is a plain `run` step, so there is nothing inside an
action to pin.

## Actions

- `backend-up` - validate every value, then apply the `azure/k8s` manifests bundled with the action; wait
  for an in-flight start/stop/update (<= 20 min), start the cluster if stopped, scale to 1, wait for `/health`
  and `/ready`.
  - Inputs: `namespace` (default `sentinel-dev`), `resource-group`, `cluster-name`, `image-tag`,
    `acr-login-server`, `api-tenant-id`, `api-audience`, `key-vault-url`, `pg-host`, `pg-database`, `pg-user`, `redis-host`, `redis-port`.
  - Outputs: `backend-url`.
- `backend-down` - delete Service `sentinel-backend` by name and scale the Deployment to 0 unless `keep-warm` is `true`.
  - Inputs: `namespace` (default `sentinel-dev`), `resource-group`, `cluster-name`, `keep-warm` (default `false`).
  - Outputs: none.
- `get-kv-secrets` - read the named secrets from Key Vault; unrequested outputs stay empty.
  - Inputs: `vault-uri` (`https://<vault>.vault.azure.net/`, the infra KV URI; the vault name is derived from
    it), `names` (comma, space or newline separated).
  - Outputs: `datadog-api-key`, `datadog-app-key`, `langfuse-public-key`, `langfuse-secret-key`,
    `langfuse-base-url`, `teams-webhook-url`.
- `get-db-token` - mint a short-lived Entra token for Azure PostgreSQL.
  - Inputs: none. Outputs: `pg-token`.
- `get-backend-token` - mint an Entra bearer token for the Sentinel API.
  - Inputs: `audience` (the comma list; the token is for the first entry, which must be a GUID), `tenant`
    (optional; every caller passes `vars.AZURE_IDENTITY_TENANT_ID`). Outputs: `bearer`.
- `resolve-stable-tag` - resolve `sentinel-backend:stable` to its `sha-<7>` tag through the ACR digest; it fails
  when none matches. Used by the incident and scale workflows.
  - Inputs: `acr-name`. Outputs: `image-tag`.
- `notify-teams` - post an Adaptive Card to a Teams webhook. Title and body render as plain text (Adaptive Card
  `TextRun`, and `](` is broken with a zero-width space), so caller-supplied text such as
  `[Approve](https://...)` cannot pose as a link; a bare URL stays visible verbatim. Never pass untrusted text
  expecting formatting.
  - Inputs: `title`, `body`, `webhook-url`, `severity` (`info`, `warning`, `critical`; default `info`).
  - Outputs: none.
- `psql-exec` - run one row-returning statement; `result` is a one-line JSON array.
  - Inputs: `sql`, `pg-token`, `pg-host`, `pg-database`, `pg-user`, `vars` (optional).
  - Outputs: `result`.
- `ops-event` - append one `ops.v1` event to the orphan `ops-ledger` branch (backend task 10.1). Validates the envelope,
  drops fields outside the per-kind allow-list, fails closed on secret-like values, caps summaries at 500 characters,
  and creates the branch on first use. Non-blocking: a ledger failure is a `::warning::` and `result=failed`, never a
  failed step. Skipped on `pull_request` and `pull_request_target`. The caller job needs `permissions: contents: write`.
  `source` is derived from `GITHUB_REPOSITORY` and `GITHUB_WORKFLOW_REF`. Runs `scripts/ops/ledger.py` from the action
  ref (`$GITHUB_ACTION_PATH/../../..`), never the workspace.
  - Inputs: `kind`, `source-id`, `state` (default empty), `ts` (RFC 3339, default now), `chain` and `data` (JSON
    objects, default `{}`), `token` (default `github.token`).
  - Outputs: `event-id`, `result` (`written`, `exists`, `failed`, `skipped`).

`get-kv-secrets` never exposes `github-pat` (bridge Function only) or `anthropic-api-key` / `openai-api-key`
(pod only, via workload identity). A requested name outside its list fails before any `az` call.

## Pinning

Callers in other repos pin an action to a full commit SHA, never to a branch or `@main`:

```yaml
- uses: Keshav0375/Sentinel/.github/actions/backend-up@0123456789abcdef0123456789abcdef01234567
```

Workflows inside this repo use the local path, `./.github/actions/<name>`.

Third-party actions in every workflow are pinned to a full commit SHA with a `# vX.Y.Z` comment
(tests/test_infra/test_workflow_hardening.py).

## Security invariants

- Every step has `shell: bash`, an `id`, and starts with `set -euo pipefail`.
- Inputs reach scripts only as environment variables; no expression is interpolated into a script.
- Every token, secret and webhook URL is masked (`::add-mask::`) before it is written to an output.
- Error messages name the input or line, never a value.
- Masked outputs are dropped from job outputs, so mint tokens and fetch secrets in the job that uses them.
  `backend-url` is not secret and may be a job output.

## HITL

These are executors for GitHub Actions only. The backend reasons and drafts; it never invokes an action in this
directory. The revert PR on `Sentinel-deployment` remains the human gate.

## Trusted ref

`backend-up` renders and applies the manifests bundled with the action itself
(`$GITHUB_ACTION_PATH/../../../azure/k8s`), so the commit pinned in `uses:` is the trusted ref: pin a reviewed
`main` commit. The caller's workspace (`$GITHUB_WORKSPACE`) is never read, so a checked-out fork head cannot
change what is applied; `backend-down` deletes the Service by name. Every rendered value is validated first
(no `"`, `\`, `$` or line break, then the per-field pattern in `azure/k8s/placeholders.txt`); a mismatch
fails before any `kubectl` call. Run in the `sentinel-dev` environment.

## Login

Run `azure/login` (OIDC) immediately before using an action: in the resource tenant for AKS, Key Vault and
PostgreSQL. `get-backend-token` needs `azure/login` in the identity tenant, where the API app registration
lives. `az` cannot mint new tokens once the GitHub OIDC assertion expires, so log in again before late steps.

## psql-exec SQL contract

`sql` is one statement that returns rows: a `SELECT`, or `INSERT` / `UPDATE` / `DELETE` ... `RETURNING`. The
action wraps it in `WITH r AS (<sql>) SELECT coalesce(jsonb_agg(to_jsonb(r)), '[]'::jsonb) FROM r;`. Values
travel only through `vars` (`name=value`, one per line) and are referenced as `:'name'`:

```yaml
sql: UPDATE incidents SET status = :'status' WHERE id = :'id' RETURNING id
vars: |
  status=rolled_back
  id=42
```

## backend-down and the shared cluster

`backend-down` never stops the shared cluster; stopping it is a separate manual operation. Pass the repository
variable as `keep-warm: ${{ vars.SENTINEL_KEEP_WARM }}`. When the cluster is already stopped there is nothing to
tear down and the action exits cleanly.

## Runner prerequisites

`az`, `kubectl`, `curl`, `jq`, `envsubst` and `psql`. The `ubuntu-latest` image has them; `kubelogin` is
installed on demand by `backend-up` and `backend-down`. `ops-event` needs `git` and `python3` (3.11 or later).
