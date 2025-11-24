output "resource_group_name" {
  description = "Name of the resource group"
  value       = azurerm_resource_group.main.name
}

output "resource_group_id" {
  description = "ID of the resource group"
  value       = azurerm_resource_group.main.id
}

output "storage_account_name" {
  description = "Name of the storage account"
  value       = module.storage.name
}

output "storage_account_id" {
  description = "ID of the storage account"
  value       = module.storage.id
}

output "storage_account_primary_blob_endpoint" {
  description = "Primary blob endpoint"
  value       = module.storage.primary_blob_endpoint
}

output "storage_account_primary_dfs_endpoint" {
  description = "Primary Data Lake endpoint"
  value       = module.storage.primary_dfs_endpoint
}

output "storage_account_primary_access_key" {
  description = "Primary access key for the storage account"
  value       = module.storage.primary_access_key
  sensitive   = true
}
