variable "subscription_id" {
  description = "Azure subscription ID"
  type        = string
  # No default - must be provided via terraform.tfvars or TF_VAR_subscription_id
}

variable "project_name" {
  description = "Project name (used in resource group name)"
  type        = string
  # No default - must be provided via terraform.tfvars or TF_VAR_project_name
}

variable "environment" {
  description = "Environment name"
  type        = string
  default     = "dev"
}

variable "location" {
  description = "Azure region for resources"
  type        = string
  default     = "francecentral"
}

variable "storage_account_name" {
  description = "Name of the storage account (must be globally unique, 3-24 chars, lowercase alphanumeric)"
  type        = string
  # No default - must be provided via terraform.tfvars or TF_VAR_storage_account_name
}

variable "storage_account_tier" {
  description = "Storage account tier"
  type        = string
  default     = "Standard"
}

variable "storage_replication_type" {
  description = "Storage account replication type"
  type        = string
  default     = "RAGRS"
}

variable "enable_data_lake_gen2" {
  description = "Enable hierarchical namespace for Data Lake Gen2"
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags to apply to resources"
  type        = map(string)
  default     = {}
}

variable "bronze_monitoring_enabled" {
  description = "Create Bronze log alerts (Azure Monitor pricing applies). The timer still emits health traces."
  type        = bool
  default     = true
}

variable "bronze_alert_action_group_ids" {
  description = "Existing Azure Monitor action group IDs. Empty: alerts are visible in Azure, without external notifications."
  type        = list(string)
  default     = []
}

variable "silver_monitoring_enabled" {
  description = "Create five Silver log alerts; the independent health timer remains active."
  type        = bool
  default     = true
}

variable "silver_enabled" {
  description = "Enable automatic Silver batches; false keeps Bronze active and emits disabled health signals."
  type        = bool
  default     = true
}

variable "silver_alert_action_group_ids" {
  description = "Existing action groups for Silver. Empty: Azure alerts without external notifications."
  type        = list(string)
  default     = []
}
