"""Independent Bronze accounting and deliberate corruption of the stored side."""
from copy import deepcopy
import json

import pyarrow as pa
import pytest

from conftest import FIXTURES, REAL_FIXTURES
from silver_rules import FORECAST_TABLE, OBSERVATION_TABLE, TABLE_SCHEMAS, bronze_blob_name
from silver_reconciliation import explain_bronze, reconcile_source


@pytest.fixture
def bronze():
    return json.loads((FIXTURES / "synthetic_minimal.json").read_bytes())


def _raw(data):
    return json.dumps(data).encode(), bronze_blob_name(data["ingestion_id"], data["ingestion_timestamp"])


def _hours(data):
    return data["cities"][0]["data"]["forecast"]["forecastday"][0]["hour"]


def _stored(raw, blob):
    # A stand-in for collected storage rows. Delta tests below exercise real I/O.
    from silver_pipeline import _quarantine_rows, _quarantine_sha256
    from silver_rules import SCHEMA_VERSION, TRANSFORM_VERSION, SOURCE, bronze_identity, json_bytes
    result, _ = explain_bronze(raw, blob)
    ingestion_id = bronze_identity(blob)
    return dict(tables=dict(result.tables), quarantine=_quarantine_rows(result, ingestion_id), publications=[{
        "ingestion_id": ingestion_id, "complete": True, "schema_version": SCHEMA_VERSION,
        "transform_version": TRANSFORM_VERSION, "source": SOURCE,
        "source_blob": blob, "source_sha256": result.source_sha256,
        "status": result.status, "collected_at_utc": result.collected_at_utc,
        "observation_row_count": result.observations.num_rows,
        "forecast_row_count": result.forecasts.num_rows,
        "quarantine_count": len(result.rejections),
        "quarantine_sha256": _quarantine_sha256(result, ingestion_id),
        "summary_json": json_bytes(result.summary).decode(),
    }])


@pytest.mark.parametrize("name", REAL_FIXTURES)
def test_real_derived_bronze_balances_including_the_entire_wrong_city(name):
    raw = (FIXTURES / name).read_bytes()
    data = json.loads(raw)
    blob = bronze_blob_name(data["ingestion_id"], data["ingestion_timestamp"])
    _, report = explain_bronze(raw, blob)
    assert report["errors"] == []
    assert report["status"] == "partial"
    assert report["rejection_codes"] == {"location_mismatch": 1}
    for table, source, excluded, valid in ((OBSERVATION_TABLE, 3, 1, 2), (FORECAST_TABLE, 18, 6, 12)):
        counts = report["tables"][table]
        assert counts["bronze_slots"] == source
        assert counts["scope_excluded_rows"] == excluded
        assert counts["valid_rows"] == valid
        assert counts["rejected_rows"] == counts["duplicates_removed"] == 0
    assert reconcile_source(raw, blob, **_stored(raw, blob))["ok"]


@pytest.mark.parametrize("scenario", ["nominal", "duplicate", "conflict", "bad_metric", "missing_current",
                                     "bad_day", "empty_hours", "bad_forecast", "bad_city", "failed_call",
                                     "all_failed", "bad_summary"])
def test_every_source_slot_has_an_explanation(bronze, scenario):
    hours = _hours(bronze)
    if scenario in ("duplicate", "conflict"):
        hours.append(deepcopy(hours[0]))
        if scenario == "conflict":
            hours[-1]["temp_c"] += 1
    elif scenario == "bad_metric":
        hours[0]["humidity"] = 150
    elif scenario == "missing_current":
        bronze["cities"][0]["data"].pop("current")
    elif scenario == "bad_day":
        bronze["cities"][0]["data"]["forecast"]["forecastday"][0]["date"] = "invalid"
    elif scenario == "empty_hours":
        hours.clear()
    elif scenario == "bad_forecast":
        bronze["cities"][0]["data"]["forecast"] = None
    elif scenario == "bad_city":
        bronze["cities"][0]["data"]["location"]["country"] = "Argentina"
    elif scenario in ("failed_call", "all_failed"):
        failed = bronze["cities"][:1] if scenario == "failed_call" else bronze["cities"]
        for city in failed:
            city.update(status="error", data=None)
        bronze["summary"].update(successful=len(bronze["cities"]) - len(failed), failed=len(failed))
        bronze["status"] = "partial" if scenario == "failed_call" else "failed"
    elif scenario == "bad_summary":
        bronze["summary"]["successful"] += 1
    raw, blob = _raw(bronze)
    result, report = explain_bronze(raw, blob)
    assert report["errors"] == []
    assert reconcile_source(raw, blob, **_stored(raw, blob))["ok"]
    if scenario == "duplicate":
        assert report["tables"][FORECAST_TABLE]["duplicates_removed"] == 1
    if scenario == "conflict":
        assert report["tables"][FORECAST_TABLE]["conflicting_rows"] == 2
    if scenario == "all_failed":
        assert result.status == "empty" and report["collection"]["failed"] == 3


def test_invalid_json_is_rejected_and_explicitly_uncountable():
    raw, blob = b"{invalid", "weather/weatherapi/2026/10/19/08/20261019_080000_weather_data.json"
    report = reconcile_source(raw, blob, **_stored(raw, blob))
    assert report["ok"] and not report["bronze_countable"]
    assert report["status"] == "rejected"
    assert report["rejection_codes"] == {"invalid_json": 1}


@pytest.mark.parametrize("corruption", ["lost", "duplicate", "balanced_loss", "changed_metric", "changed_hash",
                                      "no_publication", "duplicate_publication", "wrong_status", "no_quarantine"])
def test_detects_losses_even_when_counts_are_unchanged(bronze, corruption):
    _hours(bronze)[0]["humidity"] = 101  # a real diagnostic must survive in quarantine
    raw, blob = _raw(bronze)
    stored = _stored(raw, blob)
    rows = stored["tables"][FORECAST_TABLE].to_pylist()
    if corruption == "lost":
        rows.pop()
    elif corruption == "duplicate":
        rows.append(deepcopy(rows[0]))
    elif corruption == "balanced_loss":
        rows[-1] = deepcopy(rows[0])
    elif corruption == "changed_metric":
        rows[0]["temperature_c"] += 0.01
    elif corruption == "changed_hash":
        rows[0]["source_sha256"] = "0" * 64
    elif corruption == "no_publication":
        stored["publications"] = []
    elif corruption == "duplicate_publication":
        stored["publications"] *= 2
    elif corruption == "wrong_status":
        stored["publications"][0]["status"] = "success"
    elif corruption == "no_quarantine":
        stored["quarantine"] = []
    stored["tables"][FORECAST_TABLE] = pa.Table.from_pylist(rows, schema=TABLE_SCHEMAS[FORECAST_TABLE])
    report = reconcile_source(raw, blob, **stored)
    assert not report["ok"], corruption
    assert report["errors"]
    if corruption == "balanced_loss":
        counts = report["tables"][FORECAST_TABLE]
        assert counts["stored_rows"] == counts["valid_rows"]
        assert counts["missing_or_changed_rows"] == counts["unexpected_or_changed_rows"] == 1


def test_independent_inventory_catches_a_transformer_silent_drop(bronze, monkeypatch):
    import silver_reconciliation as reconciliation
    raw, blob = _raw(bronze)
    original = reconciliation.transform_bronze

    def broken_transform(raw, blob):
        data = json.loads(raw)
        _hours(data).pop()  # simulate a parser silently skipping a source hour
        return original(json.dumps(data).encode(), blob)

    monkeypatch.setattr(reconciliation, "transform_bronze", broken_transform)
    _, report = reconciliation.explain_bronze(raw, blob)
    assert f"{FORECAST_TABLE}: bronze_inventory_mismatch" in report["errors"]
