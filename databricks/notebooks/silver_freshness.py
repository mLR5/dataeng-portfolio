# Databricks notebook source
# MAGIC %md
# MAGIC # Silver WeatherAPI : fraîcheur
# MAGIC
# MAGIC Tâche 2 du job, après la publication. Échoue, donc notifie, si la dernière
# MAGIC collecte publiée est trop ancienne ou si les dernières 24 heures comptent trop
# MAGIC peu de publications (Bronze collecte 24 fois par jour).

# COMMAND ----------

# MAGIC %run ./silver_pipeline

# COMMAND ----------

dbutils.widgets.text("silver_root", "", "Racine Silver")
dbutils.widgets.text("max_age_hours", "26", "Âge maximal de la dernière collecte publiée (heures)")
dbutils.widgets.text("min_daily", "20", "Publications minimales sur 24 heures")

silver_root = dbutils.widgets.get("silver_root")
if not silver_root:
    raise ValueError("silver_root is required")

# COMMAND ----------

spark.conf.set("spark.sql.session.timeZone", "UTC")
report = freshness(spark, silver_root=silver_root,
                   max_age_hours=float(dbutils.widgets.get("max_age_hours")),
                   min_daily=int(dbutils.widgets.get("min_daily")))
if not report["fresh"]:
    raise RuntimeError(f"Silver publications are stale: {report}")
report
