"""Real Delta tables written by Spark, compared with the S3 delta-rs runner.

These tests need the Delta Lake jars (resolved from Maven in CI). They prove:
parity with S3 on real-derived Bronze, replay without commit, repair after an
interruption between tables, refusal of changed rows or Bronze, the Gold barrier,
and that delta-rs 1.2.1 can still read what Spark wrote.
"""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pyarrow as pa
import pytest
from deltalake import DeltaTable

from shared.silver_checks import PublicationConflict
from shared.silver_contract import FORECAST_TABLE, OBSERVATION_TABLE, TABLE_KEYS, TABLE_SCHEMAS
from shared.silver_delta import DeltaSnapshotWriter
from shared.silver_publication import publication_name
from shared.silver_runner import SilverRunner
from shared.silver_transform import transform_bronze
from silver_spark import engine as engine_module
from silver_spark.engine import SnapshotConflict, Source, ensure_tables, process_sources
from silver_spark.gold import published_read_plan, read_published
from silver_spark.job import freshness, run
from silver_spark.schemas import PUBLICATIONS, QUARANTINE, SilverPaths

from conftest import real_sources, write_bronze

INVALID_BLOB = "weather/weatherapi/2026/10/19/08/20261019_080000_weather_data.json"


def _rows(path, name):
    table = DeltaTable(path).to_pyarrow_table().cast(TABLE_SCHEMAS[name])
    return sorted(table.to_pylist(), key=lambda r: tuple(str(r[k]) for k in TABLE_KEYS[name]))


def _versions(spark, paths):
    return {name: spark.sql(f"DESCRIBE HISTORY delta.`{paths.table(name)}`").count()
            for name in (OBSERVATION_TABLE, FORECAST_TABLE, PUBLICATIONS, QUARANTINE)}


def _s3_publish(root, sources):
    """Reference: the merged S3 runner with in-memory controls and a no-op lease."""
    controls = {}
    store = SimpleNamespace(read=controls.get, create=lambda k, v: controls.setdefault(k, v))
    class Lease:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def assert_held(self):
            pass

    publications = {}
    for blob, raw in sources:
        container = SimpleNamespace(get_blob_client=lambda _b, raw=raw: SimpleNamespace(
            download_blob=lambda: SimpleNamespace(readall=lambda: raw)))
        runner = SilverRunner(container, store, DeltaSnapshotWriter(root), Lease, transform_bronze)
        result = runner.process_ingestion(blob)
        publications[result.ingestion_id] = result.publication
    return publications, controls


@pytest.fixture
def silver(delta_spark, tmp_path):
    paths = SilverPaths(f"file://{tmp_path}/silver")
    ensure_tables(delta_spark, paths)
    return paths


def _sources(items):
    return [Source(blob, raw) for blob, raw in items]


def test_spark_matches_the_s3_runner_row_for_row(delta_spark, silver, tmp_path):
    items = real_sources() + [(INVALID_BLOB, b"{not json")]
    batch = process_sources(delta_spark, silver, _sources(items))
    assert sorted(batch.statuses.items()) == [("partial", 2), ("rejected", 1)]

    reference, controls = _s3_publish(str(tmp_path / "s3"), items)
    for name in (OBSERVATION_TABLE, FORECAST_TABLE):
        assert _rows(silver.table(name), name) == _rows(str(tmp_path / "s3" / name), name)

    published = {r["ingestion_id"]: r for r in delta_spark.read.format("delta")
                 .load(silver.table(PUBLICATIONS)).collect()}
    assert set(published) == set(reference)
    for ingestion_id, expected in reference.items():
        row = published[ingestion_id]
        assert row["status"] == expected["status"]
        assert json.loads(row["summary_json"]) == expected["summary"]
        assert row["source_sha256"] == expected["source_sha256"]
        assert row["quarantine_count"] == expected["quarantine"]["count"]
        assert row["quarantine_sha256"] == expected["quarantine"]["sha256"]
        assert row["observation_row_count"] == expected["tables"][OBSERVATION_TABLE]["row_count"]
        assert row["forecast_row_count"] == expected["tables"][FORECAST_TABLE]["row_count"]
        assert controls[publication_name(ingestion_id)]


def test_real_fixtures_keep_the_known_london_rejection(delta_spark, silver):
    process_sources(delta_spark, silver, _sources(real_sources()))
    codes = {r["code"] for r in delta_spark.read.format("delta").load(silver.table(QUARANTINE)).collect()}
    assert "location_mismatch" in codes
    observations = delta_spark.read.format("delta").load(silver.table(OBSERVATION_TABLE))
    assert {r["city_id"] for r in observations.select("city_id").distinct().collect()} == {"fr_paris", "be_brussels"}


def test_replay_publishes_nothing_and_creates_no_commit(delta_spark, silver):
    process_sources(delta_spark, silver, _sources(real_sources()))
    before = _versions(delta_spark, silver)
    again = process_sources(delta_spark, silver, _sources(real_sources()))
    assert again.published == [] and len(again.already_processed) == 2
    assert _versions(delta_spark, silver) == before


def test_interruption_between_tables_is_repaired_without_duplicates(delta_spark, silver, monkeypatch):
    original = engine_module.merge_insert_only

    def fail_on_forecasts(spark, paths, name, rows, ids):
        if name == FORECAST_TABLE:
            raise RuntimeError("simulated crash after the observations commit")
        return original(spark, paths, name, rows, ids)

    monkeypatch.setattr(engine_module, "merge_insert_only", fail_on_forecasts)
    with pytest.raises(RuntimeError):
        process_sources(delta_spark, silver, _sources(real_sources()))
    assert delta_spark.read.format("delta").load(silver.table(PUBLICATIONS)).count() == 0
    observations_after_crash = delta_spark.read.format("delta").load(silver.table(OBSERVATION_TABLE)).count()
    assert observations_after_crash > 0

    monkeypatch.setattr(engine_module, "merge_insert_only", original)
    batch = process_sources(delta_spark, silver, _sources(real_sources()))
    assert len(batch.published) == 2
    assert batch.inserted[OBSERVATION_TABLE] == 0  # already there, compared equal
    assert delta_spark.read.format("delta").load(silver.table(OBSERVATION_TABLE)).count() == observations_after_crash


def test_a_differing_existing_row_is_refused(delta_spark, silver):
    blob, raw = real_sources()[0]
    rows = transform_bronze(raw, blob).tables[OBSERVATION_TABLE].to_pylist()
    rows[0]["temperature_c"] += 1.0
    delta_spark.createDataFrame(
        [tuple(r[f.name] for f in engine_module.SPARK_SCHEMAS[OBSERVATION_TABLE]) for r in rows[:1]],
        engine_module.SPARK_SCHEMAS[OBSERVATION_TABLE],
    ).write.format("delta").mode("append").save(silver.table(OBSERVATION_TABLE))
    with pytest.raises(SnapshotConflict):
        process_sources(delta_spark, silver, _sources([(blob, raw)]))


def test_changed_bronze_under_a_published_id_is_visible(delta_spark, silver):
    blob, raw = real_sources()[0]
    process_sources(delta_spark, silver, _sources([(blob, raw)]))
    with pytest.raises(PublicationConflict):
        process_sources(delta_spark, silver, _sources([(blob, raw + b" ")]))


def test_gold_reads_only_complete_publications_at_frozen_versions(delta_spark, silver):
    first, second = real_sources()
    process_sources(delta_spark, silver, _sources([first]))
    plan = published_read_plan(delta_spark, silver)
    process_sources(delta_spark, silver, _sources([second]))
    frozen = read_published(delta_spark, plan, FORECAST_TABLE)
    assert {r["ingestion_id"] for r in frozen.select("ingestion_id").distinct().collect()} == set(plan["ingestion_ids"])
    assert len(published_read_plan(delta_spark, silver)["ingestion_ids"]) == 2


def test_job_run_discovers_publishes_then_is_idempotent(delta_spark, tmp_path):
    bronze = tmp_path / "bronze"
    for blob, raw in real_sources():
        write_bronze(bronze, raw, blob)
    root = f"file://{tmp_path}/silver"
    first = run(delta_spark, bronze_root=f"file://{bronze}", silver_root=root, batch_size=1)
    assert first["published"] == 2 and first["batches"] == 2 and first["remaining"] == 0
    second = run(delta_spark, bronze_root=f"file://{bronze}", silver_root=root)
    assert second["published"] == 0 and second["batches"] == 0
    reconcile = run(delta_spark, bronze_root=f"file://{bronze}", silver_root=root, reconcile=True)
    assert reconcile["already_processed"] == 2 and reconcile["published"] == 0


def test_freshness_fails_on_old_or_thin_publications(delta_spark, silver):
    process_sources(delta_spark, silver, _sources(real_sources()))
    collected = datetime(2026, 10, 18, 15, 0, tzinfo=timezone.utc)
    ok = freshness(delta_spark, silver_root=silver.root, min_daily=2, now=collected)
    assert ok["fresh"] is True
    stale = freshness(delta_spark, silver_root=silver.root, min_daily=2, now=collected + timedelta(days=3))
    assert stale["fresh"] is False


def test_spark_tables_stay_readable_by_delta_rs(delta_spark, silver):
    process_sources(delta_spark, silver, _sources(real_sources()))
    for name in (OBSERVATION_TABLE, FORECAST_TABLE):
        table = DeltaTable(silver.table(name))
        assert table.to_pyarrow_table().cast(TABLE_SCHEMAS[name]).num_rows > 0
        assert table.metadata().configuration.get("delta.appendOnly") == "true"
    assert isinstance(DeltaTable(silver.table(PUBLICATIONS)).to_pyarrow_table(), pa.Table)
