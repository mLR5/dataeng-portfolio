variable "name" {
  description = "Name of the Key Vault"
  type        = string
}

variable "resource_group_name" {
  description = "Name of the resource group"
  type        = string
}

variable "location" {
  description = "Azure region"
  type        = string
}

variable "sku_name" {
  description = "SKU name (standard or premium)"
  type        = string
  default     = "standard"
}

variable "soft_delete_retention_days" {
  description = "Number of days to retain soft-deleted keys"
  type        = number
  default     = 7
}

variable "purge_protection_enabled" {
  description = "Enable purge protection"
  type        = bool
  default     = false
}

variable "enable_rbac_authorization" {
  description = "Enable RBAC authorization"
  type        = bool
  default     = true
}

variable "network_default_action" {
  description = "Default action for network rules"
  type        = string
  default     = "Allow"
}

variable "network_bypass" {
  description = "Bypass network rules for Azure services"
  type        = string
  default     = "AzureServices"
}

variable "tags" {
  description = "Tags to apply to the Key Vault"
  type        = map(string)
  default     = {}
}
