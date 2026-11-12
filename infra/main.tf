terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    azapi = {
      source  = "Azure/azapi"
      version = "~> 2.0"
    }
    databricks = {
      source  = "databricks/databricks"
      version = "~> 1.80"
    }
  }

  backend "azurerm" {
    container_name = "tfstate"
    key            = "terraform.tfstate"
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

# The two providers below are only used by infra/databricks.tf (disabled by default).
provider "azapi" {
  subscription_id = var.subscription_id
}

# Workspace created in this same configuration. With the workspace resource ID the
# provider also sends the Azure management token, which makes the deployment service
# principal workspace admin on first use (validated by probe/databricks-serverless).
provider "databricks" {
  host                        = local.databricks_url
  azure_workspace_resource_id = var.databricks_enabled ? azapi_resource.databricks_workspace[0].id : null
  azure_client_id             = var.databricks_enabled ? var.databricks_client_id : null
  azure_client_secret         = var.databricks_enabled ? var.databricks_client_secret : null
  azure_tenant_id             = var.databricks_enabled ? var.databricks_tenant_id : null
}

# Resource Group
resource "azurerm_resource_group" "main" {
  name     = "rg-${var.project_name}-${var.environment}"
  location = var.location

  tags = var.tags
}

# Storage Account Module
module "storage" {
  source = "./modules/storage"

  name                = var.storage_account_name
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location

  account_tier             = var.storage_account_tier
  account_replication_type = var.storage_replication_type
  enable_hns               = var.enable_data_lake_gen2
  queue_name               = "ingestion-weatherapi"

  tags = var.tags
}

# Key Vault Module
module "keyvault" {
  source = "./modules/keyvault"

  name                = "kv-${var.project_name}-${var.environment}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location

  tags = var.tags
}

# Application Insights Module
module "application_insights" {
  source = "./modules/application_insights"

  name                = "appi-${var.project_name}-${var.environment}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location

  tags = var.tags
}

# Function App Module
module "function_app" {
  source = "./modules/function_app"

  name                = "func-${var.project_name}-${var.environment}"
  service_plan_name   = "asp-${var.project_name}-${var.environment}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location

  storage_account_name       = module.storage.name
  storage_account_id         = module.storage.id
  storage_account_access_key = module.storage.primary_access_key
  storage_connection_string  = module.storage.primary_connection_string

  application_insights_key               = module.application_insights.instrumentation_key
  application_insights_connection_string = module.application_insights.connection_string
  # Same spelling as the value the portal writes, so the plan shows no tag change.
  # (No leading slash: Terraform reads a pattern wrapped in slashes as a regex.)
  application_insights_id                = replace(module.application_insights.id, "providers/Microsoft.Insights/", "providers/microsoft.insights/")

  weatherapi_secret_uri = "${module.keyvault.vault_uri}secrets/weather-api-key"
  queue_name            = module.storage.queue_name
  datalake_container    = "datalake"

  tags = var.tags

  depends_on = [module.storage, module.keyvault, module.application_insights]
}

# RBAC: Grant Function App access to Key Vault secrets
resource "azurerm_role_assignment" "function_app_keyvault_secrets_user" {
  scope                = module.keyvault.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = module.function_app.principal_id
}

# RBAC: Grant Function App access to Storage Blob Data Contributor
resource "azurerm_role_assignment" "function_app_storage_blob_contributor" {
  scope                = module.storage.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = module.function_app.principal_id
}

# RBAC: Grant Function App access to Storage Queue Data Contributor
resource "azurerm_role_assignment" "function_app_storage_queue_contributor" {
  scope                = module.storage.id
  role_definition_name = "Storage Queue Data Contributor"
  principal_id         = module.function_app.principal_id
}
