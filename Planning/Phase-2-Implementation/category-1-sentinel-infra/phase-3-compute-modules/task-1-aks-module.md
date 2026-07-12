# task-1 — AKS module (+ AcrPull, scale-to-zero lifecycle)   ·   [category-1-sentinel-infra / phase-3-compute-modules]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel-infra` |
| **Phase branch** | `impl/infra-phase-3-compute-modules` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel-infra/ARCHITECTURE.md §3.7; sentinel §8 (consumer) |
| **Depends on** | [[task-1-acr-module]], [[task-3-oidc-federation]] |
| **Referenced by** | [[task-2-k8s-manifests]], [[task-3-composite-actions]] (backend-up/down), [[task-2-ci-backend-deployment]] |

## Spec
Single-node AKS that hosts the backend. Node pool idles at 0; workflows scale it — so
Terraform must NOT fight `node_count`.

**Files created:** `modules/aks/{main.tf,variables.tf,outputs.tf}`
- `azurerm_kubernetes_cluster "sentinel"` — name `sentinel-aks`, dns_prefix `sentinel`, `sku_tier = "Free"`, default_node_pool `default` node_count 1 vm_size `Standard_B2ats_v2`, `identity { type = "SystemAssigned" }`.
- `lifecycle { ignore_changes = [default_node_pool[0].node_count] }` — the scale-to-zero rule.
- `azurerm_role_assignment "aks_acr_pull"` — `AcrPull` to `kubelet_identity[0].object_id` scoped to the ACR id (input).
- `azurerm_role_assignment "gha_aks_user"` — `Azure Kubernetes Service Cluster User Role` to the GHA SP object id.
- `variables.tf` — `resource_group_name`, `location`, `acr_id`, `gha_sp_object_id`.
- `outputs.tf` — `aks_cluster_name`, `aks_resource_group`.

## Prerequisites
- [ ] task 2.1 ACR id, task 1.3 GHA SP id. [ ] B2ats_v2 available in region (R3). [ ] ⛔ B1 to apply.

## Acceptance Criteria
- [ ] Validates; `ignore_changes` on node_count present (else applies fight scale-to-zero).
- [ ] AcrPull + Cluster User role assignments wired to the right principals.
- [ ] Outputs expose cluster name + RG.

## Tests
- **Validate:** validate, tflint, tfsec.
- **Integration (⛔ B1):** apply; `az aks get-credentials` + `kubectl get nodes` shows 1 node; kubelet pulls from ACR without imagePullSecrets.
- **Quality gate:** `--repo infra`.

## How to Verify (phase gate)
1. `terraform plan -target=module.aks` → cluster + 2 role assignments; a second plan after a manual `az aks nodepool scale --node-count 0` shows NO node_count drift.
2. (post-apply) `kubectl get nodes`.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Apply/verify ⛔ B1. Code writable now._
