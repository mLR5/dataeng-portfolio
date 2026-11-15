# Databricks notebook source
# MAGIC %md
# MAGIC # Coûts WeatherAPI : lecture des métriques, sans traitement métier
# MAGIC Les prix catalogue en USD sont une valorisation estimée, distincte des coûts
# MAGIC Azure enregistrés dans la devise de facturation. Aucune table n'est modifiée.

# COMMAND ----------
import json
import re
from datetime import datetime, timezone, date
from pyspark.sql import functions as F

for name, default in (("workspace_id", ""), ("period_start", "2026-11-01"),
                      ("period_end", "2026-11-16"), ("catalog", "")):
    dbutils.widgets.text(name, default)
workspace_id = dbutils.widgets.get("workspace_id")
period_start, period_end = dbutils.widgets.get("period_start"), dbutils.widgets.get("period_end")
catalog = dbutils.widgets.get("catalog")
if not workspace_id.isdigit() or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", catalog):
    raise ValueError("Invalid workspace or catalog")
if date.fromisoformat(period_start) >= date.fromisoformat(period_end):
    raise ValueError("Invalid cost period")
spark.conf.set("spark.sql.session.timeZone", "UTC")
report = {"collected_at_utc": datetime.now(timezone.utc).isoformat(),
          "period_start_inclusive": period_start, "period_end_exclusive": period_end,
          "queries": {}, "gold_tables": {}, "notes": [
              "usage quantities include signed billing corrections",
              "Databricks effective list-price estimates in USD are not Azure invoices or negotiated prices",
              "DESCRIBE DETAIL size is active files, not physical retained versions or all container storage"]}


def query(name, sql, limit=5000):
    try:
        frame = spark.sql(sql)
        rows = frame.limit(limit + 1).collect()
        item = {"available": True, "truncated": len(rows) > limit,
                "rows": [r.asDict(recursive=True) for r in rows[:limit]]}
    except Exception as error:
        message = re.sub(r"(?:https?|abfss)://[^\s\"'<>]+", "<location>", str(error))
        item = {"available": False, "error_type": type(error).__name__, "error": message[:1800]}
    report["queries"][name] = item
    return item


predicate = (f"workspace_id = '{workspace_id}' AND usage_date >= DATE '{period_start}' "
             f"AND usage_date < DATE '{period_end}'")
query("billing_usage_freshness", f"""
SELECT MIN(usage_start_time) AS earliest_usage, MAX(usage_end_time) AS latest_usage,
       MAX(ingestion_date) AS latest_ingestion_date, COUNT(*) AS records
FROM system.billing.usage WHERE {predicate}
""")
query("daily_usage", f"""
SELECT usage_date, sku_name, billing_origin_product, usage_unit,
       SUM(usage_quantity) AS usage_quantity, COUNT(*) AS records
FROM system.billing.usage WHERE {predicate}
GROUP BY ALL ORDER BY usage_date, sku_name, usage_unit
""")
query("record_types", f"""
SELECT record_type, usage_unit, SUM(usage_quantity) AS signed_quantity, COUNT(*) AS records
FROM system.billing.usage WHERE {predicate} GROUP BY ALL ORDER BY record_type, usage_unit
""")

# Field names evolve; inspect the actual schema before optional metadata access.
metadata_fields = set()
try:
    metadata_fields = {f.name for f in spark.sql(
        f"SELECT usage_metadata FROM system.billing.usage WHERE {predicate} LIMIT 0"
    ).schema["usage_metadata"].dataType}
except Exception:
    pass
report["usage_metadata_fields"] = sorted(metadata_fields)


def metadata(name, prefix=""):
    return f"CAST({prefix}usage_metadata.{name} AS STRING)" if name in metadata_fields else "CAST(NULL AS STRING)"


job_id, run_id = metadata("job_id"), metadata("job_run_id")
task_id = metadata("job_task_run_id")
query("job_run_usage", f"""
SELECT usage_date, sku_name, usage_unit, {job_id} AS job_id,
       {run_id} AS job_run_id, {task_id} AS job_task_run_id,
       SUM(usage_quantity) AS usage_quantity, COUNT(*) AS records
FROM system.billing.usage WHERE {predicate}
GROUP BY ALL ORDER BY usage_date, job_id, job_run_id, sku_name
""")

price_predicate = (f"u.workspace_id = '{workspace_id}' AND u.usage_date >= DATE '{period_start}' "
                   f"AND u.usage_date < DATE '{period_end}'")
price_join = """u.sku_name = p.sku_name AND u.cloud = p.cloud
 AND u.usage_unit = p.usage_unit AND p.currency_code = 'USD'
 AND u.usage_start_time >= p.price_start_time
 AND (p.price_end_time IS NULL OR u.usage_start_time < p.price_end_time)"""
query("daily_list_price_estimate_usd", f"""
SELECT u.usage_date, u.sku_name, u.usage_unit, p.currency_code,
       SUM(u.usage_quantity) AS usage_quantity,
       MIN(CAST(COALESCE(p.pricing.effective_list.default, p.pricing.default) AS DOUBLE)) AS min_unit_price,
       MAX(CAST(COALESCE(p.pricing.effective_list.default, p.pricing.default) AS DOUBLE)) AS max_unit_price,
       SUM(u.usage_quantity * CAST(COALESCE(p.pricing.effective_list.default, p.pricing.default) AS DOUBLE)) AS list_price_estimate,
       COUNT_IF(p.sku_name IS NULL) AS records_without_price
FROM system.billing.usage u LEFT JOIN system.billing.list_prices p ON {price_join}
WHERE {price_predicate} GROUP BY ALL ORDER BY u.usage_date, u.sku_name
""")
query("job_run_list_price_estimate_usd", f"""
SELECT {metadata('job_id', 'u.')} AS job_id, {metadata('job_run_id', 'u.')} AS job_run_id,
       {metadata('job_task_run_id', 'u.')} AS job_task_run_id,
       u.sku_name, u.usage_unit, p.currency_code,
       MIN(u.usage_start_time) AS first_usage, MAX(u.usage_end_time) AS last_usage,
       SUM(u.usage_quantity) AS usage_quantity,
       SUM(u.usage_quantity * CAST(COALESCE(p.pricing.effective_list.default, p.pricing.default) AS DOUBLE)) AS list_price_estimate,
       COUNT_IF(p.sku_name IS NULL) AS records_without_price
FROM system.billing.usage u LEFT JOIN system.billing.list_prices p ON {price_join}
WHERE {price_predicate} GROUP BY ALL ORDER BY first_usage, job_id, job_run_id
""")

tables = ("dim_city", "dim_date", "dim_condition", "dim_collection", "fact_collection_city",
          "fact_observation_hourly", "fact_forecast_hourly", "fact_forecast_hourly_error",
          "fact_forecast_daily", "fact_forecast_daily_error")
for name in tables:
    qualified = f"`{catalog}`.`gold`.`{name}`"
    try:
        d = spark.sql(f"DESCRIBE DETAIL {qualified}").first().asDict(recursive=True)
        report["gold_tables"][name] = {k: d.get(k) for k in
            ("numFiles", "sizeInBytes", "createdAt", "lastModified", "clusteringColumns", "properties")}
        history = spark.sql(f"DESCRIBE HISTORY {qualified} LIMIT 6")
        fields = [f for f in ("version", "timestamp", "operation", "operationMetrics", "job") if f in history.columns]
        report["gold_tables"][name]["recent_history"] = [r.asDict(recursive=True) for r in history.select(*fields).collect()]
    except Exception as error:
        report["gold_tables"][name] = {"available": False, "error_type": type(error).__name__, "error": str(error)[:800]}

# No lakeflow timeline grants are assumed. A failed query remains an explicit
# availability limit; the Azure charge queries still succeed independently.
try:
    available = spark.sql("SELECT * FROM system.lakeflow.job_task_run_timeline LIMIT 0").schema.fieldNames()
    columns = [name for name in ("job_id", "run_id", "task_key", "task_run_id", "parent_run_id",
                "period_start_time", "period_end_time", "result_state", "termination_code", "compute_ids")
               if name in available]
    query("job_task_timeline", f"""
    SELECT {', '.join(columns)} FROM system.lakeflow.job_task_run_timeline
    WHERE workspace_id = '{workspace_id}'
      AND period_start_time >= TIMESTAMP '{period_start} 00:00:00'
      AND period_start_time < TIMESTAMP '{period_end} 00:00:00'
    ORDER BY period_start_time DESC
    """, limit=1000)
except Exception as error:
    report["queries"]["job_task_timeline"] = {
        "available": False, "error_type": type(error).__name__, "error": str(error)[:1000]}

# COMMAND ----------
dbutils.notebook.exit(json.dumps(report, sort_keys=True, default=str))
