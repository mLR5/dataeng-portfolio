"""Publish Bronze ingestions into Silver Delta tables with Spark.

Same guarantees as the S3 delta-rs runner, without a Blob lease:
- one job run at a time (Databricks ``max_concurrent_runs = 1``) plus Delta's
  optimistic concurrency; a conflicting commit fails the run, which is replayed;
- insert-only ``MERGE`` on the contract keys, after proving that rows already
  present for these ingestions are identical to the expected ones;
- the publication row is written last, so a reader filtering on publications
  never sees an ingestion that is missing from one of the two tables.

A run stopped between two writes is repaired by the next run: rows already
written compare equal, nothing is inserted twice, and the publication follows.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import uuid

from pyspark.sql import functions as F

from shared.silver_checks import (
    PublicationConflict, bronze_identity, json_bytes, quarantine_records,
    validate_transform_result,
)
from shared.silver_contract import (
    FORECAST_TABLE, OBSERVATION_TABLE, SCHEMA_VERSION, SOURCE, TRANSFORM_VERSION,
)
from shared.silver_transform import transform_bronze

from .schemas import KEYS, PUBLICATIONS, QUARANTINE, SPARK_SCHEMAS, TABLE_PROPERTIES, SilverPaths, ddl

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
