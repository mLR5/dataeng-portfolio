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

# COMMAND ----------

# MAGIC %md
# MAGIC ## Vues de lecture du dashboard
# MAGIC
# MAGIC La météo courante est la dernière observation publiée, avec son âge explicite.
# MAGIC Les prévisions sont sélectionnées par cible, parmi les émissions disponibles.
# MAGIC Les versions figées portent sur le snapshot Gold ; l'horloge reste celle de la
# MAGIC consultation. Ces fonctions retournent du SQL et ne créent aucun objet.

# COMMAND ----------

from collections.abc import Mapping
from datetime import datetime, timezone
import re


_DASHBOARD_SOURCE_TABLES = (
    "dim_city", "fact_observation_hourly", "fact_forecast_hourly", "fact_forecast_daily",
)
_DASHBOARD_SCHEMA_NAME = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]{0,254}\.[A-Za-z_][A-Za-z0-9_]{0,254}\Z")


def gold_dashboard_view_queries(source_uc_schema, *, source_versions=None, as_of=None):
    """Return the three SELECT bodies for stable, time-aware dashboard views.

    ``source_uc_schema`` is an unquoted catalog.schema identifier. A frozen Gold
    manifest may provide ``source_versions`` (the four used tables are required;
    other contract tables are accepted). Versions must be non-negative integers.
    ``as_of`` is an optional timezone-aware datetime for reproducible tests or
    historical inspection. Arbitrary SQL clock expressions are never accepted.

    UTC instants govern hourly targets and emissions; daily eligibility uses the
    current local date of each city. The 26h stale threshold reflects the daily
    Gold cadence. Current weather represents published observations, not a live
    WeatherAPI request. The caller owns CREATE/REPLACE VIEW and its permissions.
    Execute these bodies, or create the persistent views, with
    ``spark.sql.session.timeZone=UTC``: the local-date conversion assumes a UTC
    SQL session. Published views must capture that creation-time configuration.
    """
    if not isinstance(source_uc_schema, str) or not _DASHBOARD_SCHEMA_NAME.fullmatch(source_uc_schema):
        raise ValueError("source_uc_schema must be an unquoted catalog.schema identifier")
    catalog, schema = source_uc_schema.split(".")
    prefix = f"`{catalog}`.`{schema}`"
    if source_versions is not None:
        if not isinstance(source_versions, Mapping):
            raise ValueError("source_versions must be a mapping of Gold table names to versions")
        if set(source_versions) - set(GOLD_SCHEMAS):
            raise ValueError("source_versions contains an unknown Gold table")
        if set(_DASHBOARD_SOURCE_TABLES) - set(source_versions):
            raise ValueError("source_versions must contain all dashboard source tables")
        if any(type(version) is not int or version < 0 for version in source_versions.values()):
            raise ValueError("Gold source versions must be non-negative integers")
    if as_of is None:
        clock = "current_timestamp()"
    else:
        if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be a timezone-aware datetime")
        instant = as_of.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
        clock = f"TIMESTAMP '{instant}+00:00'"

    def table(name):
        reference = f"{prefix}.`{name}`"
        if source_versions is not None:
            reference += f" VERSION AS OF {source_versions[name]}"
        return reference

    def columns(name, alias):
        return ",\n       ".join(f"{alias}.`{field.name}`" for field in GOLD_SCHEMAS[name]
                                  if field.name != "city_id")

    city_fields = ", ".join(f"c.`{name}`" for name in
                            ("city_id", "city_name", "country_name", "latitude", "longitude", "timezone"))
    current = f"""WITH dashboard_clock AS (SELECT {clock} AS as_of_utc),
ranked AS (
    SELECT o.*, row_number() OVER (
        PARTITION BY o.city_id
        ORDER BY o.observed_at_utc DESC, o.collected_at_utc DESC,
                 o.ingestion_id DESC, o.hour_utc DESC) AS dashboard_rank
    FROM {table('fact_observation_hourly')} AS o
    CROSS JOIN dashboard_clock AS n
    WHERE o.observed_at_utc <= n.as_of_utc AND o.collected_at_utc <= n.as_of_utc
)
SELECT {city_fields},
       {columns('fact_observation_hourly', 'r')},
       n.as_of_utc,
       CAST((unix_micros(n.as_of_utc) - unix_micros(r.observed_at_utc)) / 3600000000.0 AS DOUBLE) AS observation_age_hours,
       (r.observed_at_utc IS NULL OR
        r.observed_at_utc < n.as_of_utc - INTERVAL 26 HOURS) AS is_stale
FROM {table('dim_city')} AS c
JOIN ranked AS r ON c.city_id = r.city_id AND r.dashboard_rank = 1
CROSS JOIN dashboard_clock AS n"""

    def forecast_view(name, target, daily=False):
        eligibility = ("f.target_local_date >= to_date(from_utc_timestamp(n.as_of_utc, c.timezone)) "
                       "AND f.lead_days >= 0" if daily else "f.target_hour_utc >= n.as_of_utc")
        return f"""WITH dashboard_clock AS (SELECT {clock} AS as_of_utc),
ranked AS (
    SELECT f.*, row_number() OVER (
        PARTITION BY f.city_id, f.{target}
        ORDER BY f.issued_at_utc DESC, f.ingestion_id DESC) AS dashboard_rank
    FROM {table(name)} AS f
    JOIN {table('dim_city')} AS c ON f.city_id = c.city_id
    CROSS JOIN dashboard_clock AS n
    WHERE f.issued_at_utc <= n.as_of_utc AND {eligibility}
)
SELECT {city_fields},
       {columns(name, 'r')},
       n.as_of_utc,
       CAST((unix_micros(n.as_of_utc) - unix_micros(r.issued_at_utc)) / 3600000000.0 AS DOUBLE) AS forecast_age_hours
FROM ranked AS r
JOIN {table('dim_city')} AS c ON r.city_id = c.city_id
CROSS JOIN dashboard_clock AS n
WHERE r.dashboard_rank = 1"""

    return {
        "v_current_weather": current,
        "v_latest_forecast_hourly": forecast_view("fact_forecast_hourly", "target_hour_utc"),
        "v_latest_forecast_daily": forecast_view("fact_forecast_daily", "target_local_date", daily=True),
    }
