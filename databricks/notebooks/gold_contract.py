# Databricks notebook source
# MAGIC %md
# MAGIC # Gold WeatherAPI : contrat dimensions et faits
# MAGIC
# MAGIC Contrat du modèle v4, chargé après `silver_pipeline` par la façade Gold.
# MAGIC Les clés et relations sont vérifiées avant toute publication Gold.

# COMMAND ----------

from datetime import datetime, timedelta, timezone
import hashlib

from pyspark.sql import types as T


# 2.0.0: incremental MERGE loads, dim_city SCD 2 (city_sk) and dim_condition SCD 1.
# The contract forbids mixing versions in one root: 2.0.0 writes gold/weatherapi_v2.
GOLD_VERSION = "2.0.0"
# v4's historical coverage acceptance applies to these two cities; London has
# no geographically valid observations before 19 October 2026.
GOLD_COVERAGE_REQUIRED_CITIES = ("fr_paris", "be_brussels")


def _gold_field(name, dtype, nullable=False):
    return T.StructField(name, dtype, nullable)


def _gold_fields(specs):
    return [_gold_field(*spec) for spec in specs]


_GS = T.StringType()
_GI = T.IntegerType()
_GL = T.LongType()
_GD = T.DoubleType()
_GT = T.TimestampType()
_GDATE = T.DateType()
_GB = T.BooleanType()

# Copy the versioned metric types rather than maintain a second weather contract.
GOLD_OBSERVATION_METRICS = tuple(f.name for f in WEATHER_FIELDS + EXTRA_OBSERVATION_FIELDS)
GOLD_HOURLY_METRICS = tuple(f.name for f in WEATHER_FIELDS) + (
    "chance_of_rain_pct", "chance_of_snow_pct",
) + tuple(f.name for f in EXTRA_FORECAST_FIELDS)
GOLD_DAILY_METRICS = tuple(f.name for f in FORECAST_DAILY_SCHEMA)[
    FORECAST_DAILY_SCHEMA.get_field_index("temperature_min_c"):
]


def _gold_metrics(table_name, columns):
    source = SPARK_SCHEMAS[table_name]
    return [_gold_field(name, source[name].dataType, source[name].nullable) for name in columns]


GOLD_TABLE_KEYS = {
    "dim_city": ("city_sk",),
    "dim_date": ("date",),
    "dim_condition": ("condition_code", "is_day"),
    "dim_collection": ("ingestion_id",),
    "fact_collection_city": ("ingestion_id", "city_id"),
    "fact_observation_hourly": ("city_id", "hour_utc"),
    "fact_forecast_hourly": ("city_id", "target_hour_utc", "issued_at_utc"),
    "fact_forecast_hourly_error": ("city_id", "target_hour_utc", "issued_at_utc"),
    "fact_forecast_daily": ("city_id", "target_local_date", "issued_at_utc"),
    "fact_forecast_daily_error": ("city_id", "target_local_date", "issued_at_utc"),
}

GOLD_SCHEMAS = {
    "dim_city": T.StructType(_gold_fields([
        ("city_sk", _GS), ("city_id", _GS), ("city_name", _GS), ("country_name", _GS),
        ("latitude", _GD, True), ("longitude", _GD, True), ("timezone", _GS),
        ("hemisphere", _GS), ("first_collected_at_utc", _GT, True), ("is_active", _GB),
        ("valid_from_utc", _GT), ("valid_to_utc", _GT, True), ("is_current", _GB),
    ])),
    "dim_date": T.StructType(_gold_fields([
        ("date", _GDATE), ("year", _GI), ("quarter", _GI), ("month", _GI),
        ("month_name", _GS), ("iso_week", _GI), ("day_of_week", _GI),
        ("day_name", _GS), ("is_weekend", _GB),
    ])),
    "dim_condition": T.StructType(_gold_fields([
        ("condition_code", _GI), ("is_day", _GB), ("label_fr", _GS, True),
        ("category", _GS), ("is_precipitation", _GB, True),
    ])),
    "dim_collection": T.StructType(_gold_fields([
        ("ingestion_id", _GS), ("collected_at_utc", _GT, True),
        ("collected_date", _GDATE, True), ("publication_status", _GS),
        ("bronze_format", _GS), ("requested_horizon_days", T.ShortType(), True),
        ("cities_requested", _GL), ("cities_successful", _GL),
        ("unknown_path_count", _GL), ("source_blob", _GS), ("source_sha256", _GS),
    ])),
    "fact_collection_city": T.StructType(_gold_fields([
        ("ingestion_id", _GS), ("city_id", _GS), ("city_sk", _GS, True), ("collection_status", _GS),
        ("observation_valid", _GL), ("forecast_hourly_valid", _GL),
        ("forecast_hourly_expected", _GL), ("forecast_daily_valid", _GL),
        ("rejected_rows", _GL), ("nulled_metrics", _GL),
    ])),
    "fact_observation_hourly": T.StructType(_gold_fields([
        ("city_id", _GS), ("city_sk", _GS), ("hour_utc", _GT), ("observed_at_utc", _GT),
        ("distance_to_hour_min", _GD), ("ingestion_id", _GS),
        ("collected_at_utc", _GT), ("local_date", _GDATE), ("local_hour", _GI),
    ]) + _gold_metrics(OBSERVATION_TABLE, GOLD_OBSERVATION_METRICS)),
    "fact_forecast_hourly": T.StructType(_gold_fields([
        ("city_id", _GS), ("city_sk", _GS), ("target_hour_utc", _GT), ("issued_at_utc", _GT),
        ("ingestion_id", _GS), ("target_local_date", _GDATE), ("local_hour", _GI),
        ("lead_time_hours", _GD),
    ]) + _gold_metrics(FORECAST_TABLE, GOLD_HOURLY_METRICS)),
    "fact_forecast_hourly_error": T.StructType(_gold_fields([
        ("city_id", _GS), ("city_sk", _GS), ("target_hour_utc", _GT), ("issued_at_utc", _GT),
        ("ingestion_id", _GS), ("observation_ingestion_id", _GS),
        ("observed_at_utc", _GT), ("forecast_temperature_c", _GD),
        ("observed_temperature_c", _GD), ("temperature_error_c", _GD),
        ("absolute_temperature_error_c", _GD), ("squared_temperature_error_c", _GD),
    ])),
    "fact_forecast_daily": T.StructType(_gold_fields([
        ("city_id", _GS), ("city_sk", _GS), ("target_local_date", _GDATE), ("issued_at_utc", _GT),
        ("ingestion_id", _GS), ("lead_days", T.ShortType()), ("is_day", _GB),
    ]) + _gold_metrics(FORECAST_DAILY_TABLE, GOLD_DAILY_METRICS)),
    "fact_forecast_daily_error": T.StructType(_gold_fields([
        ("city_id", _GS), ("city_sk", _GS), ("target_local_date", _GDATE), ("issued_at_utc", _GT),
        ("ingestion_id", _GS), ("expected_hours", _GL), ("observed_hours", _GL),
    ]) + _gold_fields([
        spec
        for metric in ("min", "max", "avg")
        for spec in (
            (f"forecast_temperature_{metric}_c", _GD, True),
            (f"observed_temperature_{metric}_c", _GD),
            (f"temperature_{metric}_error_c", _GD, True),
        )
    ])),
}

# (child columns, parent table, parent columns). Only complete optional references
# require a parent. In particular daily code=NULL with is_day=true is valid.
GOLD_FOREIGN_KEYS = {
    "dim_collection": ((("collected_date",), "dim_date", ("date",)),),
    "fact_collection_city": (
        (("ingestion_id",), "dim_collection", ("ingestion_id",)),
        (("city_id",), "dim_city", ("city_id",)),
        (("city_sk",), "dim_city", ("city_sk",)),
    ),
    "fact_observation_hourly": (
        (("city_id",), "dim_city", ("city_id",)),
        (("city_sk",), "dim_city", ("city_sk",)),
        (("ingestion_id",), "dim_collection", ("ingestion_id",)),
        (("local_date",), "dim_date", ("date",)),
        (("condition_code", "is_day"), "dim_condition", ("condition_code", "is_day")),
    ),
    "fact_forecast_hourly": (
        (("city_id",), "dim_city", ("city_id",)),
        (("city_sk",), "dim_city", ("city_sk",)),
        (("ingestion_id",), "dim_collection", ("ingestion_id",)),
        (("target_local_date",), "dim_date", ("date",)),
        (("condition_code", "is_day"), "dim_condition", ("condition_code", "is_day")),
    ),
    "fact_forecast_hourly_error": (
        (("city_sk",), "dim_city", ("city_sk",)),
        (("city_id", "target_hour_utc", "issued_at_utc"), "fact_forecast_hourly",
         ("city_id", "target_hour_utc", "issued_at_utc")),
        (("city_id", "target_hour_utc"), "fact_observation_hourly", ("city_id", "hour_utc")),
        (("observation_ingestion_id",), "dim_collection", ("ingestion_id",)),
    ),
    "fact_forecast_daily": (
        (("city_id",), "dim_city", ("city_id",)),
        (("city_sk",), "dim_city", ("city_sk",)),
        (("ingestion_id",), "dim_collection", ("ingestion_id",)),
        (("target_local_date",), "dim_date", ("date",)),
        (("condition_code", "is_day"), "dim_condition", ("condition_code", "is_day")),
    ),
    "fact_forecast_daily_error": (
        (("city_sk",), "dim_city", ("city_sk",)),
        (("city_id", "target_local_date", "issued_at_utc"), "fact_forecast_daily",
         ("city_id", "target_local_date", "issued_at_utc")),
    ),
}

GOLD_COLLECTION_STATUSES = (
    "collected", "collection_failed", "location_rejected", "not_requested",
)

GOLD_CLUSTER_COLUMNS = {
    "dim_city": ("city_id",), "dim_date": ("date",),
    "dim_condition": ("condition_code",), "dim_collection": ("collected_at_utc",),
    "fact_collection_city": ("city_id", "ingestion_id"),
    "fact_observation_hourly": ("city_id", "hour_utc"),
    "fact_forecast_hourly": ("city_id", "target_hour_utc"),
    "fact_forecast_hourly_error": ("city_id", "target_hour_utc"),
    "fact_forecast_daily": ("city_id", "target_local_date"),
    "fact_forecast_daily_error": ("city_id", "target_local_date"),
}

# --- Load strategy, SCD and incremental window (GOLD_VERSION 2.0.0) -----------

# mode=incremental applies these strategies; mode=full recomputes and overwrites
# every table and is the reference the incremental result must equal.
GOLD_LOAD_STRATEGIES = {
    "dim_city": "scd2",
    "dim_date": "insert",
    "dim_condition": "scd1",
    "dim_collection": "insert",
    "fact_collection_city": "insert",
    "fact_observation_hourly": "window_upsert",
    "fact_forecast_hourly": "insert",
    "fact_forecast_hourly_error": "window_upsert",
    "fact_forecast_daily": "insert",
    "fact_forecast_daily_error": "window_upsert",
}

# Instant that selects the dim_city version of each fact row (the row's collection).
# fact_collection_city takes dim_collection.collected_at_utc through ingestion_id.
GOLD_CITY_SK_INSTANTS = {
    "fact_collection_city": "collected_at_utc",
    "fact_observation_hourly": "collected_at_utc",
    "fact_forecast_hourly": "issued_at_utc",
    "fact_forecast_hourly_error": "issued_at_utc",
    "fact_forecast_daily": "issued_at_utc",
    "fact_forecast_daily_error": "issued_at_utc",
}

# SCD 2 of dim_city. The first version of every city starts at this fixed origin
# (no validated coordinates yet) so that rejected collections before the first
# valid row still resolve a version. Never the job time.
GOLD_CITY_VALID_FROM_ORIGIN = datetime(1970, 1, 1, tzinfo=timezone.utc)
GOLD_CITY_TRACKED_ATTRIBUTES = (
    "city_name", "country_name", "latitude", "longitude", "timezone", "is_active",
)
# WeatherAPI supplies 4 decimals; rounding keeps float noise from creating versions.
GOLD_CITY_COORDINATE_DECIMALS = 4
# Reviewed registry changes: (city_id, effective_from_utc, {attribute: value}).
# The version starts at the first collection at or after the effective instant,
# identically in full and incremental builds. Empty for GOLD_VERSION 2.0.0.
GOLD_CITY_REGISTRY_CHANGES = ()
GOLD_CITY_SK_LENGTH = 16
GOLD_CITY_SK_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"  # Spark: yyyy-MM-dd'T'HH:mm:ss.SSSSSS'Z'


def gold_city_sk(city_id, valid_from_utc):
    """Deterministic surrogate: sha256(city_id|valid_from ISO µs UTC), 16 hex chars."""
    instant = valid_from_utc.astimezone(timezone.utc) if valid_from_utc.tzinfo else valid_from_utc
    text = f"{city_id}|{instant.strftime(GOLD_CITY_SK_FORMAT)}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:GOLD_CITY_SK_LENGTH]


# Affected window, per city, computed from the NEW ingestions only:
# [min(collected_at) - 1 day, max(collected_at) + 3 days], widened if needed to
# every observation hour, hourly target and daily local-day span those ingestions
# carry. Canonical hours are recomputed from Silver observations of all consumed
# ingestions whose observed_at lies in the window plus a 1 h margin.
GOLD_WINDOW_BEFORE = timedelta(days=1)
GOLD_WINDOW_AFTER = timedelta(days=3)
GOLD_OBSERVATION_MARGIN = timedelta(hours=1)

# Small Gold-owned control tables below <gold_root>/_control/, pinned by the
# build manifest like the ten tables (never registered in Unity Catalog).
GOLD_CONTROL_SCHEMAS = {
    "condition_label_counts": T.StructType(_gold_fields([
        ("condition_code", _GI), ("is_day", _GB), ("label", _GS, True), ("n", _GL),
    ])),
    "city_locations": T.StructType(_gold_fields([
        ("ingestion_id", _GS), ("city_id", _GS), ("collected_at_utc", _GT),
        ("latitude", _GD), ("longitude", _GD),
    ])),
}
GOLD_CONTROL_KEYS = {
    "condition_label_counts": ("condition_code", "is_day", "label"),
    "city_locations": ("ingestion_id", "city_id"),
}
