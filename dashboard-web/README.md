# Sentinel Ops dashboard

A static, read-only React SPA that shows Sentinel end to end: builds, deploys, incidents,
AI agent runs, revert PRs, reviews and merges, correlated into chains. It reads a public
snapshot of the ops ledger and nothing else: no backend, no secrets, no write path.
Merging and approvals happen on GitHub; this page only shows them.

## Commands

pnpm comes from Corepack (pinned by `packageManager` in `package.json`); Node is pinned in
`.node-version`.

```sh
corepack pnpm install --frozen-lockfile
corepack pnpm dev          # http://localhost:5173/Sentinel/ — serves src/fixtures/
corepack pnpm typecheck
corepack pnpm lint
corepack pnpm test         # Vitest + Testing Library
corepack pnpm build        # → dist/
corepack pnpm e2e:install  # once: Playwright's Chromium
corepack pnpm e2e          # builds, serves dist/ on :4173 and runs e2e/ (light, dark, 360 px)
```

In dev the data comes from `src/fixtures/` (the frozen contract samples). Pick a state with
the query string: `?fixture=populated` (default), `?fixture=empty`, `?fixture=error`, and
`&delay=<ms>` (0–5000) to see the loading skeleton. `VITE_SNAPSHOT_SOURCE=http` makes dev
fetch `/Sentinel/snapshot/` instead; a production build always fetches it and ships no fixture.

## Data contract

The page reads `ops.snapshot.v1` files, defined by `scripts/ops/schema/snapshot.v1.json`
at the repo root and written by the reconciler:

| file | used for |
|---|---|
| `snapshot/meta.json` | snapshot age, ledger head, source health |
| `snapshot/chains.json` | timeline and needs-action lane |
| `snapshot/metrics.json` | KPIs, daily activity, eval scorecard |
| `snapshot/entities/<chain_id>.json` | the chain drawer |

The browser checks only `schema` and `file` on each response; the schema itself is enforced
by the writer and, here, by `src/test/schema.test.ts` against every fixture.

## Publishing (GitHub Pages)

```sh
corepack pnpm --dir dashboard-web install --frozen-lockfile
corepack pnpm --dir dashboard-web build
```

The site is built for the base path `/Sentinel/`. Publish `dashboard-web/dist/` with the
snapshot copied in at `dist/snapshot/{meta,chains,metrics}.json` and
`dist/snapshot/entities/<chain_id>.json`. View state lives in the query string only, so no
404 fallback page is needed. In CI, run e2e with
`corepack pnpm --dir dashboard-web exec playwright install --with-deps chromium` first, then
`corepack pnpm --dir dashboard-web e2e`.

Read-only; public data only: no logs, secrets or raw tool output.
