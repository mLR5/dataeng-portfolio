"""Real Delta proofs for the separate dashboard publication boundary.

The fixtures publish the ten-table Gold contract directly. These tests exercise
the dashboard's own snapshot/replay guarantees without rebuilding Silver or
changing the original Gold publication contract.
"""
from datetime import datetime, timezone
import json
from uuid import uuid4

from deltalake import DeltaTable
from pyspark.sql import functions as F
from pyspark.sql import types as T
import pytest

import gold_pipeline as gold


NOW = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)


def _record(table, **values):
    record = {}
    for field in gold.GOLD_SCHEMAS[table]:
        if field.nullable:
            record[field.name] = None
        elif isinstance(field.dataType, T.StringType):
            record[field.name] = "fixture"
        elif isinstance(field.dataType, T.TimestampType):
            record[field.name] = NOW
        elif isinstance(field.dataType, T.DateType):
            record[field.name] = NOW.date()
        elif isinstance(field.dataType, T.BooleanType):
            record[field.name] = True
        elif isinstance(field.dataType, T.DoubleType):
            record[field.name] = 0.0
        else:
            record[field.name] = 0
    record.update(values)
    return record


def _observation(temperature):
    return _record(
        "fact_observation_hourly", city_id="fr_paris", hour_utc=NOW,
        observed_at_utc=NOW, ingestion_id="source-one", collected_at_utc=NOW,
        local_date=NOW.date(), local_hour=14, temperature_c=temperature,
        precipitation_mm=1.0,
    )


def _write_observation(spark, paths, temperature):
    frame = spark.createDataFrame([_observation(temperature)], gold.GOLD_SCHEMAS["fact_observation_hourly"])
    frame.write.format("delta").mode("overwrite").save(paths.table("fact_observation_hourly"))


def _versions(paths):
    return {name: DeltaTable(paths.table(name)).version() for name in gold.GOLD_SCHEMAS}


def _publish_core_manifest(spark, paths, build_id, sequence):
    versions = _versions(paths)
    record = {
        "build_id": build_id,
        "build_sequence": sequence,
        "gold_version": gold.GOLD_VERSION,
        "completed_at_utc": NOW,
        "silver_root": paths.root.rsplit("/", 1)[0] + "/silver",
        "source_ids_json": json.dumps(["source-one"]),
        "source_fingerprints_json": json.dumps({"source-one": ["capture.json", "sha256"]}),
        "silver_versions_json": "{}",
        "table_versions_json": json.dumps(versions, sort_keys=True),
        "row_counts_json": "{}",
        "quality_json": "{}",
    }
    (spark.createDataFrame([record], gold.GOLD_BUILD_SCHEMA).write.format("delta")
     .mode("append").save(paths.table("builds")))
    return versions


@pytest.fixture
def published_gold(delta_spark, tmp_path):
    paths = gold.GoldPaths(f"file://{tmp_path}/gold")
    records = {
        "dim_city": [_record(
            "dim_city", city_id="fr_paris", city_name="Paris", country_name="France",
            timezone="Europe/Paris", hemisphere="north", first_collected_at_utc=NOW,
        )],
        "dim_date": [_record(
            "dim_date", date=NOW.date(), year=2026, quarter=4, month=10,
            month_name="October", iso_week=42, day_of_week=6,
            day_name="Saturday", is_weekend=True,
        )],
        "dim_collection": [_record(
            "dim_collection", ingestion_id="source-one", collected_at_utc=NOW,
            collected_date=NOW.date(), publication_status="complete", bronze_format="v3",
            cities_requested=1, cities_successful=1,
            source_blob="capture.json", source_sha256="sha256",
        )],
        "fact_collection_city": [_record(
            "fact_collection_city", ingestion_id="source-one", city_id="fr_paris",
            collection_status="collected", observation_valid=1,
        )],
        "fact_observation_hourly": [_observation(10.0)],
    }
    for name, schema in gold.GOLD_SCHEMAS.items():
        frame = delta_spark.createDataFrame(records.get(name, []), schema)
        frame.write.format("delta").mode("overwrite").save(paths.table(name))
    _publish_core_manifest(delta_spark, paths, "core-first", 1)
    return paths


def _aggregate_rows(spark, plan):
    spec = plan["aggregate"]
    return (spark.read.format("delta").option("versionAsOf", spec["version"])
            .load(spec["path"]).orderBy("city_id", "local_date").collect())


def _dashboard_versions(paths):
    return {name: DeltaTable(paths.table(name)).version()
            for name in ("agg_city_daily", "dashboard_builds")}


def test_dashboard_initial_build_then_replay_has_no_delta_commit(delta_spark, published_gold):
    paths = published_gold
    core_plan = gold.gold_read_plan(delta_spark, paths.root)
    core_versions = _versions(paths)
    report = gold.run_gold_dashboard(delta_spark, gold_root=paths.root, now=NOW)
    assert report["changed"] is True
    assert report["source_gold_build_id"] == "core-first"
    assert report["row_count"] == 1
    plan = gold.gold_dashboard_read_plan(delta_spark, paths.root)
    rows = _aggregate_rows(delta_spark, plan)
    assert rows[0].temperature_avg_c == 10.0
    assert rows[0].observed_hours == 1
    assert rows[0].is_complete is False
    versions = _dashboard_versions(paths)

    replay = gold.run_gold_dashboard(delta_spark, gold_root=paths.root, now=NOW)
    assert replay["changed"] is False
    assert replay["build_id"] == report["build_id"]
    assert gold.gold_dashboard_read_plan(delta_spark, paths.root) == plan
    assert _dashboard_versions(paths) == versions
    assert _versions(paths) == core_versions
    assert gold.gold_read_plan(delta_spark, paths.root) == core_plan
    assert len(gold.GOLD_SCHEMAS) == 10


def test_dashboard_refreshes_on_new_core_build_with_same_source_ids(delta_spark, published_gold):
    paths = published_gold
    original = gold.run_gold_dashboard(delta_spark, gold_root=paths.root, now=NOW)
    old_plan = gold.gold_dashboard_read_plan(delta_spark, paths.root)
    _write_observation(delta_spark, paths, 21.0)
    source_versions = _publish_core_manifest(delta_spark, paths, "core-second", 2)

    refreshed = gold.run_gold_dashboard(delta_spark, gold_root=paths.root, now=NOW)
    assert refreshed["changed"] is True
    assert refreshed["build_id"] != original["build_id"]
    assert refreshed["source_gold_build_id"] == "core-second"
    new_plan = gold.gold_dashboard_read_plan(delta_spark, paths.root)
    assert _aggregate_rows(delta_spark, new_plan)[0].temperature_avg_c == 21.0
    # Both readers retain stable answers from their independently committed plans.
    assert _aggregate_rows(delta_spark, old_plan)[0].temperature_avg_c == 10.0
    manifest = gold.latest_gold_dashboard_build(delta_spark, paths)
    assert json.loads(manifest["source_table_versions_json"]) == source_versions
    assert _versions(paths) == source_versions


def test_dashboard_interruption_preserves_previous_plan_and_retry_recovers(
    delta_spark, published_gold, monkeypatch,
):
    paths = published_gold
    original = gold.run_gold_dashboard(delta_spark, gold_root=paths.root, now=NOW)
    old_plan = gold.gold_dashboard_read_plan(delta_spark, paths.root)
    old_versions = _dashboard_versions(paths)
    _write_observation(delta_spark, paths, 21.0)
    _publish_core_manifest(delta_spark, paths, "core-second", 2)
    write = gold._write_gold_dashboard_aggregate

    def interrupt_after_commit(*args, **kwargs):
        write(*args, **kwargs)
        raise RuntimeError("simulated interruption after aggregate commit")

    monkeypatch.setattr(gold, "_write_gold_dashboard_aggregate", interrupt_after_commit)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        gold.run_gold_dashboard(delta_spark, gold_root=paths.root, now=NOW)
    assert _dashboard_versions(paths)["agg_city_daily"] > old_versions["agg_city_daily"]
    assert _dashboard_versions(paths)["dashboard_builds"] == old_versions["dashboard_builds"]
    assert gold.gold_dashboard_read_plan(delta_spark, paths.root) == old_plan
    assert _aggregate_rows(delta_spark, old_plan)[0].temperature_avg_c == 10.0

    monkeypatch.setattr(gold, "_write_gold_dashboard_aggregate", write)
    recovered = gold.run_gold_dashboard(delta_spark, gold_root=paths.root, now=NOW)
    assert recovered["changed"] is True
    assert recovered["build_id"] != original["build_id"]
    assert recovered["source_gold_build_id"] == "core-second"
    assert _aggregate_rows(delta_spark, gold.gold_dashboard_read_plan(delta_spark, paths.root))[0].temperature_avg_c == 21.0
    assert delta_spark.read.format("delta").load(paths.table("dashboard_builds")).count() == 2


def test_dashboard_rejects_missing_core_publication_before_writing(delta_spark, tmp_path):
    paths = gold.GoldPaths(f"file://{tmp_path}/missing-gold")
    with pytest.raises(ValueError, match="[Gg]old|[Pp]ublish|[Bb]uild"):
        gold.run_gold_dashboard(delta_spark, gold_root=paths.root, now=NOW)
    assert not (tmp_path / "missing-gold" / "agg_city_daily").exists()
    assert not (tmp_path / "missing-gold" / "_control" / "dashboard_builds").exists()


def test_dashboard_rejects_incompatible_version_before_overwriting(delta_spark, published_gold):
    paths = published_gold
    gold.run_gold_dashboard(delta_spark, gold_root=paths.root, now=NOW)
    delta_spark.sql(
        f"UPDATE delta.`{paths.table('dashboard_builds')}` SET dashboard_version = '0.0.0'"
    )
    versions = _dashboard_versions(paths)
    with pytest.raises(gold.PublicationConflict, match="[Vv]ersion|[Dd]ashboard|root"):
        gold.run_gold_dashboard(delta_spark, gold_root=paths.root, now=NOW)
    assert _dashboard_versions(paths) == versions


def test_dashboard_forced_replay_ignores_unpublished_physical_core_changes(delta_spark, published_gold):
    paths = published_gold
    original = gold.run_gold_dashboard(delta_spark, gold_root=paths.root, now=NOW)
    frozen_core = gold.gold_read_plan(delta_spark, paths.root)
    _write_observation(delta_spark, paths, 99.0)
    # There is deliberately no new Gold manifest after the physical overwrite.
    forced = gold.run_gold_dashboard(delta_spark, gold_root=paths.root, now=NOW, force=True)
    assert forced["changed"] is True
    assert forced["build_id"] != original["build_id"]
    assert forced["source_gold_build_id"] == "core-first"
    plan = gold.gold_dashboard_read_plan(delta_spark, paths.root)
    assert _aggregate_rows(delta_spark, plan)[0].temperature_avg_c == 10.0
    assert gold.gold_read_plan(delta_spark, paths.root) == frozen_core


def test_dashboard_recovers_unpublished_aggregate_overwrite_without_new_core_build(
    delta_spark, published_gold,
):
    spark = delta_spark
    paths = published_gold
    original = gold.run_gold_dashboard(spark, gold_root=paths.root, now=NOW)
    previous = gold.gold_dashboard_read_plan(spark, paths.root)
    core_plan = gold.gold_read_plan(spark, paths.root)
    core_versions = _versions(paths)
    unpublished = (spark.read.format("delta").load(paths.table("agg_city_daily"))
                   .withColumn("temperature_avg_c", F.lit(99.0)))
    unpublished.write.format("delta").mode("overwrite").save(paths.table("agg_city_daily"))
    assert spark.read.format("delta").load(paths.table("agg_city_daily")).first().temperature_avg_c == 99.0
    assert _aggregate_rows(spark, previous)[0].temperature_avg_c == 10.0

    # No new source build and no force flag: physical drift itself requires a
    # fresh, validated dashboard publication instead of an incorrect no-op.
    repaired = gold.run_gold_dashboard(spark, gold_root=paths.root, now=NOW)
    assert repaired["changed"] is True
    assert repaired["build_id"] != original["build_id"]
    assert repaired["source_gold_build_id"] == "core-first"
    current = gold.gold_dashboard_read_plan(spark, paths.root)
    assert _aggregate_rows(spark, current)[0].temperature_avg_c == 10.0
    assert spark.read.format("delta").load(paths.table("agg_city_daily")).first().temperature_avg_c == 10.0
    assert gold.gold_read_plan(spark, paths.root) == core_plan
    assert _versions(paths) == core_versions


def test_dashboard_catalog_publication_and_conflicts_preserve_committed_data(
    delta_spark, published_gold, tmp_path,
):
    """Exercise real catalog DDL, ownership and all three collision shapes."""
    spark = delta_spark
    paths = published_gold
    database = "dashboard_publish_" + uuid4().hex
    qualified = f"spark_catalog.{database}"
    spark.sql(f"CREATE DATABASE {database} LOCATION '{tmp_path.as_uri()}/catalog'")
    try:
        gold.register_gold_tables(spark, paths, qualified)
        report = gold.run_gold_dashboard(spark, gold_root=paths.root, uc_schema=qualified, now=NOW)
        assert report["changed"] is True
        assert spark.table(f"{qualified}.agg_city_daily").first().temperature_avg_c == 10.0
        assert spark.table(f"{qualified}.v_current_weather").first().temperature_c == 10.0
        for name in ("v_current_weather", "v_latest_forecast_hourly", "v_latest_forecast_daily"):
            assert spark.catalog.getTable(f"{qualified}.{name}").tableType == "VIEW"
            properties = {row["key"]: row["value"] for row in spark.sql(
                f"SHOW TBLPROPERTIES {qualified}.{name}").collect()}
            assert properties["weatherapi.dashboard"] == gold.DASHBOARD_VERSION
            # Analysis alone is insufficient: read each persisted view too.
            spark.table(f"{qualified}.{name}").collect()
        replay = gold.run_gold_dashboard(spark, gold_root=paths.root, uc_schema=qualified, now=NOW)
        assert replay["changed"] is False

        # A missing owned view is recoverable even when the source Gold build
        # did not change; a no-op must not leave the serving interface broken.
        current = f"{qualified}.v_current_weather"
        spark.sql(f"DROP VIEW {current}")
        repaired = gold.run_gold_dashboard(spark, gold_root=paths.root, uc_schema=qualified, now=NOW)
        assert repaired["changed"] is True
        assert repaired["source_gold_build_id"] == "core-first"
        assert spark.table(current).first().temperature_c == 10.0
        properties = {row["key"]: row["value"] for row in spark.sql(
            f"SHOW TBLPROPERTIES {current}").collect()}
        assert properties["weatherapi.dashboard"] == gold.DASHBOARD_VERSION
        before = _dashboard_versions(paths)
        plan = gold.gold_dashboard_read_plan(spark, paths.root)

        # An unrelated view is never silently replaced, even on a no-op run.
        spark.sql(f"DROP VIEW {current}")
        spark.sql(f"CREATE VIEW {current} AS SELECT 'foreign' AS owner")
        with pytest.raises(gold.CatalogConflict, match="unowned"):
            gold.run_gold_dashboard(spark, gold_root=paths.root, uc_schema=qualified, now=NOW)
        assert _dashboard_versions(paths) == before
        assert spark.table(current).first().owner == "foreign"

        # A table occupying a serving-view name must also remain untouched.
        spark.sql(f"DROP VIEW {current}")
        spark.sql(f"CREATE TABLE {current} USING DELTA AS SELECT 'foreign' AS owner")
        with pytest.raises(gold.CatalogConflict, match="already a table"):
            gold.run_gold_dashboard(spark, gold_root=paths.root, uc_schema=qualified, now=NOW)
        assert _dashboard_versions(paths) == before
        assert spark.table(current).first().owner == "foreign"
        spark.sql(f"DROP TABLE {current}")

        # A correctly named aggregate pointing elsewhere is a conflict, not an
        # invitation to overwrite that other table or move its catalog binding.
        aggregate = f"{qualified}.agg_city_daily"
        other_path = (tmp_path / "unrelated-aggregate").as_uri()
        (spark.createDataFrame([], gold.AGG_CITY_DAILY_SCHEMA).write.format("delta")
         .save(other_path))
        spark.sql(f"DROP TABLE {aggregate}")
        spark.sql(f"CREATE TABLE {aggregate} USING DELTA LOCATION '{other_path}'")
        with pytest.raises(gold.CatalogConflict, match="elsewhere"):
            gold.run_gold_dashboard(spark, gold_root=paths.root, uc_schema=qualified, now=NOW)
        assert _dashboard_versions(paths) == before
        assert spark.sql(f"DESCRIBE DETAIL {aggregate}").first()["location"].endswith("/unrelated-aggregate")
        assert gold.gold_dashboard_read_plan(spark, paths.root) == plan
        assert _aggregate_rows(spark, plan)[0].temperature_avg_c == 10.0
    finally:
        spark.sql(f"DROP DATABASE IF EXISTS {database} CASCADE")
