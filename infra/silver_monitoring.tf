locals {
  silver_alerts = {
    freshness = {
      description = "Silver sans donnees utilisables recentes, coordinateur arrete ou sonde en erreur."
      query       = <<-KQL
        traces
        | where cloud_RoleName == "func-${var.project_name}-${var.environment}"
        | where message startswith "SILVER_HEALTH "
        | top 1 by timestamp desc
        | extend health = parse_json(substring(message, 14))
        | where tostring(health.freshness) in ("stale", "error")
      KQL
    }
    failures = {
      description = "Echec technique Silver ; les travaux restent persistants pour reprise."
      query       = <<-KQL
        traces
        | where cloud_RoleName == "func-${var.project_name}-${var.environment}"
        | where timestamp > ago(30m) and message startswith "SILVER_RUN "
        | extend run = parse_json(substring(message, 11))
        | where tostring(run.outcome) == "error"
            or tolong(run.technical_errors) > 0 or tolong(run.discovery_errors) > 0
      KQL
    }
    backlog = {
      description = "Travaux Silver persistants plus anciens que le seuil de reprise."
      query       = <<-KQL
        traces
        | where cloud_RoleName == "func-${var.project_name}-${var.environment}"
        | where message startswith "SILVER_HEALTH "
        | top 1 by timestamp desc
        | extend health = parse_json(substring(message, 14))
        | where tobool(health.backlog_stale)
      KQL
    }
    quality = {
      description = "Derniere collecte Silver partielle, rejetee, vide ou horizon incomplet."
      query       = <<-KQL
        traces
        | where cloud_RoleName == "func-${var.project_name}-${var.environment}"
        | where message startswith "SILVER_HEALTH "
        | top 1 by timestamp desc
        | extend health = parse_json(substring(message, 14))
        | where tobool(health.quality_degraded)
      KQL
    }
    monitor_missing = {
      description = "Aucun signal de supervision Silver depuis 45 minutes."
      query       = <<-KQL
        traces
        | where cloud_RoleName == "func-${var.project_name}-${var.environment}"
        | where timestamp > ago(45m) and message startswith "SILVER_HEALTH "
        | summarize Signals = count()
        | where Signals == 0
      KQL
    }
  }
}

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "silver" {
  for_each = var.silver_monitoring_enabled ? local.silver_alerts : {}

  name                     = "silver-${each.key}-${var.project_name}-${var.environment}"
  resource_group_name      = azurerm_resource_group.main.name
  location                 = azurerm_resource_group.main.location
  description              = each.value.description
  scopes                   = [module.application_insights.id]
  severity                 = 2
  enabled                  = true
  evaluation_frequency     = "PT15M"
  window_duration          = "PT1H"
  auto_mitigation_enabled  = true
  criteria {
    query                   = each.value.query
    time_aggregation_method = "Count"
    operator                = "GreaterThan"
    threshold               = 0
    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }
  dynamic "action" {
    for_each = length(var.silver_alert_action_group_ids) > 0 ? [true] : []
    content {
      action_groups = var.silver_alert_action_group_ids
    }
  }
  tags = var.tags
}
