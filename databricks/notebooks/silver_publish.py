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

bronze_root = dbutils.widgets.get("bronze_root")
silver_root = dbutils.widgets.get("silver_root")
batch_size = int(dbutils.widgets.get("batch_size"))
max_batches = int(dbutils.widgets.get("max_batches"))
reconcile = dbutils.widgets.get("reconcile") == "true"
if not bronze_root or not silver_root:
    raise ValueError("bronze_root and silver_root are required")
if not 1 <= batch_size <= 1000 or not 1 <= max_batches <= 500:
    raise ValueError("batch_size 1..1000, max_batches 1..500")

# COMMAND ----------

spark.conf.set("spark.sql.session.timeZone", "UTC")
totals = run(spark, bronze_root=bronze_root, silver_root=silver_root,
             batch_size=batch_size, max_batches=max_batches, reconcile=reconcile)
totals
