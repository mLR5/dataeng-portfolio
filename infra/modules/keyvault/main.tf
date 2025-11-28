data "azurerm_client_config" "current" {}

resource "azurerm_key_vault" "this" {
  name                       = var.name
  location                   = var.location
  resource_group_name        = var.resource_group_name
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = var.sku_name
  soft_delete_retention_days = var.soft_delete_retention_days
  purge_protection_enabled   = var.purge_protection_enabled

  # Enable RBAC authorization
  enable_rbac_authorization = var.enable_rbac_authorization

  network_acls {
    default_action = var.network_default_action
    bypass         = var.network_bypass
  }

  tags = var.tags
}

# Note: Secrets will be added manually via Azure CLI or Portal for security
# Example: az keyvault secret set --vault-name <vault-name> --name weatherapi-api-key --value <your-key>
