"""Real Delta proof of cumulative Gold builds, replay and the publication barrier.

Sources are the project's two Bronze fixtures derived from real captures. No
Azure access is required, and every output lives below pytest's temporary root.
"""
import json

from deltalake import DeltaTable
import pytest

import gold_pipeline as gold
import silver_pipeline as silver
from conftest import real_sources


def _publish(spark, paths, items):
    return silver.process_sources(spark, paths, [silver.Source(blob, raw) for blob, raw in items])


def _read_all(spark, root):
    plan = gold.gold_read_plan(spark, root)
    return {
        name: sorted(
            (row.asDict(recursive=True) for row in gold.read_gold(spark, plan, name).collect()),
            key=lambda row: json.dumps(row, sort_keys=True, default=str),
        )
        for name in gold.GOLD_SCHEMAS
    }


def _physical_versions(root):
    return {name: DeltaTable(f"{root}/{name}").version() for name in gold.GOLD_SCHEMAS}


@pytest.fixture
def silver_root(delta_spark, tmp_path):
    paths = silver.SilverPaths(f"file://{tmp_path}/silver")
    silver.ensure_tables(delta_spark, paths)
    return paths


def test_period_additions_equal_full_build_and_replay_has_no_commit(delta_spark, silver_root, tmp_path):
    first, second = real_sources()
    _publish(delta_spark, silver_root, [first, second])
    incremental_root = f"file://{tmp_path}/incremental"
    full_root = f"file://{tmp_path}/full"

    # Explicit bounds select collection dates, beginning inclusive/end exclusive.
    initial = gold.run_gold(
        delta_spark, silver_root=silver_root.root, gold_root=incremental_root,
        start_date="2026-10-17", end_date="2026-10-18",
    )
    assert initial["changed"] is True
    assert initial["source_count"] == initial["new_sources"] == 1
    before = _read_all(delta_spark, incremental_root)
    assert {r["ingestion_id"] for r in before["dim_collection"]} == {silver.Source(*first).ingestion_id}
    # Partial ingestions are deliberately usable: known London rejection must not
    # discard the valid Paris/Brussels observations from the same publication.
    assert {r["city_id"] for r in before["fact_observation_hourly"]} == {"fr_paris", "be_brussels"}

    addition = gold.run_gold(
        delta_spark, silver_root=silver_root.root, gold_root=incremental_root,
        start_date="2026-10-18", end_date="2026-10-19",
    )
    assert addition["changed"] is True
    assert addition["source_count"] == 2
    assert addition["new_sources"] == 1
    reference = gold.run_gold(
        delta_spark, silver_root=silver_root.root, gold_root=full_root, mode="full",
    )
    assert reference["source_count"] == 2
    assert _read_all(delta_spark, incremental_root) == _read_all(delta_spark, full_root)

    versions = _physical_versions(incremental_root)
    plan = gold.gold_read_plan(delta_spark, incremental_root)
    replay = gold.run_gold(delta_spark, silver_root=silver_root.root, gold_root=incremental_root)
    assert replay["changed"] is False
    assert replay["new_sources"] == 0
    assert replay["build_id"] == addition["build_id"]
    assert gold.gold_read_plan(delta_spark, incremental_root) == plan
    assert _physical_versions(incremental_root) == versions

    # Simulate corruption in an already consumed source publication. Even a
    # no-op run must detect it, before changing any Gold table or build ledger.
    publication_path = silver_root.table(silver.PUBLICATIONS)
    delta_spark.sql(
        f"ALTER TABLE delta.`{publication_path}` "
        "SET TBLPROPERTIES ('delta.appendOnly' = 'false')"
    )
    ingestion = silver.Source(*first).ingestion_id
    delta_spark.sql(
        f"UPDATE delta.`{publication_path}` SET source_sha256 = repeat('0', 64) "
        f"WHERE ingestion_id = '{ingestion}'"
    )
    with pytest.raises(gold.PublicationConflict, match="identity changed"):
        gold.run_gold(delta_spark, silver_root=silver_root.root, gold_root=incremental_root)
    assert gold.gold_read_plan(delta_spark, incremental_root) == plan
    assert _physical_versions(incremental_root) == versions


def test_failed_build_keeps_previous_snapshot_visible_then_recovers(
    delta_spark, silver_root, tmp_path, monkeypatch,
):
    first, second = real_sources()
    _publish(delta_spark, silver_root, [first])
    root = f"file://{tmp_path}/gold"
    original = gold.run_gold(delta_spark, silver_root=silver_root.root, gold_root=root)
    previous_plan = gold.gold_read_plan(delta_spark, root)
    previous_rows = _read_all(delta_spark, root)
    _publish(delta_spark, silver_root, [second])

    write = gold._write_gold_table
    calls = []

    def interrupt(spark, paths, name, frame):
        calls.append(name)
        if len(calls) == 3:
            raise RuntimeError("simulated interruption between Gold table commits")
        return write(spark, paths, name, frame)

    monkeypatch.setattr(gold, "_write_gold_table", interrupt)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        gold.run_gold(delta_spark, silver_root=silver_root.root, gold_root=root)
    assert len(calls) == 3
    # Some physical tables were replaced, but the completed-build ledger still
    # exposes only the old versions, never the partially written new build.
    assert gold.gold_read_plan(delta_spark, root) == previous_plan
    assert _read_all(delta_spark, root) == previous_rows

    monkeypatch.setattr(gold, "_write_gold_table", write)
    repaired = gold.run_gold(delta_spark, silver_root=silver_root.root, gold_root=root)
    assert repaired["changed"] is True
    assert repaired["source_count"] == 2
    assert repaired["new_sources"] == 1
    assert repaired["build_id"] != original["build_id"]
    collections = _read_all(delta_spark, root)["dim_collection"]
    assert {row["ingestion_id"] for row in collections} == {
        silver.Source(*first).ingestion_id, silver.Source(*second).ingestion_id,
    }
    assert len(collections) == 2

    # Check the physical contract, including the CHECK metadata commit captured
    # in the build manifest (the row write alone is an earlier Delta version).
    manifest = gold.latest_gold_build(delta_spark, gold.GoldPaths(root))
    versions = json.loads(manifest["table_versions_json"])
    for name in gold.GOLD_SCHEMAS:
        path = f"{root}/{name}"
        detail = delta_spark.sql(f"DESCRIBE DETAIL delta.`{path}`").first()
        assert detail["properties"]["delta.enableDeletionVectors"] == "false"
        assert detail["clusteringColumns"] == list(gold.GOLD_CLUSTER_COLUMNS[name])
        assert versions[name] == DeltaTable(path).version()
        if name == "fact_forecast_daily":
            check = "".join(detail["properties"]["delta.constraints.lead_days_range"].lower().split())
            assert "lead_days" in check and "-1" in check and "2" in check


@pytest.mark.parametrize("bounds", [
    {"start_date": "2026-10-19", "end_date": "2026-10-18"},
    {"start_date": "2026-10-18", "end_date": "2026-10-18"},
    {"start_date": "2026-02-30", "end_date": "2026-03-02"},
    {"start_date": "2026-10-17T00:00:00Z", "end_date": "2026-10-19"},
])
def test_invalid_collection_period_is_rejected_before_reading_storage(tmp_path, bounds):
    with pytest.raises(ValueError):
        gold.run_gold(
            None, silver_root=f"file://{tmp_path}/missing-silver",
            gold_root=f"file://{tmp_path}/gold", **bounds,
        )


def test_invalid_mode_is_rejected_before_reading_storage(tmp_path):
    with pytest.raises(ValueError):
        gold.run_gold(
            None, silver_root=f"file://{tmp_path}/missing-silver",
            gold_root=f"file://{tmp_path}/gold", mode="append-and-hope",
        )
