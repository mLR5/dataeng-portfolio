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

# Data Lake Gen2 Filesystem (Container) for parameters
resource "azurerm_storage_data_lake_gen2_filesystem" "parameters" {
  name               = "parameters"
  storage_account_id = azurerm_storage_account.this.id
}

# Data Lake Gen2 Filesystem (Container) for medallion architecture
# Folders will be created dynamically by Azure Functions during data ingestion
resource "azurerm_storage_data_lake_gen2_filesystem" "datalake" {
  name               = "datalake"
  storage_account_id = azurerm_storage_account.this.id
}

resource "azurerm_storage_queue" "ingestion_weatherapi" {
  name               = "ingestion-weatherapi"
  storage_account_id = azurerm_storage_account.this.id
}

# Blob container for staging (Claim Check pattern)
resource "azurerm_storage_container" "staging" {
  name                  = "staging"
  storage_account_id    = azurerm_storage_account.this.id
  container_access_type = "private"
}

# Lifecycle management policy - auto-delete staging blobs after 7 days
resource "azurerm_storage_management_policy" "staging_cleanup" {
  storage_account_id = azurerm_storage_account.this.id

  rule {
    name    = "delete-old-staging-blobs"
    enabled = true

    filters {
      prefix_match = ["staging/raw-ingestion/"]
      blob_types   = ["blockBlob"]
    }

    actions {
      base_blob {
        delete_after_days_since_modification_greater_than = 7
      }
    }
  }
}
