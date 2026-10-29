# ops-ledger

Append-only event ledger for the Sentinel ops dashboard (backend phase 10).

- One JSON file per event: `events/YYYY/MM/DD/<YYYYMMDDTHHMMSSZ>-<kind>-<event_id>.json`.
- Every event is valid against `schema/event.v1.json` (`ops.v1`).
- Files are never edited or deleted; git history is the audit log.
- Written only by GitHub Actions (`.github/actions/ops-event`) and the reconciler.
- Public data: no logs, tool output, secrets, tokens, keys or IP addresses.
