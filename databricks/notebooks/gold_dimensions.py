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


def _gold_union_locations(inputs):
    columns = ["city_id", "ingestion_id", "collected_at_utc", "latitude", "longitude"]
    return reduce(lambda left, right: left.unionByName(right),
                  [inputs[name].select(*columns) for name in SILVER_DATA_TABLES])


def _gold_city_locations(inputs):
    """Control ledger: one validated location per (ingestion, city), rounded.

    Every Silver row of a city in one ingestion comes from the same location
    object; the ordering only makes an unexpected disagreement deterministic.
    """
    digits = GOLD_CITY_COORDINATE_DECIMALS
    rows = (_gold_union_locations(inputs)
            .withColumn("latitude", F.round("latitude", digits))
            .withColumn("longitude", F.round("longitude", digits)))
    order = Window.partitionBy("ingestion_id", "city_id").orderBy("latitude", "longitude")
    rows = rows.withColumn("_rank", F.row_number().over(order)).where("_rank = 1")
    return _gold_project_control(rows, "city_locations")


def _gold_project_control(frame, name):
    return frame.select(*(F.col(field.name).cast(field.dataType).alias(field.name)
                          for field in GOLD_CONTROL_SCHEMAS[name]))


def gold_timestamp_literal(instant):
    """UTC timestamp literal independent of the Python process time zone."""
    if instant.tzinfo is None:
        raise ValueError("Gold instants must be timezone-aware")
    text = instant.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    return F.to_timestamp(F.lit(text))  # The session zone is fixed to UTC by run_gold.


def _gold_city_sk_column(city_id, valid_from):
    text = F.concat_ws("|", city_id, F.date_format(valid_from, "yyyy-MM-dd'T'HH:mm:ss.SSSSSS'Z'"))
    return F.substring(F.sha2(text, 256), 1, GOLD_CITY_SK_LENGTH)


def gold_city_history(spark, locations, collections):
    """dim_city SCD 2 from the location ledger and the collection instants.

    Deterministic for a given set of ingestions, whatever their arrival order:
    full and incremental builds derive the same history from the same ledger.
    For each city and each collection instant the tracked attributes are the
    registry values in force at that instant (GOLD_CITY_REGISTRY_CHANGES) and the
    last validated coordinates at or before it. A new version starts at the first
    instant whose tracked tuple differs from the previous one; the first version
    starts at GOLD_CITY_VALID_FROM_ORIGIN, before any validated coordinate.
    """
    origin = gold_timestamp_literal(GOLD_CITY_VALID_FROM_ORIGIN)
    registry = _gold_city_registry(spark)
    instants = (collections.where("collected_at_utc IS NOT NULL").select("collected_at_utc")
                .unionByName(locations.select("collected_at_utc"))
                .unionByName(spark.range(1).select(origin.alias("collected_at_utc")))
                .distinct().withColumnRenamed("collected_at_utc", "_instant"))
    per_instant = (locations.withColumn("_rank", F.row_number().over(
                       Window.partitionBy("city_id", "collected_at_utc").orderBy("ingestion_id", "latitude", "longitude")))
                   .where("_rank = 1")
                   .select("city_id", F.col("collected_at_utc").alias("_instant"),
                           F.struct("latitude", "longitude").alias("_seen")))
    stream = registry.crossJoin(instants).join(per_instant, ["city_id", "_instant"], "left")
    by_time = Window.partitionBy("city_id").orderBy("_instant")
    stream = stream.withColumn("_coords", F.last("_seen", ignorenulls=True).over(
        by_time.rowsBetween(Window.unboundedPreceding, Window.currentRow)))
    stream = stream.withColumn("latitude", F.col("_coords.latitude")).withColumn("longitude", F.col("_coords.longitude"))
    for city_id, effective, changes in sorted(GOLD_CITY_REGISTRY_CHANGES, key=lambda item: (item[1], item[0])):
        if set(changes) - set(GOLD_CITY_TRACKED_ATTRIBUTES) - {"hemisphere"}:
            raise ValueError("GOLD_CITY_REGISTRY_CHANGES may only change tracked attributes")
        applies = (F.col("city_id") == F.lit(city_id)) & (
            F.col("_instant") >= gold_timestamp_literal(effective))
        for column, value in changes.items():
            stream = stream.withColumn(column, F.when(applies, F.lit(value)).otherwise(F.col(column)))
    same = reduce(lambda left, right: left & right, (
        F.col(name).eqNullSafe(F.lag(name).over(by_time)) for name in GOLD_CITY_TRACKED_ATTRIBUTES))
    starts = (stream.withColumn("_start", (F.row_number().over(by_time) == 1) | ~same)
              .where(F.col("_start")))
    versions = Window.partitionBy("city_id").orderBy("_instant")
    first = locations.groupBy("city_id").agg(F.min("collected_at_utc").alias("first_collected_at_utc"))
    history = (starts.withColumn("valid_from_utc", F.col("_instant"))
               .withColumn("valid_to_utc", F.lead("_instant").over(versions))
               .withColumn("is_current", F.col("valid_to_utc").isNull())
               .withColumn("city_sk", _gold_city_sk_column(F.col("city_id"), F.col("valid_from_utc")))
               .join(first, "city_id", "left"))
    return _gold_dimension_project(history, "dim_city")


def _gold_dim_city(spark, inputs):
    """Full-history dim_city from Silver inputs (reference build)."""
    collections = inputs[PUBLICATIONS].select("collected_at_utc")
    return gold_city_history(spark, _gold_city_locations(inputs), collections)


def gold_resolve_city_sk(frame, dim_city, instant, table):
    """Attach the dim_city version valid at ``instant`` (valid_from <= t < valid_to)."""
    versions = F.broadcast(dim_city.select(
        F.col("city_id").alias("_city_id"), F.col("city_sk").alias("_city_sk"),
        F.col("valid_from_utc").alias("_valid_from"), F.col("valid_to_utc").alias("_valid_to")))
    at = F.col(instant)
    condition = ((F.col("city_id") == F.col("_city_id")) & (F.col("_valid_from") <= at)
                 & (F.col("_valid_to").isNull() | (at < F.col("_valid_to"))))
    base = frame.drop("city_sk") if "city_sk" in frame.columns else frame
    resolved = base.join(versions, condition, "left").withColumn("city_sk", F.col("_city_sk"))
    return _gold_dimension_project(resolved, table)


def gold_canonical_observations(raw):
    """One canonical observation per (city, hour_utc), without city_sk resolution."""
    candidates = (raw.withColumn("hour_utc", F.date_trunc("hour", F.col("observed_at_utc") + F.expr("INTERVAL 30 MINUTES")))
                  .withColumn("distance_to_hour_min", F.abs(F.unix_micros("observed_at_utc")
                      - F.unix_micros("hour_utc")) / F.lit(60_000_000.0)))
    order = Window.partitionBy("city_id", "hour_utc").orderBy(
        F.col("distance_to_hour_min"), F.col("collected_at_utc"),
        F.col("observed_at_utc"), F.col("ingestion_id"), F.col("source_sha256"))
    kept = candidates.withColumn("_rank", F.row_number().over(order)).where("_rank = 1")
    local = F.from_utc_timestamp(F.col("hour_utc"), F.col("source_timezone"))
    kept = kept.withColumn("local_date", F.to_date(local)).withColumn("local_hour", F.hour(local))
    if "city_sk" not in kept.columns:
        kept = kept.withColumn("city_sk", F.lit(None).cast("string"))
    return _gold_dimension_project(kept, "fact_observation_hourly")


def _gold_observations(inputs, dim_city=None):
    observations = gold_canonical_observations(inputs[OBSERVATION_TABLE])
    if dim_city is None:
        return observations
    return gold_resolve_city_sk(observations, dim_city, "collected_at_utc", "fact_observation_hourly")


def gold_reference_dates(inputs, observations):
    """Dates referenced by collections, canonical observations and forecast targets."""
    dates = inputs[PUBLICATIONS].select(F.to_date("collected_at_utc").alias("date"))
    dates = dates.unionByName(observations.select(F.col("local_date").alias("date")))
    for name in (FORECAST_TABLE, FORECAST_DAILY_TABLE):
        dates = dates.unionByName(inputs[name].select(F.col("target_local_date").alias("date")))
    return dates


def gold_calendar(dates):
    """Contiguous calendar between the first and last referenced dates."""
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


def _gold_dim_date(inputs, observations):
    return gold_calendar(gold_reference_dates(inputs, observations))


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


def gold_condition_counts(inputs):
    """SCD 1 control: label counts per (code, is_day, label) for these ingestions."""
    conditions = inputs[OBSERVATION_TABLE].select("condition_code", "is_day", "condition_text")
    conditions = conditions.unionByName(inputs[FORECAST_TABLE].select("condition_code", "is_day", "condition_text"))
    conditions = conditions.unionByName(inputs[FORECAST_DAILY_TABLE].select(
        "condition_code", F.lit(True).alias("is_day"), "condition_text"))
    counts = (conditions.where("condition_code IS NOT NULL AND is_day IS NOT NULL")
              .groupBy("condition_code", "is_day", F.col("condition_text").alias("label"))
              .agg(F.count("*").alias("n")))
    return _gold_project_control(counts, "condition_label_counts")


def gold_add_condition_counts(old, new):
    """Absolute totals of old+new counts (idempotent when rewritten by MERGE)."""
    total = old.unionByName(new).groupBy("condition_code", "is_day", "label").agg(F.sum("n").alias("n"))
    return _gold_project_control(total, "condition_label_counts")


def gold_dim_condition_from_counts(counts):
    """Most frequent label, lexical tie-break (NULL label last), and its category."""
    order = Window.partitionBy("condition_code", "is_day").orderBy(F.col("n").desc(), F.col("label").asc_nulls_last())
    dimension = (counts.withColumn("_rank", F.row_number().over(order)).where("_rank = 1")
                  .select("condition_code", "is_day", F.col("label").alias("label_fr")))
    category = F.lit("unknown")
    for name, codes in _GOLD_CONDITION_GROUPS.items():
        category = F.when(F.col("condition_code").isin(*codes), F.lit(name)).otherwise(category)
    dimension = dimension.withColumn("category", category)
    dimension = dimension.withColumn("is_precipitation",
        F.when(F.col("category") == "unknown", F.lit(None).cast("boolean"))
         .otherwise(F.col("category").isin("rain", "snow", "sleet")
                    | (F.col("category").eqNullSafe("thunder") & (F.col("condition_code") != 1087))))
    return _gold_dimension_project(dimension, "dim_condition")


def _gold_dim_condition(inputs):
    return gold_dim_condition_from_counts(gold_condition_counts(inputs))


def _gold_dim_collection(publications):
    frame = publications.select("*", F.from_json("summary_json", _GOLD_SUMMARY_SCHEMA).alias("_summary"))
    frame = (frame.withColumn("collected_date", F.to_date("collected_at_utc"))
              .withColumn("publication_status", F.col("status"))
              .withColumn("cities_requested", F.size(F.coalesce("_summary.cities.requested", F.array().cast("array<string>"))))
              .withColumn("cities_successful", F.size(F.coalesce("_summary.cities.collection_successful", F.array().cast("array<string>")))))
    return _gold_dimension_project(frame, "dim_collection")


def _gold_collection_city(spark, inputs, dim_city=None):
    pubs = inputs[PUBLICATIONS].select("ingestion_id", "collected_at_utc",
                                       F.from_json("summary_json", _GOLD_SUMMARY_SCHEMA).alias("_summary"))
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
    if dim_city is None:
        return _gold_dimension_project(frame.withColumn("city_sk", F.lit(None).cast("string")), "fact_collection_city")
    # An untrusted (NULL) collection instant resolves no version: city_sk stays NULL.
    return gold_resolve_city_sk(frame, dim_city, "collected_at_utc", "fact_collection_city")


def build_gold_dimensions(spark, inputs, controls=None):
    """Build four dimensions, collection-city coverage and canonical observations.

    The caller fixes the UTC Spark session zone and supplies complete-publication
    inputs. No table is written and no processing-time value affects the result.
    ``controls`` may carry precomputed city_locations and condition_label_counts.
    """
    controls = controls or gold_controls(inputs)
    dim_city = gold_city_history(spark, controls["city_locations"], inputs[PUBLICATIONS].select("collected_at_utc"))
    observations = _gold_observations(inputs, dim_city)
    return {
        "dim_city": dim_city,
        "dim_date": _gold_dim_date(inputs, observations),
        "dim_condition": gold_dim_condition_from_counts(controls["condition_label_counts"]),
        "dim_collection": _gold_dim_collection(inputs[PUBLICATIONS]),
        "fact_collection_city": _gold_collection_city(spark, inputs, dim_city),
        "fact_observation_hourly": observations,
    }


def gold_controls(inputs):
    return {"city_locations": _gold_city_locations(inputs),
            "condition_label_counts": gold_condition_counts(inputs)}
