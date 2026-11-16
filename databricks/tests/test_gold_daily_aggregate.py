"""Daily sampled-weather aggregates: local calendars, missing data and solar pairs."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from pyspark.sql import types as T

import gold_pipeline as gold


UTC = timezone.utc
BASE = datetime(2026, 1, 3, 12, tzinfo=UTC)


def _record(table, **values):
    row = {}
    for field in gold.GOLD_SCHEMAS[table]:
        if field.nullable:
            row[field.name] = None
        elif isinstance(field.dataType, T.StringType):
            row[field.name] = "fixture"
        elif isinstance(field.dataType, T.TimestampType):
            row[field.name] = BASE
        elif isinstance(field.dataType, T.DateType):
            row[field.name] = BASE.date()
        elif isinstance(field.dataType, T.BooleanType):
            row[field.name] = True
        elif isinstance(field.dataType, T.DoubleType):
            row[field.name] = 0.0
        else:
            row[field.name] = 0
    row.update(values)
    return row


def _city(city_id="fr_paris", first=BASE, zone="Europe/Paris"):
    return _record("dim_city", city_id=city_id, first_collected_at_utc=first, timezone=zone)


def _observation(hour=BASE, city_id="fr_paris", zone="Europe/Paris", **metrics):
    local = hour.astimezone(ZoneInfo(zone))
    return _record("fact_observation_hourly", city_id=city_id, hour_utc=hour,
        observed_at_utc=hour, local_date=local.date(), local_hour=local.hour, **metrics)


def _tables(spark, cities=(), observations=(), forecasts=()):
    rows = {"dim_city": cities, "fact_observation_hourly": observations, "fact_forecast_daily": forecasts}
    return {name: spark.createDataFrame(list(records), gold.GOLD_SCHEMAS[name]) for name, records in rows.items()}


def test_daily_aggregate_is_separate_from_the_ten_table_gold_contract():
    assert gold.GOLD_VERSION == "1.0.0"
    assert len(gold.GOLD_SCHEMAS) == 10
    assert "agg_city_daily" not in gold.GOLD_SCHEMAS
    assert gold.AGG_CITY_DAILY_KEYS == ("city_id", "local_date")
    assert gold.AGG_CITY_DAILY_SCHEMA["sunrise_utc"].nullable
    assert gold.AGG_CITY_DAILY_SCHEMA["precipitation_total_mm"].nullable


def test_daily_aggregate_empty_sources_do_not_invent_calendar_days(spark):
    frame = gold.build_gold_daily_aggregate(spark, _tables(spark, cities=[_city(first=None)]))
    assert frame.count() == 0
    assert frame.schema.names == gold.AGG_CITY_DAILY_SCHEMA.names


@pytest.mark.parametrize("day,expected", [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)])
def test_daily_aggregate_counts_utc_hours_across_local_dst_days(spark, day, expected):
    midnight = datetime.combine(day, datetime.min.time(), ZoneInfo("Europe/Paris")).astimezone(UTC)
    observations = [_observation(midnight + timedelta(hours=n), temperature_c=float(n), precipitation_mm=1.0)
                    for n in range(expected)]
    tables = _tables(spark, cities=[_city(first=midnight)], observations=observations)
    actual = gold.build_gold_daily_aggregate(spark, tables).first()
    assert actual.local_date == day
    assert actual.expected_hours == actual.observed_hours == expected
    assert actual.coverage_pct == 100.0 and actual.is_complete is True
    assert actual.precipitation_is_complete is True
    assert actual.precipitation_observed_hours == expected
    assert actual.precipitation_total_mm == float(expected)
    assert actual.temperature_min_c == 0.0
    assert actual.temperature_max_c == expected - 1
    assert actual.temperature_avg_c == (expected - 1) / 2


def test_daily_aggregate_keeps_missing_days_but_no_prevalid_london_history(spark):
    london_first = datetime(2026, 10, 19, 11, tzinfo=UTC)
    paris_first = datetime(2026, 10, 17, 10, tzinfo=UTC)
    cities = [_city(first=paris_first), _city("gb_london", london_first, "Europe/London"),
              _city("be_brussels", None, "Europe/Brussels")]
    observations = [_observation(paris_first, temperature_c=12.0),
                    _observation(paris_first + timedelta(days=2), temperature_c=13.0),
                    _observation(london_first, "gb_london", "Europe/London", temperature_c=10.0)]
    rows = gold.build_gold_daily_aggregate(spark, _tables(spark, cities, observations)).collect()
    assert {(r.city_id, r.local_date) for r in rows} == {
        ("fr_paris", date(2026, 10, d)) for d in (17, 18, 19)
    } | {("gb_london", date(2026, 10, 19))}
    missing = next(r for r in rows if r.city_id == "fr_paris" and r.local_date.day == 18)
    assert missing.expected_hours == 24
    assert missing.observed_hours == missing.temperature_observed_hours == missing.precipitation_observed_hours == 0
    assert missing.coverage_pct == 0.0 and missing.is_complete is False
    assert missing.temperature_min_c is None and missing.temperature_avg_c is None
    assert missing.precipitation_total_mm is None


def test_daily_aggregate_partial_days_and_nullable_metrics_keep_counts(spark):
    observations = [_observation(temperature_c=10.0, humidity_pct=60, wind_kph=5.0,
                                  wind_gust_kph=9.0, precipitation_mm=1.5),
                    _observation(BASE + timedelta(hours=1), temperature_c=14.0, wind_kph=7.0)]
    row = gold.build_gold_daily_aggregate(spark, _tables(spark, [_city()], observations)).first()
    assert row.expected_hours == 24 and row.observed_hours == 2
    assert row.coverage_pct == pytest.approx(100.0 / 12)
    assert row.is_complete is False
    assert (row.temperature_min_c, row.temperature_max_c, row.temperature_avg_c) == (10.0, 14.0, 12.0)
    assert row.humidity_observed_hours == 1 and row.humidity_avg_pct == 60.0
    assert row.wind_observed_hours == 2 and row.wind_avg_kph == 6.0 and row.wind_max_kph == 7.0
    assert row.wind_gust_observed_hours == 1 and row.wind_gust_max_kph == 9.0
    assert row.precipitation_observed_hours == 1 and row.precipitation_total_mm == 1.5
    assert row.precipitation_is_complete is False


@pytest.mark.parametrize("first_collection", [datetime(2026, 1, 4, 0, 15, tzinfo=UTC), None])
def test_daily_aggregate_keeps_observations_before_first_collection_day_or_without_metadata(spark, first_collection):
    # Paris is already on 4 January at collection. The API's canonical observation
    # still belongs to 3 January locally, and must not disappear from the aggregate.
    preceding_hour = datetime(2026, 1, 3, 22, tzinfo=UTC)
    observations = [_observation(preceding_hour, temperature_c=10.0),
                    _observation(preceding_hour + timedelta(hours=2), temperature_c=12.0)]
    tables = _tables(spark, [_city(first=first_collection)], observations)
    rows = gold.build_gold_daily_aggregate(spark, tables).orderBy("local_date").collect()
    assert [row.local_date for row in rows] == [date(2026, 1, 3), date(2026, 1, 4)]
    assert [row.observed_hours for row in rows] == [1, 1]
    assert sum(row.observed_hours for row in rows) == len(observations)
    assert sum(row.temperature_observed_hours for row in rows) == len(observations)
    assert rows[0].temperature_avg_c == 10.0 and rows[1].temperature_avg_c == 12.0


def _forecast(issued, ingestion_id, sunrise, sunset, target=BASE.date()):
    return _record("fact_forecast_daily", city_id="fr_paris", target_local_date=target,
                   issued_at_utc=issued, ingestion_id=ingestion_id, sunrise_utc=sunrise, sunset_utc=sunset)


def test_daily_aggregate_solar_falls_back_to_valid_pair_and_never_mixes_events(spark):
    sunrise = BASE.replace(hour=7)
    sunset = BASE.replace(hour=16)
    forecasts = [
        _forecast(BASE - timedelta(days=1), "older-valid", sunrise, sunset),
        _forecast(BASE, "newer-incomplete", sunrise + timedelta(minutes=5), None),
        _forecast(BASE + timedelta(hours=1), "newest-inverted", sunset, sunrise),
    ]
    inputs = _tables(spark, [_city()], [_observation(temperature_c=10.0)], forecasts)
    row = gold.build_gold_daily_aggregate(spark, inputs).first()
    assert row.sunrise_utc == sunrise.replace(tzinfo=None)
    assert row.sunset_utc == sunset.replace(tzinfo=None)
    assert row.daylight_hours == 9.0


def test_daily_aggregate_uses_latest_valid_solar_pair_with_deterministic_tie(spark):
    sunrise = BASE.replace(hour=7)
    sunset = BASE.replace(hour=16)
    forecasts = [
        _forecast(BASE - timedelta(days=1), "old", sunrise, sunset),
        _forecast(BASE, "b", sunrise + timedelta(minutes=2), sunset + timedelta(minutes=2)),
        _forecast(BASE, "a", sunrise + timedelta(minutes=1), sunset + timedelta(minutes=1)),
    ]
    row = gold.build_gold_daily_aggregate(spark,
        _tables(spark, [_city()], [_observation(temperature_c=10.0)], forecasts)).first()
    assert row.sunrise_utc == (sunrise + timedelta(minutes=1)).replace(tzinfo=None)
    assert row.sunset_utc == (sunset + timedelta(minutes=1)).replace(tzinfo=None)


def test_daily_aggregate_unpaired_or_wrong_date_solar_stays_null(spark):
    forecasts = [
        _forecast(BASE, "rise-only", BASE.replace(hour=7), None),
        _forecast(BASE, "set-only", None, BASE.replace(hour=16)),
        _forecast(BASE, "wrong-day", BASE.replace(hour=7) + timedelta(days=1),
                  BASE.replace(hour=16) + timedelta(days=1)),
    ]
    row = gold.build_gold_daily_aggregate(spark,
        _tables(spark, [_city()], [_observation(temperature_c=10.0)], forecasts)).first()
    assert row.sunrise_utc is None and row.sunset_utc is None and row.daylight_hours is None


def test_daily_aggregate_rejects_non_utc_session_before_computing(spark):
    original = spark.conf.get("spark.sql.session.timeZone")
    try:
        spark.conf.set("spark.sql.session.timeZone", "Europe/Paris")
        with pytest.raises(ValueError, match="UTC"):
            gold.build_gold_daily_aggregate(spark, {})
    finally:
        spark.conf.set("spark.sql.session.timeZone", original)
