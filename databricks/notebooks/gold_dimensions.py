# Databricks notebook source
# MAGIC %md
# MAGIC # Gold WeatherAPI : dimensions et observations canoniques
# MAGIC
# MAGIC Composant chargé après `silver_pipeline` et `gold_contract` par la façade Gold.
# MAGIC Les transformations restent distribuées ; seules les trois villes du registre
# MAGIC sont des données de référence locales. Les sources sont déjà figées et filtrées
# MAGIC sur les ingestions Silver complètement publiées par le lecteur Gold.

# COMMAND ----------

from functools import reduce

from pyspark.sql import functions as F
from pyspark.sql import types as T
from pyspark.sql.window import Window


def _gold_dimension_project(frame, table):
    return frame.select(*(F.col(field.name).cast(field.dataType).alias(field.name)
                          for field in GOLD_SCHEMAS[table]))


def _gold_query_normalized(column):
    # Mirrors the relevant Latin aliases of Silver's immutable city registry.
    return F.regexp_replace(F.lower(F.trim(F.translate(column,
        "àáâäãåèéêëìíîïòóôöõùúûüçñ", "aaaaaaeeeeiiiiooooouuuucn"))), r"\s+", " ")


_GOLD_SUMMARY_COUNT = T.MapType(T.StringType(), T.LongType())
_GOLD_SUMMARY_SCHEMA = T.StructType([
    T.StructField("cities", T.StructType([
        T.StructField("requested", T.ArrayType(T.StringType())),
        T.StructField("collection_successful", T.ArrayType(T.StringType())),
        T.StructField("collection_failed", T.ArrayType(T.StringType())),
    ])),
    T.StructField("city_tables", T.MapType(T.StringType(),
        T.MapType(T.StringType(), _GOLD_SUMMARY_COUNT))),
    T.StructField("horizons", T.MapType(T.StringType(), T.StructType([
        T.StructField("daily_coverage", T.ArrayType(T.StructType([
            T.StructField("expected_hour_slots", T.LongType()),
        ]))),
    ]))),
])


def _gold_city_registry(spark):
    labels = {
        "fr_paris": ("Paris", "France"),
        "gb_london": ("Londres", "United Kingdom"),
        "be_brussels": ("Bruxelles", "Belgium"),
    }
    return spark.createDataFrame([
        (city.city_id, *labels[city.city_id], city.timezone,
         "north" if sum(city.latitude_bounds) >= 0 else "south", True)
        for city in CITIES
    ], "city_id string, city_name string, country_name string, timezone string, hemisphere string, is_active boolean")


def _gold_city_aliases(spark):
    return spark.createDataFrame([
        (city.city_id, _normalise(alias))
        for city in CITIES for alias in city.query_aliases
    ], "city_id string, query_alias string")


def _gold_dim_city(spark, inputs):
    columns = ["city_id", "collected_at_utc", "latitude", "longitude", "ingestion_id", "source_sha256"]
    locations = reduce(lambda left, right: left.unionByName(right),
                       [inputs[name].select(*columns) for name in SILVER_DATA_TABLES]).distinct()
    first = (locations.withColumn("_rank", F.row_number().over(Window.partitionBy("city_id")
             .orderBy("collected_at_utc", "ingestion_id", "source_sha256", "latitude", "longitude")))
             .where("_rank = 1").select("city_id", "latitude", "longitude",
                 F.col("collected_at_utc").alias("first_collected_at_utc")))
    return _gold_dimension_project(_gold_city_registry(spark).join(first, "city_id", "left"), "dim_city")


def _gold_observations(inputs):
    raw = inputs[OBSERVATION_TABLE]
    candidates = (raw.withColumn("hour_utc", F.date_trunc("hour", F.col("observed_at_utc") + F.expr("INTERVAL 30 MINUTES")))
                  .withColumn("distance_to_hour_min", F.abs(F.unix_micros("observed_at_utc")
                      - F.unix_micros("hour_utc")) / F.lit(60_000_000.0)))
    order = Window.partitionBy("city_id", "hour_utc").orderBy(
        F.col("distance_to_hour_min"), F.col("collected_at_utc"),
        F.col("observed_at_utc"), F.col("ingestion_id"), F.col("source_sha256"))
    kept = candidates.withColumn("_rank", F.row_number().over(order)).where("_rank = 1")
    local = F.from_utc_timestamp(F.col("hour_utc"), F.col("source_timezone"))
    kept = kept.withColumn("local_date", F.to_date(local)).withColumn("local_hour", F.hour(local))
    return _gold_dimension_project(kept, "fact_observation_hourly")


def _gold_dim_date(inputs, observations):
    dates = inputs[PUBLICATIONS].select(F.to_date("collected_at_utc").alias("date"))
    dates = dates.unionByName(observations.select(F.col("local_date").alias("date")))
    for name in (FORECAST_TABLE, FORECAST_DAILY_TABLE):
        dates = dates.unionByName(inputs[name].select(F.col("target_local_date").alias("date")))
    span = dates.agg(F.min("date").alias("_first"), F.max("date").alias("_last"))
    calendar = (span.where("_first IS NOT NULL")
                .select(F.explode(F.sequence("_first", "_last")).alias("date")))
    iso_day = (F.dayofweek("date") + F.lit(5)) % F.lit(7) + F.lit(1)
    month_names = F.array(*(F.lit(x) for x in (
        "janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre")))
    day_names = F.array(*(F.lit(x) for x in ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")))
    calendar = calendar.select("date", F.year("date").alias("year"), F.quarter("date").alias("quarter"),
        F.month("date").alias("month"), F.element_at(month_names, F.month("date")).alias("month_name"),
        F.weekofyear("date").alias("iso_week"), iso_day.alias("day_of_week"),
        F.element_at(day_names, iso_day).alias("day_name"), iso_day.isin(6, 7).alias("is_weekend"))
    return _gold_dimension_project(calendar, "dim_date")


# WeatherAPI's published condition-code groups. New codes are visible as unknown;
# their precipitation flag is null instead of asserting that no precipitation occurs.
_GOLD_CONDITION_GROUPS = {
    "clear": (1000,),
    "cloud": (1003, 1006, 1009),
    "fog": (1030, 1135, 1147),
    "rain": (1063, 1072, 1150, 1153, 1168, 1171, 1180, 1183, 1186, 1189, 1192, 1195, 1198, 1201, 1240, 1243, 1246),
    "snow": (1066, 1114, 1117, 1210, 1213, 1216, 1219, 1222, 1225, 1255, 1258),
    "sleet": (1069, 1204, 1207, 1237, 1249, 1252, 1261, 1264),
    "thunder": (1087, 1273, 1276, 1279, 1282),
}


def _gold_dim_condition(inputs):
    conditions = inputs[OBSERVATION_TABLE].select("condition_code", "is_day", "condition_text")
    conditions = conditions.unionByName(inputs[FORECAST_TABLE].select("condition_code", "is_day", "condition_text"))
    conditions = conditions.unionByName(inputs[FORECAST_DAILY_TABLE].select(
        "condition_code", F.lit(True).alias("is_day"), "condition_text"))
    counts = (conditions.where("condition_code IS NOT NULL AND is_day IS NOT NULL")
               .groupBy("condition_code", "is_day", "condition_text").count())
    order = Window.partitionBy("condition_code", "is_day").orderBy(F.col("count").desc(), F.col("condition_text").asc_nulls_last())
    dimension = (counts.withColumn("_rank", F.row_number().over(order)).where("_rank = 1")
                  .select("condition_code", "is_day", F.col("condition_text").alias("label_fr")))
    category = F.lit("unknown")
    for name, codes in _GOLD_CONDITION_GROUPS.items():
        category = F.when(F.col("condition_code").isin(*codes), F.lit(name)).otherwise(category)
    dimension = dimension.withColumn("category", category)
    dimension = dimension.withColumn("is_precipitation",
        F.when(F.col("category") == "unknown", F.lit(None).cast("boolean"))
         .otherwise(F.col("category").isin("rain", "snow", "sleet")
                    | (F.col("category").eqNullSafe("thunder") & (F.col("condition_code") != 1087))))
    return _gold_dimension_project(dimension, "dim_condition")


def _gold_dim_collection(publications):
    frame = publications.select("*", F.from_json("summary_json", _GOLD_SUMMARY_SCHEMA).alias("_summary"))
    frame = (frame.withColumn("collected_date", F.to_date("collected_at_utc"))
              .withColumn("publication_status", F.col("status"))
              .withColumn("cities_requested", F.size(F.coalesce("_summary.cities.requested", F.array().cast("array<string>"))))
              .withColumn("cities_successful", F.size(F.coalesce("_summary.cities.collection_successful", F.array().cast("array<string>")))))
    return _gold_dimension_project(frame, "dim_collection")


def _gold_collection_city(spark, inputs):
    pubs = inputs[PUBLICATIONS].select("ingestion_id", F.from_json("summary_json", _GOLD_SUMMARY_SCHEMA).alias("_summary"))
    cities = _gold_city_registry(spark).select("city_id")
    aliases = _gold_city_aliases(spark)
    empty = F.array().cast("array<string>")
    requested = (pubs.select("ingestion_id", F.explode(F.coalesce("_summary.cities.requested", empty)).alias("_query"))
                  .withColumn("query_alias", _gold_query_normalized(F.col("_query")))
                  .join(aliases, "query_alias").select("ingestion_id", "city_id").distinct()
                  .withColumn("_requested", F.lit(True)))
    failed = (pubs.select("ingestion_id", F.explode(F.coalesce("_summary.cities.collection_failed", empty)).alias("_query"))
               .withColumn("query_alias", _gold_query_normalized(F.col("_query")))
               .join(aliases, "query_alias").select("ingestion_id", "city_id").distinct()
               .withColumn("_failed", F.lit(True)))
    quarantine = (inputs[QUARANTINE].withColumn("query_alias", _gold_query_normalized(F.col("city_query")))
                   .join(aliases, "query_alias"))
    errors = quarantine.groupBy("ingestion_id", "city_id").agg(
        F.max(F.when(F.col("code") == "location_mismatch", 1).otherwise(0)).alias("_location_rejected"),
        F.max(F.when(F.col("code") == "collection_failed", 1).otherwise(0)).alias("_failed_error"),
        F.sum(F.when(F.col("scope") == "field", 1).otherwise(0)).alias("nulled_metrics"),
        F.count("*").alias("_error_count"))
    frame = pubs.crossJoin(cities).join(requested, ["ingestion_id", "city_id"], "left")
    frame = frame.join(failed, ["ingestion_id", "city_id"], "left").join(errors, ["ingestion_id", "city_id"], "left")
    valid_columns = [(OBSERVATION_TABLE, "observation_valid"), (FORECAST_TABLE, "forecast_hourly_valid"),
                     (FORECAST_DAILY_TABLE, "forecast_daily_valid")]
    for table, column in valid_columns:
        counts = inputs[table].groupBy("ingestion_id", "city_id").agg(F.count("*").alias(column))
        frame = frame.join(counts, ["ingestion_id", "city_id"], "left")
    frame = frame.fillna(0, subset=["observation_valid", "forecast_hourly_valid", "forecast_daily_valid",
                                   "nulled_metrics", "_location_rejected", "_failed_error", "_error_count"])
    city_counts = F.col("_summary.city_tables")[F.col("city_id")]
    rejected = reduce(lambda total, table: total + F.coalesce(city_counts[table]["rejected_rows"], F.lit(0)),
                      SILVER_DATA_TABLES, F.lit(0))
    coverage = F.col("_summary.horizons")[F.col("city_id")]["daily_coverage"]
    expected = F.aggregate(coverage, F.lit(0).cast("long"),
                           lambda total, day: total + F.coalesce(day["expected_hour_slots"], F.lit(0)))
    evidence = (F.coalesce(F.col("_requested"), F.lit(False)) | F.coalesce(F.col("_failed"), F.lit(False))
                | (F.col("_error_count") > 0)
                | ((F.col("observation_valid") + F.col("forecast_hourly_valid") + F.col("forecast_daily_valid")) > 0))
    status = (F.when(F.coalesce(F.col("_failed"), F.lit(False)) | (F.col("_failed_error") > 0), "collection_failed")
               .when(F.col("_location_rejected") > 0, "location_rejected")
               .when(evidence, "collected").otherwise("not_requested"))
    frame = frame.withColumn("collection_status", status).withColumn("rejected_rows", rejected)
    frame = frame.withColumn("forecast_hourly_expected", F.coalesce(expected, F.lit(0)))
    return _gold_dimension_project(frame, "fact_collection_city")


def build_gold_dimensions(spark, inputs):
    """Build four dimensions, collection-city coverage and canonical observations.

    The caller fixes the UTC Spark session zone and supplies complete-publication
    inputs. No table is written and no processing-time value affects the result.
    """
    observations = _gold_observations(inputs)
    return {
        "dim_city": _gold_dim_city(spark, inputs),
        "dim_date": _gold_dim_date(inputs, observations),
        "dim_condition": _gold_dim_condition(inputs),
        "dim_collection": _gold_dim_collection(inputs[PUBLICATIONS]),
        "fact_collection_city": _gold_collection_city(spark, inputs),
        "fact_observation_hourly": observations,
    }
