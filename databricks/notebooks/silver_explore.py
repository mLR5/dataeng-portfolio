# Databricks notebook source
# MAGIC %md
# MAGIC # Silver WeatherAPI : exploration
# MAGIC
# MAGIC Lecture seule. Montre l'état des publications, les rejets récents et lit les
# MAGIC prévisions comme le fera Gold : uniquement les ingestions publiées, à la
# MAGIC version Delta figée par le plan de lecture.

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
        .groupBy("table_name", "code", "city_query").count().orderBy(F.desc("count")))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Prévisions lues comme Gold

# COMMAND ----------

plan = published_read_plan(spark, paths)
forecasts = read_published(spark, plan, FORECAST_TABLE)
display(forecasts.groupBy("city_id", "target_local_date")
        .agg(F.count("*").alias("snapshots"), F.avg("temperature_c").alias("temperature_moyenne_c"))
        .orderBy(F.desc("target_local_date"), "city_id"))
