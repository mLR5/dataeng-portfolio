"""Spark-only checks that need no Delta jars: contract mapping, paths, discovery."""
from datetime import date, datetime, timezone
import hashlib

import pyarrow as pa
import pytest
from pyspark.sql import types as T

from silver_pipeline import (
    FORECAST_DAILY_TABLE, FORECAST_TABLE, KEYS, OBSERVATION_TABLE, PUBLICATION_ROW_COUNT_COLUMNS,
    PUBLICATION_VERSION_COLUMNS, PUBLICATIONS, QUARANTINE, SILVER_DATA_TABLES, SPARK_SCHEMAS,
    TABLE_SCHEMAS, SilverPaths, bronze_identity, bronze_location, ddl, list_bronze, load_sources,
    recent_days, spark_type,
)

from conftest import real_sources, write_bronze


def test_the_three_business_tables_and_two_control_tables_are_declared():
    assert SILVER_DATA_TABLES == (OBSERVATION_TABLE, FORECAST_TABLE, FORECAST_DAILY_TABLE)
    assert set(SPARK_SCHEMAS) == {*SILVER_DATA_TABLES, QUARANTINE, PUBLICATIONS}
    assert set(KEYS) == set(SPARK_SCHEMAS)
    assert KEYS[FORECAST_DAILY_TABLE] == ("ingestion_id", "city_id", "target_local_date")


@pytest.mark.parametrize("name", [OBSERVATION_TABLE, FORECAST_TABLE, FORECAST_DAILY_TABLE])
def test_spark_schema_follows_the_arrow_contract_exactly(name):
    arrow, spark = TABLE_SCHEMAS[name], SPARK_SCHEMAS[name]
    assert [f.name for f in spark] == arrow.names
    assert [f.nullable for f in spark] == [f.nullable for f in arrow]
    assert [f.dataType for f in spark] == [spark_type(f.type) for f in arrow]
    assert spark["collected_at_utc"].dataType == T.TimestampType()
    assert spark["ingestion_date"].dataType == T.DateType()
    assert set(KEYS[name]) <= set(arrow.names)


def test_business_column_types_that_readers_rely_on():
    assert SPARK_SCHEMAS[OBSERVATION_TABLE]["observed_at_utc"].dataType == T.TimestampType()
    assert SPARK_SCHEMAS[FORECAST_TABLE]["target_at_utc"].dataType == T.TimestampType()
    assert SPARK_SCHEMAS[FORECAST_TABLE]["humidity_pct"].dataType == T.ShortType()
    assert SPARK_SCHEMAS[FORECAST_TABLE]["chance_of_rain_pct"].dataType == T.DoubleType()
    daily = SPARK_SCHEMAS[FORECAST_DAILY_TABLE]
    assert daily["target_local_date"].dataType == T.DateType() and not daily["target_local_date"].nullable
    assert daily["lead_days"].dataType == T.ShortType()
    assert daily["will_it_rain"].dataType == T.BooleanType()
    assert daily["sunrise_utc"].dataType == T.TimestampType() and daily["sunrise_utc"].nullable
    assert daily["sunrise_local"].dataType == T.StringType()


def test_quarantine_records_the_rejection_scope():
    scope = SPARK_SCHEMAS[QUARANTINE]["scope"]
    assert scope.dataType == T.StringType() and not scope.nullable
    assert "`scope` STRING NOT NULL" in ddl(SPARK_SCHEMAS[QUARANTINE])


def test_publication_has_one_version_and_count_per_business_table_and_v2_controls():
    schema = SPARK_SCHEMAS[PUBLICATIONS]
    assert set(PUBLICATION_VERSION_COLUMNS) == set(PUBLICATION_ROW_COUNT_COLUMNS) == set(SILVER_DATA_TABLES)
    for name in SILVER_DATA_TABLES:
        version, count = schema[PUBLICATION_VERSION_COLUMNS[name]], schema[PUBLICATION_ROW_COUNT_COLUMNS[name]]
        assert (version.dataType, version.nullable) == (T.LongType(), False)
        assert (count.dataType, count.nullable) == (T.IntegerType(), False)
    expected = {
        "forecast_daily_version": (T.LongType(), False), "forecast_daily_row_count": (T.IntegerType(), False),
        "bronze_format": (T.StringType(), False), "requested_horizon_days": (T.ShortType(), True),
        "nulled_metrics": (T.IntegerType(), False), "unknown_path_count": (T.IntegerType(), False),
        "unknown_paths_json": (T.StringType(), False),
    }
    for name, (data_type, nullable) in expected.items():
        assert (schema[name].dataType, schema[name].nullable) == (data_type, nullable), name


def test_non_utc_or_non_microsecond_timestamps_are_refused():
    with pytest.raises(ValueError):
        spark_type(pa.timestamp("ms", tz="UTC"))
    with pytest.raises(ValueError):
        spark_type(pa.timestamp("us"))


def test_ddl_keeps_not_null_and_quotes_names():
    text = ddl(SPARK_SCHEMAS[OBSERVATION_TABLE])
    assert "`ingestion_id` STRING NOT NULL" in text
    assert "`feels_like_c` DOUBLE," in text
    assert "`humidity_pct` SMALLINT" in text


@pytest.mark.parametrize("root", ["relative/path", "abfss://x@y/z`; DROP", "file:///tmp/a/../b", "s3://b/it's",
                                  "abfss://x@y/a\\b", "abfss://x@y/a b"])
def test_unsafe_roots_are_refused(root):
    with pytest.raises(ValueError):
        SilverPaths(root)


def test_control_tables_live_under_the_silver_root():
    paths = SilverPaths("abfss://datalake@acct.dfs.core.windows.net/silver/weatherapi_v2/")
    assert paths.table("publications").endswith("/silver/weatherapi_v2/_control/publications")
    assert paths.table(QUARANTINE).endswith("/silver/weatherapi_v2/_control/quarantine")
    assert paths.table(OBSERVATION_TABLE).endswith("/silver/weatherapi_v2/observation_snapshots")
    assert paths.table(FORECAST_TABLE).endswith("/silver/weatherapi_v2/forecast_hourly_snapshots")
    assert paths.table(FORECAST_DAILY_TABLE).endswith("/silver/weatherapi_v2/forecast_daily_snapshots")
    with pytest.raises(ValueError):
        paths.table("forecast_snapshots")


def test_listing_extracts_canonical_identity_and_ignores_other_files(spark, tmp_path):
    for blob, raw in real_sources():
        write_bronze(tmp_path, raw, blob)
    write_bronze(tmp_path, b"{}", "weather/weatherapi/2026/10/18/10/not-an-id_weather_data.json")
    write_bronze(tmp_path, b"{}", "weather/weatherapi/2026/10/18/notes.txt")
    rows = list_bronze(spark, f"file://{tmp_path}").collect()
    canonical = sorted(r["source_blob"] for r in rows if r["source_blob"])
    assert canonical == sorted(blob for blob, _ in real_sources())
    assert len(rows) == 3  # the .txt file is filtered by name, the bad ID is listed then ignored
    for row in rows:
        if row["source_blob"]:
            assert row["ingestion_id"] == bronze_identity(row["source_blob"])


def test_loaded_bytes_are_exact_so_hashes_match_bronze(spark, tmp_path):
    expected = {}
    selected = []
    for blob, raw in real_sources():
        path = write_bronze(tmp_path, raw, blob)
        expected[blob] = hashlib.sha256(raw).hexdigest()
        selected.append((f"file:{path}", blob))
    sources = load_sources(spark, selected)
    assert {s.source_blob: s.sha256 for s in sources} == expected


def test_recent_days_are_utc_dates_including_today():
    now = datetime(2026, 11, 14, 1, 30, tzinfo=timezone.utc)
    assert recent_days(now, 3) == [date(2026, 11, 14), date(2026, 11, 13), date(2026, 11, 12)]
    assert bronze_location("abfss://bronze@a.dfs.core.windows.net/", recent_days(now, 2)).endswith(
        "/weather/weatherapi/{2026/11/13,2026/11/14}")
    assert bronze_location("abfss://bronze@a.dfs.core.windows.net", None).endswith("/weather/weatherapi")


def _blob(day, hour, suffix):
    return f"weather/weatherapi/{day}/{hour:02d}/{day.replace('/', '')}_{hour:02d}0000_{suffix * 32}_weather_data.json"


def test_listing_by_day_reads_only_those_partitions(spark, tmp_path):
    for day, suffix in (("2026/11/10", "a"), ("2026/11/12", "b"), ("2026/11/13", "c")):
        write_bronze(tmp_path, b"{}", _blob(day, 5, suffix))
    days = [date(2026, 11, 13), date(2026, 11, 12), date(2026, 11, 11)]  # 11 is missing on purpose
    recent = sorted(r["source_blob"] for r in list_bronze(spark, f"file://{tmp_path}", days).collect())
    assert recent == [_blob("2026/11/12", 5, "b"), _blob("2026/11/13", 5, "c")]
    assert len(list_bronze(spark, f"file://{tmp_path}").collect()) == 3
