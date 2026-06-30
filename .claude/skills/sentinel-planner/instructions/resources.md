# Available Resources & Budget

Reference for what's available when making architecture and infrastructure decisions.

## Cloud Resources

### Azure (Student Account)
- **Credit:** $139 (expires 05/18/2027)
- **Source:** `Planning/Subscription-plans/azure-reference.md`
- **Key free resources for Sentinel:**

| Resource | Tier | Free Limit | Expires |
|----------|------|------------|---------|
| App Service | F1 (always free) | 10 apps, 1 GB, 60 min CPU/day | Never |
| Azure Functions | Consumption (always free) | 1M requests/month | Never |
| Event Grid | Always free | 100K ops/month | Never |
| AKS | Control plane (always free) | Free | Never |
| PostgreSQL | B1MS (12-month) | 750 hrs + 32 GB | 05/2027 |
| Container Registry | Standard (12-month) | 100 GB | 05/2027 |
| Key Vault | Always free | 10K transactions | Never |
| Cosmos DB | Always free | 1K RU/s + 25 GB | Never |

### DigitalOcean
- **Credit:** $200 (1 year from activation)
- **Status:** Requires payment method — avoided for now
- **Source:** `Planning/Subscription-plans/github-pro-reference.md`

### GitHub
- **Plan:** Pro (free via Student Pack)
- **Actions:** 3,000 min/month
- **Source:** `Planning/Subscription-plans/github-pro-reference.md`

### Datadog
- **Plan:** Pro (free via Student Pack, 2 years)
- **Servers:** Up to 10
- **Includes:** APM, Logs, Infrastructure, Events, Dashboards

### LLM Providers
- **Source:** `Planning/LLM-providers.md/available_models.md`
- **Groq:** Free tier (primary for dev)
- **Gemini:** Free tier (backup)
- **OpenAI/Anthropic/Azure:** Paid (use only if specific capability needed)

### Other Student Pack Resources
- **Heroku:** $13/month credit, 24 months
- **MongoDB Atlas:** $50 credit + free cert
- **Sentry:** 50K errors, 100K transactions, 1 year
- **New Relic:** Free student access

## Budget Rules

1. **$0/month target** — use only free tiers and credits
2. **Azure credit ($139)** — reserve for things that can't be done free (e.g., if we need Azure OpenAI later)
3. **GHA minutes** — 3,000/month is generous but agentic loops can burn it fast; estimate per-workflow
4. **Datadog** — Pro with 10 servers is more than enough; main constraint is log retention (15 days on Pro)
5. **No payment method required** — any service that requires payment info is deprioritized unless no alternative exists

## Notification Channel

- **Microsoft Teams** — incoming webhook for notifications
- No Slack (not in use)

## Decision Constraints

When making architecture decisions, always check:
1. Does this stay within free tier limits?
2. What happens when free tier expires? (12-month items expire 05/2027)
3. Is there a simpler/cheaper alternative?
4. Does the user already have the account/credentials set up?
