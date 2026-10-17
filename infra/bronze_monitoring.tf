locals {
  bronze_alerts = {
    freshness = {
      description = "Aucune donnee meteo Bronze recente, ou sonde en erreur (seuil 150 minutes)."
      severity    = 2
      query       = <<-KQL
        traces
        | where message startswith "BRONZE_HEALTH "
        | top 1 by timestamp desc
        | extend health = parse_json(substring(message, 14))
        | where tostring(health.freshness) != "fresh"
      KQL
    }
    poison = {
      description = "La queue poison Bronze contient au moins un message (compteur approximatif Azure)."
      severity    = 2
      query       = <<-KQL
        traces
        | where message startswith "BRONZE_HEALTH "
        | top 1 by timestamp desc
        | extend health = parse_json(substring(message, 14))
        | where tolong(health.poison_messages) > 0
      KQL
    }
    failures = {
      description = "Erreur de collecte/traitement Bronze, message invalide ou collecte partielle."
      severity    = 2
      query       = <<-KQL
        traces
        | where timestamp > ago(30m)
        | where message contains "Ingestion Failed"
            or message contains "Bronze Processing Failed"
            or message contains "Invalid Claim Check Message"
            or message contains "Ingestion Completed: partial"
            or message contains "Ingestion Completed: failed"
      KQL
    }
    monitor_missing = {
      description = "Aucun signal de la sonde Bronze depuis 45 minutes ; verifier Function App et telemetrie."
      severity    = 2
      query       = <<-KQL
        traces
        | where timestamp > ago(45m)
        | where message startswith "BRONZE_HEALTH "
        | summarize Signals = count()
        | where Signals == 0
      KQL
    }
  }
}

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "bronze" {
  for_each = var.bronze_monitoring_enabled ? local.bronze_alerts : {}

  name                = "bronze-${each.key}-${var.project_name}-${var.environment}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  description         = each.value.description
  scopes              = [module.application_insights.id]
  severity            = each.value.severity
  enabled             = true
  evaluation_frequency = "PT15M"
  window_duration      = "PT1H"
  auto_mitigation_enabled = true
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
    for_each = length(var.bronze_alert_action_group_ids) > 0 ? [true] : []
    content {
      action_groups = var.bronze_alert_action_group_ids
    }
  }

  tags = var.tags
}
