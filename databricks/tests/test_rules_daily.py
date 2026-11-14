"""Official daily forecast rows (forecastday[].day + astro), independent of Delta or Azure."""
from datetime import date, datetime, timezone
import copy
import json
from pathlib import Path

import pyarrow as pa
import pytest

from silver_rules import (CITY_BY_ID, FORECAST_DAILY_SCHEMA, REJECTION_CODES, SCHEMA_VERSION,
                          TRANSFORM_VERSION, QualityError, daily_row)

FIXTURES = Path(__file__).parent / "fixtures" / "weatherapi"
PARIS = CITY_BY_ID["fr_paris"]
# 2026-10-17 23:30 UTC is already 2026-10-18 01:30 in Paris (CEST).
COLLECTED = datetime(2026, 10, 17, 23, 30, tzinfo=timezone.utc)
COMMON = {
    "schema_version": SCHEMA_VERSION, "transform_version": TRANSFORM_VERSION, "source": "weatherapi",
    "ingestion_id": "20261017_233000", "collected_at_utc": COLLECTED, "ingestion_date": date(2026, 10, 17),
    "city_id": "fr_paris", "city_query": "Paris", "source_location_name": "Paris",
    "source_country": "France", "latitude": 48.8667, "longitude": 2.3333,
    "source_timezone": "Europe/Paris", "source_blob": "weather/weatherapi/synthetic_daily.json",
    "source_sha256": "0" * 64,
}


def _days():
    payload = json.loads((FIXTURES / "synthetic_daily_paris.json").read_text(encoding="utf-8"))
    return {entry["date"]: entry for entry in payload["forecastday"]}


def _row(forecastday, local_day=None, collected=COLLECTED, nulled=None):
    local_day = local_day or date.fromisoformat(forecastday["date"])
    nulled = [] if nulled is None else nulled
    return daily_row(forecastday, common=COMMON, city=PARIS, local_day=local_day,
                     collected=collected, nulled=nulled)


def _utc(*parts):
    return datetime(*parts, tzinfo=timezone.utc)


def test_fixture_is_declared_synthetic():
    provenance = json.loads((FIXTURES / "synthetic_daily_paris.provenance.json").read_text())
    payload = json.loads((FIXTURES / "synthetic_daily_paris.json").read_text(encoding="utf-8"))
    assert provenance["kind"] == "synthetic"
    assert len(payload["forecastday"]) == provenance["expected_days"]
    assert PARIS.timezone == payload["tz_id"]


def test_nominal_mapping_of_day_and_astro():
    nulled = []
    row = _row(_days()["2026-10-18"], nulled=nulled)
    assert nulled == []
    assert {k: row[k] for k in COMMON} == COMMON
    assert row["target_local_date"] == date(2026, 10, 18)
    assert row["lead_days"] == 0
    assert row["temperature_min_c"] == 9.2
    assert row["temperature_max_c"] == 15.0
    assert row["temperature_avg_c"] == 12.1
    assert row["wind_max_kph"] == 14.8
    assert row["precipitation_total_mm"] == 0.05
    assert row["snowfall_total_cm"] == 0.0
    assert row["visibility_avg_km"] == 10.0
    assert row["humidity_avg_pct"] == 57.0
    assert row["chance_of_rain_pct"] == 0.0
    assert row["chance_of_snow_pct"] == 0.0
    assert row["will_it_rain"] is False
    assert row["will_it_snow"] is False
    assert row["uv_index"] == 2.2
    assert row["wet_bulb_avg_c"] == 7.9
    assert row["wet_bulb_max_c"] == 8.6
    assert row["condition_code"] == 1000
    assert row["condition_text"] == "Ensoleillé"
    # Paris is UTC+2 on 18 October 2026 (CEST).
    assert row["sunrise_local"] == "08:15 AM"
    assert row["sunset_local"] == "06:55 PM"
    assert row["sunrise_utc"] == _utc(2026, 10, 18, 6, 15)
    assert row["sunset_utc"] == _utc(2026, 10, 18, 16, 55)


def test_row_matches_daily_schema_exactly():
    rows = [_row(day) for day in _days().values()]
    assert all(set(row) == set(FORECAST_DAILY_SCHEMA.names) for row in rows)
    assert all(len(row) == len(FORECAST_DAILY_SCHEMA) for row in rows)
    table = pa.Table.from_pylist(rows, schema=FORECAST_DAILY_SCHEMA)
    assert table.num_rows == 5
    assert table.schema == FORECAST_DAILY_SCHEMA


def test_decimals_are_kept_and_flags_and_condition_mapped():
    nulled = []
    row = _row(_days()["2026-03-28"], collected=_utc(2026, 3, 27, 12), nulled=nulled)
    assert nulled == []
    assert row["chance_of_rain_pct"] == 55.80025  # never rounded
    assert row["humidity_avg_pct"] == 81.5
    assert row["will_it_rain"] is True
    assert row["uv_index"] == 1.0 and isinstance(row["uv_index"], float)
    assert row["condition_code"] == 1183
    assert row["condition_text"] == "Pluie légère"


def test_wet_bulb_absent_in_older_archives_is_null_without_error():
    nulled = []
    row = _row(_days()["2026-03-29"], collected=_utc(2026, 3, 28, 12), nulled=nulled)
    assert row["wet_bulb_avg_c"] is None and row["wet_bulb_max_c"] is None
    assert nulled == []


@pytest.mark.parametrize("key,raw,column,code", [
    ("avghumidity", 100.5, "humidity_avg_pct", "out_of_range"),
    ("daily_chance_of_rain", -0.1, "chance_of_rain_pct", "out_of_range"),
    ("daily_chance_of_snow", "50%", "chance_of_snow_pct", "invalid_type"),
    ("maxwind_kph", -1, "wind_max_kph", "out_of_range"),
    ("totalprecip_mm", -0.01, "precipitation_total_mm", "out_of_range"),
    ("totalsnow_cm", True, "snowfall_total_cm", "invalid_type"),
    ("avgvis_km", -2.0, "visibility_avg_km", "out_of_range"),
    ("uv", -1, "uv_index", "out_of_range"),
    ("mintemp_c", "NaN", "temperature_min_c", "invalid_type"),
    ("maxwetbulb_c", [], "wet_bulb_max_c", "invalid_type"),
    ("daily_will_it_rain", 2, "will_it_rain", "out_of_range"),
])
def test_invalid_optional_measure_is_nulled_and_recorded(key, raw, column, code):
    forecastday = copy.deepcopy(_days()["2026-10-18"])
    forecastday["day"][key] = raw
    nulled = []
    row = _row(forecastday, nulled=nulled)
    assert row[column] is None
    assert [(e.code, e.field) for e in nulled] == [(code, f"day.{key}")]
    assert row["temperature_max_c"] == 15.0  # the rest of the row is kept


def test_missing_keys_stay_null_never_zero():
    forecastday = copy.deepcopy(_days()["2026-10-18"])
    for key in ("totalprecip_mm", "daily_chance_of_rain", "daily_will_it_rain", "condition"):
        del forecastday["day"][key]
    nulled = []
    row = _row(forecastday, nulled=nulled)
    assert row["precipitation_total_mm"] is None
    assert row["chance_of_rain_pct"] is None
    assert row["will_it_rain"] is None
    assert row["condition_code"] is None and row["condition_text"] is None
    assert nulled == []


def test_invalid_condition_nulls_only_condition_fields():
    forecastday = copy.deepcopy(_days()["2026-10-18"])
    forecastday["day"]["condition"] = {"code": 1000.5, "text": 7}
    nulled = []
    row = _row(forecastday, nulled=nulled)
    assert row["condition_code"] is None and row["condition_text"] is None
    assert {(e.code, e.field) for e in nulled} == {
        ("invalid_type", "day.condition.code"), ("invalid_type", "day.condition.text")}
    forecastday["day"]["condition"] = "Sunny"
    nulled = []
    row = _row(forecastday, nulled=nulled)
    assert row["condition_code"] is None and row["condition_text"] is None
    assert [(e.code, e.field) for e in nulled] == [("invalid_type", "day.condition")]


def test_missing_day_leaves_measures_null_but_keeps_astro():
    forecastday = copy.deepcopy(_days()["2026-10-18"])
    del forecastday["day"]
    nulled = []
    row = _row(forecastday, nulled=nulled)
    measures = [name for name in FORECAST_DAILY_SCHEMA.names
                if name not in COMMON and name not in ("target_local_date", "lead_days")
                and not name.startswith(("sunrise", "sunset"))]
    assert all(row[name] is None for name in measures)
    assert row["sunrise_utc"] == _utc(2026, 10, 18, 6, 15)
    assert nulled == []
    pa.Table.from_pylist([row], schema=FORECAST_DAILY_SCHEMA)


@pytest.mark.parametrize("bad", ["sunny", [], 3])
def test_non_object_day_makes_the_day_unusable(bad):
    forecastday = copy.deepcopy(_days()["2026-10-18"])
    forecastday["day"] = bad
    with pytest.raises(QualityError) as failure:
        _row(forecastday)
    assert (failure.value.code, failure.value.field) == ("invalid_type", "day")


@pytest.mark.parametrize("marker", ["No sunrise", "No sunset", "no sunrise"])
def test_non_occurring_sun_event_is_null_without_error(marker):
    forecastday = copy.deepcopy(_days()["2026-10-18"])
    forecastday["astro"]["sunrise"] = marker
    nulled = []
    row = _row(forecastday, nulled=nulled)
    assert row["sunrise_local"] is None and row["sunrise_utc"] is None
    assert row["sunset_utc"] == _utc(2026, 10, 18, 16, 55)
    assert nulled == []


def test_missing_astro_leaves_sun_events_null():
    forecastday = copy.deepcopy(_days()["2026-10-18"])
    del forecastday["astro"]
    nulled = []
    row = _row(forecastday, nulled=nulled)
    assert [row[k] for k in ("sunrise_local", "sunrise_utc", "sunset_local", "sunset_utc")] == [None] * 4
    assert nulled == []
    forecastday["astro"] = "08:15 AM"
    row = _row(forecastday, nulled=nulled)
    assert row["sunrise_utc"] is None and row["temperature_max_c"] == 15.0
    assert [(e.code, e.field) for e in nulled] == [("invalid_type", "astro")]


def test_spring_forward_sun_events_use_summer_offset():
    days = _days()
    before = _row(days["2026-03-28"], collected=_utc(2026, 3, 27, 12))
    after = _row(days["2026-03-29"], collected=_utc(2026, 3, 28, 12))
    assert before["sunrise_utc"] == _utc(2026, 3, 28, 5, 24)   # 06:24 CET (UTC+1)
    assert before["sunset_utc"] == _utc(2026, 3, 28, 18, 18)   # 07:18 PM CET
    assert after["sunrise_utc"] == _utc(2026, 3, 29, 5, 22)    # 07:22 CEST (UTC+2)
    assert after["sunset_utc"] == _utc(2026, 3, 29, 18, 20)    # 08:20 PM CEST
    assert after["sunrise_local"] == "07:22 AM"


def test_fall_back_sun_events_use_winter_offset():
    days = _days()
    before = _row(days["2026-10-24"], collected=_utc(2026, 10, 23, 12))
    after = _row(days["2026-10-25"], collected=_utc(2026, 10, 24, 12))
    assert before["sunrise_utc"] == _utc(2026, 10, 24, 6, 22)  # 08:22 CEST (UTC+2)
    assert before["sunset_utc"] == _utc(2026, 10, 24, 16, 37)
    assert after["sunrise_utc"] == _utc(2026, 10, 25, 6, 23)   # 07:23 CET (UTC+1)
    assert after["sunset_utc"] == _utc(2026, 10, 25, 16, 35)   # 05:35 PM CET


@pytest.mark.parametrize("local_day,clock,code", [
    (date(2026, 3, 29), "02:30 AM", "nonexistent_local_time"),
    (date(2026, 10, 25), "02:30 AM", "ambiguous_local_time"),
])
def test_dst_gap_or_overlap_nulls_only_the_utc_instant(local_day, clock, code):
    forecastday = copy.deepcopy(_days()[local_day.isoformat()])
    forecastday["astro"]["sunrise"] = clock
    nulled = []
    row = _row(forecastday, collected=_utc(2026, 1, 1), nulled=nulled)
    assert row["sunrise_local"] == clock and row["sunrise_utc"] is None
    assert [(e.code, e.field) for e in nulled] == [(code, "astro.sunrise")]


@pytest.mark.parametrize("raw,code", [
    ("13:10 PM", "invalid_time"), ("00:15 AM", "invalid_time"), ("07:60 AM", "invalid_time"),
    ("07:42", "invalid_time"), ("07:42 am", "invalid_time"), ("", "invalid_time"),
    (742, "invalid_type"),
])
def test_malformed_sun_event_is_nulled_and_recorded(raw, code):
    forecastday = copy.deepcopy(_days()["2026-10-18"])
    forecastday["astro"]["sunset"] = raw
    nulled = []
    row = _row(forecastday, nulled=nulled)
    assert row["sunset_local"] is None and row["sunset_utc"] is None
    assert [(e.code, e.field) for e in nulled] == [(code, "astro.sunset")]


@pytest.mark.parametrize("clock,expected", [
    ("12:05 AM", _utc(2026, 10, 17, 22, 5)), ("12:05 PM", _utc(2026, 10, 18, 10, 5)),
    ("7:05 PM", _utc(2026, 10, 18, 17, 5)),
])
def test_twelve_hour_clock_edges(clock, expected):
    forecastday = copy.deepcopy(_days()["2026-10-18"])
    forecastday["astro"]["sunrise"] = clock
    assert _row(forecastday)["sunrise_utc"] == expected


@pytest.mark.parametrize("local_day,lead", [
    (date(2026, 10, 18), 0), (date(2026, 10, 19), 1), (date(2026, 10, 20), 2),
])
def test_lead_days_use_the_city_local_collection_date(local_day, lead):
    # Collected 2026-10-17 23:30 UTC = 2026-10-18 in Paris, so 18 October is lead 0.
    forecastday = copy.deepcopy(_days()["2026-10-18"])
    assert _row(forecastday, local_day=local_day)["lead_days"] == lead


def test_moon_fields_are_not_stored():
    row = _row(_days()["2026-10-18"])
    assert not any("moon" in name or name == "is_sun_up" for name in row)


def test_every_daily_error_code_is_a_contract_code():
    used = {"invalid_type", "out_of_range", "invalid_time", "nonexistent_local_time",
            "ambiguous_local_time"}
    assert used <= REJECTION_CODES
