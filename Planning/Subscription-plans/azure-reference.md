# Azure for Students — Full Reference

## Account Status (live from portal)

```
Available credits     : $139 out of $139
Days until expiry      : 323
Credit expires on      : 05/18/2027
June costs              : $0.00
```

> Note: balance shows $139, not the standard $100 — likely a regional/promo top-up tied to your University of Windsor enrollment. Treat $139 as your real ceiling, not $100.

---

## 💰 Tier 1 — Free for 12 months only (new customers, expires 05/18/2027)

```
RESOURCE                          | CATEGORY          | FREE LIMIT
-----------------------------------|--------------------|----------------------------------------------
Windows VM (B2pts v2 / B2ats v2)   | Compute            | 750 hrs each (Arm + AMD burstable)
Linux VM (B2pts v2 / B2ats v2)     | Compute            | 750 hrs each (Arm + AMD burstable)
Azure Managed Disks                | Storage            | 64 GB x2 (P6 SSD) + 1 GB snapshot + 2M IOPS
Azure Blob Storage                 | Storage            | 5 GB LRS hot, 20K read / 10K write ops
Azure Files                        | Storage            | 100 GB LRS + 2M read/list/file ops
Key Vault                          | Security           | 10,000 transactions (RSA 2048 / secrets, Standard)
Azure Database for MySQL           | Databases          | 750 hrs B1MS + 32 GB storage + 32 GB backup
Azure Database for PostgreSQL      | Databases          | 750 hrs B1MS + 32 GB storage + 32 GB backup
Azure Cosmos DB (12-mo tier)       | Databases          | 400 RU/s + 25 GB storage
Bandwidth (Data Transfer Out)      | Networking         | 15 GB outbound
AI Custom Vision                   | Foundry Tools      | 5,000 transactions (S1/S2/S3)
Azure Vision                       | Foundry Tools      | 10,000 predictions S0 + 1 training hr + 2 projects
Face API (12-mo tier)              | Foundry Tools      | 30,000 transactions S0
Azure Document Intelligence        | Foundry Tools      | 500 pages S0
Service Bus                        | Databases          | 750 hrs + 13M ops, Standard base unit
Container Registry                 | Containers         | 1 Standard registry, 100 GB storage, 10 webhooks
Azure Load Balancer                | Networking         | 750 hrs + 15 GB data + 5 rules (Standard)
Archive Storage                    | Storage            | 10 GB LRS + 10 GB write/retrieval + 100 reads
VPN Gateway                        | Networking/Security| 750 hrs (VpnGw1)
Azure SQL Managed Instance         | Databases          | 720 vCore-hrs/month + 64 GB storage + 300 IOPS
```

---

## ♾️ Tier 2 — Always free (every Azure account, no expiry)

```
RESOURCE                          | CATEGORY               | FREE LIMIT
-----------------------------------|-------------------------|----------------------------------------------
Azure SQL Database                 | Databases               | 100,000 vCore-seconds/month + 32 GB storage
Azure App Service                  | Compute                 | 10 apps, 1 GB storage, 1 hr/day
Azure Functions                    | Compute                 | 1,000,000 requests/month
Microsoft Entra ID                 | Security + Identity     | 50,000 stored objects + SSO
Service Fabric                     | Containers              | Free
Azure DevOps                       | Developer Tools         | Unlimited private Git repos
DevTest Labs                       | Developer Tools         | Free
Security Center                    | Security + Identity     | Free
Advisor                            | Monitoring + Mgmt       | Unlimited
Microsoft IoT Hub                  | IoT                     | 8,000 msgs/day, 0.5 KB meter size
Data Factory                       | Databases               | 5 low-frequency activities
Azure AI Search                    | Foundry IQ              | 50 MB storage, 10K docs, 3 indexes
Batch                              | Compute                 | Free
Notification Hubs                  | Mobile                  | 1,000,000 push notifications
Automation                         | Monitoring + Mgmt       | 500 min job run time
Data Catalog                       | Data + Analytics        | Unlimited users
Face API (always-free instance)    | Foundry Tools           | 30,000 transactions
Virtual Network                    | Networking              | 50 VNets
Bandwidth (always-free)            | Networking              | 100 GB outbound
Machine Learning                   | Azure ML                | Free (R/Python model dev)
Azure Logic Apps                   | Integration             | 4,000 built-in actions (Consumption)
App Configuration                  | Developer Tools         | 1,000 requests/day + 10 MB storage
Azure API Management               | Integration             | 1M calls/month (Consumption)
Azure Arc                          | Hybrid + Multicloud     | Free
Azure Cloud Shell                  | Mgmt + Governance       | 5 GB storage (12 months)
Azure Cosmos DB (always-free)      | Databases               | 1,000 RU/s + 25 GB storage
Azure Kubernetes Service (AKS)     | Compute                 | Free (control plane)
Azure Lighthouse                   | Mgmt + Governance       | Free
Service Catalog / Managed Apps     | Mgmt + Governance       | Free publishing
Azure Maps                         | IoT                     | 1,000–5,000 transactions
Azure Migrate                      | Migration               | Free
Azure Policy                       | Mgmt + Governance       | Free
Azure SignalR Service              | Web                     | 20 concurrent conns/unit, 20K msgs
AI Bot Service                     | Foundry Tools           | 10,000 premium msgs + unlimited standard
Azure Storage Mover                | Migration               | Free
Database Migration Service         | Databases               | Free (Standard Compute)
Event Grid                         | Integration             | 100,000 ops/month
AI Immersive Reader                | Foundry Tools           | 3,000,000 characters
IoT Edge                           | IoT                     | Free open-source runtime
Azure Monitor                      | Mgmt + Governance       | Free amounts per feature (varies)
Network Watcher                    | Networking              | 5 GB storage, 1K checks, 10 tests
Open Datasets                      | Azure ML                | Free (egress may apply)
Private Link                       | Networking              | Free
Speech to Text (Realtime)          | Azure Speech            | 5 audio hrs/month
Speech Translation                 | Azure Speech            | 5 audio hrs (Standard)
Static Web Apps                    | Compute                 | 100 GB bandwidth, 2 domains, 0.5 GB storage
Container Apps                     | Containers              | 180K vCPU-sec, 360K GiB-sec, 2M requests
Health Bot                         | Foundry Tools           | 3,000 messages (10/sec max)
Health Data Services                | Integration             | 1 GB storage, 50K API reqs, 5 GB transform
Azure Resource Manager              | Mgmt + Governance       | Free
Azure Resource Mover                | Mgmt + Governance       | Free (ingress/egress may apply)
Cost Management                     | Mgmt + Governance       | Free
Azure Attestation                   | Security                | Free
Web PubSub                          | Web                     | 20K msgs/unit/day, 20 conns/unit
Text to Speech                      | Azure Speech            | 0.5M characters/month (Neural Standard)
Visual Studio Code                  | Developer Tools         | Free
Azure Automanage                    | Mgmt + Governance       | Free
Azure Update Manager                | Mgmt + Governance       | Free
Content Safety                      | Foundry Control Plane   | 5,000 text records or 5,000 images
Translator                          | Azure Translator        | 2,000,000 characters/month
Azure Language                      | Foundry Tools           | 5,000 text records
Active Directory B2C                | Identity                | 50,000 MAU
Azure Container Storage             | Containers              | 5 TiB free tier
Azure Deployment Environments       | Developer Tools         | Free (other resources billed separately)
Azure DocumentDB                    | Databases               | 1 Mongo DB cluster, 32 GB storage
```

---

## 💸 Not free anywhere — billed straight against your $139 credit

```
RESOURCE                          | PRICING                          | NOTES
-----------------------------------|-----------------------------------|--------------------------------
Azure OpenAI Service                | Per-token (in/out)                | GPT-5-nano $0.05/$0.40 per 1M; GPT-4o $2.50/$10; GPT-5 $1.25/$10
Azure AI Foundry (DeepSeek/Grok/etc)| Per-token / PTU                   | Same logic as Azure OpenAI
GPU VMs (NC/ND series)              | Per-hour                          | Needed for local training/fine-tuning
VM sizes above free B-series        | Per-hour                          | Anything beyond included burstable VMs
Standard support plan               | $100+/month                       | Skip unless going to production
Egress beyond free GB allowance     | $0.087/GB                         | Watch with large datasets
Fine-tuned model hosting            | $1.70–3.00/hr                     | Bills 24/7 even idle
Log Analytics overage               | ~$2.30/GB                         | Beyond free ingestion cap
```

---

## Summary — how it actually works

Two completely separate systems running side by side on your account:

1. **The $139 credit** — a spending pool that drains only when you use something *not* covered by a free allowance, or when you blow past a free allowance's monthly cap. Right now you're at $139/$139 because June cost = $0.00, meaning nothing you've touched has needed billing yet.

2. **Free service allowances** — fixed monthly quotas per resource, split into two tiers:
   - **12-month tier** (expires 05/18/2027): things like VMs, managed disks, SQL Managed Instance — generous compute/storage freebies meant to get you building, but they stop being free once your account turns 1 year old.
   - **Always-free tier**: smaller, permanent quotas (Functions, App Service, Cosmos DB, Translator, etc.) that exist for every Azure account ever, student or not — these don't expire with your subscription at all.

Staying under any quota's monthly number = $0, credit untouched. Going over = the overage is priced at standard pay-as-you-go rates and silently subtracted from your $139. Once the $139 hits zero, paid services stop working until you upgrade or renew next year — but always-free tier resources keep running regardless, since they're not tied to credit at all.

For your AI work specifically: Azure OpenAI sits outside both free tiers entirely, so every token you push through it eats your $139 directly — budget that against the rest of your stack.