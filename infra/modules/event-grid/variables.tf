variable "resource_group_name" {
  description = "Resource group for the topic. Supplied by the root."
  type        = string
}

variable "location" {
  description = "Azure region. Supplied by the root from var.location — no default anywhere (R3)."
  type        = string
}

variable "topic_name" {
  description = "Custom topic name — its endpoint is a public DNS name, hence the 0375 convention."
  type        = string
}

variable "function_app_id" {
  description = "Bridge function app resource id (module.functions.function_app_id); /functions/bridge is appended for the subscription endpoint."
  type        = string
}
