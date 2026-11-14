"""Pipeline logic that needs neither Spark nor Delta: publication controls, quarantine
rows and the Unity Catalog registration statements (run against a recording fake).

The real catalog behaviour (CREATE ... LOCATION, external DROP keeping the data) is
proven against Delta in test_pipeline_delta.py, in CI.
"""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import re
from types import SimpleNamespace

import pytest

import silver_pipeline as engine
from silver_pipeline import (
    FORECAST_DAILY_TABLE, FORECAST_TABLE, OBSERVATION_TABLE, PUBLICATIONS, QUARANTINE,
    SPARK_SCHEMAS, CatalogConflict, PublicationConflict, Rejection, SilverPaths, Source,
    _quarantine_rows, _quarantine_sha256, publication_controls, quarantine_records,
    register_tables, transform_bronze, uc_table_name,
)

from conftest import real_sources

ROOT = "abfss://datalake@acct.dfs.core.windows.net/silver/weatherapi_v2"
INVALID_BLOB = "weather/weatherapi/2026/10/19/08/20261019_080000_weather_data.json"


def _result(**changes):
    blob, raw = real_sources()[0]
    return replace(transform_bronze(raw, blob), **changes)


# Publication controls ---------------------------------------------------------------

def test_controls_default_safely_when_the_summary_lacks_v2_keys():
    result = _result(summary={}, rejections=())
    assert publication_controls(result) == {
        "bronze_format": "current", "requested_horizon_days": None, "nulled_metrics": 0,
        "unknown_path_count": 0, "unknown_paths_json": "[]",
    }


def test_controls_of_an_unreadable_bronze_use_the_defaults():
    controls = publication_controls(transform_bronze(b"{not json", INVALID_BLOB))
    assert controls["bronze_format"] == "current" and controls["unknown_path_count"] == 0


def test_controls_copy_the_summary_and_count_field_rejections():
    rejections = (
        Rejection(FORECAST_TABLE, "out_of_range", "Paris", "0/1", "uv", "UV out of range", "field"),
        Rejection(FORECAST_TABLE, "invalid_type", "Paris", "0/2", "gust_kph", "Not a number", "field"),
        Rejection(OBSERVATION_TABLE, "missing_field", "Paris", None, "temp_c", "Missing", "row"),
    )
    result = _result(rejections=rejections, summary={
        "bronze_format": "legacy", "requested_horizon_days": 3,
        "unknown_paths": ["data.x.new_metric", "data.é"]})
    controls = publication_controls(result)
    assert controls["bronze_format"] == "legacy"
    assert controls["requested_horizon_days"] == 3
    assert controls["nulled_metrics"] == 2
    assert controls["unknown_path_count"] == 2
    assert json.loads(controls["unknown_paths_json"]) == ["data.x.new_metric", "data.é"]


@pytest.mark.parametrize("summary", [
    {"bronze_format": "v3"}, {"requested_horizon_days": "3"}, {"requested_horizon_days": True},
    {"requested_horizon_days": -1}, {"unknown_paths": "data.x"}, {"unknown_paths": [1]},
])
def test_a_malformed_control_value_fails_instead_of_being_defaulted(summary):
    with pytest.raises(PublicationConflict):
        publication_controls(_result(summary=summary))


def test_the_real_transformation_yields_publishable_controls():
    for blob, raw in real_sources():
        result = transform_bronze(raw, blob)
        controls = publication_controls(result)
        assert controls["nulled_metrics"] == sum(r.scope == "field" for r in result.rejections)
        assert controls["unknown_path_count"] == len(result.summary.get("unknown_paths", []))


# Write order and publication rows (storage replaced by a recorder) ---------------------

def test_three_tables_then_quarantine_then_the_publication_last(spark, monkeypatch):
    calls, published_rows = [], {}
    versions = {OBSERVATION_TABLE: 11, FORECAST_TABLE: 12, FORECAST_DAILY_TABLE: 13, QUARANTINE: 14, PUBLICATIONS: 15}

    def record(spark_, paths, name, rows, ids):
        calls.append(name)
        # The rows must fit the Spark schema (types and NOT NULL), as in the real MERGE.
        engine._frame(spark, name, rows).collect()
        if name == PUBLICATIONS:
            published_rows.update({row["ingestion_id"]: row for row in rows})
        return len(rows), versions[name]

    def published(spark_, paths, ids):
        return {i: (published_rows[i]["source_blob"], published_rows[i]["source_sha256"])
                for i in ids if i in published_rows}

    monkeypatch.setattr(engine, "merge_insert_only", record)
    monkeypatch.setattr(engine, "published", published)
    items = real_sources() + [(INVALID_BLOB, b"{not json")]
    batch = engine.process_sources(None, SilverPaths(ROOT), [Source(b, r) for b, r in items],
                                   now=datetime(2026, 11, 14, 2, tzinfo=timezone.utc))
    assert calls == [OBSERVATION_TABLE, FORECAST_TABLE, FORECAST_DAILY_TABLE, QUARANTINE, PUBLICATIONS]
    assert batch.versions == versions
    assert len(published_rows) == 3
    for blob, raw in items:
        result = transform_bronze(raw, blob)
        row = published_rows[Source(blob, raw).ingestion_id]
        assert set(row) == {f.name for f in SPARK_SCHEMAS[PUBLICATIONS]}
        assert (row["observation_version"], row["forecast_version"], row["forecast_daily_version"]) == (11, 12, 13)
        assert row["observation_row_count"] == result.observations.num_rows
        assert row["forecast_row_count"] == result.forecasts.num_rows
        assert row["forecast_daily_row_count"] == result.forecasts_daily.num_rows
        assert {k: row[k] for k in publication_controls(result)} == publication_controls(result)


# Quarantine -------------------------------------------------------------------------

def test_quarantine_rows_carry_the_scope_and_match_the_table_schema():
    rejections = (Rejection(FORECAST_TABLE, "out_of_range", "Paris", "0/1", "uv", "UV", "field"),
                  Rejection(OBSERVATION_TABLE, "missing_field", "Paris", None, "temp_c", "Missing"))
    result = _result(rejections=rejections)
    rows = _quarantine_rows(result, result.ingestion_id)
    assert [set(row) for row in rows] == [{f.name for f in SPARK_SCHEMAS[QUARANTINE]}] * 2
    assert [(r["rejection_index"], r["scope"]) for r in rows] == [(0, "field"), (1, "row")]


def test_quarantine_hash_covers_the_scope():
    rejection = Rejection(FORECAST_TABLE, "out_of_range", "Paris", "0/1", "uv", "UV", "field")
    field = _result(rejections=(rejection,))
    row = _result(rejections=(replace(rejection, scope="row"),))
    records = quarantine_records(field, field.ingestion_id)
    assert records[0]["scope"] == "field"
    expected = hashlib.sha256(b"".join(
        json.dumps(r, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        for r in records)).hexdigest()
    assert _quarantine_sha256(field, field.ingestion_id) == expected
    assert _quarantine_sha256(field, field.ingestion_id) != _quarantine_sha256(row, row.ingestion_id)


def test_an_unknown_scope_is_refused():
    result = _result(rejections=(Rejection(FORECAST_TABLE, "out_of_range", scope="cell"),))
    with pytest.raises(PublicationConflict):
        _quarantine_rows(result, result.ingestion_id)


# Unity Catalog names ----------------------------------------------------------------

class FakeSpark:
    """Records SQL and keeps {qualified name: (location, table type)} like a catalog."""

    def __init__(self, tables=None):
        self.tables = dict(tables or {})
        self.statements = []
        self.catalog = SimpleNamespace(
            tableExists=lambda name: name in self.tables,
            getTable=lambda name: SimpleNamespace(tableType=self.tables[name][1]))

    def sql(self, statement):
        self.statements.append(statement)
        if match := re.fullmatch(r"DESCRIBE DETAIL (\S+)", statement):
            return SimpleNamespace(collect=lambda: [{"location": self.tables[match.group(1)][0]}])
        if match := re.fullmatch(r"DROP TABLE (\S+)", statement):
            del self.tables[match.group(1)]
        elif match := re.fullmatch(r"CREATE TABLE (\S+) USING DELTA LOCATION '([^']+)'", statement):
            assert match.group(1) not in self.tables
            self.tables[match.group(1)] = (match.group(2), "EXTERNAL")
        else:
            raise AssertionError(f"Unexpected SQL: {statement}")
        return None

    def ddl(self):
        return [s for s in self.statements if not s.startswith("DESCRIBE")]


@pytest.mark.parametrize("uc_schema", [
    "", "weather", "weather.silver.tables", "weather.silver;DROP", "`weather`.silver",
    "weather.sil ver", "weather.silver-v2", "weather..silver", "1weather.silver", None,
])
def test_malformed_uc_schemas_are_refused_before_any_sql(uc_schema):
    spark = FakeSpark()
    with pytest.raises(ValueError):
        register_tables(spark, SilverPaths(ROOT), uc_schema)
    assert spark.statements == []


def test_uc_names_are_quoted_three_part_names_of_known_tables():
    assert uc_table_name("weather.silver", OBSERVATION_TABLE) == "`weather`.`silver`.`observation_snapshots`"
    with pytest.raises(ValueError):
        uc_table_name("weather.silver", "forecast_snapshots")


def test_registration_creates_the_five_external_tables_at_their_paths():
    spark, paths = FakeSpark(), SilverPaths(ROOT)
    actions = register_tables(spark, paths, "weather.silver")
    assert actions == {name: "created" for name in SPARK_SCHEMAS}
    assert len(actions) == 5
    assert spark.ddl() == [
        f"CREATE TABLE `weather`.`silver`.`{name}` USING DELTA LOCATION '{paths.table(name)}'"
        for name in SPARK_SCHEMAS]
    assert spark.tables["`weather`.`silver`.`publications`"][0] == f"{ROOT}/_control/publications"


def test_registration_is_idempotent():
    spark = FakeSpark()
    register_tables(spark, SilverPaths(ROOT), "weather.silver")
    spark.statements.clear()
    assert set(register_tables(spark, SilverPaths(ROOT), "weather.silver").values()) == {"unchanged"}
    assert spark.ddl() == []


def test_local_paths_reported_as_file_colon_slash_are_the_same_location():
    paths = SilverPaths("file:///tmp/silver")
    spark = FakeSpark({uc_table_name("main.silver", name): (
        paths.table(name).replace("file:///", "file:/") + "/", "EXTERNAL") for name in SPARK_SCHEMAS})
    assert set(register_tables(spark, paths, "main.silver").values()) == {"unchanged"}
    assert spark.ddl() == []


def test_a_new_root_relocates_each_name_by_drop_then_create():
    spark = FakeSpark()
    register_tables(spark, SilverPaths(f"{ROOT}_old"), "weather.silver")
    spark.statements.clear()
    paths = SilverPaths(ROOT)
    assert set(register_tables(spark, paths, "weather.silver").values()) == {"relocated"}
    expected = []
    for name in SPARK_SCHEMAS:
        qualified = uc_table_name("weather.silver", name)
        expected += [f"DROP TABLE {qualified}", f"CREATE TABLE {qualified} USING DELTA LOCATION '{paths.table(name)}'"]
    assert spark.ddl() == expected
    assert {location for location, _ in spark.tables.values()} == {paths.table(n) for n in SPARK_SCHEMAS}


def test_a_managed_table_under_the_name_is_never_dropped():
    qualified = uc_table_name("weather.silver", OBSERVATION_TABLE)
    spark = FakeSpark({qualified: ("abfss://managed@acct.dfs.core.windows.net/x", "MANAGED")})
    with pytest.raises(CatalogConflict):
        register_tables(spark, SilverPaths(ROOT), "weather.silver")
    assert not any(s.startswith("DROP") for s in spark.statements)
    assert spark.tables[qualified][1] == "MANAGED"
