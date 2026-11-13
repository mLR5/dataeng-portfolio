# Silver on Databricks serverless (ADR 0001): one more brick of this infrastructure,
# applied by the same Deploy Infrastructure workflow as the rest.
# Disabled by default: nothing below exists until the GitHub variable
# DATABRICKS_ENABLED is true. The Function App only ingests Bronze.

variable "databricks_enabled" {
  description = "Create the serverless workspace, Unity Catalog access, Silver notebooks and job."
  type        = bool
  default     = false
}

variable "databricks_silver_path" {
  description = "Silver root inside the datalake container: silver/weatherapi in production (set by the workflows)."
  type        = string
  default     = "silver/_validation/spark"

  validation {
    condition     = can(regex("^silver/[A-Za-z0-9_./=-]+$", var.databricks_silver_path)) && !strcontains(var.databricks_silver_path, "..")
    error_message = "Expected a relative path under silver/."
  }
}

variable "databricks_schedule_paused" {
  description = "Pause the daily 02:00 UTC schedule (the workflows run it unpaused in production)."
  type        = bool
  default     = true
}

variable "databricks_alert_emails" {
  description = "Addresses notified when a Silver run fails."
  type        = list(string)
  default     = []
}

# Deployment service principal, also the owner of the job and its Unity Catalog grants.
variable "databricks_client_id" {
  type      = string
  default   = ""
  sensitive = true
}

variable "databricks_client_secret" {
  type      = string
  default   = ""
  sensitive = true
}

variable "databricks_tenant_id" {
  type      = string
  default   = ""
  sensitive = true
}

locals {
  databricks_count = var.databricks_enabled ? 1 : 0
  # Metastore-level names are shared by every workspace of the region.
  databricks_prefix   = "${var.project_name}-${var.environment}"
  databricks_url      = var.databricks_enabled ? "https://${azapi_resource.databricks_workspace[0].output.properties.workspaceUrl}" : null
  bronze_root         = "abfss://bronze@${module.storage.name}.dfs.core.windows.net"
  silver_root         = "abfss://datalake@${module.storage.name}.dfs.core.windows.net/${var.databricks_silver_path}"
  silver_notebook_dir = "/Shared/weather-silver"
  silver_notebooks    = var.databricks_enabled ? toset(["silver_rules", "silver_pipeline", "silver_publish", "silver_freshness", "silver_explore"]) : toset([])
}

# --- Azure ------------------------------------------------------------------

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

# Unity Catalog reaches ADLS with user delegation SAS. Generating the delegation key
# is a storage-account action that container-scoped data roles do not grant; this
# role allows only that, no data access (the first job run failed without it).
resource "azurerm_role_assignment" "databricks_blob_delegator" {
  count                = local.databricks_count
  scope                = module.storage.id
  role_definition_name = "Storage Blob Delegator"
  principal_id         = azurerm_databricks_access_connector.silver[0].identity[0].principal_id
}

# --- Unity Catalog ----------------------------------------------------------

resource "databricks_storage_credential" "adls" {
  count   = local.databricks_count
  name    = "${local.databricks_prefix}-adls"
  comment = "Access Connector identity: Bronze read-only, datalake read/write (Azure RBAC)"

  azure_managed_identity {
    access_connector_id = azurerm_databricks_access_connector.silver[0].id
  }
}

# Validation is skipped because new role assignments take minutes to propagate;
# the first job run proves the access instead.
resource "databricks_external_location" "bronze" {
  count           = local.databricks_count
  name            = "${local.databricks_prefix}-bronze"
  url             = "${local.bronze_root}/weather/weatherapi"
  credential_name = databricks_storage_credential.adls[0].name
  read_only       = true
  skip_validation = true
  comment         = "Bronze WeatherAPI archives written by the Function App"
  depends_on      = [azurerm_role_assignment.databricks_bronze_reader, azurerm_role_assignment.databricks_blob_delegator]
}

resource "databricks_external_location" "silver" {
  count           = local.databricks_count
  name            = "${local.databricks_prefix}-silver"
  url             = "abfss://datalake@${module.storage.name}.dfs.core.windows.net/silver"
  credential_name = databricks_storage_credential.adls[0].name
  skip_validation = true
  comment         = "Silver Delta tables and their _control tables, validation roots included"
  depends_on      = [azurerm_role_assignment.databricks_datalake_contributor, azurerm_role_assignment.databricks_blob_delegator]
}

resource "databricks_grants" "bronze" {
  count             = local.databricks_count
  external_location = databricks_external_location.bronze[0].id
  grant {
    principal  = var.databricks_client_id
    privileges = ["READ_FILES"]
  }
}

resource "databricks_grants" "silver" {
  count             = local.databricks_count
  external_location = databricks_external_location.silver[0].id
  grant {
    principal  = var.databricks_client_id
    privileges = ["READ_FILES", "WRITE_FILES"]
  }
}

# --- Notebooks and job --------------------------------------------------------

resource "databricks_notebook" "silver" {
  for_each = local.silver_notebooks
  source   = "${path.module}/../databricks/notebooks/${each.key}.py"
  path     = "${local.silver_notebook_dir}/${each.key}"
}

resource "databricks_job" "silver" {
  count       = local.databricks_count
  name        = "silver-weatherapi"
  description = "Publish pending Bronze ingestions into Silver Delta tables, then check freshness."
  # Replaces the Blob lease: never two writers at once.
  max_concurrent_runs = 1
  # The first production run replays the whole Bronze history (47 min in
  # validation); daily runs take a few minutes.
  timeout_seconds = 7200

  schedule {
    quartz_cron_expression = "0 0 2 * * ?"
    timezone_id            = "UTC"
    pause_status           = var.databricks_schedule_paused ? "PAUSED" : "UNPAUSED"
  }

  dynamic "email_notifications" {
    for_each = length(var.databricks_alert_emails) > 0 ? [1] : []
    content {
      on_failure = var.databricks_alert_emails
    }
  }

  # No cluster block: notebook tasks run on serverless compute.
  # Tasks are declared in task_key order, as the Jobs API returns them, otherwise
  # every plan shows the two tasks swapping. Execution order comes from depends_on.
  task {
    task_key = "freshness"
    depends_on {
      task_key = "publish"
    }

    notebook_task {
      notebook_path = databricks_notebook.silver["silver_freshness"].path
      base_parameters = {
        silver_root = local.silver_root
      }
    }
  }

  task {
    task_key = "publish"
    # A failed run is safe to replay: publications make it idempotent.
    max_retries               = 2
    min_retry_interval_millis = 300000

    notebook_task {
      notebook_path = databricks_notebook.silver["silver_publish"].path
      base_parameters = {
        bronze_root = local.bronze_root
        silver_root = local.silver_root
        # Nightly listing limited to the last 3 UTC days; whole Bronze on Sundays.
        lookback_days     = "3"
        full_scan_weekday = "6"
      }
    }
  }

  depends_on = [databricks_grants.bronze, databricks_grants.silver, databricks_notebook.silver]
}

output "databricks_workspace_url" {
  description = "Serverless workspace URL (null while Databricks is disabled)"
  value       = local.databricks_url
}

output "databricks_silver_job_id" {
  description = "ID of the silver-weatherapi job, to start a run by hand"
  value       = var.databricks_enabled ? databricks_job.silver[0].id : null
}

output "databricks_silver_root" {
  value = var.databricks_enabled ? local.silver_root : null
}
