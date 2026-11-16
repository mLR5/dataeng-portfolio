# Databricks notebook source
# MAGIC %md
# MAGIC # Gold WeatherAPI : contrat dimensions et faits
# MAGIC
# MAGIC Contrat du modèle v4, chargé après `silver_pipeline` par la façade Gold.
# MAGIC Les clés et relations sont vérifiées avant toute publication Gold.

# COMMAND ----------

from pyspark.sql import types as T


GOLD_VERSION = "1.0.0"
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
    "dim_city": ("city_id",),
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
        ("city_id", _GS), ("city_name", _GS), ("country_name", _GS),
        ("latitude", _GD, True), ("longitude", _GD, True), ("timezone", _GS),
        ("hemisphere", _GS), ("first_collected_at_utc", _GT, True), ("is_active", _GB),
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
        ("ingestion_id", _GS), ("city_id", _GS), ("collection_status", _GS),
        ("observation_valid", _GL), ("forecast_hourly_valid", _GL),
        ("forecast_hourly_expected", _GL), ("forecast_daily_valid", _GL),
        ("rejected_rows", _GL), ("nulled_metrics", _GL),
    ])),
    "fact_observation_hourly": T.StructType(_gold_fields([
        ("city_id", _GS), ("hour_utc", _GT), ("observed_at_utc", _GT),
        ("distance_to_hour_min", _GD), ("ingestion_id", _GS),
        ("collected_at_utc", _GT), ("local_date", _GDATE), ("local_hour", _GI),
    ]) + _gold_metrics(OBSERVATION_TABLE, GOLD_OBSERVATION_METRICS)),
    "fact_forecast_hourly": T.StructType(_gold_fields([
        ("city_id", _GS), ("target_hour_utc", _GT), ("issued_at_utc", _GT),
        ("ingestion_id", _GS), ("target_local_date", _GDATE), ("local_hour", _GI),
        ("lead_time_hours", _GD),
    ]) + _gold_metrics(FORECAST_TABLE, GOLD_HOURLY_METRICS)),
    "fact_forecast_hourly_error": T.StructType(_gold_fields([
        ("city_id", _GS), ("target_hour_utc", _GT), ("issued_at_utc", _GT),
        ("ingestion_id", _GS), ("observation_ingestion_id", _GS),
        ("observed_at_utc", _GT), ("forecast_temperature_c", _GD),
        ("observed_temperature_c", _GD), ("temperature_error_c", _GD),
        ("absolute_temperature_error_c", _GD), ("squared_temperature_error_c", _GD),
    ])),
    "fact_forecast_daily": T.StructType(_gold_fields([
        ("city_id", _GS), ("target_local_date", _GDATE), ("issued_at_utc", _GT),
        ("ingestion_id", _GS), ("lead_days", T.ShortType()), ("is_day", _GB),
    ]) + _gold_metrics(FORECAST_DAILY_TABLE, GOLD_DAILY_METRICS)),
    "fact_forecast_daily_error": T.StructType(_gold_fields([
        ("city_id", _GS), ("target_local_date", _GDATE), ("issued_at_utc", _GT),
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
    ),
    "fact_observation_hourly": (
        (("city_id",), "dim_city", ("city_id",)),
        (("ingestion_id",), "dim_collection", ("ingestion_id",)),
        (("local_date",), "dim_date", ("date",)),
        (("condition_code", "is_day"), "dim_condition", ("condition_code", "is_day")),
    ),
    "fact_forecast_hourly": (
        (("city_id",), "dim_city", ("city_id",)),
        (("ingestion_id",), "dim_collection", ("ingestion_id",)),
        (("target_local_date",), "dim_date", ("date",)),
        (("condition_code", "is_day"), "dim_condition", ("condition_code", "is_day")),
    ),
    "fact_forecast_hourly_error": (
        (("city_id", "target_hour_utc", "issued_at_utc"), "fact_forecast_hourly",
         ("city_id", "target_hour_utc", "issued_at_utc")),
        (("city_id", "target_hour_utc"), "fact_observation_hourly", ("city_id", "hour_utc")),
        (("observation_ingestion_id",), "dim_collection", ("ingestion_id",)),
    ),
    "fact_forecast_daily": (
        (("city_id",), "dim_city", ("city_id",)),
        (("ingestion_id",), "dim_collection", ("ingestion_id",)),
        (("target_local_date",), "dim_date", ("date",)),
        (("condition_code", "is_day"), "dim_condition", ("condition_code", "is_day")),
    ),
    "fact_forecast_daily_error": (
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

# COMMAND ----------

# Step 5 is a separate derived contract. These definitions intentionally do not
# modify the ten-table Gold v1 contract or its transformation version.
DASHBOARD_VERSION = "1.0.0"
AGG_CITY_DAILY_KEYS = ("city_id", "local_date")
AGG_CITY_DAILY_SCHEMA = T.StructType(_gold_fields([
    ("city_id", _GS), ("local_date", _GDATE),
    ("expected_hours", _GL), ("observed_hours", _GL),
    ("coverage_pct", _GD), ("is_complete", _GB),
    ("temperature_observed_hours", _GL),
    ("temperature_min_c", _GD, True), ("temperature_max_c", _GD, True),
    ("temperature_avg_c", _GD, True),
    ("humidity_observed_hours", _GL), ("humidity_avg_pct", _GD, True),
    ("wind_observed_hours", _GL), ("wind_avg_kph", _GD, True),
    ("wind_max_kph", _GD, True),
    ("wind_gust_observed_hours", _GL), ("wind_gust_max_kph", _GD, True),
    ("precipitation_observed_hours", _GL),
    ("precipitation_total_mm", _GD, True), ("precipitation_is_complete", _GB),
    ("sunrise_utc", _GT, True), ("sunset_utc", _GT, True),
    ("daylight_hours", _GD, True),
]))
