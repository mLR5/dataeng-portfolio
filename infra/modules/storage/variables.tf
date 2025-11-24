variable "name" {
  description = "Name of the storage account"
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

variable "account_tier" {
  description = "Storage account tier (Standard or Premium)"
  type        = string
  default     = "Standard"
}

variable "account_replication_type" {
  description = "Replication type (LRS, GRS, RAGRS, ZRS, etc.)"
  type        = string
  default     = "RAGRS"
}

variable "access_tier" {
  description = "Access tier (Hot or Cool)"
  type        = string
  default     = "Hot"
}

variable "allow_public_access" {
  description = "Allow public access to blobs"
  type        = bool
  default     = false
}

variable "shared_access_key_enabled" {
  description = "Enable shared access key"
  type        = bool
  default     = true
}

variable "min_tls_version" {
  description = "Minimum TLS version"
  type        = string
  default     = "TLS1_2"
}

variable "enable_hns" {
  description = "Enable hierarchical namespace for Data Lake Gen2"
  type        = bool
  default     = false
}

variable "network_default_action" {
  description = "Default action for network rules"
  type        = string
  default     = "Allow"
}

variable "network_bypass" {
  description = "Bypass network rules for Azure services"
  type        = list(string)
  default     = ["AzureServices"]
}

variable "network_ip_rules" {
  description = "List of IP addresses or CIDR blocks to allow"
  type        = list(string)
  default     = []
}

variable "network_subnet_ids" {
  description = "List of virtual network subnet IDs to allow"
  type        = list(string)
  default     = []
}

variable "large_file_share_enabled" {
  description = "Enable large file shares"
  type        = bool
  default     = true
}

variable "cross_tenant_replication_enabled" {
  description = "Enable cross-tenant replication"
  type        = bool
  default     = false
}

variable "tags" {
  description = "Tags to apply to the storage account"
  type        = map(string)
  default     = {}
}
