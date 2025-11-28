output "id" {
  description = "Function App ID"
  value       = azurerm_function_app_flex_consumption.this.id
}

output "name" {
  description = "Function App name"
  value       = azurerm_function_app_flex_consumption.this.name
}

output "default_hostname" {
  description = "Function App default hostname"
  value       = azurerm_function_app_flex_consumption.this.default_hostname
}

output "principal_id" {
  description = "Function App managed identity principal ID"
  value       = azurerm_function_app_flex_consumption.this.identity[0].principal_id
}

output "tenant_id" {
  description = "Function App managed identity tenant ID"
  value       = azurerm_function_app_flex_consumption.this.identity[0].tenant_id
}
