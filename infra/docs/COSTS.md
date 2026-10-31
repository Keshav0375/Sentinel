# Costs — what the agent-memory pieces add

Monthly figures are Azure list prices, approximate, and billed only while the resource exists.
They are the numbers recorded in the decisions log (2026-10-25 / 2026-10-28) or, where marked,
the published list price. **Check the Azure pricing page for your region** before relying on any
of them; regional prices differ, sometimes by double.

| Item | Size | About | Billed while | Survives destroy |
|---|---|---|---|---|
| Azure Managed Redis | Balanced B0, single node (HA off) | **$13.14/mo** | the deployment exists — created and destroyed with it (`redis.tf`) | no; holds no durable state |
| Private endpoint for Redis | one per deployment | about **$0.01/h per endpoint** (~$7.30/mo, Azure list price) + negligible data processing | the deployment exists | no |
| `memory-backups` blobs | a few MB per dump × 10 dumps per deployment, Standard LRS | **cents/mo** | always (out-of-band container) | **yes** — that is its purpose |
| Postgres Flexible Server (baseline, unchanged) | B1ms + 32 GB storage | about **$12/mo** on-demand in East US, up to about $26 elsewhere, plus storage | the estate is up | no (the dump above is what survives) |

## Notes

- **Redis HA is off on purpose.** A replica would roughly double the Redis line to protect data
  that Postgres already holds durably; losing the cache costs a warm-up, not a record.
- **The private endpoint is the price of private networking.** The cache has public network
  access disabled; the endpoint in the AKS subnet is the only way in. The private DNS zone it
  registers in is shared by every deployment and lives in the platform layer (about $0.50/mo per
  zone, Azure list price).
- **Backups are pruned by count, not age.** `scripts/memory-backup.sh` keeps the newest 10 dumps
  per `<deployment>-<env>/` prefix, so the storage line stays flat however often you destroy.
- **A demo deployment without `namespace`** (for example `demo1`) gets no Redis and no endpoint,
  so it adds nothing here.
- **`scope: all` destroy reaches zero for everything above except the blobs**, which sit in the
  bootstrap state account with the Terraform state and cost cents.
