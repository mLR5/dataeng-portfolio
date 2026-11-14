"""Real Delta tables written by Spark from the notebook code.

These tests need the Delta Lake jars (resolved from Maven in CI). They prove:
the stored rows equal the transformer output on real-derived Bronze (three business
tables), replay without commit, repair after an interruption between any two writes,
refusal of changed rows or Bronze, the Gold barrier, the stable catalog names, and
that delta-rs 1.2.1 can still read what Spark wrote.
"""
from datetime import datetime, timedelta, timezone
import json
import uuid

import pyarrow as pa
import pytest
from deltalake import DeltaTable

import silver_pipeline as engine_module
from silver_pipeline import (
    FORECAST_DAILY_TABLE, FORECAST_TABLE, OBSERVATION_TABLE, PUBLICATIONS, QUARANTINE,
    SILVER_DATA_TABLES, SPARK_SCHEMAS, TABLE_KEYS, TABLE_SCHEMAS, PublicationConflict, SilverPaths,
    SnapshotConflict, Source, ensure_tables, freshness, process_sources, publication_controls,
    published_read_plan, read_published, register_tables, run, transform_bronze, uc_table_name,
)

from conftest import real_sources, write_bronze

INVALID_BLOB = "weather/weatherapi/2026/10/19/08/20261019_080000_weather_data.json"


def _rows(path, name):
    table = DeltaTable(path).to_pyarrow_table().cast(TABLE_SCHEMAS[name])
    return sorted(table.to_pylist(), key=lambda r: tuple(str(r[k]) for k in TABLE_KEYS[name]))


def _versions(spark, paths):
    return {name: spark.sql(f"DESCRIBE HISTORY delta.`{paths.table(name)}`").count()
            for name in SPARK_SCHEMAS}


def _count(spark, paths, name):
    return spark.read.format("delta").load(paths.table(name)).count()


def _latest_version(spark, paths, name):
    return spark.sql(f"DESCRIBE HISTORY delta.`{paths.table(name)}` LIMIT 1").collect()[0]["version"]


@pytest.fixture
def silver(delta_spark, tmp_path):
    paths = SilverPaths(f"file://{tmp_path}/silver")
    ensure_tables(delta_spark, paths)
    return paths


def _sources(items):
    return [Source(blob, raw) for blob, raw in items]


def _sorted(rows, name):
    return sorted(rows, key=lambda r: tuple(str(r[k]) for k in TABLE_KEYS[name]))


def test_delta_tables_hold_exactly_the_transformer_output(delta_spark, silver):
    items = real_sources() + [(INVALID_BLOB, b"{not json")]
    batch = process_sources(delta_spark, silver, _sources(items))
    assert sorted(batch.statuses.items()) == [("partial", 2), ("rejected", 1)]

    results = {Source(blob, raw).ingestion_id: transform_bronze(raw, blob) for blob, raw in items}
    for name in SILVER_DATA_TABLES:
        expected = [row for result in results.values() for row in result.tables[name].to_pylist()]
        assert _rows(silver.table(name), name) == _sorted(expected, name)

    published = {r["ingestion_id"]: r for r in delta_spark.read.format("delta")
                 .load(silver.table(PUBLICATIONS)).collect()}
    assert set(published) == set(results)
    quarantine = delta_spark.read.format("delta").load(silver.table(QUARANTINE)).collect()
    for ingestion_id, result in results.items():
        row = published[ingestion_id]
        assert row["status"] == result.status
        assert json.loads(row["summary_json"]) == result.summary
        assert row["source_sha256"] == result.source_sha256
        assert row["quarantine_count"] == len(result.rejections)
        assert row["observation_row_count"] == result.observations.num_rows
        assert row["forecast_row_count"] == result.forecasts.num_rows
        assert row["forecast_daily_row_count"] == result.forecasts_daily.num_rows
        for name, column in ((OBSERVATION_TABLE, "observation_version"), (FORECAST_TABLE, "forecast_version"),
                             (FORECAST_DAILY_TABLE, "forecast_daily_version")):
            assert row[column] == _latest_version(delta_spark, silver, name)
        controls = publication_controls(result)
        assert {key: row[key] for key in controls} == controls
        assert row["bronze_format"] == result.summary.get("bronze_format", "current")
        assert json.loads(row["unknown_paths_json"]) == result.summary.get("unknown_paths", [])
        assert row["nulled_metrics"] == sum(r.scope == "field" for r in result.rejections)
        codes = [(q["rejection_index"], q["code"], q["scope"])
                 for q in quarantine if q["ingestion_id"] == ingestion_id]
        assert sorted(codes) == [(i, r.code, r.scope) for i, r in enumerate(result.rejections)]


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


WRITE_ORDER = (OBSERVATION_TABLE, FORECAST_TABLE, FORECAST_DAILY_TABLE, QUARANTINE, PUBLICATIONS)


def test_tables_are_written_in_order_and_the_publication_last(delta_spark, silver, monkeypatch):
    original, calls = engine_module.merge_insert_only, []

    def record(spark, paths, name, rows, ids):
        calls.append(name)
        return original(spark, paths, name, rows, ids)

    monkeypatch.setattr(engine_module, "merge_insert_only", record)
    process_sources(delta_spark, silver, _sources(real_sources()))
    assert tuple(calls) == WRITE_ORDER


@pytest.mark.parametrize("crash_at", WRITE_ORDER[1:])
def test_interruption_between_any_two_writes_is_repaired_without_duplicates(delta_spark, silver, monkeypatch, crash_at):
    original = engine_module.merge_insert_only
    written_before = WRITE_ORDER[:WRITE_ORDER.index(crash_at)]

    def crash(spark, paths, name, rows, ids):
        if name == crash_at:
            raise RuntimeError(f"simulated crash before the {name} commit")
        return original(spark, paths, name, rows, ids)

    monkeypatch.setattr(engine_module, "merge_insert_only", crash)
    with pytest.raises(RuntimeError):
        process_sources(delta_spark, silver, _sources(real_sources()))
    assert _count(delta_spark, silver, PUBLICATIONS) == 0
    assert _count(delta_spark, silver, OBSERVATION_TABLE) > 0
    after_crash = {name: _count(delta_spark, silver, name) for name in written_before}

    monkeypatch.setattr(engine_module, "merge_insert_only", original)
    batch = process_sources(delta_spark, silver, _sources(real_sources()))
    assert len(batch.published) == 2
    for name in written_before:
        assert batch.inserted[name] == 0  # already there, compared equal
        assert _count(delta_spark, silver, name) == after_crash[name]
    results = [transform_bronze(raw, blob) for blob, raw in real_sources()]
    for name in SILVER_DATA_TABLES:
        expected = [row for result in results for row in result.tables[name].to_pylist()]
        assert _rows(silver.table(name), name) == _sorted(expected, name)
    assert _count(delta_spark, silver, QUARANTINE) == sum(len(r.rejections) for r in results)
    assert _count(delta_spark, silver, PUBLICATIONS) == 2


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
    assert set(plan["tables"]) == set(SILVER_DATA_TABLES)
    frozen = read_published(delta_spark, plan, FORECAST_TABLE)
    assert {r["ingestion_id"] for r in frozen.select("ingestion_id").distinct().collect()} == set(plan["ingestion_ids"])
    daily = read_published(delta_spark, plan, FORECAST_DAILY_TABLE)
    assert {r["ingestion_id"] for r in daily.select("ingestion_id").distinct().collect()} <= set(plan["ingestion_ids"])
    assert daily.count() == transform_bronze(first[1], first[0]).forecasts_daily.num_rows
    later = published_read_plan(delta_spark, silver)
    assert len(later["ingestion_ids"]) == 2
    for name in SILVER_DATA_TABLES:
        assert later["tables"][name]["version"] >= plan["tables"][name]["version"]
        assert later["tables"][name]["path"] == silver.table(name)


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
    results = [transform_bronze(raw, blob) for blob, raw in real_sources()]
    for name in SILVER_DATA_TABLES:
        table = DeltaTable(silver.table(name))
        expected = sum(result.tables[name].num_rows for result in results)
        assert table.to_pyarrow_table().cast(TABLE_SCHEMAS[name]).num_rows == expected
        assert table.metadata().configuration.get("delta.appendOnly") == "true"
    assert sum(r.observations.num_rows for r in results) > 0
    for name in (QUARANTINE, PUBLICATIONS):
        assert isinstance(DeltaTable(silver.table(name)).to_pyarrow_table(), pa.Table)


def test_daily_run_lists_recent_days_and_sunday_lists_everything(delta_spark, tmp_path):
    bronze = tmp_path / "bronze"
    (old_blob, old_raw), (new_blob, new_raw) = real_sources()  # collected Oct 17 and Oct 18
    root = f"file://{tmp_path}/silver"
    write_bronze(bronze, new_raw, new_blob)
    first = run(delta_spark, bronze_root=f"file://{bronze}", silver_root=root,
                now=datetime(2026, 10, 19, 2, tzinfo=timezone.utc))  # Monday, first run: full
    assert first["published"] == 1
    write_bronze(bronze, old_raw, old_blob)  # a late archive
    # Monday 03:00, window Oct 18-19: the Oct 17 archive is outside it.
    daily = run(delta_spark, bronze_root=f"file://{bronze}", silver_root=root, lookback_days=2,
                now=datetime(2026, 10, 19, 3, tzinfo=timezone.utc))
    assert daily["published"] == 0
    sunday = run(delta_spark, bronze_root=f"file://{bronze}", silver_root=root, lookback_days=2,
                 now=datetime(2026, 10, 25, 2, tzinfo=timezone.utc))  # Sunday: full scan
    assert sunday["published"] == 1


def test_a_window_without_any_partition_falls_back_to_the_full_listing(delta_spark, tmp_path):
    bronze = tmp_path / "bronze"
    for blob, raw in real_sources():
        write_bronze(bronze, raw, blob)
    root = f"file://{tmp_path}/silver"
    first_blob, first_raw = real_sources()[0]
    ensure_tables(delta_spark, SilverPaths(root))
    process_sources(delta_spark, SilverPaths(root), _sources([(first_blob, first_raw)]))
    later = run(delta_spark, bronze_root=f"file://{bronze}", silver_root=root, lookback_days=2,
                now=datetime(2026, 12, 1, 2, tzinfo=timezone.utc))  # no partition in the window
    assert later["published"] == 1


def _location(spark, qualified):
    return spark.sql(f"DESCRIBE DETAIL {qualified}").collect()[0]["location"].rstrip("/")


def test_catalog_names_are_external_tables_that_follow_the_silver_root(delta_spark, tmp_path):
    # spark_catalog stands for the Unity Catalog catalog: same three-part names and DDL.
    schema = f"silver_catalog_{uuid.uuid4().hex[:12]}"
    uc_schema = f"spark_catalog.{schema}"
    delta_spark.sql(f"CREATE SCHEMA {uc_schema} LOCATION '{tmp_path}/catalog'")
    try:
        first = SilverPaths(f"file://{tmp_path}/silver_a")
        ensure_tables(delta_spark, first)
        process_sources(delta_spark, first, _sources(real_sources()))

        assert register_tables(delta_spark, first, uc_schema) == {name: "created" for name in SPARK_SCHEMAS}
        for name in SPARK_SCHEMAS:
            qualified = uc_table_name(uc_schema, name)
            assert delta_spark.catalog.getTable(qualified).tableType == "EXTERNAL"
            assert engine_module._same_location(_location(delta_spark, qualified), first.table(name))
            assert delta_spark.table(qualified).count() == _count(delta_spark, first, name)
        assert delta_spark.table(uc_table_name(uc_schema, PUBLICATIONS)).count() == 2

        before = _versions(delta_spark, first)
        assert register_tables(delta_spark, first, uc_schema) == {name: "unchanged" for name in SPARK_SCHEMAS}
        assert _versions(delta_spark, first) == before  # registration never writes data

        second = SilverPaths(f"file://{tmp_path}/silver_b")
        ensure_tables(delta_spark, second)
        assert register_tables(delta_spark, second, uc_schema) == {name: "relocated" for name in SPARK_SCHEMAS}
        for name in SPARK_SCHEMAS:
            qualified = uc_table_name(uc_schema, name)
            assert engine_module._same_location(_location(delta_spark, qualified), second.table(name))
            assert delta_spark.table(qualified).count() == 0
        # Dropping the external definitions kept every file of the first root.
        assert _count(delta_spark, first, PUBLICATIONS) == 2
        assert _versions(delta_spark, first) == before
        assert register_tables(delta_spark, second, uc_schema) == {name: "unchanged" for name in SPARK_SCHEMAS}
    finally:
        delta_spark.sql(f"DROP SCHEMA IF EXISTS {uc_schema} CASCADE")
