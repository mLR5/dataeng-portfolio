# Databricks notebook source
# MAGIC %md
# MAGIC # Silver WeatherAPI : publication
# MAGIC
# MAGIC Tâche 1 du job `silver-weatherapi`. Liste les archives Bronze, retire les
# MAGIC ingestions déjà présentes dans la table `publications`, transforme les autres
# MAGIC et les publie : observations, prévisions, quarantaine, puis publications.
# MAGIC
# MAGIC Rejouable sans risque : une ingestion publiée n'est jamais réécrite, et une
# MAGIC exécution interrompue est réparée par la suivante.

# COMMAND ----------

# MAGIC %run ./silver_pipeline

# COMMAND ----------

dbutils.widgets.text("bronze_root", "", "Racine Bronze (abfss://bronze@<compte>.dfs.core.windows.net)")
dbutils.widgets.text("silver_root", "", "Racine Silver (abfss://datalake@<compte>.dfs.core.windows.net/silver/...)")
dbutils.widgets.text("batch_size", "200", "Ingestions par lot")
dbutils.widgets.text("max_batches", "50", "Lots maximum par exécution")
dbutils.widgets.dropdown("reconcile", "false", ["false", "true"], "Relire aussi les ingestions publiées")
dbutils.widgets.text("lookback_days", "3", "Jours Bronze listés chaque nuit (0 = tout)")
dbutils.widgets.text("full_scan_weekday", "6", "Jour du listing complet (0 = lundi, 6 = dimanche)")

bronze_root = dbutils.widgets.get("bronze_root")
silver_root = dbutils.widgets.get("silver_root")
batch_size = int(dbutils.widgets.get("batch_size"))
max_batches = int(dbutils.widgets.get("max_batches"))
reconcile = dbutils.widgets.get("reconcile") == "true"
lookback_days = int(dbutils.widgets.get("lookback_days"))
full_scan_weekday = int(dbutils.widgets.get("full_scan_weekday"))
if not bronze_root or not silver_root:
    raise ValueError("bronze_root and silver_root are required")
if not 1 <= batch_size <= 1000 or not 1 <= max_batches <= 500:
    raise ValueError("batch_size 1..1000, max_batches 1..500")
if not 0 <= lookback_days <= 31 or not 0 <= full_scan_weekday <= 6:
    raise ValueError("lookback_days 0..31, full_scan_weekday 0..6")

# COMMAND ----------

spark.conf.set("spark.sql.session.timeZone", "UTC")
totals = run(spark, bronze_root=bronze_root, silver_root=silver_root,
             batch_size=batch_size, max_batches=max_batches, reconcile=reconcile,
             lookback_days=lookback_days, full_scan_weekday=full_scan_weekday)
totals
