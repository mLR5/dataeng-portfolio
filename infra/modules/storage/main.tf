resource "azurerm_storage_account" "this" {
  name                     = var.name
  resource_group_name      = var.resource_group_name
  location                 = var.location
  account_tier             = var.account_tier
  account_replication_type = var.account_replication_type
  account_kind             = "StorageV2"
  access_tier              = var.access_tier

  # Security settings
  allow_nested_items_to_be_public = var.allow_public_access
  shared_access_key_enabled       = var.shared_access_key_enabled
  https_traffic_only_enabled      = true
  min_tls_version                 = var.min_tls_version

  # Data Lake Gen2
  is_hns_enabled = var.enable_hns

  # Network rules
  network_rules {
    default_action             = var.network_default_action
    bypass                     = var.network_bypass
    ip_rules                   = var.network_ip_rules
    virtual_network_subnet_ids = var.network_subnet_ids
  }

  # Enable large file shares
  large_file_share_enabled = var.large_file_share_enabled

  # Cross-tenant replication
  cross_tenant_replication_enabled = var.cross_tenant_replication_enabled

  tags = var.tags
}
