"""Legacy Bronze envelope (ingestion_metadata / metadata / raw_data) -> current shape.

The fixture is synthetic: it reproduces the key paths and types of the 20 real
legacy archives with artificial values and placeholder URL/host/endpoint fields.
The conversion invents nothing; the usual identity, geography and time checks
still decide, so `Londres` resolved to Argentina stays rejected.
"""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest

from silver_rules import BRONZE_FORMATS, LEGACY_FIELD_DECISIONS
from silver_rules import bronze_blob_name, legacy_unknown_paths, normalize_envelope, transform_bronze

FIXTURES = Path(__file__).parent / "fixtures" / "weatherapi"
RAW = (FIXTURES / "synthetic_legacy_envelope.json").read_bytes()
PROVENANCE = json.loads((FIXTURES / "synthetic_legacy_envelope.provenance.json").read_text())
SOURCE_BLOB = "weather/weatherapi/2026/01/19/12/20260119_120000_weather_data.json"
PLACEHOLDERS = ("synthetic-source-placeholder", "synthetic-host-placeholder", "synthetic-endpoint-placeholder")


@pytest.fixture
def legacy():
    return json.loads(RAW)


def _run(data, source_blob=SOURCE_BLOB):
    return transform_bronze(json.dumps(data, ensure_ascii=False).encode("utf-8"), source_blob)


def _codes(result):
    return Counter(rejection.code for rejection in result.rejections)


def _paths(value, path=""):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _paths(child, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for child in value:
            yield from _paths(child, f"{path}[]")
    else:
        yield path


def test_fixture_is_synthetic_without_urls_and_matches_its_provenance():
    assert PROVENANCE["kind"] == "synthetic"
    assert hashlib.sha256(RAW).hexdigest() == PROVENANCE["derived_sha256"]
    assert PROVENANCE["source_blob"] == SOURCE_BLOB
    text = RAW.decode("utf-8")
    assert "://" not in text and "key=" not in text and "?" not in text


def test_conversion_maps_only_existing_fields_and_keeps_the_response_object(legacy):
    converted, bronze_format = normalize_envelope(legacy)
    assert bronze_format == "legacy" and bronze_format in BRONZE_FORMATS
    assert set(converted) == {"ingestion_id", "ingestion_timestamp", "forecast_request", "cities", "summary"}
    assert converted["ingestion_id"] == "20260119_120000"
    assert converted["ingestion_timestamp"] == "2026-01-19T12:00:00.123456Z"
    assert converted["forecast_request"] == {"days": 7}
    assert "status" not in converted  # never invented; derived by the envelope check
    assert converted["summary"] == {"total_cities": 3, "successful": 2, "failed": 1}
    assert [(c["city"], c["status"]) for c in converted["cities"]] == [
        ("Paris", "success"), ("Londres", "success"), ("Bruxelles", "error")]
    for city, entry in zip(converted["cities"], legacy["cities"]):
        assert set(city) == {"city", "data", "status"}
        assert city["data"] is entry["raw_data"]  # exact response, not a copy or a rewrite
    assert bronze_blob_name(converted["ingestion_id"], converted["ingestion_timestamp"]) == SOURCE_BLOB


def test_sensitive_and_technical_legacy_values_are_never_copied(legacy):
    converted, _ = normalize_envelope(legacy)
    text = json.dumps(converted)
    for placeholder in PLACEHOLDERS:
        assert placeholder not in text
    assert "duration_ms" not in text and "http_status" not in text and "error_message" not in text


def test_legacy_archive_goes_through_the_normal_checks():
    result = transform_bronze(RAW, SOURCE_BLOB)
    assert result.status == PROVENANCE["expected_status"]
    assert result.summary["bronze_format"] == PROVENANCE["expected_bronze_format"]
    assert result.observations.num_rows == PROVENANCE["expected_observations"]
    assert result.forecasts.num_rows == PROVENANCE["expected_forecasts"]
    assert _codes(result) == Counter(PROVENANCE["expected_rejection_codes"])
    assert result.ingestion_id == "20260119_120000"
    assert result.collected_at_utc == datetime(2026, 1, 19, 12, 0, 0, 123456, tzinfo=timezone.utc)
    # Provenance is the hash of the original legacy bytes, not of the converted envelope.
    assert result.source_sha256 == hashlib.sha256(RAW).hexdigest()
    assert result.summary["collection"] == {"total_cities": 3, "successful": 2, "failed": 1}
    assert result.summary["cities"]["collection_failed"] == ["Bruxelles"]
    rows = result.observations.to_pylist() + result.forecasts.to_pylist()
    assert {row["city_id"] for row in rows} == {"fr_paris"}
    assert {row["source_blob"] for row in rows} == {SOURCE_BLOB}
    paris = result.observations.to_pylist()[0]
    assert paris["temperature_c"] == 7.2 and paris["source_country"] == "France"
    assert paris["observed_at_utc"] == datetime(2026, 1, 19, 11, 45, tzinfo=timezone.utc)


def test_argentina_londres_is_rejected_and_never_becomes_london():
    result = transform_bronze(RAW, SOURCE_BLOB)
    rejection = next(r for r in result.rejections if r.city_query == "Londres")
    assert rejection.code == "location_mismatch"
    assert "gb_london" not in result.summary["city_tables"]
    assert "gb_london" not in {row["city_id"] for row in result.forecasts.to_pylist()}


def test_paris_response_resolved_elsewhere_is_rejected_too(legacy):
    legacy["cities"][0]["raw_data"]["location"] = deepcopy(legacy["cities"][1]["raw_data"]["location"])
    result = _run(legacy)
    assert result.status == "rejected" and result.summary["reason"] == "no_valid_rows"
    assert result.observations.num_rows == result.forecasts.num_rows == 0
    assert _codes(result) == Counter({"location_mismatch": 2, "collection_failed": 1})


def test_current_envelope_is_returned_unchanged():
    current = json.loads((FIXTURES / "synthetic_minimal.json").read_bytes())
    snapshot = deepcopy(current)
    converted, bronze_format = normalize_envelope(current)
    assert converted is current and converted == snapshot and bronze_format == "current"
    raw = (FIXTURES / "synthetic_minimal.json").read_bytes()
    result = transform_bronze(raw, bronze_blob_name(current["ingestion_id"], current["ingestion_timestamp"]))
    assert result.summary["bronze_format"] == "current"
    assert result.status == "success"


@pytest.mark.parametrize("value", [None, [], "text", 3])
def test_non_envelopes_are_not_legacy(value):
    assert normalize_envelope(value) == (value, "current")


def test_envelope_with_both_identities_is_not_rewritten(legacy):
    legacy["ingestion_id"] = "20260119_120000"
    converted, bronze_format = normalize_envelope(legacy)
    assert converted is legacy and bronze_format == "current"
    assert _codes(_run(legacy)) == Counter({"invalid_timestamp": 1})


def _drop(mapping, key):
    mapping.pop(key)


@pytest.mark.parametrize("modify,code", [
    (lambda d: _drop(d["ingestion_metadata"], "ingestion_id"), "invalid_ingestion_id"),
    (lambda d: d["ingestion_metadata"].update(ingestion_id="20260119-120000"), "invalid_ingestion_id"),
    (lambda d: d.update(ingestion_metadata="20260119_120000"), "invalid_ingestion_id"),
    (lambda d: _drop(d["ingestion_metadata"], "ingestion_timestamp"), "invalid_timestamp"),
    (lambda d: d["ingestion_metadata"].update(ingestion_timestamp="2026-01-19"), "invalid_timestamp"),
    (lambda d: d["ingestion_metadata"].update(ingestion_timestamp="2026-01-19T13:00:00Z"), "source_path_mismatch"),
    (lambda d: _drop(d, "cities"), "invalid_envelope"),
    (lambda d: d.update(cities={}), "invalid_envelope"),
    (lambda d: d["cities"].__setitem__(0, {"raw_data": d["cities"][0]["raw_data"]}), "invalid_envelope"),
    (lambda d: _drop(d["cities"][0]["metadata"], "city"), "invalid_envelope"),
    (lambda d: _drop(d["cities"][0]["metadata"], "http_status"), "invalid_envelope"),
    (lambda d: d["cities"][0]["metadata"].update(http_status=True), "invalid_envelope"),
    (lambda d: d["cities"][0]["metadata"].update(http_status="200"), "invalid_envelope"),
    (lambda d: d["cities"][0].update(raw_data=None), "invalid_envelope"),
    (lambda d: d["cities"][0]["metadata"].update(error="HTTP 500"), "invalid_envelope"),
    (lambda d: d["cities"][2].update(raw_data={"location": {}}), "invalid_envelope"),
    (lambda d: d["cities"][2]["metadata"].update(http_status=200), "invalid_envelope"),
    (lambda d: d["ingestion_metadata"].update(cities_requested=["Paris", "London", "Bruxelles"]), "invalid_envelope"),
    (lambda d: d["ingestion_metadata"].update(cities_requested="Paris"), "invalid_envelope"),
    (lambda d: _drop(d, "summary"), "summary_mismatch"),
    (lambda d: _drop(d["summary"], "total_cities_failed"), "summary_mismatch"),
    (lambda d: d["summary"].update(total_cities_retrieved=3), "summary_mismatch"),
    (lambda d: d["summary"].update(total_cities_requested=3.0), "summary_mismatch"),
])
def test_missing_or_inconsistent_legacy_pieces_keep_the_usual_codes(legacy, modify, code):
    modify(legacy)
    result = _run(legacy)
    assert result.status == "rejected"
    assert result.observations.num_rows == result.forecasts.num_rows == 0
    assert [r.code for r in result.rejections] == [code]
    assert result.summary["bronze_format"] == "legacy"


def test_missing_requested_list_is_not_required(legacy):
    legacy["ingestion_metadata"].pop("cities_requested")
    assert _run(legacy).observations.num_rows == 1


def test_trustworthy_identity_survives_a_rejected_legacy_envelope(legacy):
    legacy.pop("summary")
    result = _run(legacy)
    assert result.ingestion_id == "20260119_120000"
    assert result.collected_at_utc == datetime(2026, 1, 19, 12, 0, 0, 123456, tzinfo=timezone.utc)


def test_every_legacy_path_has_a_decision(legacy):
    assert legacy_unknown_paths(legacy) == []
    observed = {path for path in _paths(legacy) if not path.startswith("cities[].raw_data.")}
    observed.discard("cities[].raw_data")  # null for a failed city
    assert observed <= set(LEGACY_FIELD_DECISIONS)
    assert all(decision.split(":")[0] in ("mapped", "control", "excluded")
               for decision in LEGACY_FIELD_DECISIONS.values())


def test_unknown_legacy_paths_are_reported_without_values(legacy):
    legacy["ingestion_metadata"]["api_key"] = "synthetic-secret-value"
    legacy["cities"][0]["metadata"]["retry"] = {"count": 2}
    legacy["extra"] = [1]
    found = legacy_unknown_paths(legacy)
    assert found == ["cities[].metadata.retry", "extra", "ingestion_metadata.api_key"]
    assert "synthetic-secret-value" not in json.dumps(found)
    converted, _ = normalize_envelope(legacy)
    assert "synthetic-secret-value" not in json.dumps(converted)
    assert legacy_unknown_paths({"ingestion_id": "20260119_120000"}) == []
