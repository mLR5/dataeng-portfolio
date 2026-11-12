# Phase 2 of ADR 0001: Azure side of the Databricks Silver platform.
# Disabled by default, so merging this file changes nothing until the GitHub
# variable DATABRICKS_ENABLED is set to true. Unity Catalog objects live in
# infra/databricks_uc (separate state: the workspace must exist first).

variable "databricks_enabled" {
  description = "Create the serverless Databricks workspace and its Access Connector for Silver."
  type        = bool
  default     = false
}

locals {
  databricks_count = var.databricks_enabled ? 1 : 0
}

# True serverless workspace: no managed VNet, NAT or managed resource group.
# azurerm does not expose computeMode, hence azapi on the ARM API validated by
# the probe/databricks-serverless branch (2026-01-01, francecentral).
resource "azapi_resource" "databricks_workspace" {
  count     = local.databricks_count
  type      = "Microsoft.Databricks/workspaces@2026-01-01"
  name      = "dbw-${var.project_name}-${var.environment}"
  parent_id = azurerm_resource_group.main.id
  location  = azurerm_resource_group.main.location
  tags      = var.tags

  # The embedded azapi schemas may predate this API version.
  schema_validation_enabled = false

  body = {
    sku = { name = "premium" }
    properties = {
      computeMode = "Serverless"
    }
  }

  response_export_values = ["properties.workspaceUrl", "properties.workspaceId"]
}

# Managed identity used by Unity Catalog to reach ADLS: no account key in Databricks.
resource "azurerm_databricks_access_connector" "silver" {
  count               = local.databricks_count
  name                = "dbac-${var.project_name}-${var.environment}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tags                = var.tags

  identity {
    type = "SystemAssigned"
  }
}

# Least privilege per container: Bronze is read-only, Silver tables are written in datalake.
resource "azurerm_role_assignment" "databricks_bronze_reader" {
  count                = local.databricks_count
  scope                = "${module.storage.id}/blobServices/default/containers/bronze"
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = azurerm_databricks_access_connector.silver[0].identity[0].principal_id
}

resource "azurerm_role_assignment" "databricks_datalake_contributor" {
  count                = local.databricks_count
  scope                = "${module.storage.id}/blobServices/default/containers/datalake"
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_databricks_access_connector.silver[0].identity[0].principal_id
}

output "databricks_workspace_url" {
  description = "Serverless workspace URL (empty while Databricks is disabled)"
  value       = var.databricks_enabled ? "https://${azapi_resource.databricks_workspace[0].output.properties.workspaceUrl}" : ""
}

output "databricks_workspace_resource_id" {
  description = "ARM ID of the serverless workspace"
  value       = var.databricks_enabled ? azapi_resource.databricks_workspace[0].id : ""
}

output "databricks_access_connector_id" {
  description = "Access Connector used by the Unity Catalog storage credential"
  value       = var.databricks_enabled ? azurerm_databricks_access_connector.silver[0].id : ""
}
