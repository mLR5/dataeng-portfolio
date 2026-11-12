# Unity Catalog objects for the Silver job, applied after infra/ created the
# workspace and the Access Connector (outputs passed as variables).
# Separate state: the Databricks provider needs an existing workspace URL.

terraform {
  required_providers {
    databricks = {
      source  = "databricks/databricks"
      version = "~> 1.80"
    }
  }

  backend "azurerm" {
    container_name = "tfstate"
    key            = "databricks-uc.tfstate"
  }
}

variable "workspace_url" {
  description = "https://adb-....azuredatabricks.net, from the infra output databricks_workspace_url"
  type        = string
}

variable "workspace_resource_id" {
  description = "ARM ID of the workspace, from the infra output databricks_workspace_resource_id"
  type        = string
}

variable "access_connector_id" {
  description = "From the infra output databricks_access_connector_id"
  type        = string
}

variable "storage_account_name" {
  type = string
}

variable "job_principal" {
  description = "Application (client) ID of the service principal that deploys and runs the job"
  type        = string
}

# ARM_CLIENT_ID / ARM_CLIENT_SECRET / ARM_TENANT_ID from the CI identity. With the
# workspace resource ID, the provider also sends the Azure management token that
# makes a not-yet-added service principal workspace admin (validated by the probe).
provider "databricks" {
  host                        = var.workspace_url
  azure_workspace_resource_id = var.workspace_resource_id
}

resource "databricks_storage_credential" "adls" {
  name    = "weather-adls"
  comment = "Access Connector managed identity; Bronze read-only and datalake write by RBAC"

  azure_managed_identity {
    access_connector_id = var.access_connector_id
  }
}

resource "databricks_external_location" "bronze" {
  name            = "weather-bronze"
  url             = "abfss://bronze@${var.storage_account_name}.dfs.core.windows.net/weather/weatherapi"
  credential_name = databricks_storage_credential.adls.name
  read_only       = true
  comment         = "Bronze WeatherAPI archives written by the Function App"
}

resource "databricks_external_location" "silver" {
  name            = "weather-silver"
  url             = "abfss://datalake@${var.storage_account_name}.dfs.core.windows.net/silver"
  credential_name = databricks_storage_credential.adls.name
  comment         = "Silver Delta tables and their _control tables, including validation roots"
}

resource "databricks_grants" "bronze" {
  external_location = databricks_external_location.bronze.id
  grant {
    principal  = var.job_principal
    privileges = ["READ_FILES"]
  }
}

resource "databricks_grants" "silver" {
  external_location = databricks_external_location.silver.id
  grant {
    principal  = var.job_principal
    privileges = ["READ_FILES", "WRITE_FILES"]
  }
}

output "bronze_root" {
  value = "abfss://bronze@${var.storage_account_name}.dfs.core.windows.net"
}

output "silver_external_location" {
  value = databricks_external_location.silver.url
}
