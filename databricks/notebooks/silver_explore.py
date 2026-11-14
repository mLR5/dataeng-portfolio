# Databricks notebook source
# MAGIC %md
# MAGIC # Silver WeatherAPI : exploration
# MAGIC
# MAGIC Lecture seule. Montre l'état des publications, les rejets récents et lit les
# MAGIC prévisions horaires et quotidiennes comme le fera Gold : uniquement les
# MAGIC ingestions publiées, à la version Delta figée par le plan de lecture.

# COMMAND ----------

# MAGIC %run ./silver_pipeline

# COMMAND ----------

dbutils.widgets.text("silver_root", "", "Racine Silver")
paths = SilverPaths(dbutils.widgets.get("silver_root"))
spark.conf.set("spark.sql.session.timeZone", "UTC")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Publications par jour de collecte et statut

# COMMAND ----------

publications = spark.read.format("delta").load(paths.table(PUBLICATIONS))
display(publications.groupBy(F.to_date("collected_at_utc").alias("jour"), "status")
        .count().orderBy(F.desc("jour"), "status"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Rejets les plus fréquents

# COMMAND ----------

display(spark.read.format("delta").load(paths.table(QUARANTINE))
        .groupBy("table_name", "scope", "code", "field").count().orderBy(F.desc("count")))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Contrôles v2 : format Bronze, mesures annulées, chemins inconnus

# COMMAND ----------

display(publications.groupBy("bronze_format", "requested_horizon_days")
        .agg(F.count("*").alias("publications"), F.sum("nulled_metrics").alias("mesures_annulees"),
             F.sum(F.when(F.col("unknown_path_count") > 0, 1).otherwise(0)).alias("avec_chemins_inconnus"))
        .orderBy("bronze_format", "requested_horizon_days"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Prévisions horaires lues comme Gold

# COMMAND ----------

plan = published_read_plan(spark, paths)
forecasts = read_published(spark, plan, FORECAST_TABLE)
display(forecasts.groupBy("city_id", "target_local_date")
        .agg(F.count("*").alias("snapshots"), F.avg("temperature_c").alias("temperature_moyenne_c"))
        .orderBy(F.desc("target_local_date"), "city_id"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Prévisions quotidiennes lues comme Gold

# COMMAND ----------

daily = read_published(spark, plan, FORECAST_DAILY_TABLE)
display(daily.groupBy("city_id", "target_local_date")
        .agg(F.count("*").alias("snapshots"), F.min("lead_days").alias("echeance_min_jours"),
             F.avg("temperature_min_c").alias("temperature_min_moyenne_c"),
             F.avg("temperature_max_c").alias("temperature_max_moyenne_c"))
        .orderBy(F.desc("target_local_date"), "city_id"))
