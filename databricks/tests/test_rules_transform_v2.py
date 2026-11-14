"""Silver v2 transformation rules: field-level rejection, new measures, daily rows.

Synthetic envelopes come from test_rules_transform helpers; real values come from
the two Bronze fixtures derived from real captures (conftest.real_sources).
"""
from copy import deepcopy
from datetime import date, datetime, timezone
import json

import pytest

import silver_rules
from conftest import real_sources
from silver_rules import (FORECAST_DAILY_KEYS, FORECAST_DAILY_SCHEMA, FORECAST_DAILY_TABLE, FORECAST_TABLE,
                          OBSERVATION_TABLE, QualityError, transform_bronze, validate_transform_result)
from test_rules_transform import COLLECTED, TARGET, _city, _hours, _run

UTC = timezone.utc


@pytest.fixture
def envelope():
    return {"ingestion_id": "20261018_201500", "ingestion_timestamp": COLLECTED.isoformat(), "status": "success",
            "cities": [_city("Paris", "France", 48.85, 2.35, "Europe/Paris"),
                       _city("Londres", "United Kingdom", 51.5, -0.12, "Europe/London"),
                       _city("Bruxelles", "Belgium", 50.85, 4.35, "Europe/Brussels")],
            "summary": {"total_cities": 3, "successful": 3, "failed": 0}}


def _paris_hour(result, target=TARGET):
    return next(row for row in result.forecasts.to_pylist()
                if row["city_id"] == "fr_paris" and row["target_at_utc"] == target)


def _paris_observation(result):
    return next(row for row in result.observations.to_pylist() if row["city_id"] == "fr_paris")


def _assert_publishable(result):
    validate_transform_result(result, ingestion_id=result.ingestion_id, source_blob=result.source_blob,
                              source_sha256=result.source_sha256)


@pytest.mark.parametrize("raw", [55.80025, "55.80025", 0.5, 100, 0])
def test_decimal_probability_is_kept_exactly(envelope, raw):
    # v1 rejected the whole hour with invalid_type (integer required).
    _hours(envelope)[0]["chance_of_rain"] = raw
    _hours(envelope)[0]["chance_of_snow"] = raw
    result = _run(envelope)
    assert result.status == "success" and result.rejections == ()
    row = _paris_hour(result)
    assert row["chance_of_rain_pct"] == row["chance_of_snow_pct"] == float(raw)


@pytest.mark.parametrize("key,column,value,code", [
    ("uv", "uv_index", -0.1, "out_of_range"),
    ("uv", "uv_index", True, "invalid_type"),
    ("gust_kph", "wind_gust_kph", -1, "out_of_range"),
    ("vis_km", "visibility_km", "10,0", "invalid_type"),
    ("snow_cm", "snowfall_cm", -0.2, "out_of_range"),
    ("dewpoint_c", "dew_point_c", "NaN", "invalid_type"),
    ("heatindex_c", "heat_index_c", [], "invalid_type"),
    ("windchill_c", "wind_chill_c", False, "invalid_type"),
    ("wetbulb_c", "wet_bulb_c", "1e999", "invalid_type"),
    ("feelslike_c", "feels_like_c", "abc", "invalid_type"),
    ("is_day", "is_day", 2, "out_of_range"),
    ("chance_of_snow", "chance_of_snow_pct", -5, "out_of_range"),
    ("humidity", "humidity_pct", 50.5, "invalid_type"),
])
def test_invalid_optional_hour_measure_is_nulled_and_row_kept(envelope, key, column, value, code):
    _hours(envelope)[0].update({"uv": 3, "gust_kph": 20.5, "vis_km": 10.0, "snow_cm": 0.0, "dewpoint_c": 5.5,
                                "heatindex_c": 11.8, "windchill_c": 10.2, "wetbulb_c": 8.6})
    _hours(envelope)[0][key] = value
    result = _run(envelope)
    assert result.status == "success"
    assert result.forecasts.num_rows == 6 and result.observations.num_rows == 3
    assert [(r.table, r.code, r.city_query, r.field, r.scope) for r in result.rejections] == [
        (FORECAST_TABLE, code, "Paris", key, "field")]
    assert result.rejections[0].position == "cities[0].forecast.forecastday[0].hour[0]"
    row = _paris_hour(result)
    assert row[column] is None
    # The other measures of the same row are untouched.
    kept = {"uv_index": 3.0, "wind_gust_kph": 20.5, "visibility_km": 10.0, "snowfall_cm": 0.0,
            "dew_point_c": 5.5, "heat_index_c": 11.8, "wind_chill_c": 10.2, "wet_bulb_c": 8.6,
            "temperature_c": 12.0, "humidity_pct": 75, "pressure_mb": 1012.0}
    assert {k: row[k] for k in kept if k != column} == {k: v for k, v in kept.items() if k != column}
    assert result.summary["nulled_metrics"] == 1
    assert result.summary["tables"][FORECAST_TABLE]["nulled_metrics"] == 1
    assert result.summary["city_tables"]["fr_paris"][FORECAST_TABLE]["nulled_metrics"] == 1
    assert result.summary["tables"][FORECAST_TABLE]["rejected_rows"] == 0
    _assert_publishable(result)


@pytest.mark.parametrize("key,column", [("uv", "uv_index"), ("gust_kph", "wind_gust_kph"),
                                        ("vis_km", "visibility_km")])
def test_invalid_optional_observation_measure_is_nulled_and_row_kept(envelope, key, column):
    envelope["cities"][0]["data"]["current"][key] = -3
    result = _run(envelope)
    assert result.status == "success" and result.observations.num_rows == 3
    assert [(r.table, r.code, r.field, r.scope, r.position) for r in result.rejections] == [
        (OBSERVATION_TABLE, "out_of_range", key, "field", "cities[0].current")]
    assert _paris_observation(result)[column] is None
    assert result.summary["tables"][OBSERVATION_TABLE]["nulled_metrics"] == 1


def test_non_object_condition_nulls_code_and_text_once(envelope):
    envelope["cities"][0]["data"]["current"]["condition"] = "Sunny"
    result = _run(envelope)
    assert result.status == "success"
    assert [(r.code, r.field, r.scope) for r in result.rejections] == [("invalid_type", "condition", "field")]
    observation = _paris_observation(result)
    assert observation["condition_code"] is None and observation["condition_text"] is None


@pytest.mark.parametrize("value,code", [(None, "missing_field"), ("warm", "invalid_type"), (True, "invalid_type")])
def test_invalid_temperature_still_rejects_its_row(envelope, value, code):
    _hours(envelope)[0]["temp_c"] = value
    _hours(envelope)[0]["uv"] = -1  # A nulled measure of a rejected row is not reported.
    result = _run(envelope)
    assert result.status == "partial" and result.forecasts.num_rows == 5
    assert [(r.table, r.code, r.field, r.scope) for r in result.rejections] == [
        (FORECAST_TABLE, code, "temp_c", "row")]
    assert result.summary["nulled_metrics"] == 0
    _assert_publishable(result)


def test_conflicting_duplicate_reports_row_rejection_not_nulled_measure(envelope):
    _hours(envelope)[0]["uv"] = -1
    conflicting = deepcopy(_hours(envelope)[0])
    conflicting["temp_c"] = 99.0
    _hours(envelope).append(conflicting)
    result = _run(envelope)
    assert result.status == "partial" and result.forecasts.num_rows == 5
    assert {(r.code, r.scope) for r in result.rejections} == {("duplicate_conflict", "row")}
    assert result.summary["nulled_metrics"] == 0


def test_field_and_row_rejections_together_are_partial_and_ordered(envelope):
    _hours(envelope)[0]["uv"] = -1
    _hours(envelope, 1)[0]["temp_c"] = None
    result = _run(envelope)
    assert result.status == "partial"
    assert [(r.scope, r.field) for r in result.rejections] == [("row", "temp_c"), ("field", "uv")]


def test_new_measures_absent_are_null_without_rejection(envelope):
    result = _run(envelope)
    row = _paris_hour(result)
    for column in ("uv_index", "wind_gust_kph", "visibility_km", "dew_point_c", "snowfall_cm",
                   "heat_index_c", "wind_chill_c", "wet_bulb_c"):
        assert row[column] is None
    assert result.status == "success" and result.rejections == ()


@pytest.mark.parametrize("index", [0, 1])
def test_new_measures_are_extracted_from_derived_real_bronze(index):
    source_blob, raw = real_sources()[index]
    source = json.loads(raw)
    result = transform_bronze(raw, source_blob)
    assert result.summary["nulled_metrics"] == 0
    assert all(r.scope == "row" for r in result.rejections)
    observations = {row["city_id"]: row for row in result.observations.to_pylist()}
    forecasts = {(row["city_id"], row["target_at_utc"]): row for row in result.forecasts.to_pylist()}
    for entry, city_id in ((source["cities"][0], "fr_paris"), (source["cities"][2], "be_brussels")):
        current = entry["data"]["current"]
        observation = observations[city_id]
        assert observation["uv_index"] == current["uv"]
        assert observation["wind_gust_kph"] == current["gust_kph"]
        assert observation["visibility_km"] == current["vis_km"]
        for day in entry["data"]["forecast"]["forecastday"]:
            for hour in day["hour"]:
                row = forecasts[(city_id, datetime.fromtimestamp(hour["time_epoch"], UTC))]
                # Bronze uv is int or float; Silver stores a DOUBLE.
                assert row["uv_index"] == float(hour["uv"]) and isinstance(row["uv_index"], float)
                assert row["wind_gust_kph"] == hour["gust_kph"]
                assert row["visibility_km"] == hour["vis_km"]
                assert row["dew_point_c"] == hour["dewpoint_c"]
                assert row["snowfall_cm"] == hour["snow_cm"]
                assert row["heat_index_c"] == hour["heatindex_c"]
                assert row["wind_chill_c"] == hour["windchill_c"]
                assert row["wet_bulb_c"] == hour["wetbulb_c"]
                assert row["chance_of_rain_pct"] == hour["chance_of_rain"]


@pytest.mark.parametrize("index", [0, 1])
def test_daily_rows_one_per_forecastday_for_valid_cities(index):
    source_blob, raw = real_sources()[index]
    source = json.loads(raw)
    result = transform_bronze(raw, source_blob)
    assert result.forecasts_daily.schema.equals(FORECAST_DAILY_SCHEMA, check_metadata=True)
    daily = result.forecasts_daily.to_pylist()
    assert len(daily) == 6  # Argentina "Londres" is rejected by the city registry.
    for entry, city_id in ((source["cities"][0], "fr_paris"), (source["cities"][2], "be_brussels")):
        rows = sorted((row for row in daily if row["city_id"] == city_id), key=lambda row: row["target_local_date"])
        assert [row["target_local_date"] for row in rows] == [
            date.fromisoformat(day["date"]) for day in entry["data"]["forecast"]["forecastday"]]
        assert [row["lead_days"] for row in rows] == [0, 1, 2]
        for row in rows:
            assert row["ingestion_id"] == source["ingestion_id"] == result.ingestion_id
            assert row["source_sha256"] == result.source_sha256
            assert row["city_query"] == entry["city"]
    assert len({tuple(row[key] for key in FORECAST_DAILY_KEYS) for row in daily}) == 6
    assert result.summary["tables"][FORECAST_DAILY_TABLE]["valid_rows"] == 6
    assert result.summary["tables"][FORECAST_DAILY_TABLE]["input_rows"] == 6
    assert result.summary["city_tables"]["fr_paris"][FORECAST_DAILY_TABLE]["valid_rows"] == 3
    assert result.summary["cities"]["with_valid_daily"] == ["be_brussels", "fr_paris"]
    _assert_publishable(result)


def test_daily_rows_from_synthetic_envelope_and_duplicates_removed(envelope):
    envelope["cities"].append(deepcopy(envelope["cities"][0]))
    envelope["summary"].update(total_cities=4, successful=4)
    result = _run(envelope)
    assert result.forecasts_daily.num_rows == 3
    counts = result.summary["tables"][FORECAST_DAILY_TABLE]
    assert (counts["input_rows"], counts["valid_rows"], counts["duplicates_removed"]) == (4, 3, 1)
    assert {row["lead_days"] for row in result.forecasts_daily.to_pylist()} == {0}


def test_invalid_forecast_date_rejects_the_daily_row_too(envelope):
    envelope["cities"][0]["data"]["forecast"]["forecastday"][0]["date"] = "2026-02-30"
    result = _run(envelope)
    assert result.status == "partial" and result.forecasts_daily.num_rows == 2
    daily = [r for r in result.rejections if r.table == FORECAST_DAILY_TABLE]
    assert [(r.code, r.scope, r.position) for r in daily] == [
        ("invalid_date", "row", "cities[0].forecast.forecastday[0]")]


def test_only_daily_rows_valid_is_partial_not_rejected(envelope):
    for entry in envelope["cities"]:
        entry["data"]["current"]["temp_c"] = None
        for hour in _hours(envelope, envelope["cities"].index(entry)):
            hour["temp_c"] = None
    result = _run(envelope)
    assert result.status == "partial"
    assert result.observations.num_rows == result.forecasts.num_rows == 0
    assert result.forecasts_daily.num_rows == 3
    _assert_publishable(result)


def test_daily_quality_error_rejects_only_that_daily_row(envelope, monkeypatch):
    original = silver_rules.daily_row

    def failing_for_paris(forecastday, **kwargs):
        if kwargs["common"]["city_id"] == "fr_paris":
            raise QualityError("invalid_type", "day", "Daily values must be an object")
        return original(forecastday, **kwargs)

    monkeypatch.setattr(silver_rules, "daily_row", failing_for_paris)
    result = _run(envelope)
    assert result.status == "partial"
    assert result.forecasts.num_rows == 6 and result.observations.num_rows == 3
    assert result.forecasts_daily.num_rows == 2
    assert [(r.table, r.code, r.field, r.scope, r.city_query) for r in result.rejections] == [
        (FORECAST_DAILY_TABLE, "invalid_type", "day", "row", "Paris")]
    assert result.summary["tables"][FORECAST_DAILY_TABLE]["rejected_rows"] == 1


def test_daily_nulled_measure_is_a_field_rejection(envelope, monkeypatch):
    original = silver_rules.daily_row

    def nulling_uv(forecastday, *, nulled, **kwargs):
        row = original(forecastday, nulled=nulled, **kwargs)
        if kwargs["common"]["city_id"] == "be_brussels":
            nulled.append(QualityError("out_of_range", "day.uv", "Metric is outside its allowed domain"))
        return row

    monkeypatch.setattr(silver_rules, "daily_row", nulling_uv)
    result = _run(envelope)
    assert result.status == "success" and result.forecasts_daily.num_rows == 3
    assert [(r.table, r.code, r.field, r.scope, r.position) for r in result.rejections] == [
        (FORECAST_DAILY_TABLE, "out_of_range", "day.uv", "field", "cities[2].forecast.forecastday[0]")]
    assert result.summary["tables"][FORECAST_DAILY_TABLE]["nulled_metrics"] == 1
    assert result.summary["nulled_metrics"] == 1


@pytest.mark.parametrize("request_value,expected", [
    ({"days": 3}, 3), ({"days": 7}, 7), ({"days": "3"}, None), ({"days": True}, None),
    ({}, None), ("3", None), (None, None)])
def test_requested_horizon_days_is_control_metadata(envelope, request_value, expected):
    if request_value is not None:
        envelope["forecast_request"] = request_value
    result = _run(envelope)
    assert result.summary["requested_horizon_days"] == expected
    assert result.status == "success"


def test_summary_v2_keys_and_no_ignored_fields(envelope):
    result = _run(envelope)
    summary = result.summary
    assert "ignored_fields" not in summary
    assert summary["bronze_format"] == "current"
    assert isinstance(summary["unknown_paths"], list)
    assert set(summary["tables"]) == {OBSERVATION_TABLE, FORECAST_TABLE, FORECAST_DAILY_TABLE}
    assert summary["nulled_metrics"] == 0
    assert summary["requested_horizon_days"] is None
    json.dumps(summary, allow_nan=False)
    for table_counts in summary["tables"].values():
        assert table_counts["input_rows"] == sum(table_counts[key] for key in (
            "valid_rows", "rejected_rows", "duplicates_removed"))
