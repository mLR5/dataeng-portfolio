"""Spark-only checks that need no Delta jars: contract mapping, paths, discovery."""
import hashlib

import pyarrow as pa
import pytest
from pyspark.sql import types as T

from shared.silver_checks import bronze_identity
from shared.silver_contract import FORECAST_TABLE, OBSERVATION_TABLE, TABLE_SCHEMAS
from silver_spark.discovery import list_bronze, load_sources
from silver_spark.schemas import KEYS, SPARK_SCHEMAS, SilverPaths, ddl, spark_type

from conftest import real_sources, write_bronze


@pytest.mark.parametrize("name", [OBSERVATION_TABLE, FORECAST_TABLE])
def test_spark_schema_follows_the_arrow_contract_exactly(name):
    arrow, spark = TABLE_SCHEMAS[name], SPARK_SCHEMAS[name]
    assert [f.name for f in spark] == arrow.names
    assert [f.nullable for f in spark] == [f.nullable for f in arrow]
    assert spark[f"{'observed' if name == OBSERVATION_TABLE else 'target'}_at_utc"].dataType == T.TimestampType()
    assert spark["humidity_pct"].dataType == T.ShortType()
    assert spark["ingestion_date"].dataType == T.DateType()
    assert set(KEYS[name]) <= set(arrow.names)


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


@pytest.mark.parametrize("root", ["relative/path", "abfss://x@y/z`; DROP", "file:///tmp/a/../b", "s3://b/it's"])
def test_unsafe_roots_are_refused(root):
    with pytest.raises(ValueError):
        SilverPaths(root)


def test_control_tables_live_under_the_version_root():
    paths = SilverPaths("abfss://datalake@acct.dfs.core.windows.net/silver/weatherapi/schema=v1/transform=1.0.0/")
    assert paths.table("publications").endswith("/transform=1.0.0/_control/publications")
    assert paths.table(OBSERVATION_TABLE).endswith("/transform=1.0.0/observation_snapshots")


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
