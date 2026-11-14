"""The audit reads actual Delta snapshots, without repairing or committing."""
from datetime import datetime, timezone

import pytest

from conftest import real_sources, write_bronze
from silver_audit import audit
from silver_pipeline import (FORECAST_TABLE, OBSERVATION_TABLE, PUBLICATIONS, QUARANTINE,
                             SilverPaths, Source, ensure_tables, process_sources, _version)


@pytest.fixture
def audit_data(delta_spark, tmp_path):
    bronze = tmp_path / "bronze"
    paths = SilverPaths(f"file://{tmp_path}/silver")
    sources = []
    for blob, raw in real_sources():
        write_bronze(bronze, raw, blob)
        sources.append(Source(blob, raw))
    ensure_tables(delta_spark, paths)
    process_sources(delta_spark, paths, sources)
    return bronze, paths


def _audit(spark, bronze, paths, **kwargs):
    return audit(spark, bronze_root=f"file://{bronze}", silver_root=paths.root,
                 collected_before=datetime(2026, 10, 19, tzinfo=timezone.utc), **kwargs)


def test_reconciles_real_delta_in_batches_without_any_commit(delta_spark, audit_data):
    bronze, paths = audit_data
    names = (PUBLICATIONS, OBSERVATION_TABLE, FORECAST_TABLE, QUARANTINE)
    before = {n: _version(delta_spark, paths.table(n)) for n in names}
    report = _audit(delta_spark, bronze, paths, batch_size=1)
    assert report["ok"], report
    assert report["checked_sources"] == 2 and report["statuses"] == {"partial": 2}
    assert {n: _version(delta_spark, paths.table(n)) for n in names} == before
    assert all(s["tables"][FORECAST_TABLE]["stored_rows"] == 12 for s in report["sources"])


def test_unpublished_bronze_and_orphan_silver_are_detected(delta_spark, audit_data):
    bronze, paths = audit_data
    blob = "weather/weatherapi/2026/10/18/22/20261018_220000_weather_data.json"
    write_bronze(bronze, b"{invalid", blob)
    (bronze / real_sources()[0][0]).unlink()  # Silver's source disappeared
    report = _audit(delta_spark, bronze, paths)
    assert not report["ok"]
    assert report["orphan_ingestions"][PUBLICATIONS] == 1
    pending = next(s for s in report["sources"] if s["source_blob"] == blob)
    assert "publications: missing_or_duplicate_publication" in pending["errors"]
    assert "quarantine: stored_rejections_differ" in pending["errors"]


def test_cutoff_excludes_newer_unpublished_bronze(delta_spark, audit_data):
    bronze, paths = audit_data
    write_bronze(bronze, b"{}", "weather/weatherapi/2026/10/19/00/20261019_000000_weather_data.json")
    assert _audit(delta_spark, bronze, paths)["ok"]
