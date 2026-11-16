# Databricks notebook source
# MAGIC %md
# MAGIC # Gold WeatherAPI : construction et publication
# MAGIC Lecture Silver figée, transformations distribuées, dix tables Delta et manifeste
# MAGIC publié en dernier. `mode=full` recalcule et remplace tout (référence) ;
# MAGIC `mode=incremental` applique des MERGE limités aux nouvelles ingestions et à
# MAGIC leur fenêtre affectée, avec bascule automatique en full si l'historique
# MAGIC SCD 2 des villes change avant des faits déjà publiés.

# COMMAND ----------
# MAGIC %run ./silver_pipeline
# COMMAND ----------
# MAGIC %run ./gold_contract
# COMMAND ----------
# MAGIC %run ./gold_dimensions
# COMMAND ----------
# MAGIC %run ./gold_forecasts
# COMMAND ----------

from functools import reduce
from datetime import date
from zoneinfo import ZoneInfo
from time import monotonic as _gold_monotonic  # never shadow datetime.time of Silver

from delta.tables import DeltaTable


GOLD_BUILD_SCHEMA = T.StructType([
    T.StructField("build_id", T.StringType(), False),
    T.StructField("build_sequence", T.LongType(), False),
    T.StructField("gold_version", T.StringType(), False),
    T.StructField("completed_at_utc", T.TimestampType(), False),
    T.StructField("silver_root", T.StringType(), False),
    T.StructField("load_mode", T.StringType(), False),
    *[T.StructField(name, T.StringType(), False) for name in (
        "source_ids_json", "source_fingerprints_json", "silver_versions_json",
        "table_versions_json", "control_versions_json", "row_counts_json", "quality_json")],
])


@dataclass(frozen=True)
class GoldPaths:
    root: str

    def __post_init__(self):
        object.__setattr__(self, "root", SilverPaths(self.root).root)

    def table(self, name):
        if name in GOLD_SCHEMAS:
            return f"{self.root}/{name}"
        if name in GOLD_CONTROL_SCHEMAS:
            return f"{self.root}/_control/{name}"
        if name == "builds":
            return f"{self.root}/_control/builds"
        raise ValueError("Unknown Gold table")


@dataclass(frozen=True)
class GoldMerge:
    """MERGE strategy. insert: insert absent keys only, never update.
    upsert: update changed rows, insert absent keys and, when ``delete_condition``
    is given, delete target rows of that condition absent from the source."""
    kind: str
    delete_condition: str = ""


def latest_gold_build(spark, paths):
    if not DeltaTable.isDeltaTable(spark, paths.table("builds")):
        return None
    rows = (spark.read.format("delta").load(paths.table("builds"))
            .orderBy(F.desc("build_sequence")).limit(1).collect())
    return rows[0].asDict() if rows else None


def gold_read_plan(spark, gold_root):
    """Freeze a complete Gold generation, including after an interrupted rebuild."""
    paths = GoldPaths(gold_root)
    build = latest_gold_build(spark, paths)
    if build is None:
        raise ValueError("No complete Gold build")
    return {"build_id": build["build_id"], "gold_version": build["gold_version"],
            "tables": {name: {"path": paths.table(name), "version": version}
                       for name, version in json.loads(build["table_versions_json"]).items()},
            "controls": {name: {"path": paths.table(name), "version": version}
                         for name, version in json.loads(build["control_versions_json"]).items()}}


def read_gold(spark, plan, name):
    spec = plan["tables"][name] if name in plan["tables"] else plan["controls"][name]
    return spark.read.format("delta").option("versionAsOf", spec["version"]).load(spec["path"])


def _gold_dates(start_date, end_date):
    def parse(value):
        if not value:
            return None
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("Dates must use YYYY-MM-DD")
        return datetime.combine(date.fromisoformat(value), datetime.min.time(), timezone.utc)
    start, end = parse(start_date), parse(end_date)
    if start and end and start >= end:
        raise ValueError("start_date must precede exclusive end_date")
    return start, end


def gold_full_rebuild_weekday(value):
    """None/"" disables the weekly rebuild; otherwise 0 = Monday ... 6 = Sunday (UTC)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError("full_rebuild_weekday must be 0..6 or empty")
    try:
        weekday = int(str(value).strip())
    except ValueError:
        raise ValueError("full_rebuild_weekday must be 0..6 or empty") from None
    if not 0 <= weekday <= 6:
        raise ValueError("full_rebuild_weekday must be 0..6 or empty")
    return weekday


def gold_effective_mode(mode, full_rebuild_weekday, now):
    """The weekly safety net forces a full rebuild on its UTC weekday."""
    if mode not in ("incremental", "full"):
        raise ValueError("mode must be incremental or full")
    weekday = gold_full_rebuild_weekday(full_rebuild_weekday)
    if mode == "incremental" and weekday is not None and now.astimezone(timezone.utc).weekday() == weekday:
        return "full", "weekly_full_rebuild"
    return mode, "requested"


def _gold_sources(spark, silver_root, previous, start, end):
    paths = SilverPaths(silver_root)
    # Freeze control tables as well as business tables: do not mix live summaries
    # with an earlier set of published business versions.
    controls = {name: _version(spark, paths.table(name)) for name in (PUBLICATIONS, QUARANTINE)}
    pubs = (spark.read.format("delta").option("versionAsOf", controls[PUBLICATIONS])
            .load(paths.table(PUBLICATIONS)).where("complete"))
    selected = pubs
    if start:
        selected = selected.where(F.col("collected_at_utc") >= F.lit(start))
    if end:
        selected = selected.where(F.col("collected_at_utc") < F.lit(end))
    old_ids = set(json.loads(previous["source_ids_json"])) if previous else set()
    new_ids = {r[0] for r in selected.select("ingestion_id").collect()} - old_ids
    ids = old_ids | new_ids
    pubs = pubs.where(F.col("ingestion_id").isin(sorted(ids)))
    rows = pubs.select("ingestion_id", "source_blob", "source_sha256", "schema_version",
                       "transform_version", *PUBLICATION_VERSION_COLUMNS.values()).collect()
    if len(rows) != len(ids) or len({r["ingestion_id"] for r in rows}) != len(rows):
        raise PublicationConflict("Missing or duplicate source publication")
    fingerprints = {}
    for row in rows:
        if row["schema_version"] != SCHEMA_VERSION or row["transform_version"] != TRANSFORM_VERSION:
            raise PublicationConflict("Gold requires the current Silver transformation version")
        fingerprints[row["ingestion_id"]] = [row["source_blob"], row["source_sha256"]]
    if previous:
        old_fingerprints = json.loads(previous["source_fingerprints_json"])
        if any(fingerprints.get(key) != value for key, value in old_fingerprints.items()):
            raise PublicationConflict("Previously consumed Silver identity changed")
    versions = {name: max((r[PUBLICATION_VERSION_COLUMNS[name]] for r in rows), default=0)
                for name in SILVER_DATA_TABLES}
    return {"paths": paths, "publications": pubs, "ids": sorted(ids), "new_ids": sorted(new_ids),
            "fingerprints": fingerprints, "versions": {**versions, **controls}}


def _gold_silver_frames(spark, source, ids):
    """Pinned Silver frames restricted to ``ids`` (all consumed, or only the new ones)."""
    ids = sorted(ids)
    paths, versions = source["paths"], source["versions"]
    frames = {name: (spark.read.format("delta").option("versionAsOf", versions[name])
                     .load(paths.table(name)).where(F.col("ingestion_id").isin(ids)))
              for name in (*SILVER_DATA_TABLES, QUARANTINE)}
    frames[PUBLICATIONS] = source["publications"].where(F.col("ingestion_id").isin(ids))
    return frames


def _gold_inputs(spark, silver_root, previous, start, end):
    source = _gold_sources(spark, silver_root, previous, start, end)
    frames = _gold_silver_frames(spark, source, source["ids"])
    return frames, source["ids"], source["fingerprints"], source["versions"], len(source["new_ids"])


def _gold_schema(name):
    return GOLD_SCHEMAS[name] if name in GOLD_SCHEMAS else GOLD_CONTROL_SCHEMAS[name]


def _gold_keys(name):
    return GOLD_TABLE_KEYS[name] if name in GOLD_TABLE_KEYS else GOLD_CONTROL_KEYS[name]


def _gold_merge(spark, path, name, frame, merge):
    if merge.kind not in ("insert", "upsert"):
        raise ValueError("Unknown Gold MERGE strategy")
    schema, keys = _gold_schema(name), _gold_keys(name)
    if not merge.delete_condition and not frame.limit(1).count():
        return  # Nothing to insert or update: no Delta commit.
    # Null-safe equality only where the contract allows a NULL key (a NULL label).
    on = " AND ".join(
        f"target.`{k}` {'<=>' if schema[k].nullable else '='} source.`{k}`" for k in keys)
    clauses = []
    if merge.kind == "upsert":
        values = [f.name for f in schema if f.name not in keys]
        if values:
            differs = " OR ".join(f"NOT (target.`{c}` <=> source.`{c}`)" for c in values)
            clauses.append(f"WHEN MATCHED AND ({differs}) THEN UPDATE SET *")
    clauses.append("WHEN NOT MATCHED THEN INSERT *")
    if merge.delete_condition:
        if merge.kind != "upsert":
            raise ValueError("Insert-only Gold tables never delete")
        clauses.append(f"WHEN NOT MATCHED BY SOURCE AND ({merge.delete_condition}) THEN DELETE")
    view = f"gold_source_{uuid.uuid4().hex}"
    frame.createOrReplaceTempView(view)
    try:
        spark.sql(f"MERGE INTO {_delta(path)} AS target USING {view} AS source ON {on} "
                  + " ".join(clauses))
    finally:
        spark.catalog.dropTempView(view)


def _write_gold_table(spark, paths, name, frame, merge=None):
    """Overwrite (merge=None, full build) or MERGE one Gold or control table."""
    schema = _gold_schema(name)
    if set(frame.columns) != set(schema.fieldNames()):
        raise ValueError(f"Gold columns differ from contract: {name}")
    frame = frame.select(*[F.col(f.name).cast(f.dataType).alias(f.name) for f in schema])
    path = paths.table(name)
    exists = DeltaTable.isDeltaTable(spark, path)
    if exists:
        existing = spark.read.format("delta").load(path).schema
        # Required values are checked by validate_gold. Spark expressions may
        # infer nullable=True, so compare physical names/types here, not flags.
        expected_fields = [(f.name, f.dataType) for f in schema]
        if [(f.name, f.dataType) for f in existing] != expected_fields:
            raise ValueError(f"Gold stored schema differs from contract: {name}")
    if merge is not None:
        if not exists:
            raise ValueError(f"Incremental Gold MERGE needs an existing table: {name}")
        # Clustering and the CHECK constraint were set by the full build that
        # created the table; MERGE keeps them and Delta enforces the CHECK.
        _gold_merge(spark, path, name, frame, merge)
        return _version(spark, path)
    # Each Delta table is atomic. Cross-table visibility is provided by builds,
    # not by the physical paths or direct Unity Catalog table reads.
    (frame.write.format("delta").mode("overwrite")
     .option("delta.enableDeletionVectors", "false").save(path))
    if name in GOLD_CONTROL_SCHEMAS:
        return _version(spark, path)
    # Spark's generic writer.clusterBy is silently ignored for Delta path writes
    # in OSS Delta 4. Use Delta's SQL operation and check persisted metadata.
    detail = spark.sql(f"DESCRIBE DETAIL {_delta(path)}").first()
    if list(detail["clusteringColumns"]) != list(GOLD_CLUSTER_COLUMNS[name]):
        columns = ", ".join(f"`{column}`" for column in GOLD_CLUSTER_COLUMNS[name])
        spark.sql(f"ALTER TABLE {_delta(path)} CLUSTER BY ({columns})")
    if name == "fact_forecast_daily":
        # Delta persists CHECK constraints and enforces them on subsequent
        # writes. The metadata commit belongs to this build's pinned version.
        properties = spark.sql(f"DESCRIBE DETAIL {_delta(path)}").first()["properties"]
        expression = "lead_days BETWEEN -1 AND 2"
        previous = properties.get("delta.constraints.lead_days_range")
        if previous is None:
            spark.sql(f"ALTER TABLE {_delta(path)} ADD CONSTRAINT lead_days_range "
                      f"CHECK ({expression})")
        elif re.sub(r"\s+", "", previous).lower() != re.sub(r"\s+", "", expression).lower():
            raise ValueError("Gold daily lead_days_range constraint differs from contract")
    return _version(spark, path)


# --- Affected window ------------------------------------------------------------

_GOLD_TS_FORMAT = "yyyy-MM-dd HH:mm:ss.SSSSSS"


def _gold_interval(delta):
    return F.expr(f"INTERVAL {int(delta.total_seconds())} SECONDS")


def gold_affected_windows(new_inputs):
    """Per city, the UTC instants whose Gold rows the NEW ingestions can change.

    [min(collected_at) - 1 day, max(collected_at) + 3 days], widened to every
    canonical observation hour, hourly target and daily local-day span carried
    by these ingestions, so no new row can fall outside its window.
    """
    obs = new_inputs[OBSERVATION_TABLE].select(
        "city_id", F.col("collected_at_utc").alias("_c"),
        F.date_trunc("hour", F.col("observed_at_utc") + F.expr("INTERVAL 30 MINUTES")).alias("_t"))
    hourly = new_inputs[FORECAST_TABLE].select(
        "city_id", F.col("collected_at_utc").alias("_c"), F.col("target_at_utc").alias("_t"))
    daily = new_inputs[FORECAST_DAILY_TABLE]
    day_start = F.to_utc_timestamp(F.col("target_local_date").cast("timestamp"), F.col("source_timezone"))
    day_last = F.to_utc_timestamp(F.date_add("target_local_date", 1).cast("timestamp"),
                                  F.col("source_timezone")) - F.expr("INTERVAL 1 HOUR")
    daily = daily.select("city_id", F.col("collected_at_utc").alias("_c"),
                         F.explode(F.array(day_start, day_last)).alias("_t"))
    events = obs.unionByName(hourly).unionByName(daily)
    return events.groupBy("city_id").agg(
        F.least(F.min("_c") - _gold_interval(GOLD_WINDOW_BEFORE), F.min("_t")).alias("window_start_utc"),
        F.greatest(F.max("_c") + _gold_interval(GOLD_WINDOW_AFTER), F.max("_t")).alias("window_end_utc"))


def gold_window_specs(windows):
    """Collect windows as UTC text (independent of the Python time zone)."""
    rows = windows.select("city_id", F.date_format("window_start_utc", _GOLD_TS_FORMAT).alias("start"),
                          F.date_format("window_end_utc", _GOLD_TS_FORMAT).alias("end")).collect()
    specs = []
    for row in sorted(rows, key=lambda r: r["city_id"]):
        start = datetime.strptime(row["start"], "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
        end = datetime.strptime(row["end"], "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
        zone = ZoneInfo(CITY_BY_ID[row["city_id"]].timezone)
        specs.append({"city_id": row["city_id"], "start": row["start"], "end": row["end"],
                      # Local dates whose day intersects the window (daily errors).
                      "first_local_date": start.astimezone(zone).date().isoformat(),
                      "last_local_date": end.astimezone(zone).date().isoformat()})
    return specs


def _gold_windows_frame(spark, specs):
    frame = spark.createDataFrame(
        [(s["city_id"], s["start"], s["end"], s["first_local_date"], s["last_local_date"]) for s in specs],
        "city_id string, s string, e string, d1 string, d2 string")
    return F.broadcast(frame.select(
        "city_id", F.to_timestamp("s").alias("_window_start"), F.to_timestamp("e").alias("_window_end"),
        F.to_date("d1").alias("_window_first_date"), F.to_date("d2").alias("_window_last_date")))


def _gold_sql_text(value):
    if not re.fullmatch(r"[A-Za-z0-9_:. -]+", value):
        raise ValueError("Unexpected Gold window literal")
    return value


def gold_window_condition(specs, column, kind="timestamp", alias="target"):
    """SQL predicate on target rows of the windows (MERGE NOT MATCHED BY SOURCE)."""
    parts = []
    for spec in specs:
        if kind == "timestamp":
            low, high = (f"TIMESTAMP'{_gold_sql_text(spec[k])}'" for k in ("start", "end"))
        else:
            low, high = (f"DATE'{_gold_sql_text(spec[k])}'" for k in ("first_local_date", "last_local_date"))
        parts.append(f"({alias}.`city_id` = '{_gold_sql_text(spec['city_id'])}' "
                     f"AND {alias}.`{column}` BETWEEN {low} AND {high})")
    return " OR ".join(parts) if parts else "false"


def _gold_in_window(frame, windows, column, kind="timestamp", margin=None):
    low, high = (("_window_start", "_window_end") if kind == "timestamp"
                 else ("_window_first_date", "_window_last_date"))
    low_value, high_value = F.col(low), F.col(high)
    if margin is not None:
        low_value, high_value = low_value - _gold_interval(margin), high_value + _gold_interval(margin)
    return (frame.join(windows, "city_id")
            .where(F.col(column).between(low_value, high_value))
            .drop("_window_start", "_window_end", "_window_first_date", "_window_last_date"))


# --- SCD 2 compatibility ------------------------------------------------------------

def gold_city_history_compatible(before, after, collections):
    """True if every fact instant already published keeps its city_sk.

    Facts of consumed ingestions have instants <= the latest consumed collection.
    Versions starting at or before it must be identical (same sk and start); later
    versions may be appended and the current one closed. Otherwise a late
    collection or a registry change rewrote history: rebuild in full.
    """
    origin = gold_timestamp_literal(GOLD_CITY_VALID_FROM_ORIGIN)
    limit = collections.agg(F.coalesce(F.max("collected_at_utc"), origin).alias("_limit"))

    def boundaries(dim):
        return (dim.crossJoin(limit).where(F.col("valid_from_utc") <= F.col("_limit"))
                .select("city_id", "city_sk", "valid_from_utc"))
    old, new = boundaries(before), boundaries(after)
    return not (old.exceptAll(new).limit(1).count() or new.exceptAll(old).limit(1).count())


# --- Validation -----------------------------------------------------------------

def _gold_validate_city_history(tables):
    cities = tables["dim_city"]
    registry = {city.city_id for city in CITIES}
    present = {row[0] for row in cities.select("city_id").distinct().collect()}
    if present != registry:
        raise ValueError("Gold dim_city must contain exactly the registry cities")
    current = cities.groupBy("city_id").agg(F.sum(F.col("is_current").cast("int")).alias("n"))
    if current.where("n != 1").limit(1).count():
        raise ValueError("Gold dim_city requires exactly one current row per city")
    order = Window.partitionBy("city_id").orderBy("valid_from_utc")
    origin = gold_timestamp_literal(GOLD_CITY_VALID_FROM_ORIGIN)
    checked = (cities.withColumn("_next", F.lead("valid_from_utc").over(order))
               .withColumn("_position", F.row_number().over(order)))
    bad = (~F.col("valid_to_utc").eqNullSafe(F.col("_next"))
           | (F.col("is_current") != F.col("valid_to_utc").isNull())
           | ((F.col("_position") == 1) & (F.col("valid_from_utc") != origin))
           | (F.col("valid_to_utc").isNotNull() & (F.col("valid_to_utc") <= F.col("valid_from_utc")))
           | (F.col("city_sk") != _gold_city_sk_column(F.col("city_id"), F.col("valid_from_utc"))))
    if checked.where(bad).limit(1).count():
        raise ValueError("Gold dim_city validity must be contiguous, non-overlapping and deterministic")
    versions = cities.select(F.col("city_sk").alias("_sk"), F.col("city_id").alias("_city"),
                             F.col("valid_from_utc").alias("_from"), F.col("valid_to_utc").alias("_to"))
    instants = tables["dim_collection"].select("ingestion_id", "collected_at_utc")
    for name, column in GOLD_CITY_SK_INSTANTS.items():
        facts = tables[name]
        if name == "fact_collection_city":
            facts = facts.join(instants, "ingestion_id", "left")
        at = F.col(column)
        resolved = facts.join(versions, F.col("city_sk") == F.col("_sk"), "left")
        wrong = (F.when(at.isNull(), F.col("city_sk").isNotNull())
                 .otherwise(F.col("_sk").isNull() | (F.col("_city") != F.col("city_id"))
                            | (F.col("_from") > at) | (F.col("_to").isNotNull() & (at >= F.col("_to")))))
        if resolved.where(wrong).limit(1).count():
            raise ValueError(f"Gold city_sk is not the version valid at the collection instant: {name}")


def validate_gold(spark, tables):
    """Blocking schema, identity and reference checks over materialised candidates."""
    if set(tables) != set(GOLD_SCHEMAS):
        raise ValueError("Gold transform did not produce all ten tables")
    counts = {}
    for name, schema in GOLD_SCHEMAS.items():
        frame = tables[name]
        if frame.schema.fieldNames() != schema.fieldNames() or any(
                frame.schema[field.name].dataType != field.dataType for field in schema):
            raise ValueError(f"Gold schema differs from contract: {name}")
        required = [F.col(f.name).isNull() for f in schema if not f.nullable]
        bad = reduce(lambda a, b: a | b, required, F.lit(False))
        stats = frame.agg(F.count("*").alias("n"),
                          F.sum(F.when(bad, 1).otherwise(0)).alias("bad")).first()
        if stats["bad"]:
            raise ValueError(f"Gold required value missing: {name}")
        if frame.groupBy(*GOLD_TABLE_KEYS[name]).count().where("count > 1").limit(1).count():
            raise ValueError(f"Gold duplicate key: {name}")
        counts[name] = stats["n"]
    if tables["dim_city"].groupBy("city_id", "valid_from_utc").count().where("count > 1").limit(1).count():
        raise ValueError("Gold duplicate key: dim_city (city_id, valid_from_utc)")

    for child, relations in GOLD_FOREIGN_KEYS.items():
        for child_columns, parent, parent_columns in relations:
            # Optional code/day and optional collection date can be incomplete.
            # In particular daily code=NULL/day=true is an absent condition, not
            # a fabricated FK to an unknown-weather dimension row.
            present = reduce(lambda a, b: a & b,
                             (F.col(column).isNotNull() for column in child_columns), F.lit(True))
            references = tables[child].where(present).select(*child_columns)
            targets = tables[parent].select(*[
                F.col(source).alias(target) for source, target in zip(parent_columns, child_columns)
            ])
            if references.join(targets, list(child_columns), "left_anti").limit(1).count():
                raise ValueError(f"Gold unresolved reference: {child} -> {parent}")
    _gold_validate_city_history(tables)

    for child, parent in (("fact_forecast_hourly_error", "fact_forecast_hourly"),
                          ("fact_forecast_daily_error", "fact_forecast_daily")):
        # The natural forecast key must identify the same source ingestion as
        # its error row, even though ingestion_id is not itself part of that key.
        identity = [*GOLD_TABLE_KEYS[parent], "ingestion_id", "city_sk"]
        if tables[child].select(*identity).join(
                tables[parent].select(*identity), identity, "left_anti").limit(1).count():
            raise ValueError(f"Gold error references another forecast ingestion: {child}")
    observation_identity = ["city_id", "target_hour_utc", "observation_ingestion_id"]
    observation_targets = tables["fact_observation_hourly"].select(
        "city_id", F.col("hour_utc").alias("target_hour_utc"),
        F.col("ingestion_id").alias("observation_ingestion_id"))
    if tables["fact_forecast_hourly_error"].select(*observation_identity).join(
            observation_targets, observation_identity, "left_anti").limit(1).count():
        raise ValueError("Gold hourly error references another canonical observation ingestion")

    collection_city = tables["fact_collection_city"]
    if collection_city.where(~F.col("collection_status").isin(*GOLD_COLLECTION_STATUSES)).limit(1).count():
        raise ValueError("Gold unknown collection status")
    counters = ["observation_valid", "forecast_hourly_valid", "forecast_hourly_expected",
                "forecast_daily_valid", "rejected_rows", "nulled_metrics"]
    negative = reduce(lambda a, b: a | b, (F.col(name) < 0 for name in counters), F.lit(False))
    if collection_city.where(negative).limit(1).count():
        raise ValueError("Gold negative collection count")
    if tables["fact_forecast_daily"].where(~F.col("lead_days").between(-1, 2)).limit(1).count():
        raise ValueError("Gold daily lead_days outside [-1,2]")
    daily_error_parent = tables["fact_forecast_daily_error"].select(
        *GOLD_TABLE_KEYS["fact_forecast_daily"]).join(
        tables["fact_forecast_daily"].select(*GOLD_TABLE_KEYS["fact_forecast_daily"], "lead_days"),
        list(GOLD_TABLE_KEYS["fact_forecast_daily"]), "inner")
    if daily_error_parent.where("lead_days < 0").limit(1).count():
        raise ValueError("Gold daily error uses a past forecast")
    daily_errors = tables["fact_forecast_daily_error"]
    if daily_errors.where(~F.col("expected_hours").isin(23, 24, 25)
                          | (F.col("observed_hours") != F.col("expected_hours"))).limit(1).count():
        raise ValueError("Gold daily error requires complete local-day observations")
    if tables["fact_forecast_hourly"].where("lead_time_hours <= 0").limit(1).count():
        raise ValueError("Gold hourly forecast must be future")
    return counts


def gold_quality(tables):
    """Coverage within each city's available span; daily temperature-order alerts."""
    coverage = tables["fact_observation_hourly"].groupBy("city_id").agg(
        F.min("hour_utc").alias("first_hour"), F.max("hour_utc").alias("last_hour"),
        F.count("*").alias("observed_hours"))
    coverage = coverage.withColumn("expected_hours",
        ((F.col("last_hour").cast("long") - F.col("first_hour").cast("long")) / 3600 + 1).cast("long"))
    coverage = coverage.withColumn("coverage_pct", F.col("observed_hours") * 100.0 / F.col("expected_hours"))
    rows = [r.asDict() for r in coverage.orderBy("city_id").collect()]
    for row in rows:
        for key in ("first_hour", "last_hour"):
            row[key] = row[key].isoformat()
    daily = tables["fact_forecast_daily"]
    bad = ((F.col("temperature_min_c") > F.col("temperature_avg_c"))
           | (F.col("temperature_avg_c") > F.col("temperature_max_c"))
           | (F.col("temperature_min_c") > F.col("temperature_max_c")))
    return {"observation_coverage": rows, "daily_temperature_order_alerts": daily.where(bad).count()}


def _enforce_gold_coverage(quality, minimum):
    coverage = quality["observation_coverage"]
    if minimum > 0 and not coverage:
        raise ValueError("Gold observation coverage unavailable: no observations")
    if minimum > 0 and set(GOLD_COVERAGE_REQUIRED_CITIES) - {row["city_id"] for row in coverage}:
        raise ValueError("Gold observation coverage unavailable for a required city")
    if any(row["coverage_pct"] < minimum for row in coverage):
        raise ValueError("Gold observation coverage below configured minimum")


def _gold_full(spark, paths, source):
    """Reference build: recompute everything from all consumed ingestions, overwrite."""
    inputs = _gold_silver_frames(spark, source, source["ids"])
    controls = gold_controls(inputs)
    frames = build_gold_dimensions(spark, inputs, controls)
    frames.update(build_gold_forecasts(spark, inputs, frames))
    if set(frames) != set(GOLD_SCHEMAS):
        raise ValueError("Gold transform did not produce all ten tables")
    control_versions = {name: _write_gold_table(spark, paths, name, controls[name])
                        for name in GOLD_CONTROL_SCHEMAS}
    versions = {name: _write_gold_table(spark, paths, name, frames[name]) for name in GOLD_SCHEMAS}
    return versions, control_versions, []


def _gold_pinned(spark, paths, name, version):
    return spark.read.format("delta").option("versionAsOf", version).load(paths.table(name))


def _gold_table_version(spark, paths, name):
    return _version(spark, paths.table(name))


def _gold_current(spark, paths, name):
    return spark.read.format("delta").load(paths.table(name))


def _gold_append_build(spark, paths, record):
    (spark.createDataFrame([record], GOLD_BUILD_SCHEMA).write.format("delta").mode("append")
     .option("delta.enableDeletionVectors", "false").save(paths.table("builds")))


def _gold_incremental(spark, paths, previous, source):
    """MERGE the new ingestions; None when the SCD 2 history requires a full build."""
    new = _gold_silver_frames(spark, source, source["new_ids"])
    old_versions = json.loads(previous["table_versions_json"])
    old_control_versions = json.loads(previous["control_versions_json"])
    old = {name: _gold_pinned(spark, paths, name, v) for name, v in old_versions.items()}
    old_controls = {name: _gold_pinned(spark, paths, name, v) for name, v in old_control_versions.items()}
    current = {name: _gold_current(spark, paths, name) for name in ("dim_city", "dim_collection")}

    # dim_city SCD 2 is recomputed from the small location ledger (any arrival order).
    new_controls = gold_controls(new)
    new_collections = _gold_dim_collection(new[PUBLICATIONS])
    locations = old_controls["city_locations"].unionByName(new_controls["city_locations"])
    collections = old["dim_collection"].select("collected_at_utc").unionByName(
        new_collections.select("collected_at_utc"))
    dim_city = gold_city_history(spark, locations, collections)
    # Also against the physical tables: an interrupted attempt may have inserted
    # facts beyond the manifest with the dimension it computed then.
    if not (gold_city_history_compatible(old["dim_city"], dim_city, old["dim_collection"])
            and gold_city_history_compatible(current["dim_city"], dim_city, current["dim_collection"])):
        return None

    specs = gold_window_specs(gold_affected_windows(new))
    windows = _gold_windows_frame(spark, specs) if specs else None

    # SCD 1 label counts: absolute totals of the affected (code, is_day) keys.
    new_counts = new_controls["condition_label_counts"]
    affected = new_counts.select("condition_code", "is_day").distinct()
    totals = gold_add_condition_counts(
        old_controls["condition_label_counts"].join(affected, ["condition_code", "is_day"], "left_semi"),
        new_counts)

    insert, upsert = GoldMerge("insert"), GoldMerge("upsert")
    control_versions = {
        "city_locations": _write_gold_table(spark, paths, "city_locations",
                                            new_controls["city_locations"], insert),
        "condition_label_counts": _write_gold_table(spark, paths, "condition_label_counts", totals, upsert),
    }
    versions = {}
    versions["dim_collection"] = _write_gold_table(spark, paths, "dim_collection", new_collections, insert)
    versions["dim_city"] = _write_gold_table(spark, paths, "dim_city", dim_city,
                                             GoldMerge("upsert", delete_condition="true"))
    versions["dim_condition"] = _write_gold_table(spark, paths, "dim_condition",
                                                  gold_dim_condition_from_counts(totals), upsert)
    versions["fact_collection_city"] = _write_gold_table(
        spark, paths, "fact_collection_city", _gold_collection_city(spark, new, dim_city), insert)
    versions["fact_forecast_hourly"] = _write_gold_table(spark, paths, "fact_forecast_hourly", gold_resolve_city_sk(
        gold_hourly_forecasts(new[FORECAST_TABLE]), dim_city, "issued_at_utc", "fact_forecast_hourly"), insert)
    versions["fact_forecast_daily"] = _write_gold_table(spark, paths, "fact_forecast_daily", gold_resolve_city_sk(
        gold_daily_forecasts(new[FORECAST_DAILY_TABLE]), dim_city, "issued_at_utc", "fact_forecast_daily"), insert)

    if windows is None:
        # The new ingestions carry no weather row: no observation or score changes.
        observations = spark.createDataFrame([], GOLD_SCHEMAS["fact_observation_hourly"])
        for name in ("fact_observation_hourly", "fact_forecast_hourly_error", "fact_forecast_daily_error"):
            versions[name] = _gold_table_version(spark, paths, name)
    else:
        # Canonical hours of the window, from Silver observations of ALL consumed
        # ingestions observed in the window plus the margin.
        all_observations = _gold_silver_frames(spark, source, source["ids"])[OBSERVATION_TABLE]
        raw = _gold_in_window(all_observations, windows, "observed_at_utc", margin=GOLD_OBSERVATION_MARGIN)
        canonical = _gold_in_window(gold_canonical_observations(raw), windows, "hour_utc")
        versions["fact_observation_hourly"] = _write_gold_table(
            spark, paths, "fact_observation_hourly",
            gold_resolve_city_sk(canonical, dim_city, "collected_at_utc", "fact_observation_hourly"),
            GoldMerge("upsert", gold_window_condition(specs, "hour_utc")))
        observations = _gold_pinned(spark, paths, "fact_observation_hourly", versions["fact_observation_hourly"])
        # Scores of every Gold forecast (old or new) whose target is in the window.
        hourly = _gold_in_window(_gold_pinned(spark, paths, "fact_forecast_hourly", versions["fact_forecast_hourly"]),
                                 windows, "target_hour_utc")
        versions["fact_forecast_hourly_error"] = _write_gold_table(
            spark, paths, "fact_forecast_hourly_error",
            gold_hourly_errors(hourly, _gold_in_window(observations, windows, "hour_utc")),
            GoldMerge("upsert", gold_window_condition(specs, "target_hour_utc")))
        daily = _gold_in_window(_gold_pinned(spark, paths, "fact_forecast_daily", versions["fact_forecast_daily"]),
                                windows, "target_local_date", kind="date")
        day_truth = _gold_in_window(observations, windows, "local_date", kind="date")
        versions["fact_forecast_daily_error"] = _write_gold_table(
            spark, paths, "fact_forecast_daily_error", gold_daily_errors(daily, day_truth, dim_city),
            GoldMerge("upsert", gold_window_condition(specs, "target_local_date", kind="date")))
        observations = _gold_in_window(observations, windows, "hour_utc")

    # Insert-only calendar: missing dates of the span old dates ∪ new references.
    dates = old["dim_date"].select("date").unionByName(gold_reference_dates(new, observations))
    versions["dim_date"] = _write_gold_table(spark, paths, "dim_date", gold_calendar(dates), insert)
    return versions, control_versions, specs


def run_gold(spark, *, silver_root, gold_root, start_date="", end_date="", mode="incremental",
             minimum_coverage_pct=0.0, full_rebuild_weekday=None, now=None):
    """Load Gold from published Silver; commit the source ledger strictly last.

    Dates select collected-at source additions [start,end). They never remove
    previously consumed IDs. ``mode=full`` recomputes and overwrites every table
    (reference); ``mode=incremental`` MERGEs only the new ingestions and their
    affected window. ``full_rebuild_weekday`` (0 = Monday ... 6 = Sunday, UTC;
    None disables) forces the full rebuild that day. A change to GOLD_VERSION
    requires a separate destination root.
    """
    if mode not in ("incremental", "full"):
        raise ValueError("mode must be incremental or full")
    minimum_coverage_pct = float(minimum_coverage_pct)
    if not 0 <= minimum_coverage_pct <= 100:
        raise ValueError("minimum_coverage_pct must be in [0,100]")
    start, end = _gold_dates(start_date, end_date)
    started = _gold_monotonic()
    current_time = now or datetime.now(timezone.utc)
    mode, reason = gold_effective_mode(mode, full_rebuild_weekday, current_time)
    spark.conf.set("spark.sql.session.timeZone", "UTC")
    paths = GoldPaths(gold_root)
    if paths.root == SilverPaths(silver_root).root or paths.root.startswith(SilverPaths(silver_root).root + "/"):
        raise ValueError("Gold root must be separate from Silver")
    previous = latest_gold_build(spark, paths)
    if previous and (previous["gold_version"] != GOLD_VERSION or previous["silver_root"] != SilverPaths(silver_root).root):
        raise PublicationConflict("Gold version or Silver source changed; use another Gold root")
    source = _gold_sources(spark, silver_root, previous, start, end)
    ids, new = source["ids"], len(source["new_ids"])
    if previous and not new and mode == "incremental":
        quality = json.loads(previous["quality_json"])
        _enforce_gold_coverage(quality, minimum_coverage_pct)
        return {"changed": False, "build_id": previous["build_id"], "load_mode": "none",
                "source_count": len(ids), "new_sources": 0,
                "row_counts": json.loads(previous["row_counts_json"]), "quality": quality,
                "duration_seconds": round(_gold_monotonic() - started, 3)}
    result = None
    if previous is None:
        mode, reason = "full", "first_build"
    elif mode == "incremental":
        result = _gold_incremental(spark, paths, previous, source)
        if result is None:
            mode, reason = "full", "city_history_changed"
    if result is None:
        result = _gold_full(spark, paths, source)
    versions, control_versions, specs = result
    candidate = {name: _gold_pinned(spark, paths, name, version) for name, version in versions.items()}
    counts = validate_gold(spark, candidate)
    quality = gold_quality(candidate)
    _enforce_gold_coverage(quality, minimum_coverage_pct)
    completed = now or datetime.now(timezone.utc)
    build_id = uuid.uuid4().hex
    record = {"build_id": build_id, "build_sequence": previous["build_sequence"] + 1 if previous else 1,
              "gold_version": GOLD_VERSION, "completed_at_utc": completed,
              "silver_root": SilverPaths(silver_root).root, "load_mode": mode,
              **{key + "_json": json.dumps(value, sort_keys=True) for key, value in (
                  ("source_ids", ids), ("source_fingerprints", source["fingerprints"]),
                  ("silver_versions", source["versions"]), ("table_versions", versions),
                  ("control_versions", control_versions), ("row_counts", counts), ("quality", quality))}}
    _gold_append_build(spark, paths, record)
    report = {"changed": True, "build_id": build_id, "load_mode": mode, "load_reason": reason,
              "source_count": len(ids), "new_sources": new, "row_counts": counts, "quality": quality,
              "windows": specs, "duration_seconds": round(_gold_monotonic() - started, 3)}
    _log("GOLD_BUILD", **{k: v for k, v in report.items() if k != "quality"})
    return report


def register_gold_tables(spark, paths, uc_schema):
    """Point ``{uc_schema}.{table}`` at each Gold path; idempotent.

    A name registered at another location (the v1 root gold/weatherapi) is
    relocated: its EXTERNAL definition is dropped, never its files, then created
    on the new path. A MANAGED table of the same name is refused.
    Returns ``{table: "created" | "relocated" | "unchanged"}``.
    """
    if not isinstance(uc_schema, str) or not _UC_SCHEMA.fullmatch(uc_schema):
        raise ValueError("uc_schema must be catalogue.schema")
    if latest_gold_build(spark, paths) is None:
        raise ValueError("Cannot register unpublished Gold")
    catalog, schema = uc_schema.split(".")
    actions = {}
    for name in GOLD_SCHEMAS:
        qualified = f"`{catalog}`.`{schema}`.`{name}`"
        location = paths.table(name)
        if spark.catalog.tableExists(qualified):
            existing = spark.sql(f"DESCRIBE DETAIL {qualified}").collect()[0]["location"]
            if _same_location(existing, location):
                actions[name] = "unchanged"
                continue
            if spark.catalog.getTable(qualified).tableType.upper() != "EXTERNAL":
                raise CatalogConflict(f"Refusing to replace a non-external Gold table: {name}")
            spark.sql(f"DROP TABLE {qualified}")
            actions[name] = "relocated"
        else:
            actions[name] = "created"
        spark.sql(f"CREATE TABLE {qualified} USING DELTA LOCATION '{location}'")
    _log("GOLD_CATALOG", uc_schema=uc_schema, **{
        action: sum(1 for a in actions.values() if a == action)
        for action in ("created", "relocated", "unchanged")})
    return actions
