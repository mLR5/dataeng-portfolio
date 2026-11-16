# Databricks notebook source
# MAGIC %md
# MAGIC # Gold WeatherAPI : dimensions et faits
# MAGIC Point d'entrée du job. Les bornes facultatives sélectionnent les collectes UTC,
# MAGIC début inclus et fin exclue. Le registre conserve les sources des périodes précédentes.
# MAGIC `mode=incremental` charge par MERGE ; `full_rebuild_weekday` (0 = lundi,
# MAGIC 6 = dimanche UTC, vide = désactivé) force un recalcul complet ce jour-là.
# COMMAND ----------
# MAGIC %run ./gold_pipeline
# COMMAND ----------
for name, default in (("silver_root", ""), ("gold_root", ""), ("uc_schema", ""),
                      ("start_date", ""), ("end_date", ""), ("minimum_coverage_pct", "98"),
                      ("full_rebuild_weekday", "6")):
    dbutils.widgets.text(name, default)
dbutils.widgets.dropdown("mode", "incremental", ["incremental", "full"])
silver_root = dbutils.widgets.get("silver_root")
gold_root = dbutils.widgets.get("gold_root")
uc_schema = dbutils.widgets.get("uc_schema").strip()
if not silver_root or not gold_root:
    raise ValueError("silver_root and gold_root are required")
if uc_schema and not _UC_SCHEMA.fullmatch(uc_schema):
    raise ValueError("uc_schema must be catalogue.schema")
full_rebuild_weekday = gold_full_rebuild_weekday(dbutils.widgets.get("full_rebuild_weekday"))
report = run_gold(spark, silver_root=silver_root, gold_root=gold_root,
                  start_date=dbutils.widgets.get("start_date"), end_date=dbutils.widgets.get("end_date"),
                  mode=dbutils.widgets.get("mode"),
                  minimum_coverage_pct=float(dbutils.widgets.get("minimum_coverage_pct")),
                  full_rebuild_weekday=full_rebuild_weekday)
if uc_schema:
    report["catalog"] = register_gold_tables(spark, GoldPaths(gold_root), uc_schema)
report
