variable "resource_group_name" {
  description = "The deployment's resource group, which holds the cache and its private endpoint."
  type        = string
}

variable "location" {
  description = "Azure region. Supplied by the root — no default anywhere."
  type        = string
}

variable "redis_name" {
  description = "Globally unique cache name (naming module managed_redis); it becomes <name>.<region>.redis.azure.net."
  type        = string
}

variable "subnet_id" {
  description = "ID of the AKS-managed `aks-subnet` (platform output aks_subnet_id), where the private endpoint is placed."
  type        = string
}

variable "private_dns_zone_id" {
  description = "ID of the platform's privatelink.redis.azure.net zone (platform output redis_private_dns_zone_id)."
  type        = string
}

variable "backend_principal_id" {
  description = "Principal (object) ID of the deployment's backend workload identity — the only principal given an access policy."
  type        = string
}

variable "tags" {
  description = "Tags applied to the cache and the private endpoint."
  type        = map(string)
  default     = {}
}
