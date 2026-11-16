"""Real Delta proof of incremental Gold (CI): MERGE batches equal the full reference.

One frozen Silver history (gold_scenario) is published in four arrival batches.
The incremental root admits each batch with MERGE; the reference root is one full
build of the final Silver state. The scenario contains a late observation closer
to the round hour, an observation arriving after the forecasts targeting it, a
Paris coordinate change (dim_city SCD 2) and its return.
"""
from datetime import datetime, timedelta, timezone
import json

from deltalake import DeltaTable
import pytest

import gold_pipeline as gold
import silver_pipeline as silver
from conftest import real_sources
from gold_scenario import PARIS_MOVED, START, batches, bronze_variant, regular

MONDAY = datetime(2026, 11, 16, 2, tzinfo=timezone.utc)
SUNDAY = datetime(2026, 11, 15, 2, tzinfo=timezone.utc)
INSERT_ONLY = [name for name, strategy in gold.GOLD_LOAD_STRATEGIES.items() if strategy == "insert"]


def _publish(spark, paths, items):
    return silver.process_sources(spark, paths, [silver.Source(blob, raw) for blob, raw in items])


def _rows(frame):
    return sorted((json.dumps(r.asDict(recursive=True), sort_keys=True, default=str) for r in frame.collect()))


def _state(spark, root):
    """Sorted rows of the ten tables and both control tables of the last build."""
    plan = gold.gold_read_plan(spark, root)
    return {name: _rows(gold.read_gold(spark, plan, name))
            for name in (*gold.GOLD_SCHEMAS, *gold.GOLD_CONTROL_SCHEMAS)}


def _physical_versions(root):
    names = (*gold.GOLD_SCHEMAS, *gold.GOLD_CONTROL_SCHEMAS)
    paths = gold.GoldPaths(root)
    return {name: DeltaTable(paths.table(name)).version() for name in names}


def _merge_metrics(spark, root, name, since):
    """operationMetrics of the MERGE commits of ``name`` after version ``since``."""
    path = gold.GoldPaths(root).table(name)
    history = spark.sql(f"DESCRIBE HISTORY delta.`{path}`").where(f"version > {since}").collect()
    return [(row["operation"], dict(row["operationMetrics"])) for row in history]


@pytest.fixture(scope="module")
def scenario(spark_session, tmp_path_factory):
    if not spark_session.delta_enabled:
        pytest.skip("Delta Lake jars unavailable in this environment")
    spark = spark_session
    base = tmp_path_factory.mktemp("gold-incremental")
    silver_paths = silver.SilverPaths(f"file://{base}/silver")
    silver.ensure_tables(spark, silver_paths)
    incremental, reference = f"file://{base}/incremental", f"file://{base}/reference"
    reports, states, versions, metrics = [], [], [], []
    for batch in batches():
        _publish(spark, silver_paths, batch)
        before = _physical_versions(incremental) if reports else None
        reports.append(gold.run_gold(spark, silver_root=silver_paths.root, gold_root=incremental, now=MONDAY))
        states.append(_state(spark, incremental))
        versions.append(_physical_versions(incremental))
        if before is not None:
            metrics.append({name: _merge_metrics(spark, incremental, name, before[name]) for name in INSERT_ONLY})
    full = gold.run_gold(spark, silver_root=silver_paths.root, gold_root=reference, mode="full", now=MONDAY)
    replay_versions = _physical_versions(incremental)
    replay_plan = gold.gold_read_plan(spark, incremental)
    replay = gold.run_gold(spark, silver_root=silver_paths.root, gold_root=incremental, now=MONDAY)
    after_replay = (_physical_versions(incremental), gold.gold_read_plan(spark, incremental))
    weekly = gold.run_gold(spark, silver_root=silver_paths.root, gold_root=incremental,
                           now=SUNDAY, full_rebuild_weekday=6)
    return {"reports": reports, "states": states, "versions": versions, "metrics": metrics,
            "full": full, "reference": _state(spark, reference), "replay": replay,
            "replay_before": (replay_versions, replay_plan), "replay_after": after_replay,
            "weekly": weekly, "weekly_state": _state(spark, incremental)}


def test_incremental_batches_equal_one_full_build(scenario):
    reports = scenario["reports"]
    assert [r["load_mode"] for r in reports] == ["full", "incremental", "incremental", "incremental"]
    assert reports[0]["load_reason"] == "first_build"
    final, reference = scenario["states"][-1], scenario["reference"]
    for name in reference:
        assert final[name] == reference[name], name
    assert reports[-1]["row_counts"] == scenario["full"]["row_counts"]
    assert reports[-1]["quality"] == scenario["full"]["quality"]


def test_scenario_exercises_late_truth_canonical_change_and_city_versions(scenario):
    states = [{name: [json.loads(r) for r in rows] for name, rows in state.items()}
              for state in scenario["states"]]
    # The observation of 22:00 UTC on 18 October (C_24) arrives in the last batch,
    # after forecasts issued by earlier batches targeting that hour.
    target = "2026-10-18 22:00:00"
    errors = [[e for e in s["fact_forecast_hourly_error"] if str(e["target_hour_utc"]) == target] for s in states]
    earlier = {str(r["collected_at_utc"]) for r in states[2]["dim_collection"]}
    assert not errors[2]
    assert len([e for e in errors[3] if e["issued_at_utc"] in earlier]) > 2
    # The closer late observation of 00:00 UTC (L_2, batch 3) replaced C_2's.
    def canonical(state, hour):
        return {o["city_id"]: (o["distance_to_hour_min"], o["temperature_c"])
                for o in state["fact_observation_hourly"] if str(o["hour_utc"]) == hour}
    first, after = canonical(states[1], "2026-10-18 00:00:00"), canonical(states[2], "2026-10-18 00:00:00")
    assert {d for d, _ in first.values()} == {5.0} and {d for d, _ in after.values()} == {0.0}
    assert all(after[c][1] == pytest.approx(first[c][1] + 1.5) for c in first)
    # Daily errors of 18 October appear with a complete day, then follow L_20's truth.
    def day_avg(state):
        return {(e["city_id"], e["issued_at_utc"]): e["observed_temperature_avg_c"]
                for e in state["fact_forecast_daily_error"] if str(e["target_local_date"]) == "2026-10-18"}
    assert not day_avg(states[1]) and day_avg(states[2])
    assert all(day_avg(states[3])[key] == pytest.approx(value + 1.5 / 24) for key, value in day_avg(states[2]).items())
    # dim_city SCD 2: origin, first coordinates, move at C_12, return at C_18.
    paris = sorted((c for c in states[3]["dim_city"] if c["city_id"] == "fr_paris"), key=lambda c: c["valid_from_utc"])
    assert [(c["latitude"], c["is_current"]) for c in paris] == [
        (None, False), (48.8667, False), (PARIS_MOVED, False), (48.8667, True)]
    assert [c["valid_from_utc"] for c in paris[1:]] == [
        str((START + timedelta(hours=k)).replace(tzinfo=None)) for k in (0, 12, 18)]
    moved = paris[2]["city_sk"]
    issued = {str((START + timedelta(hours=k)).replace(tzinfo=None)) for k in range(12, 18)}
    assert {f["city_sk"] for f in states[3]["fact_forecast_hourly"]
            if f["city_id"] == "fr_paris" and f["issued_at_utc"] in issued} == {moved}


def test_insert_only_tables_never_rewrite_existing_rows(scenario):
    states = scenario["states"]
    for previous, current in zip(states, states[1:]):
        for name in INSERT_ONLY:
            assert set(previous[name]) <= set(current[name]), name
            assert len(current[name]) >= len(previous[name]), name
    for batch in scenario["metrics"]:
        for name, commits in batch.items():
            for operation, values in commits:
                assert operation == "MERGE", (name, operation)
                assert int(values.get("numTargetRowsUpdated", 0)) == 0, name
                assert int(values.get("numTargetRowsDeleted", 0)) == 0, name
                assert int(values.get("numTargetFilesRemoved", 0)) == 0, name


def test_incremental_batch_without_new_ingestion_writes_nothing(scenario):
    replay = scenario["replay"]
    assert replay["changed"] is False and replay["new_sources"] == 0 and replay["load_mode"] == "none"
    assert scenario["replay_after"] == scenario["replay_before"]


def test_weekly_full_rebuild_equals_the_incremental_state(scenario):
    weekly = scenario["weekly"]
    assert weekly["changed"] is True
    assert (weekly["load_mode"], weekly["load_reason"]) == ("full", "weekly_full_rebuild")
    assert scenario["weekly_state"] == scenario["states"][-1] == scenario["reference"]


def test_late_collection_rewriting_city_history_falls_back_to_full(delta_spark, tmp_path):
    silver_paths = silver.SilverPaths(f"file://{tmp_path}/silver")
    silver.ensure_tables(delta_spark, silver_paths)
    root, reference = f"file://{tmp_path}/incremental", f"file://{tmp_path}/reference"
    _publish(delta_spark, silver_paths, [regular(0), regular(2)])
    assert gold.run_gold(delta_spark, silver_root=silver_paths.root, gold_root=root, now=MONDAY)["load_mode"] == "full"
    # Collected between two consumed collections, with other Paris coordinates.
    late = START + timedelta(hours=1)
    _publish(delta_spark, silver_paths, [bronze_variant(late, observed=late - timedelta(minutes=5),
                                                        paris_latitude=PARIS_MOVED)])
    report = gold.run_gold(delta_spark, silver_root=silver_paths.root, gold_root=root, now=MONDAY)
    assert (report["load_mode"], report["load_reason"]) == ("full", "city_history_changed")
    gold.run_gold(delta_spark, silver_root=silver_paths.root, gold_root=reference, mode="full", now=MONDAY)
    assert _state(delta_spark, root) == _state(delta_spark, reference)


def test_real_captures_keep_stable_coordinates(delta_spark, tmp_path):
    """Measured on the two real-derived captures: no coordinate drift, one version."""
    silver_paths = silver.SilverPaths(f"file://{tmp_path}/silver")
    silver.ensure_tables(delta_spark, silver_paths)
    _publish(delta_spark, silver_paths, real_sources())
    root = f"file://{tmp_path}/gold"
    gold.run_gold(delta_spark, silver_root=silver_paths.root, gold_root=root)
    cities = [json.loads(r) for r in _state(delta_spark, root)["dim_city"]]
    assert sorted((c["city_id"], c["latitude"], c["longitude"]) for c in cities if c["latitude"] is not None) == [
        ("be_brussels", 50.8333, 4.3333), ("fr_paris", 48.8667, 2.3333)]
