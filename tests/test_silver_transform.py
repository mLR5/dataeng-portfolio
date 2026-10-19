"""Value-level Silver checks with explicitly synthetic source envelopes.

No request, Azure account, Delta writer or wall clock is involved here. The last
test also covers real captured values in S1's explicitly reduced Bronze samples.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pytest

from shared.claim_check import bronze_blob_name
from shared.silver_contract import FORECAST_SCHEMA, FORECAST_TABLE, OBSERVATION_SCHEMA, OBSERVATION_TABLE
from shared.silver_transform import transform_bronze


UTC = timezone.utc
COLLECTED = datetime(2026, 10, 18, 20, 15, tzinfo=UTC)
TARGET = datetime(2026, 10, 18, 20, tzinfo=UTC)


def _metrics(temperature=12.5):
    return {"temp_c": temperature, "feelslike_c": 11.8, "humidity": 75,
            "pressure_mb": 1012.0, "wind_kph": 9.0, "wind_degree": 240,
            "precip_mm": 0.3, "cloud": 60, "condition": {"code": 1003, "text": "Partiellement nuageux"},
            "is_day": 0}


def _city(query, country, lat, lon, zone):
    hours = []
    for hour in (20, 21):
        target = TARGET.replace(hour=hour)
        hours.append({**_metrics(hour - 8.0), "time_epoch": int(target.timestamp()),
                      "time": target.astimezone(ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M"),
                      "chance_of_rain": "25", "chance_of_snow": 0})
    return {"city": query, "status": "success", "data": {
        "location": {"name": query, "country": country, "lat": lat, "lon": lon, "tz_id": zone},
        "current": {**_metrics(), "last_updated_epoch": int(TARGET.timestamp()), "last_updated": "2026-10-18 22:00"},
        "forecast": {"forecastday": [{"date": "2026-10-18", "hour": hours}]},
    }}


@pytest.fixture
def envelope():
    return {"ingestion_id": "20261018_201500", "ingestion_timestamp": COLLECTED.isoformat(), "status": "success",
            "cities": [_city("Paris", "France", 48.85, 2.35, "Europe/Paris"),
                       _city("Londres", "United Kingdom", 51.5, -0.12, "Europe/London"),
                       _city("Bruxelles", "Belgium", 50.85, 4.35, "Europe/Brussels")],
            "summary": {"total_cities": 3, "successful": 3, "failed": 0}}


def _run(data, *, raw=None, source_blob=None):
    if raw is None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    if source_blob is None:
        source_blob = bronze_blob_name(data["ingestion_id"], data["ingestion_timestamp"])
    return transform_bronze(raw, source_blob)


def _hours(envelope, city=0):
    return envelope["cities"][city]["data"]["forecast"]["forecastday"][0]["hour"]


def test_nominal_exact_values_schemas_utc_units_and_lineage(envelope):
    raw = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
    result = _run(envelope, raw=raw)
    assert result.status == "success"
    assert result.observations.schema.equals(OBSERVATION_SCHEMA, check_metadata=True)
    assert result.forecasts.schema.equals(FORECAST_SCHEMA, check_metadata=True)
    assert result.observations.num_rows == 3
    assert result.forecasts.num_rows == 6
    paris = next(row for row in result.observations.to_pylist() if row["city_id"] == "fr_paris")
    assert paris["temperature_c"] == 12.5
    assert paris["humidity_pct"] == 75
    assert paris["precipitation_mm"] == 0.3
    assert paris["wind_kph"] == 9.0
    assert paris["condition_code"] == 1003
    assert paris["condition_text"] == "Partiellement nuageux"
    assert paris["is_day"] is False
    assert paris["observed_at_utc"] == TARGET
    assert paris["collected_at_utc"] == COLLECTED
    assert paris["ingestion_date"] == COLLECTED.date()
    assert paris["source_sha256"] == hashlib.sha256(raw).hexdigest() == result.source_sha256
    assert paris["source_blob"] == result.source_blob
    forecasts = sorted((row for row in result.forecasts.to_pylist() if row["city_id"] == "fr_paris"),
                       key=lambda row: row["target_at_utc"])
    assert [(row["lead_time_hours"], row["target_is_past_at_collection"]) for row in forecasts] == [(-0.25, True), (0.75, False)]
    assert forecasts[0]["chance_of_rain_pct"] == 25
    assert result.summary["horizons"]["fr_paris"]["received_days"] == 1
    assert result.summary["horizons"]["fr_paris"]["received_hours"] == 2
    assert result.summary["tables"][FORECAST_TABLE]["valid_rows"] == 6


def test_repeated_exact_bytes_have_identical_results_and_sha(envelope):
    first, second = _run(envelope), _run(envelope)
    assert first.observations.equals(second.observations)
    assert first.forecasts.equals(second.forecasts)
    assert first.rejections == second.rejections
    assert first.summary == second.summary
    assert first.source_sha256 == second.source_sha256
    # JSON whitespace is part of provenance, not normalized away by hashing.
    assert _run(envelope, raw=json.dumps(envelope, indent=2).encode()).source_sha256 != first.source_sha256


def test_forecast_request_metadata_is_compatible_with_silver(envelope):
    envelope["forecast_request"] = {"days": 3}
    result = _run(envelope)
    assert result.status == "success"
    assert result.observations.num_rows == 3
    assert result.forecasts.num_rows == 6
    # Requesting three days does not create missing forecasts in this one-day fixture.
    assert result.summary["horizons"]["gb_london"]["received_days"] == 1


def test_forecast_revisions_and_repeated_observations_remain_distinct_snapshots(envelope):
    first = _run(envelope)
    second_envelope = deepcopy(envelope)
    second_envelope["ingestion_id"] = "20261018_211500"
    second_envelope["ingestion_timestamp"] = "2026-10-18T21:15:00Z"
    _hours(second_envelope)[0]["temp_c"] = 19.0
    second = _run(second_envelope)
    observations = pa.concat_tables([first.observations, second.observations]).to_pylist()
    assert len([row for row in observations if row["city_id"] == "fr_paris" and row["observed_at_utc"] == TARGET]) == 2
    forecasts = pa.concat_tables([first.forecasts, second.forecasts]).to_pylist()
    snapshots = [row for row in forecasts if row["city_id"] == "fr_paris" and row["target_at_utc"] == TARGET]
    assert {(row["ingestion_id"], row["temperature_c"]) for row in snapshots} == {
        ("20261018_201500", 12.0), ("20261018_211500", 19.0)}


def test_partial_collection_does_not_invent_failed_city_rows(envelope):
    envelope["cities"][1].update(status="error", data=None)
    envelope["summary"].update(successful=2, failed=1)
    envelope["status"] = "partial"
    result = _run(envelope)
    assert result.status == "partial"
    assert result.observations.num_rows == 2 and result.forecasts.num_rows == 4
    assert "gb_london" not in result.observations.column("city_id").to_pylist()
    assert result.summary["cities"]["collection_failed"] == ["Londres"]
    assert result.rejections[0].code == "collection_failed"


def test_total_collection_failure_is_explicit_empty_with_schemas(envelope):
    for entry in envelope["cities"]:
        entry.update(status="error", data=None)
    envelope["summary"].update(successful=0, failed=3)
    envelope["status"] = "failed"
    result = _run(envelope)
    assert result.status == "empty" and result.summary["reason"] == "collection_failed"
    assert result.observations.num_rows == result.forecasts.num_rows == 0
    assert result.observations.schema == OBSERVATION_SCHEMA and result.forecasts.schema == FORECAST_SCHEMA
    assert len(result.rejections) == 3


def test_invalid_observation_does_not_drop_valid_forecasts_or_invent_observation(envelope):
    for entry in envelope["cities"]:
        entry["data"]["current"] = None
    result = _run(envelope)
    assert result.status == "partial"
    assert result.observations.num_rows == 0 and result.forecasts.num_rows == 6
    assert result.summary["tables"][OBSERVATION_TABLE]["rejected_rows"] == 3


def test_all_source_rows_invalid_is_rejected_rather_than_success_or_empty(envelope):
    for entry in envelope["cities"]:
        entry["data"]["current"]["temp_c"] = None
        for hour in entry["data"]["forecast"]["forecastday"][0]["hour"]:
            hour["temp_c"] = None
    result = _run(envelope)
    assert result.status == "rejected" and result.summary["reason"] == "no_valid_rows"
    assert result.summary["tables"][FORECAST_TABLE]["rejected_rows"] == 6


def test_exact_internal_duplicates_removed_without_collapsing_other_ingestions(envelope):
    _hours(envelope).append(deepcopy(_hours(envelope)[0]))
    result = _run(envelope)
    assert result.status == "success"
    assert result.forecasts.num_rows == 6
    assert result.summary["tables"][FORECAST_TABLE]["input_rows"] == 7
    assert result.summary["tables"][FORECAST_TABLE]["duplicates_removed"] == 1
    assert result.summary["horizons"]["fr_paris"]["received_hours"] == 3


def test_conflicting_key_rejects_entire_group_and_preserves_other_hours(envelope):
    conflicting = deepcopy(_hours(envelope)[0])
    conflicting["temp_c"] = 99.0
    _hours(envelope).append(conflicting)
    result = _run(envelope)
    assert result.status == "partial" and result.forecasts.num_rows == 5
    assert not any(row["city_id"] == "fr_paris" and row["target_at_utc"] == TARGET for row in result.forecasts.to_pylist())
    assert result.summary["tables"][FORECAST_TABLE]["conflicting_rows"] == 2
    assert [r.code for r in result.rejections] == ["duplicate_conflict", "duplicate_conflict"]


def test_duplicate_city_entry_deduplicates_observation_and_forecasts(envelope):
    envelope["cities"].append(deepcopy(envelope["cities"][0]))
    envelope["summary"].update(total_cities=4, successful=4)
    result = _run(envelope)
    assert result.observations.num_rows == 3 and result.forecasts.num_rows == 6
    assert result.summary["tables"][OBSERVATION_TABLE]["duplicates_removed"] == 1
    assert result.summary["tables"][FORECAST_TABLE]["duplicates_removed"] == 2


def test_conflicting_duplicate_observation_does_not_drop_deduplicated_forecasts(envelope):
    duplicate = deepcopy(envelope["cities"][0])
    duplicate["data"]["current"]["temp_c"] = 20.0
    envelope["cities"].append(duplicate)
    envelope["summary"].update(total_cities=4, successful=4)
    result = _run(envelope)
    assert result.observations.num_rows == 2 and result.forecasts.num_rows == 6
    assert result.summary["tables"][OBSERVATION_TABLE]["conflicting_rows"] == 2
    assert result.summary["city_tables"]["fr_paris"][OBSERVATION_TABLE]["rejected_rows"] == 2
    for table_counts in result.summary["tables"].values():
        assert table_counts["input_rows"] == sum(table_counts[key] for key in (
            "valid_rows", "rejected_rows", "duplicates_removed"))


@pytest.mark.parametrize("timestamp", ["2026-10-18T20:15:00", "2026-10-18T22:15:00+02:00", "2026-10-18T20:15:00Z"])
def test_legacy_naive_and_offset_collection_normalize_to_same_utc_partition(envelope, timestamp):
    envelope["ingestion_timestamp"] = timestamp
    result = _run(envelope)
    assert result.status == "success" and result.collected_at_utc == COLLECTED
    assert "/2026/10/18/20/" in result.source_blob


def test_old_envelope_without_top_level_status_is_supported(envelope):
    envelope.pop("status")
    assert _run(envelope).status == "success"


def test_requested_cities_are_historical_not_current_configuration(envelope):
    envelope["cities"] = envelope["cities"][:1]
    envelope["summary"].update(total_cities=1, successful=1)
    result = _run(envelope)
    assert result.status == "success"
    assert result.summary["cities"]["requested"] == ["Paris"]
    assert result.observations.num_rows == 1


@pytest.mark.parametrize("modify,code", [
    (lambda d: d["summary"].update(successful=2), "summary_mismatch"),
    (lambda d: d.update(status="failed"), "summary_mismatch"),
    (lambda d: d.update(ingestion_timestamp="2026-10-18"), "invalid_timestamp"),
    (lambda d: d.update(ingestion_id="bad"), "invalid_ingestion_id"),
])
def test_invalid_envelope_is_terminal_without_valid_looking_rows(envelope, modify, code):
    source_blob = bronze_blob_name(envelope["ingestion_id"], envelope["ingestion_timestamp"])
    modify(envelope)
    result = _run(envelope, source_blob=source_blob)
    assert result.status == "rejected"
    assert result.observations.num_rows == result.forecasts.num_rows == 0
    assert result.rejections[0].code == code


def test_wrong_partition_rejected_before_transform(envelope):
    path = bronze_blob_name(envelope["ingestion_id"], envelope["ingestion_timestamp"]).replace("/20/", "/21/")
    result = _run(envelope, source_blob=path)
    assert result.status == "rejected" and result.rejections[0].code == "source_path_mismatch"


@pytest.mark.parametrize("raw", [b"{", b"[]", b"null", b'{"x":1,"x":2}', b'{"x":NaN}', b"\xff"])
def test_malformed_payload_keeps_exact_hash_and_source_identity(envelope, raw):
    result = _run(envelope, raw=raw)
    assert result.status == "rejected"
    assert result.ingestion_id == envelope["ingestion_id"]
    assert result.source_sha256 == hashlib.sha256(raw).hexdigest()
    assert result.observations.schema == OBSERVATION_SCHEMA
    json.dumps(result.summary, allow_nan=False)
    json.dumps([r.to_dict() for r in result.rejections], allow_nan=False)


@pytest.mark.parametrize("field,value", [("humidity", 101), ("cloud", -1), ("wind_degree", 361),
                                        ("wind_kph", -0.1), ("precip_mm", -1), ("chance_of_rain", "100.5")])
def test_one_bad_hour_is_quarantined_without_losing_other_hours(envelope, field, value):
    _hours(envelope)[0][field] = value
    result = _run(envelope)
    assert result.status == "partial" and result.forecasts.num_rows == 5
    assert result.rejections[0].field == field
    assert result.rejections[0].position.endswith("hour[0]")


def test_missing_optional_metric_is_null_required_temperature_is_not_zero(envelope):
    current = envelope["cities"][0]["data"]["current"]
    current.pop("humidity")
    current["wind_kph"] = None
    current["pressure_mb"] = None
    current["temp_c"] = "-2.75"
    result = _run(envelope)
    paris = next(row for row in result.observations.to_pylist() if row["city_id"] == "fr_paris")
    assert paris["temperature_c"] == -2.75
    assert paris["humidity_pct"] is None and paris["wind_kph"] is None
    assert paris["pressure_mb"] is None


@pytest.mark.parametrize("section", ["current", "hour"])
def test_zero_pressure_rejects_only_its_source_row(envelope, section):
    if section == "current":
        envelope["cities"][0]["data"]["current"]["pressure_mb"] = 0
    else:
        _hours(envelope)[0]["pressure_mb"] = 0
    result = _run(envelope)
    assert result.status == "partial"
    assert result.observations.num_rows == (2 if section == "current" else 3)
    assert result.forecasts.num_rows == (6 if section == "current" else 5)
    assert result.rejections[0].code == "out_of_range"
    assert result.rejections[0].field == "pressure_mb"


@pytest.mark.parametrize("modify,code", [
    (lambda city: city.update(city="Atlantis"), "unknown_city"),
    (lambda city: city["data"]["location"].update(lat=33.66, lon=-95.55), "location_mismatch"),
    (lambda city: city["data"]["location"].update(tz_id="America/Chicago"), "location_mismatch"),
])
def test_unregistered_or_wrong_resolved_location_never_receives_paris_id(envelope, modify, code):
    modify(envelope["cities"][0])
    result = _run(envelope)
    assert result.status == "partial"
    assert result.observations.num_rows == 2 and result.forecasts.num_rows == 4
    assert result.rejections[0].code == code


def test_forecast_day_must_match_target_local_date(envelope):
    envelope["cities"][0]["data"]["forecast"]["forecastday"][0]["date"] = "2026-10-19"
    result = _run(envelope)
    assert result.forecasts.num_rows == 4 and result.observations.num_rows == 3
    assert {r.code for r in result.rejections} == {"invalid_date"}


def test_unrepresentable_forecast_day_is_rejected_without_technical_retry(envelope):
    envelope["cities"][0]["data"]["forecast"]["forecastday"][0]["date"] = "9999-12-31"
    result = _run(envelope)
    assert result.status == "partial" and result.forecasts.num_rows == 4
    assert {r.code for r in result.rejections} == {"invalid_date"}


def test_target_local_date_does_not_follow_utc_collection_date(envelope):
    target = datetime(2026, 10, 18, 22, tzinfo=UTC)
    day = envelope["cities"][0]["data"]["forecast"]["forecastday"][0]
    day["date"] = "2026-10-19"
    day["hour"] = [{**_metrics(), "time_epoch": int(target.timestamp()), "time": "2026-10-19 00:00"}]
    result = _run(envelope)
    row = next(row for row in result.forecasts.to_pylist() if row["city_id"] == "fr_paris")
    assert row["ingestion_date"].isoformat() == "2026-10-18"
    assert row["target_local_date"].isoformat() == "2026-10-19"
    assert row["target_at_utc"] == target


def test_missing_forecast_section_is_partial_without_dropping_observation(envelope):
    envelope["cities"][0]["data"].pop("forecast")
    result = _run(envelope)
    assert result.status == "partial" and result.observations.num_rows == 3
    assert result.forecasts.num_rows == 4
    assert result.rejections[0].code == "invalid_forecast"


def test_invalid_unicode_text_is_a_data_rejection_instead_of_arrow_failure(envelope):
    _hours(envelope)[0]["condition"]["text"] = "\ud800"
    raw = json.dumps(envelope, ensure_ascii=True).encode()
    result = _run(envelope, raw=raw)
    assert result.status == "partial" and result.forecasts.num_rows == 5
    assert result.rejections[0].code == "invalid_type"
    assert result.rejections[0].field == "condition.text"


def test_invalid_unicode_city_label_returns_fixed_utf8_safe_diagnostic(envelope):
    envelope["cities"][0]["city"] = "bad\ud800"
    result = _run(envelope, raw=json.dumps(envelope, ensure_ascii=True).encode())
    assert result.status == "rejected"
    assert result.observations.num_rows == result.forecasts.num_rows == 0
    assert result.rejections[0].code == "invalid_envelope"
    assert result.rejections[0].city_query is None
    assert result.rejections[0].message == "Requested city labels must be valid Unicode"
    json.dumps([r.to_dict() for r in result.rejections], ensure_ascii=False).encode("utf-8")
    json.dumps(result.summary, ensure_ascii=False).encode("utf-8")


def test_invalid_unicode_provider_key_cannot_leak_into_summary(envelope):
    _hours(envelope)[0]["bad\ud800"] = 1
    result = _run(envelope, raw=json.dumps(envelope, ensure_ascii=True).encode())
    assert result.status == "rejected" and result.rejections[0].code == "invalid_json"
    json.dumps(result.summary, ensure_ascii=False).encode("utf-8")


def test_new_provider_metric_is_ignored_but_reported_without_its_value(envelope):
    _hours(envelope)[0]["new_metric"] = "sensitive-unneeded-value"
    result = _run(envelope)
    assert result.status == "success"
    assert result.summary["ignored_fields"] == {"hour": ["new_metric"]}
    assert "sensitive-unneeded-value" not in json.dumps(result.summary)


@pytest.mark.parametrize("path", ["https://account.example/blob?sig=secret", "weather/../blob", "/absolute/blob"])
def test_caller_cannot_pass_a_signed_url_as_provenance(path):
    with pytest.raises(ValueError, match="relative blob path"):
        transform_bronze(b"{}", path)


@pytest.mark.parametrize("day,expected_hours", [("2026-03-29", 23), ("2026-10-25", 25)])
def test_dst_daily_coverage_uses_23_and_25_hour_days(envelope, day, expected_hours):
    city = envelope["cities"][0]
    zone = ZoneInfo("Europe/Paris")
    start = datetime.fromisoformat(day).replace(tzinfo=zone).astimezone(UTC)
    from datetime import timedelta
    hours = []
    for i in range(expected_hours):
        target = start + timedelta(hours=i)
        hours.append({**_metrics(), "time_epoch": int(target.timestamp()),
                      "time": target.astimezone(zone).strftime("%Y-%m-%d %H:%M")})
    city["data"]["forecast"]["forecastday"] = [{"date": day, "hour": hours}]
    result = _run(envelope)
    rows = [row for row in result.forecasts.to_pylist() if row["city_id"] == "fr_paris"]
    assert len(rows) == expected_hours
    assert len({row["target_at_utc"] for row in rows}) == expected_hours
    assert result.summary["horizons"]["fr_paris"]["daily_coverage"] == [{
        "local_date": day, "expected_hour_slots": expected_hours,
        "valid_hour_slots": expected_hours, "missing_hour_slots": 0}]


@pytest.mark.parametrize("capture_date", ["20261017", "20261018"])
def test_derived_real_bronze_keeps_values_and_rejects_argentina_londres(capture_date):
    directory = Path(__file__).parent / "fixtures" / "weatherapi"
    stem = f"bronze_success_{capture_date}_derived_real"
    provenance = json.loads((directory / f"{stem}.provenance.json").read_text())
    raw = (directory / f"{stem}.json").read_bytes()
    source = json.loads(raw)
    assert provenance["kind"] == "derived_from_real_bronze"
    assert hashlib.sha256(raw).hexdigest() == provenance["derived_sha256"]
    assert provenance["derived_sha256"] != provenance["original_source_sha256"]
    assert source["cities"][1]["city"] == "Londres"
    assert source["cities"][1]["data"]["location"]["tz_id"] == "America/Argentina/Catamarca"
    result = transform_bronze(raw, provenance["original_source_blob"])
    assert result.status == "partial" and source["status"] == "success"
    assert result.observations.num_rows == 2
    assert result.forecasts.num_rows == 12  # The fixture retains two hours/day, not all 72.
    assert result.source_sha256 == provenance["derived_sha256"]
    assert result.collected_at_utc == datetime.fromisoformat(source["ingestion_timestamp"])
    assert len(result.rejections) == 1
    assert result.rejections[0].code == "location_mismatch"
    assert result.rejections[0].city_query == "Londres"
    expected_ids = {"Paris": "fr_paris", "Bruxelles": "be_brussels"}
    observations = result.observations.to_pylist()
    forecasts = result.forecasts.to_pylist()
    assert {row["city_id"] for row in observations} == {"fr_paris", "be_brussels"}
    assert {row["city_id"] for row in forecasts} == {"fr_paris", "be_brussels"}
    for entry in (source["cities"][0], source["cities"][2]):
        city_id = expected_ids[entry["city"]]
        observation = next(row for row in observations if row["city_id"] == city_id)
        assert observation["temperature_c"] == entry["data"]["current"]["temp_c"]
        assert observation["observed_at_utc"] == datetime.fromtimestamp(entry["data"]["current"]["last_updated_epoch"], UTC)
        assert observation["condition_code"] == entry["data"]["current"]["condition"]["code"]
        assert result.summary["horizons"][city_id]["received_days"] == 3
        assert result.summary["horizons"][city_id]["received_hours"] == 6
        for day in entry["data"]["forecast"]["forecastday"]:
            for hour in day["hour"]:
                target = datetime.fromtimestamp(hour["time_epoch"], UTC)
                row = next(row for row in forecasts if row["city_id"] == city_id and row["target_at_utc"] == target)
                assert row["temperature_c"] == hour["temp_c"]
                assert row["humidity_pct"] == hour["humidity"]
                assert row["precipitation_mm"] == hour["precip_mm"]
                assert row["target_local_date"].isoformat() == day["date"]
                assert row["source_sha256"] == provenance["derived_sha256"]
