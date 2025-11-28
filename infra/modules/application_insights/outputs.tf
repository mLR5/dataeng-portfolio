output "id" {
  description = "ID of the Application Insights instance"
  value       = azurerm_application_insights.this.id
}

output "instrumentation_key" {
  description = "Instrumentation key for Application Insights"
  value       = azurerm_application_insights.this.instrumentation_key
  sensitive   = true
}

output "connection_string" {
  description = "Connection string for Application Insights"
  value       = azurerm_application_insights.this.connection_string
  sensitive   = true
}

output "app_id" {
  description = "Application ID"
  value       = azurerm_application_insights.this.app_id
}
