# Databricks notebook source
# MAGIC %md
# MAGIC # Gold WeatherAPI : construction et publication
# MAGIC Lecture Silver figée, transformations distribuées, dix tables Delta et manifeste
# MAGIC publié en dernier. L'incrémental suit les sources ; les résultats dérivés sont
# MAGIC reconstruits sur l'ensemble cumulatif pour traiter aussi les observations tardives.

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
from delta.tables import DeltaTable


GOLD_BUILD_SCHEMA = T.StructType([
    T.StructField("build_id", T.StringType(), False),
    T.StructField("build_sequence", T.LongType(), False),
    T.StructField("gold_version", T.StringType(), False),
    T.StructField("completed_at_utc", T.TimestampType(), False),
    T.StructField("silver_root", T.StringType(), False),
    *[T.StructField(name, T.StringType(), False) for name in (
        "source_ids_json", "source_fingerprints_json", "silver_versions_json",
        "table_versions_json", "row_counts_json", "quality_json")],
])


@dataclass(frozen=True)
class GoldPaths:
    root: str

    def __post_init__(self):
        object.__setattr__(self, "root", SilverPaths(self.root).root)

    def table(self, name):
        if name in GOLD_SCHEMAS:
            return f"{self.root}/{name}"
        if name == "builds":
            return f"{self.root}/_control/builds"
        if name == "agg_city_daily":
            return f"{self.root}/{name}"
        if name == "dashboard_builds":
            return f"{self.root}/_control/dashboard_builds"
        raise ValueError("Unknown Gold table")


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
                       for name, version in json.loads(build["table_versions_json"]).items()}}


def read_gold(spark, plan, name):
    spec = plan["tables"][name]
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


def _gold_inputs(spark, silver_root, previous, start, end):
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
    frames = {name: (spark.read.format("delta").option("versionAsOf", version)
                     .load(paths.table(name)).where(F.col("ingestion_id").isin(sorted(ids))))
              for name, version in versions.items()}
    frames[PUBLICATIONS] = pubs
    frames[QUARANTINE] = (spark.read.format("delta").option("versionAsOf", controls[QUARANTINE])
                          .load(paths.table(QUARANTINE))
                          .where(F.col("ingestion_id").isin(sorted(ids))))
    return frames, sorted(ids), fingerprints, {**versions, **controls}, len(new_ids)


def _write_gold_table(spark, paths, name, frame):
    schema = GOLD_SCHEMAS[name]
    if set(frame.columns) != set(schema.fieldNames()):
        raise ValueError(f"Gold columns differ from contract: {name}")
    frame = frame.select(*[F.col(f.name).cast(f.dataType).alias(f.name) for f in schema])
    path = paths.table(name)
    if DeltaTable.isDeltaTable(spark, path):
        existing = spark.read.format("delta").load(path).schema
        # Required values are checked by validate_gold. Spark expressions may
        # infer nullable=True, so compare physical names/types here, not flags.
        expected_fields = [(f.name, f.dataType) for f in schema]
        if [(f.name, f.dataType) for f in existing] != expected_fields:
            raise ValueError(f"Gold stored schema differs from contract: {name}")
    # Each Delta table is atomic. Cross-table visibility is provided by builds,
    # not by the physical paths or direct Unity Catalog table reads.
    (frame.write.format("delta").mode("overwrite")
     .option("delta.enableDeletionVectors", "false").save(path))
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

    for child, parent in (("fact_forecast_hourly_error", "fact_forecast_hourly"),
                          ("fact_forecast_daily_error", "fact_forecast_daily")):
        # The natural forecast key must identify the same source ingestion as
        # its error row, even though ingestion_id is not itself part of that key.
        identity = [*GOLD_TABLE_KEYS[parent], "ingestion_id"]
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


def run_gold(spark, *, silver_root, gold_root, start_date="", end_date="", mode="incremental",
             minimum_coverage_pct=0.0, now=None):
    """Rebuild cumulative derived results; commit the source ledger strictly last.

    Dates select collected-at source additions [start,end). They never remove
    previously consumed IDs. Full forces recomputation even without new sources.
    A change to GOLD_VERSION requires a separate destination root.
    """
    if mode not in ("incremental", "full"):
        raise ValueError("mode must be incremental or full")
    minimum_coverage_pct = float(minimum_coverage_pct)
    if not 0 <= minimum_coverage_pct <= 100:
        raise ValueError("minimum_coverage_pct must be in [0,100]")
    start, end = _gold_dates(start_date, end_date)
    spark.conf.set("spark.sql.session.timeZone", "UTC")
    paths = GoldPaths(gold_root)
    if paths.root == SilverPaths(silver_root).root or paths.root.startswith(SilverPaths(silver_root).root + "/"):
        raise ValueError("Gold root must be separate from Silver")
    previous = latest_gold_build(spark, paths)
    if previous and (previous["gold_version"] != GOLD_VERSION or previous["silver_root"] != SilverPaths(silver_root).root):
        raise PublicationConflict("Gold version or Silver source changed; use another Gold root")
    inputs, ids, fingerprints, silver_versions, new = _gold_inputs(spark, silver_root, previous, start, end)
    if previous and not new and mode == "incremental":
        quality = json.loads(previous["quality_json"])
        _enforce_gold_coverage(quality, minimum_coverage_pct)
        return {"changed": False, "build_id": previous["build_id"], "source_count": len(ids),
                "new_sources": 0, "row_counts": json.loads(previous["row_counts_json"]),
                "quality": quality}
    frames = build_gold_dimensions(spark, inputs)
    frames.update(build_gold_forecasts(spark, inputs, frames))
    if set(frames) != set(GOLD_SCHEMAS):
        raise ValueError("Gold transform did not produce all ten tables")
    versions = {name: _write_gold_table(spark, paths, name, frames[name]) for name in GOLD_SCHEMAS}
    candidate = {name: spark.read.format("delta").option("versionAsOf", version).load(paths.table(name))
                 for name, version in versions.items()}
    counts = validate_gold(spark, candidate)
    quality = gold_quality(candidate)
    _enforce_gold_coverage(quality, minimum_coverage_pct)
    completed = now or datetime.now(timezone.utc)
    build_id = uuid.uuid4().hex
    record = {"build_id": build_id, "build_sequence": previous["build_sequence"] + 1 if previous else 1,
              "gold_version": GOLD_VERSION, "completed_at_utc": completed,
              "silver_root": SilverPaths(silver_root).root,
              **{key + "_json": json.dumps(value, sort_keys=True) for key, value in (
                  ("source_ids", ids), ("source_fingerprints", fingerprints),
                  ("silver_versions", silver_versions), ("table_versions", versions), ("row_counts", counts),
                  ("quality", quality))}}
    (spark.createDataFrame([record], GOLD_BUILD_SCHEMA).write.format("delta").mode("append")
     .option("delta.enableDeletionVectors", "false").save(paths.table("builds")))
    return {"changed": True, "build_id": build_id, "source_count": len(ids),
            "new_sources": new, "row_counts": counts, "quality": quality}


def register_gold_tables(spark, paths, uc_schema):
    if not isinstance(uc_schema, str) or not _UC_SCHEMA.fullmatch(uc_schema):
        raise ValueError("uc_schema must be catalogue.schema")
    if latest_gold_build(spark, paths) is None:
        raise ValueError("Cannot register unpublished Gold")
    catalog, schema = uc_schema.split(".")
    for name in GOLD_SCHEMAS:
        qualified = f"`{catalog}`.`{schema}`.`{name}`"
        if spark.catalog.tableExists(qualified):
            location = spark.sql(f"DESCRIBE DETAIL {qualified}").first()["location"]
            if not _same_location(location, paths.table(name)):
                raise CatalogConflict(f"Gold name already points elsewhere: {name}")
        else:
            spark.sql(f"CREATE TABLE {qualified} USING DELTA LOCATION '{paths.table(name)}'")

# COMMAND ----------
# MAGIC %md
# MAGIC ## Publication des lectures du dashboard
# MAGIC Le contrat des dix tables reste indépendant. L'agrégat a son propre registre,
# MAGIC publié après validation et création des vues. Les vues sont mises à jour une
# MAGIC par une : leur DDL ne constitue pas une transaction multi-objets.
# COMMAND ----------

DASHBOARD_BUILD_SCHEMA = T.StructType([
    T.StructField("build_id", T.StringType(), False),
    T.StructField("build_sequence", T.LongType(), False),
    T.StructField("dashboard_version", T.StringType(), False),
    T.StructField("source_gold_build_id", T.StringType(), False),
    T.StructField("source_table_versions_json", T.StringType(), False),
    T.StructField("aggregate_version", T.LongType(), False),
    T.StructField("row_count", T.LongType(), False),
    T.StructField("uc_schema", T.StringType(), False),
    T.StructField("view_signature", T.StringType(), False),
    T.StructField("completed_at_utc", T.TimestampType(), False),
])


def latest_gold_dashboard_build(spark, paths):
    if not DeltaTable.isDeltaTable(spark, paths.table("dashboard_builds")):
        return None
    rows = (spark.read.format("delta").load(paths.table("dashboard_builds"))
            .orderBy(F.desc("build_sequence")).limit(1).collect())
    return rows[0].asDict() if rows else None


def gold_dashboard_read_plan(spark, gold_root):
    """Pin the last completed aggregate and the exact core Gold inputs it used."""
    paths = GoldPaths(gold_root)
    build = latest_gold_dashboard_build(spark, paths)
    if build is None:
        raise ValueError("No complete Gold dashboard build")
    return {
        "build_id": build["build_id"], "source_gold_build_id": build["source_gold_build_id"],
        "dashboard_version": build["dashboard_version"],
        "aggregate": {"path": paths.table("agg_city_daily"), "version": build["aggregate_version"]},
        "source_tables": {name: {"path": paths.table(name), "version": version}
                          for name, version in json.loads(build["source_table_versions_json"]).items()},
    }


def validate_gold_daily_aggregate(frame):
    """Reject broken identities, coverage and missing-value semantics before publish."""
    schema = AGG_CITY_DAILY_SCHEMA
    if frame.schema.fieldNames() != schema.fieldNames() or any(
            frame.schema[f.name].dataType != f.dataType for f in schema):
        raise ValueError("Gold daily aggregate schema differs from contract")
    required = reduce(lambda a, b: a | b,
                      (F.col(f.name).isNull() for f in schema if not f.nullable), F.lit(False))
    if frame.where(required).limit(1).count():
        raise ValueError("Gold daily aggregate required value missing")
    if frame.groupBy(*AGG_CITY_DAILY_KEYS).count().where("count > 1").limit(1).count():
        raise ValueError("Gold daily aggregate duplicate key")
    invalid = (~F.col("expected_hours").isin(23, 24, 25)
               | (F.col("observed_hours") < 0)
               | (F.col("observed_hours") > F.col("expected_hours"))
               | (F.abs(F.col("coverage_pct") - F.col("observed_hours") * 100.0
                        / F.col("expected_hours")) > 1e-8)
               | (F.col("is_complete") != (F.col("observed_hours") == F.col("expected_hours"))))
    for metric in ("temperature", "humidity", "wind", "wind_gust", "precipitation"):
        count = F.col(f"{metric}_observed_hours")
        invalid = invalid | (count < 0) | (count > F.col("observed_hours"))
    for count, metrics in (
            ("temperature_observed_hours", ("temperature_min_c", "temperature_max_c", "temperature_avg_c")),
            ("humidity_observed_hours", ("humidity_avg_pct",)),
            ("wind_observed_hours", ("wind_avg_kph", "wind_max_kph")),
            ("wind_gust_observed_hours", ("wind_gust_max_kph",)),
            ("precipitation_observed_hours", ("precipitation_total_mm",))):
        for metric in metrics:
            invalid = invalid | ((F.col(count) == 0) != F.col(metric).isNull())
    invalid = (invalid
               | (F.col("precipitation_is_complete") != (
                   F.col("precipitation_observed_hours") == F.col("expected_hours")))
               | (F.col("sunrise_utc").isNull() != F.col("sunset_utc").isNull())
               | (F.col("sunrise_utc").isNull() != F.col("daylight_hours").isNull())
               | (F.col("sunset_utc") < F.col("sunrise_utc"))
               | (F.col("daylight_hours") < 0) | (F.col("daylight_hours") > 25))
    if frame.where(invalid).limit(1).count():
        raise ValueError("Gold daily aggregate invalid coverage or metric values")
    return frame.count()


def _write_gold_dashboard_aggregate(spark, paths, frame):
    path = paths.table("agg_city_daily")
    if DeltaTable.isDeltaTable(spark, path):
        stored = spark.read.format("delta").load(path).schema
        if [(f.name, f.dataType) for f in stored] != [(f.name, f.dataType) for f in AGG_CITY_DAILY_SCHEMA]:
            raise ValueError("Gold daily aggregate stored schema differs from contract")
    (frame.write.format("delta").mode("overwrite")
     .option("delta.enableDeletionVectors", "false").save(path))
    detail = spark.sql(f"DESCRIBE DETAIL {_delta(path)}").first()
    if list(detail["clusteringColumns"]) != ["city_id", "local_date"]:
        spark.sql(f"ALTER TABLE {_delta(path)} CLUSTER BY (`city_id`, `local_date`)")
    return _version(spark, path)


def _dashboard_qualified(uc_schema, name):
    catalog, schema = uc_schema.split(".")
    return f"`{catalog}`.`{schema}`.`{name}`"


def _check_dashboard_catalog(spark, paths, uc_schema, queries):
    """Preflight every name before overwriting data or replacing any view."""
    complete = True
    for name in (*GOLD_SCHEMAS, "agg_city_daily"):
        qualified = _dashboard_qualified(uc_schema, name)
        if spark.catalog.tableExists(qualified):
            if spark.catalog.getTable(qualified).tableType == "VIEW":
                raise CatalogConflict(f"Gold dashboard table name is already a view: {name}")
            location = spark.sql(f"DESCRIBE DETAIL {qualified}").first()["location"]
            if not _same_location(location, paths.table(name)):
                raise CatalogConflict(f"Gold dashboard name already points elsewhere: {name}")
        elif name in GOLD_SCHEMAS:
            raise CatalogConflict("Register the complete core Gold tables before dashboard views")
        else:
            complete = False
    for name in queries:
        qualified = _dashboard_qualified(uc_schema, name)
        if spark.catalog.tableExists(qualified):
            if spark.catalog.getTable(qualified).tableType != "VIEW":
                raise CatalogConflict(f"Gold dashboard view name is already a table: {name}")
            properties = {row["key"]: row["value"] for row in spark.sql(
                f"SHOW TBLPROPERTIES {qualified}").collect()}
            if properties.get("weatherapi.dashboard") != DASHBOARD_VERSION:
                raise CatalogConflict(f"Gold dashboard refuses to replace an unowned view: {name}")
        else:
            complete = False
    return complete


def _publish_gold_dashboard_views(spark, paths, uc_schema, queries):
    aggregate = _dashboard_qualified(uc_schema, "agg_city_daily")
    if not spark.catalog.tableExists(aggregate):
        spark.sql(f"CREATE TABLE {aggregate} USING DELTA LOCATION '{paths.table('agg_city_daily')}'")
    for name, query in queries.items():
        qualified = _dashboard_qualified(uc_schema, name)
        spark.sql(f"CREATE OR REPLACE VIEW {qualified} "
                  f"TBLPROPERTIES ('weatherapi.dashboard' = '{DASHBOARD_VERSION}') AS {query}")
        # Resolve the persisted definition before publishing the completion marker.
        spark.sql(f"SELECT * FROM {qualified} LIMIT 0").collect()


def run_gold_dashboard(spark, *, gold_root, uc_schema="", now=None, force=False):
    """Publish aggregate + optional serving views from a committed core Gold build.

    Exactly one writer may publish to a Gold root. Core tables and their ledger
    are never modified here. A failed aggregate/DDL write leaves the previous
    completed dashboard read plan available; retry finishes the publication.
    View replacement is sequential, so clients needing cross-view consistency
    must coordinate with the ledger rather than assume a multi-view transaction.
    """
    if not isinstance(uc_schema, str) or (uc_schema and not _UC_SCHEMA.fullmatch(uc_schema)):
        raise ValueError("uc_schema must be catalogue.schema")
    if type(force) is not bool:
        raise ValueError("force must be a boolean")
    spark.conf.set("spark.sql.session.timeZone", "UTC")
    paths = GoldPaths(gold_root)
    source = gold_read_plan(spark, paths.root)
    if source["gold_version"] != GOLD_VERSION or set(source["tables"]) != set(GOLD_SCHEMAS):
        raise PublicationConflict("Gold dashboard requires a complete current core Gold contract")
    versions = {name: spec["version"] for name, spec in source["tables"].items()}
    queries = gold_dashboard_view_queries(uc_schema, source_versions=versions) if uc_schema else {}
    signature = hashlib.sha256(json.dumps(queries, sort_keys=True).encode()).hexdigest()
    previous = latest_gold_dashboard_build(spark, paths)
    if previous and previous["dashboard_version"] != DASHBOARD_VERSION:
        raise PublicationConflict("Dashboard version changed; use another Gold root")
    catalog_complete = not uc_schema or _check_dashboard_catalog(spark, paths, uc_schema, queries)
    aggregate_complete = (previous is not None
                          and DeltaTable.isDeltaTable(spark, paths.table("agg_city_daily"))
                          and _version(spark, paths.table("agg_city_daily")) == previous["aggregate_version"])
    if (previous and not force and previous["source_gold_build_id"] == source["build_id"]
            and previous["uc_schema"] == uc_schema and previous["view_signature"] == signature
            and catalog_complete and aggregate_complete):
        return {"changed": False, "build_id": previous["build_id"],
                "source_gold_build_id": source["build_id"], "row_count": previous["row_count"],
                "aggregate_version": previous["aggregate_version"], "views": list(queries)}
    tables = {name: read_gold(spark, source, name) for name in
              ("dim_city", "fact_observation_hourly", "fact_forecast_daily")}
    aggregate = build_gold_daily_aggregate(spark, tables)
    version = _write_gold_dashboard_aggregate(spark, paths, aggregate)
    candidate = spark.read.format("delta").option("versionAsOf", version).load(paths.table("agg_city_daily"))
    count = validate_gold_daily_aggregate(candidate)
    if uc_schema:
        _publish_gold_dashboard_views(spark, paths, uc_schema, queries)
    record = {
        "build_id": uuid.uuid4().hex,
        "build_sequence": previous["build_sequence"] + 1 if previous else 1,
        "dashboard_version": DASHBOARD_VERSION, "source_gold_build_id": source["build_id"],
        "source_table_versions_json": json.dumps(versions, sort_keys=True),
        "aggregate_version": version, "row_count": count, "uc_schema": uc_schema,
        "view_signature": signature, "completed_at_utc": now or datetime.now(timezone.utc),
    }
    (spark.createDataFrame([record], DASHBOARD_BUILD_SCHEMA).write.format("delta").mode("append")
     .option("delta.enableDeletionVectors", "false").save(paths.table("dashboard_builds")))
    return {"changed": True, "build_id": record["build_id"], "source_gold_build_id": source["build_id"],
            "row_count": count, "aggregate_version": version, "views": list(queries)}
