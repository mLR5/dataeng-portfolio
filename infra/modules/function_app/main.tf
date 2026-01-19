# App Service Plan (Flex Consumption)
resource "azurerm_service_plan" "this" {
  name                = var.service_plan_name
  location            = var.location
  resource_group_name = var.resource_group_name
  os_type             = "Linux"
  sku_name            = "FC1" # Flex Consumption

  tags = var.tags
}

# Storage Container for deployment
resource "azurerm_storage_container" "deployment" {
  name                  = "deploymentpackage"
  storage_account_name  = var.storage_account_name
  container_access_type = "private"
}

# Function App Flex Consumption
resource "azurerm_function_app_flex_consumption" "this" {
  name                           = var.name
  location                       = var.location
  resource_group_name            = var.resource_group_name
  service_plan_id                = azurerm_service_plan.this.id

  # Storage configuration
  storage_container_type         = "blobContainer"
  storage_container_endpoint     = "https://${var.storage_account_name}.blob.core.windows.net/${azurerm_storage_container.deployment.name}"
  storage_authentication_type    = "SystemAssignedIdentity"

  # Runtime configuration
  runtime_name                   = "python"
  runtime_version                = "3.11"

  # Scale configuration
  maximum_instance_count         = 100
  instance_memory_in_mb          = 2048

  site_config {
    application_insights_key               = var.application_insights_key
    application_insights_connection_string = var.application_insights_connection_string
  }

  app_settings = merge(
    {
      "AzureWebJobsStorage__accountName"   = var.storage_account_name
      "AzureWebJobsStorage__queueServiceUri" = "https://${var.storage_account_name}.queue.core.windows.net"
      "AzureWebJobsStorage__blobServiceUri"  = "https://${var.storage_account_name}.blob.core.windows.net"
      "AzureWebJobsSTORAGE_CONNECTION_STRING" = var.storage_connection_string
      "AzureWebJobsFeatureFlags"          = "EnableWorkerIndexing"
      "WEATHERAPI_KEY"                    = "@Microsoft.KeyVault(SecretUri=${var.weatherapi_secret_uri})"
      "STORAGE_ACCOUNT_NAME"              = var.storage_account_name
      "STORAGE_CONNECTION_STRING"         = var.storage_connection_string
      "QUEUE_NAME"                        = var.queue_name
      "DATALAKE_CONTAINER"                = var.datalake_container
      "STAGING_CONTAINER"                 = "staging"
      "BRONZE_CONTAINER"                  = "bronze"
    },
    var.additional_app_settings
  )

  identity {
    type = "SystemAssigned"
  }

  tags = var.tags

  depends_on = [azurerm_storage_container.deployment]
}
