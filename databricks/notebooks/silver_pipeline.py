# Databricks notebook source
# MAGIC %md
# MAGIC # Silver WeatherAPI : pipeline Spark
# MAGIC
# MAGIC Schémas Spark, écriture Delta insert-only, découverte Bronze, lecture Gold et
# MAGIC fraîcheur. Chargé par `%run` depuis les notebooks du job.

# COMMAND ----------

# MAGIC %run ./silver_rules

# COMMAND ----------

# MAGIC %md
# MAGIC ## Schémas et chemins
# MAGIC
# MAGIC Spark schemas derived from the single Arrow contract, plus control tables.

# COMMAND ----------

from dataclasses import dataclass
import re

import pyarrow as pa
from pyspark.sql import types as T


PUBLICATIONS = "publications"
QUARANTINE = "quarantine"

_ARROW_TO_SPARK = (
    (pa.types.is_int16, T.ShortType()),
    (pa.types.is_int32, T.IntegerType()),
    (pa.types.is_int64, T.LongType()),
    (pa.types.is_float64, T.DoubleType()),
    (pa.types.is_boolean, T.BooleanType()),
    (pa.types.is_date32, T.DateType()),
    (pa.types.is_string, T.StringType()),
    (pa.types.is_large_string, T.StringType()),
)


def spark_type(arrow_type):
    if pa.types.is_timestamp(arrow_type):
        # The contract only uses UTC instants in microseconds; Spark TIMESTAMP is an instant.
        if arrow_type.unit != "us" or arrow_type.tz != "UTC":
            raise ValueError("Only timestamp(us, UTC) is supported")
        return T.TimestampType()
    for predicate, result in _ARROW_TO_SPARK:
        if predicate(arrow_type):
            return result
    raise ValueError(f"Unsupported Arrow type {arrow_type}")


def spark_schema(arrow_schema):
    return T.StructType([
        T.StructField(f.name, spark_type(f.type), nullable=f.nullable) for f in arrow_schema
    ])


PUBLICATION_SCHEMA = T.StructType([
    T.StructField("ingestion_id", T.StringType(), False),
    T.StructField("complete", T.BooleanType(), False),
    T.StructField("engine", T.StringType(), False),
    T.StructField("schema_version", T.ShortType(), False),
    T.StructField("transform_version", T.StringType(), False),
    T.StructField("source", T.StringType(), False),
    T.StructField("source_blob", T.StringType(), False),
    T.StructField("source_sha256", T.StringType(), False),
    T.StructField("collected_at_utc", T.TimestampType(), True),
    T.StructField("published_at_utc", T.TimestampType(), False),
    T.StructField("status", T.StringType(), False),
    T.StructField("summary_json", T.StringType(), False),
    T.StructField("observation_version", T.LongType(), False),
    T.StructField("observation_row_count", T.IntegerType(), False),
    T.StructField("forecast_version", T.LongType(), False),
    T.StructField("forecast_row_count", T.IntegerType(), False),
    T.StructField("quarantine_count", T.IntegerType(), False),
    T.StructField("quarantine_sha256", T.StringType(), False),
])

QUARANTINE_SCHEMA = T.StructType([
    T.StructField("ingestion_id", T.StringType(), False),
    T.StructField("rejection_index", T.IntegerType(), False),
    T.StructField("source_blob", T.StringType(), False),
    T.StructField("source_sha256", T.StringType(), False),
    T.StructField("table_name", T.StringType(), False),
    T.StructField("code", T.StringType(), False),
    T.StructField("city_query", T.StringType(), True),
    T.StructField("position", T.StringType(), True),
    T.StructField("field", T.StringType(), True),
    T.StructField("message", T.StringType(), False),
])

SPARK_SCHEMAS = {
    OBSERVATION_TABLE: spark_schema(TABLE_SCHEMAS[OBSERVATION_TABLE]),
    FORECAST_TABLE: spark_schema(TABLE_SCHEMAS[FORECAST_TABLE]),
    PUBLICATIONS: PUBLICATION_SCHEMA,
    QUARANTINE: QUARANTINE_SCHEMA,
}
KEYS = {**TABLE_KEYS, PUBLICATIONS: ("ingestion_id",), QUARANTINE: ("ingestion_id", "rejection_index")}

# Insert-only tables. Deletion vectors stay off so delta-rs readers keep working.
TABLE_PROPERTIES = {
    "delta.appendOnly": "true",
    "delta.enableDeletionVectors": "false",
}

_SAFE_ROOT = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:/{1,3}[^`'\s]+$|^/[^`'\s]+$")


@dataclass(frozen=True)
class SilverPaths:
    """One Silver root; control tables live under _control. Versions are row columns."""
    root: str

    def __post_init__(self):
        root = self.root.rstrip("/")
        if not _SAFE_ROOT.fullmatch(root) or "/../" in f"{root}/":
            raise ValueError("Silver root must be an absolute path or URI without quotes")
        object.__setattr__(self, "root", root)

    def table(self, name):
        if name in (OBSERVATION_TABLE, FORECAST_TABLE):
            return f"{self.root}/{name}"
        if name in (PUBLICATIONS, QUARANTINE):
            return f"{self.root}/_control/{name}"
        raise ValueError("Unknown Silver table")


def ddl(schema):
    names = {
        T.ShortType: "SMALLINT", T.IntegerType: "INT", T.LongType: "BIGINT",
        T.DoubleType: "DOUBLE", T.BooleanType: "BOOLEAN", T.DateType: "DATE",
        T.StringType: "STRING", T.TimestampType: "TIMESTAMP",
    }
    return ", ".join(
        f"`{f.name}` {names[type(f.dataType)]}{'' if f.nullable else ' NOT NULL'}" for f in schema
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Écriture et publication
# MAGIC
# MAGIC Publish Bronze ingestions into Silver Delta tables with Spark.
# MAGIC
# MAGIC Guarantees, without any lease or checkpoint:
# MAGIC - one job run at a time (Databricks ``max_concurrent_runs = 1``) plus Delta's
# MAGIC   optimistic concurrency; a conflicting commit fails the run, which is replayed;
# MAGIC - insert-only ``MERGE`` on the contract keys, after proving that rows already
# MAGIC   present for these ingestions are identical to the expected ones;
# MAGIC - the publication row is written last, so a reader filtering on publications
# MAGIC   never sees an ingestion that is missing from one of the two tables.
# MAGIC
# MAGIC A run stopped between two writes is repaired by the next run: rows already
# MAGIC written compare equal, nothing is inserted twice, and the publication follows.

# COMMAND ----------

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import uuid

from pyspark.sql import functions as F



ENGINE = "spark"


class SnapshotConflict(ValueError):
    """Existing immutable Silver rows differ from what this Bronze produces."""


@dataclass(frozen=True)
class Source:
    source_blob: str
    raw: bytes

    @property
    def ingestion_id(self):
        return bronze_identity(self.source_blob)

    @property
    def sha256(self):
        return hashlib.sha256(self.raw).hexdigest()


@dataclass
class BatchResult:
    published: list = field(default_factory=list)
    already_processed: list = field(default_factory=list)
    statuses: dict = field(default_factory=dict)
    inserted: dict = field(default_factory=dict)
    versions: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "published": len(self.published), "already_processed": len(self.already_processed),
            "statuses": dict(sorted(self.statuses.items())), "inserted": self.inserted,
            "versions": self.versions,
        }


def _delta(path):
    return f"delta.`{path}`"


def ensure_tables(spark, paths):
    """Create the four tables with explicit schemas; never infer or evolve them."""
    properties = ", ".join(f"'{k}' = '{v}'" for k, v in TABLE_PROPERTIES.items())
    for name, schema in SPARK_SCHEMAS.items():
        spark.sql(
            f"CREATE TABLE IF NOT EXISTS {_delta(paths.table(name))} ({ddl(schema)}) "
            f"USING DELTA TBLPROPERTIES ({properties})"
        )
        actual = spark.read.format("delta").load(paths.table(name)).schema
        if [(f.name, f.dataType) for f in actual] != [(f.name, f.dataType) for f in schema]:
            raise SnapshotConflict(f"Existing Delta schema differs from contract: {name}")


def _version(spark, path):
    return int(spark.sql(f"DESCRIBE HISTORY {_delta(path)} LIMIT 1").collect()[0]["version"])


def _frame(spark, name, rows):
    schema = SPARK_SCHEMAS[name]
    return spark.createDataFrame([tuple(r[f.name] for f in schema) for r in rows], schema)


def _existing(spark, path, ingestion_ids, columns):
    return (spark.read.format("delta").load(path)
            .where(F.col("ingestion_id").isin(sorted(ingestion_ids))).select(*columns))


def merge_insert_only(spark, paths, name, rows, ingestion_ids):
    """Insert missing rows for these ingestions; refuse any differing existing row.

    Returns (inserted, version). ``exceptAll`` compares whole rows with null-safe
    equality, so a changed metric, a foreign key or a duplicate is a conflict.
    """
    path, keys = paths.table(name), KEYS[name]
    columns = [f.name for f in SPARK_SCHEMAS[name]]
    expected = _frame(spark, name, rows).select(*columns)
    if expected.groupBy(*keys).count().where("count > 1").limit(1).count():
        raise SnapshotConflict(f"Duplicate key in incoming rows: {name}")
    current = _existing(spark, path, ingestion_ids, columns)
    if current.exceptAll(expected).limit(1).count():
        raise SnapshotConflict(f"Existing immutable rows differ: {name}")
    missing = expected.count() - current.count()
    if missing:
        view = f"silver_source_{uuid.uuid4().hex}"
        expected.createOrReplaceTempView(view)
        try:
            condition = " AND ".join(f"target.`{k}` = source.`{k}`" for k in keys)
            spark.sql(
                f"MERGE INTO {_delta(path)} AS target USING {view} AS source "
                f"ON {condition} WHEN NOT MATCHED THEN INSERT *"
            )
        finally:
            spark.catalog.dropTempView(view)
        # A committed MERGE does not prove uniqueness: compare both directions.
        written = _existing(spark, path, ingestion_ids, columns)
        if written.exceptAll(expected).limit(1).count() or expected.exceptAll(written).limit(1).count():
            raise SnapshotConflict(f"Committed rows failed verification: {name}")
    return missing, _version(spark, path)


def published(spark, paths, ingestion_ids):
    """Existing publications for these IDs: {ingestion_id: (source_blob, source_sha256)}."""
    if not ingestion_ids:
        return {}
    rows = (spark.read.format("delta").load(paths.table(PUBLICATIONS))
            .where(F.col("ingestion_id").isin(sorted(ingestion_ids)))
            .select("ingestion_id", "source_blob", "source_sha256").collect())
    return {r["ingestion_id"]: (r["source_blob"], r["source_sha256"]) for r in rows}


def _quarantine_rows(result, ingestion_id):
    rows = []
    for index, record in enumerate(quarantine_records(result, ingestion_id)):
        rows.append({
            "ingestion_id": ingestion_id, "rejection_index": index,
            "source_blob": record["source_blob"], "source_sha256": record["source_sha256"],
            "table_name": record["table"], "code": record["code"],
            "city_query": record["city_query"], "position": record["position"],
            "field": record["field"], "message": record["message"],
        })
    return rows


def _quarantine_sha256(result, ingestion_id):
    # Byte-identical to the S3 JSONL object, so both engines can be compared.
    payload = b"".join(json_bytes(r) + b"\n" for r in quarantine_records(result, ingestion_id))
    return hashlib.sha256(payload).hexdigest()


def process_sources(spark, paths, sources, *, transform=transform_bronze, now=None):
    """Transform and publish a batch of Bronze sources in one commit per table."""
    batch = BatchResult()
    by_id = {}
    for source in sources:
        if source.ingestion_id in by_id:
            raise PublicationConflict("Two Bronze sources share an ingestion identity")
        by_id[source.ingestion_id] = source
    previous = published(spark, paths, set(by_id))
    pending = []
    for ingestion_id, source in sorted(by_id.items()):
        if ingestion_id in previous:
            if previous[ingestion_id] != (source.source_blob, source.sha256):
                raise PublicationConflict("Published Bronze identity or hash differs")
            batch.already_processed.append(ingestion_id)
            continue
        result = transform(source.raw, source.source_blob)
        validate_transform_result(result, ingestion_id=ingestion_id,
                                  source_blob=source.source_blob, source_sha256=source.sha256)
        pending.append((ingestion_id, source, result))
    if not pending:
        return batch

    ids = {ingestion_id for ingestion_id, _, _ in pending}
    commits = {}
    for name in (OBSERVATION_TABLE, FORECAST_TABLE):
        rows = [row for _, _, result in pending for row in result.tables[name].to_pylist()]
        commits[name] = merge_insert_only(spark, paths, name, rows, ids)
    quarantine = [row for ingestion_id, _, result in pending for row in _quarantine_rows(result, ingestion_id)]
    commits[QUARANTINE] = merge_insert_only(spark, paths, QUARANTINE, quarantine, ids)

    published_at = now or datetime.now(timezone.utc)
    publications = []
    for ingestion_id, source, result in pending:
        observations, forecasts = result.tables[OBSERVATION_TABLE], result.tables[FORECAST_TABLE]
        publications.append({
            "ingestion_id": ingestion_id, "complete": True, "engine": ENGINE,
            "schema_version": SCHEMA_VERSION, "transform_version": TRANSFORM_VERSION,
            "source": SOURCE, "source_blob": source.source_blob, "source_sha256": source.sha256,
            "collected_at_utc": result.collected_at_utc, "published_at_utc": published_at,
            "status": result.status, "summary_json": json_bytes(result.summary).decode("utf-8"),
            "observation_version": commits[OBSERVATION_TABLE][1],
            "observation_row_count": observations.num_rows,
            "forecast_version": commits[FORECAST_TABLE][1],
            "forecast_row_count": forecasts.num_rows,
            "quarantine_count": len(result.rejections),
            "quarantine_sha256": _quarantine_sha256(result, ingestion_id),
        })
        batch.statuses[result.status] = batch.statuses.get(result.status, 0) + 1
    # Publications are not compared row by row (published_at differs on a replay):
    # only missing IDs are inserted, then identity and hash are checked.
    missing = [p for p in publications if p["ingestion_id"] not in published(spark, paths, ids)]
    if missing:
        commits[PUBLICATIONS] = merge_insert_only(
            spark, paths, PUBLICATIONS, missing, {p["ingestion_id"] for p in missing})
    after = published(spark, paths, ids)
    for ingestion_id, source, _ in pending:
        if after.get(ingestion_id) != (source.source_blob, source.sha256):
            raise PublicationConflict("Publication failed verification")
    batch.published = sorted(ids)
    batch.inserted = {name: commit[0] for name, commit in commits.items()}
    batch.versions = {name: commit[1] for name, commit in commits.items()}
    return batch

# COMMAND ----------

# MAGIC %md
# MAGIC ## Découverte Bronze
# MAGIC
# MAGIC List canonical Bronze archives and keep those without a publication.
# MAGIC
# MAGIC The publications table is the only progress record: no cursor, no checkpoint.
# MAGIC Listing selects only file paths, so Bronze contents are read for pending files only.

# COMMAND ----------

from dataclasses import dataclass
from datetime import timedelta, timezone

from pyspark.sql import functions as F



CANONICAL = r"(weather/weatherapi/\d{4}/\d{2}/\d{2}/\d{2}/((\d{8}_\d{6}(_[0-9a-f]{32})?))_weather_data\.json)$"


@dataclass(frozen=True)
class Discovery:
    pending: list
    pending_total: int
    listed: int
    ignored: int
    scan: str = "full"

    def to_dict(self):
        return {"scan": self.scan, "listed": self.listed, "ignored_non_canonical": self.ignored,
                "pending_total": self.pending_total, "selected": len(self.pending)}


def bronze_location(bronze_root, days=None):
    """Whole Bronze prefix, or only the UTC day partitions given (a brace glob)."""
    base = f"{bronze_root.rstrip('/')}/{BRONZE_PATH_PREFIX}"
    if not days:
        return base
    return base + "/{" + ",".join(f"{d:%Y/%m/%d}" for d in sorted(set(days))) + "}"


def recent_days(now, lookback_days):
    """The last ``lookback_days`` UTC dates, today included."""
    today = now.astimezone(timezone.utc).date()
    return [today - timedelta(days=offset) for offset in range(lookback_days)]


def list_bronze(spark, bronze_root, days=None):
    files = (spark.read.format("binaryFile")
             .option("recursiveFileLookup", "true")
             .option("pathGlobFilter", "*_weather_data.json")
             .load(bronze_location(bronze_root, days))
             .select("path"))
    return (files
            .withColumn("source_blob", F.regexp_extract("path", CANONICAL, 1))
            .withColumn("ingestion_id", F.regexp_extract("path", CANONICAL, 2)))


def discover(spark, paths, bronze_root, *, limit, reconcile=False, days=None):
    """Pending canonical sources, oldest first, at most ``limit``.

    ``days`` limits the listing to those UTC day partitions (the daily run);
    ``None`` lists the whole Bronze prefix. If the partial listing fails (for
    example no partition matches), the whole prefix is listed instead.
    ``reconcile=True`` returns published ingestions too, so the engine re-reads
    them and fails visibly if a Bronze object changed under a published ID.
    """
    if days:
        try:
            return _discover(spark, paths, bronze_root, limit=limit, reconcile=reconcile, days=days)
        except Exception as error:  # noqa: BLE001 - fall back to the safe, complete scan
            _log("SILVER_DISCOVERY_FALLBACK", reason=type(error).__name__)
    return _discover(spark, paths, bronze_root, limit=limit, reconcile=reconcile, days=None)


def _discover(spark, paths, bronze_root, *, limit, reconcile, days):
    listing = list_bronze(spark, bronze_root, days)
    canonical = listing.where(F.col("source_blob") != "")
    if not reconcile:
        done = spark.read.format("delta").load(paths.table(PUBLICATIONS)).select("ingestion_id")
        canonical = canonical.join(done, "ingestion_id", "left_anti")
    counts = listing.agg(
        F.count(F.lit(1)).alias("listed"),
        F.sum(F.when(F.col("source_blob") == "", 1).otherwise(0)).alias("ignored"),
    ).collect()[0]
    pending_total = canonical.count()
    selected = canonical.orderBy("source_blob").limit(limit).select("path", "source_blob").collect()
    return Discovery([(r["path"], r["source_blob"]) for r in selected], pending_total,
                     int(counts["listed"] or 0), int(counts["ignored"] or 0),
                     "full" if not days else f"recent:{len(set(days))}d")


def load_sources(spark, selected):
    """Read the exact bytes of the selected Bronze files (binaryFile keeps them intact)."""
    if not selected:
        return []
    relative = dict(selected)
    rows = spark.read.format("binaryFile").load(list(relative)).select("path", "content").collect()
    sources = [Source(relative[r["path"]], bytes(r["content"])) for r in rows]
    if len(sources) != len(relative):
        raise RuntimeError("Some selected Bronze files could not be read")
    return sources

# COMMAND ----------

# MAGIC %md
# MAGIC ## Lecture Gold
# MAGIC
# MAGIC Gold read barrier on the Spark publications table (same rule as S3).
# MAGIC
# MAGIC Freeze the complete publications, read each Silver table at the maximum
# MAGIC published version and filter on these ingestion IDs. Insert-only tables mean
# MAGIC the maximum version contains every earlier publication. Never glob Parquet.

# COMMAND ----------

from pyspark.sql import functions as F




def published_read_plan(spark, paths, *, collected_before=None):
    publications = spark.read.format("delta").load(paths.table(PUBLICATIONS)).where("complete")
    if collected_before is not None:
        publications = publications.where(F.col("collected_at_utc") < F.lit(collected_before))
    rows = publications.select(
        "ingestion_id", "schema_version", "transform_version",
        "observation_version", "forecast_version").collect()
    ids = set()
    for row in rows:
        if row["schema_version"] != SCHEMA_VERSION or row["transform_version"] != TRANSFORM_VERSION:
            raise PublicationConflict("Gold cannot mix transformation versions")
        if row["ingestion_id"] in ids:
            raise PublicationConflict("Duplicate publication identity")
        ids.add(row["ingestion_id"])
    versions = {
        OBSERVATION_TABLE: max((r["observation_version"] for r in rows), default=None),
        FORECAST_TABLE: max((r["forecast_version"] for r in rows), default=None),
    }
    return {"ingestion_ids": sorted(ids), "tables": {
        name: {"path": paths.table(name), "version": version}
        for name, version in versions.items() if version is not None
    }}


def read_published(spark, plan, table_name):
    spec = plan["tables"][table_name]
    return (spark.read.format("delta").option("versionAsOf", spec["version"]).load(spec["path"])
            .where(F.col("ingestion_id").isin(plan["ingestion_ids"])))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Exécution et fraîcheur
# MAGIC
# MAGIC Functions called by the job notebooks `silver_publish` and `silver_freshness`.
# MAGIC
# MAGIC Only fixed counters and status names are printed: no Bronze contents, no secrets.
# MAGIC A non-zero exit fails the task, so the job retries and notifies.

# COMMAND ----------

from datetime import datetime, timedelta, timezone
import json

from pyspark.sql import functions as F



def _log(event, **values):
    print(json.dumps({"event": event, **values}, sort_keys=True, default=str), flush=True)


def run(spark, *, bronze_root, silver_root, batch_size=200, max_batches=50, reconcile=False,
        lookback_days=3, full_scan_weekday=6, now=None):
    """Publish pending Bronze ingestions.

    The daily run lists only the last ``lookback_days`` UTC partitions, which is
    what drives the storage listing cost. The whole Bronze prefix is listed on
    the first run (no publication yet), on ``full_scan_weekday`` (0 = Monday,
    6 = Sunday) to catch late archives, with ``reconcile``, or when
    ``lookback_days`` is 0.
    """
    paths = SilverPaths(silver_root)
    ensure_tables(spark, paths)
    now = now or datetime.now(timezone.utc)
    first_run = not spark.read.format("delta").load(paths.table(PUBLICATIONS)).limit(1).count()
    full = reconcile or first_run or lookback_days <= 0 or now.weekday() == full_scan_weekday
    days = None if full else recent_days(now, lookback_days)
    found = discover(spark, paths, bronze_root, limit=batch_size * max_batches, reconcile=reconcile, days=days)
    _log("SILVER_DISCOVERY", **found.to_dict())
    totals = {"published": 0, "already_processed": 0, "batches": 0, "statuses": {}}
    for start in range(0, len(found.pending), batch_size):
        batch = process_sources(spark, paths, load_sources(spark, found.pending[start:start + batch_size]))
        totals["batches"] += 1
        totals["published"] += len(batch.published)
        totals["already_processed"] += len(batch.already_processed)
        for status, count in batch.statuses.items():
            totals["statuses"][status] = totals["statuses"].get(status, 0) + count
        _log("SILVER_BATCH", **batch.to_dict())
    # Not an error: the next run continues from the publications table.
    totals["remaining"] = found.pending_total - len(found.pending)
    _log("SILVER_RUN", **totals)
    return totals


def freshness(spark, *, silver_root, max_age_hours=26, min_daily=20, now=None):
    """Fail when the newest publication is too old or the last 24 h are too thin."""
    now = now or datetime.now(timezone.utc)
    publications = spark.read.format("delta").load(SilverPaths(silver_root).table(PUBLICATIONS))
    row = publications.agg(
        F.max("collected_at_utc").alias("latest"),
        F.sum(F.when(F.col("collected_at_utc") >= F.lit(now - timedelta(hours=24)), 1).otherwise(0)).alias("last_24h"),
        F.sum(F.when((F.col("collected_at_utc") >= F.lit(now - timedelta(hours=24)))
                     & F.col("status").isin("rejected", "empty"), 1).otherwise(0)).alias("unusable_24h"),
    ).collect()[0]
    latest = row["latest"]
    if latest is not None and latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    report = {
        "latest_collected_at_utc": latest.isoformat() if latest else None,
        "publications_last_24h": int(row["last_24h"] or 0),
        "unusable_last_24h": int(row["unusable_24h"] or 0),
        "max_age_hours": max_age_hours, "min_daily": min_daily,
    }
    report["fresh"] = bool(latest and now - latest <= timedelta(hours=max_age_hours)
                           and report["publications_last_24h"] >= min_daily)
    _log("SILVER_FRESHNESS", **report)
    return report

