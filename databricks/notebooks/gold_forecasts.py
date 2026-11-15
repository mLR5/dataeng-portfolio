# Databricks notebook source
# MAGIC %md
# MAGIC # Gold WeatherAPI : prévisions et erreurs
# MAGIC
# MAGIC Composant chargé après `gold_contract` et `gold_dimensions` par le pipeline.
# MAGIC Les erreurs valent prévision moins observation. Les températures quotidiennes
# MAGIC de référence sont les statistiques des observations horaires canoniques, pas
# MAGIC des extrema mesurés en continu. Aucune vérité n'est inventée pour un jour incomplet.

# COMMAND ----------

from pyspark.sql import functions as F


def _gf_project(frame, table_name):
    """Order and cast an explicit contract without silently adding missing fields."""
    return frame.select(*[
        F.col(field.name).cast(field.dataType).alias(field.name)
        for field in GOLD_SCHEMAS[table_name]
    ])


def _gold_daily_observed_truth(observations, cities):
    """One sampled temperature truth for each fully covered local calendar day.

    UTC hour identities distinguish the repeated local hour at autumn DST. The
    difference between consecutive local midnights supplies 23/24/25 expected
    slots. This is deliberately not a count of distinct local clock labels.
    """
    observed = observations.groupBy("city_id", "local_date").agg(
        F.countDistinct("hour_utc").alias("observed_hours"),
        F.count("temperature_c").alias("temperature_hours"),
        F.min("temperature_c").alias("observed_temperature_min_c"),
        F.max("temperature_c").alias("observed_temperature_max_c"),
        F.avg("temperature_c").alias("observed_temperature_avg_c"),
    )
    zoned = observed.join(cities.select("city_id", "timezone"), "city_id")
    start = F.to_utc_timestamp(F.col("local_date").cast("timestamp"), F.col("timezone"))
    end = F.to_utc_timestamp(F.date_add("local_date", 1).cast("timestamp"), F.col("timezone"))
    return (zoned.withColumn("expected_hours", ((end.cast("long") - start.cast("long")) / 3600).cast("long"))
            .where((F.col("observed_hours") == F.col("expected_hours"))
                   & (F.col("temperature_hours") == F.col("expected_hours")))
            .withColumnRenamed("local_date", "target_local_date"))


def build_gold_forecasts(spark, inputs, dimensions):
    """Build the four forecast facts from frozen Silver inputs and canonical truth.

    Missing truth gives no error row yet; a subsequent build can score the same
    earlier forecast once its observation becomes available. Daily lead -1 rows
    remain in the forecast history but never enter the daily error table.
    """
    hourly_source = inputs[FORECAST_TABLE]
    hourly = (hourly_source.where(F.col("lead_time_hours") > 0)
              .withColumnRenamed("target_at_utc", "target_hour_utc")
              .withColumnRenamed("collected_at_utc", "issued_at_utc")
              .withColumn("local_hour", F.hour(F.from_utc_timestamp("target_hour_utc", F.col("source_timezone")))))
    hourly = _gf_project(hourly, "fact_forecast_hourly")

    observations = dimensions["fact_observation_hourly"]
    truth = observations.select(
        "city_id", F.col("hour_utc").alias("target_hour_utc"),
        F.col("ingestion_id").alias("observation_ingestion_id"), "observed_at_utc",
        F.col("temperature_c").alias("observed_temperature_c"),
    )
    hourly_error = (hourly.withColumnRenamed("temperature_c", "forecast_temperature_c")
                    .join(truth, ["city_id", "target_hour_utc"], "inner")
                    .withColumn("temperature_error_c", F.col("forecast_temperature_c") - F.col("observed_temperature_c"))
                    .withColumn("absolute_temperature_error_c", F.abs("temperature_error_c"))
                    .withColumn("squared_temperature_error_c", F.pow("temperature_error_c", 2)))
    hourly_error = _gf_project(hourly_error, "fact_forecast_hourly_error")

    daily = (inputs[FORECAST_DAILY_TABLE]
             .withColumnRenamed("collected_at_utc", "issued_at_utc")
             .withColumn("is_day", F.lit(True)))
    daily = _gf_project(daily, "fact_forecast_daily")

    daily_truth = _gold_daily_observed_truth(observations, dimensions["dim_city"])
    daily_error = daily.where(F.col("lead_days") >= 0)
    for metric in ("min", "max", "avg"):
        daily_error = daily_error.withColumnRenamed(
            f"temperature_{metric}_c", f"forecast_temperature_{metric}_c")
    daily_error = daily_error.join(daily_truth, ["city_id", "target_local_date"], "inner")
    for metric in ("min", "max", "avg"):
        daily_error = daily_error.withColumn(
            f"temperature_{metric}_error_c",
            F.col(f"forecast_temperature_{metric}_c") - F.col(f"observed_temperature_{metric}_c"))
    daily_error = _gf_project(daily_error, "fact_forecast_daily_error")

    return {
        "fact_forecast_hourly": hourly,
        "fact_forecast_hourly_error": hourly_error,
        "fact_forecast_daily": daily,
        "fact_forecast_daily_error": daily_error,
    }
