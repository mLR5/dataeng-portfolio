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
    # The average sums the sorted temperatures: a full build and an incremental
    # window read different partitions, and a floating sum must not depend on order.
    observed = observations.groupBy("city_id", "local_date").agg(
        F.countDistinct("hour_utc").alias("observed_hours"),
        F.count("temperature_c").alias("temperature_hours"),
        F.min("temperature_c").alias("observed_temperature_min_c"),
        F.max("temperature_c").alias("observed_temperature_max_c"),
        F.array_sort(F.collect_list("temperature_c")).alias("_temperatures"),
    ).withColumn("observed_temperature_avg_c", F.when(F.col("temperature_hours") > 0, F.aggregate(
        "_temperatures", F.lit(0.0).cast("double"), lambda total, value: total + value)
        / F.col("temperature_hours")))
    # One time zone per city: the current SCD 2 version.
    zones = cities.where("is_current").select("city_id", "timezone") if "is_current" in cities.columns \
        else cities.select("city_id", "timezone")
    zoned = observed.join(zones, "city_id")
    start = F.to_utc_timestamp(F.col("local_date").cast("timestamp"), F.col("timezone"))
    end = F.to_utc_timestamp(F.date_add("local_date", 1).cast("timestamp"), F.col("timezone"))
    return (zoned.withColumn("expected_hours", ((end.cast("long") - start.cast("long")) / 3600).cast("long"))
            .where((F.col("observed_hours") == F.col("expected_hours"))
                   & (F.col("temperature_hours") == F.col("expected_hours")))
            .withColumnRenamed("local_date", "target_local_date"))


def gold_hourly_forecasts(source):
    """Strictly future hourly emissions of the given Silver rows (city_sk unresolved)."""
    hourly = (source.where(F.col("lead_time_hours") > 0)
              .withColumnRenamed("target_at_utc", "target_hour_utc")
              .withColumnRenamed("collected_at_utc", "issued_at_utc")
              .withColumn("local_hour", F.hour(F.from_utc_timestamp("target_hour_utc", F.col("source_timezone"))))
              .withColumn("city_sk", F.lit(None).cast("string")))
    return _gf_project(hourly, "fact_forecast_hourly")


def gold_daily_forecasts(source):
    daily = (source.withColumnRenamed("collected_at_utc", "issued_at_utc")
             .withColumn("is_day", F.lit(True))
             .withColumn("city_sk", F.lit(None).cast("string")))
    return _gf_project(daily, "fact_forecast_daily")


def gold_hourly_errors(hourly, observations):
    """Score forecasts (Gold rows, city_sk kept) against canonical observations."""
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
    return _gf_project(hourly_error, "fact_forecast_hourly_error")


def gold_daily_errors(daily, observations, cities):
    """Score non-past daily forecasts against complete local days of observations."""
    daily_truth = _gold_daily_observed_truth(observations, cities)
    daily_error = daily.where(F.col("lead_days") >= 0)
    for metric in ("min", "max", "avg"):
        daily_error = daily_error.withColumnRenamed(
            f"temperature_{metric}_c", f"forecast_temperature_{metric}_c")
    daily_error = daily_error.join(daily_truth, ["city_id", "target_local_date"], "inner")
    for metric in ("min", "max", "avg"):
        daily_error = daily_error.withColumn(
            f"temperature_{metric}_error_c",
            F.col(f"forecast_temperature_{metric}_c") - F.col(f"observed_temperature_{metric}_c"))
    return _gf_project(daily_error, "fact_forecast_daily_error")


def build_gold_forecasts(spark, inputs, dimensions):
    """Build the four forecast facts from frozen Silver inputs and canonical truth.

    Missing truth gives no error row yet; a subsequent build can score the same
    earlier forecast once its observation becomes available. Daily lead -1 rows
    remain in the forecast history but never enter the daily error table.
    city_sk is the dim_city version valid at the issuing collection.
    """
    cities = dimensions["dim_city"]
    hourly = gold_resolve_city_sk(gold_hourly_forecasts(inputs[FORECAST_TABLE]), cities,
                                  "issued_at_utc", "fact_forecast_hourly")
    daily = gold_resolve_city_sk(gold_daily_forecasts(inputs[FORECAST_DAILY_TABLE]), cities,
                                 "issued_at_utc", "fact_forecast_daily")
    observations = dimensions["fact_observation_hourly"]
    return {
        "fact_forecast_hourly": hourly,
        "fact_forecast_hourly_error": gold_hourly_errors(hourly, observations),
        "fact_forecast_daily": daily,
        "fact_forecast_daily_error": gold_daily_errors(daily, observations, cities),
    }
