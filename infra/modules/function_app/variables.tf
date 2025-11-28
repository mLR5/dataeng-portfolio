variable "name" {
  description = "Name of the Function App"
  type        = string
}

variable "service_plan_name" {
  description = "Name of the App Service Plan"
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

variable "storage_account_name" {
  description = "Storage account name for Function App"
  type        = string
}

variable "storage_account_access_key" {
  description = "Storage account access key"
  type        = string
  sensitive   = true
}

variable "storage_connection_string" {
  description = "Storage connection string"
  type        = string
  sensitive   = true
}

variable "application_insights_key" {
  description = "Application Insights instrumentation key"
  type        = string
  sensitive   = true
  default     = null
}

variable "application_insights_connection_string" {
  description = "Application Insights connection string"
  type        = string
  sensitive   = true
  default     = null
}

variable "weatherapi_secret_uri" {
  description = "URI of WeatherAPI secret in Key Vault"
  type        = string
}

variable "queue_name" {
  description = "Name of the storage queue"
  type        = string
}

variable "datalake_container" {
  description = "Name of the Data Lake container"
  type        = string
}

variable "additional_app_settings" {
  description = "Additional app settings"
  type        = map(string)
  default     = {}
}

variable "tags" {
  description = "Tags to apply to resources"
  type        = map(string)
  default     = {}
}
