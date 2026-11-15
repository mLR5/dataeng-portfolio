"""Forecast scores use canonical samples, preserving history and DST coverage."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from pyspark.sql import types as T

import gold_pipeline as gold


def _row(schema, **values):
    """Uninteresting required lineage fields have explicit type-safe toy defaults."""
    defaults = {
        T.StringType: "fixture", T.BooleanType: True, T.ShortType: 0,
        T.IntegerType: 0, T.LongType: 0, T.DoubleType: 0.0,
        T.DateType: date(2026, 1, 1),
        T.TimestampType: datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    row = {field.name: None if field.nullable else defaults[type(field.dataType)] for field in schema}
    row.update(values)
    return row


def _inputs(spark, hourly=(), daily=()):
    return {
        gold.FORECAST_TABLE: spark.createDataFrame(list(hourly), gold.SPARK_SCHEMAS[gold.FORECAST_TABLE]),
        gold.FORECAST_DAILY_TABLE: spark.createDataFrame(list(daily), gold.SPARK_SCHEMAS[gold.FORECAST_DAILY_TABLE]),
    }


def _dimensions(spark, observations=()):
    return {
        "fact_observation_hourly": spark.createDataFrame(list(observations), gold.GOLD_SCHEMAS["fact_observation_hourly"]),
        "dim_city": spark.createDataFrame([_row(gold.GOLD_SCHEMAS["dim_city"],
                                              city_id="fr_paris", timezone="Europe/Paris")],
                                          gold.GOLD_SCHEMAS["dim_city"]),
    }


def test_hourly_future_filter_and_signed_score(spark):
    issued = datetime(2026, 4, 1, 9, tzinfo=timezone.utc)
    rows = [_row(gold.SPARK_SCHEMAS[gold.FORECAST_TABLE],
                 city_id="fr_paris", ingestion_id=f"forecast-{lead}",
                 collected_at_utc=issued, target_at_utc=issued + timedelta(hours=lead),
                 target_local_date=date(2026, 4, 1), source_timezone="Europe/Paris",
                 lead_time_hours=float(lead), temperature_c=15.0)
            for lead in (-1, 0, 1, 2)]
    observation = _row(gold.GOLD_SCHEMAS["fact_observation_hourly"], city_id="fr_paris",
                       ingestion_id="observed", hour_utc=issued + timedelta(hours=1),
                       observed_at_utc=issued + timedelta(minutes=45),
                       local_date=date(2026, 4, 1), temperature_c=12.0)
    result = gold.build_gold_forecasts(spark, _inputs(spark, rows), _dimensions(spark, [observation]))
    forecasts = result["fact_forecast_hourly"].orderBy("target_hour_utc").collect()
    assert [row.lead_time_hours for row in forecasts] == [1.0, 2.0]
    assert [row.local_hour for row in forecasts] == [12, 13]
    errors = result["fact_forecast_hourly_error"].collect()
    assert len(errors) == 1  # The second forecast has no observation yet.
    error = errors[0]
    assert error.ingestion_id == "forecast-1"
    assert error.observation_ingestion_id == "observed"
    assert (error.temperature_error_c, error.absolute_temperature_error_c,
            error.squared_temperature_error_c) == (3.0, 3.0, 9.0)


def test_daily_scores_need_complete_utc_slots_including_dst(spark):
    zone = ZoneInfo("Europe/Paris")
    cases = [
        (date(2026, 3, 29), 23, "complete"),
        (date(2026, 4, 1), 24, "complete"),
        (date(2026, 10, 25), 25, "complete"),
        (date(2026, 4, 2), 24, "missing"),
        (date(2026, 4, 3), 24, "duplicate"),
        (date(2026, 4, 4), 24, "negative"),
    ]
    forecasts, observations = [], []
    for local_day, expected, mode in cases:
        first = datetime.combine(local_day, time.min, zone).astimezone(timezone.utc)
        end = datetime.combine(local_day + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
        assert (end - first).total_seconds() / 3600 == expected
        forecasts.append(_row(gold.SPARK_SCHEMAS[gold.FORECAST_DAILY_TABLE],
                              city_id="fr_paris", target_local_date=local_day,
                              ingestion_id=mode + str(local_day), collected_at_utc=first - timedelta(hours=12),
                              lead_days=-1 if mode == "negative" else 1,
                              temperature_min_c=None, temperature_max_c=20.0, temperature_avg_c=15.0))
        slots = list(range(expected))
        if mode in ("missing", "duplicate"):
            slots.pop()
        if mode == "duplicate":
            slots.append(0)  # Same raw count as a complete day, but one UTC hour is absent.
        for slot in slots:
            instant = first + timedelta(hours=slot)
            observations.append(_row(gold.GOLD_SCHEMAS["fact_observation_hourly"],
                                     city_id="fr_paris", hour_utc=instant, observed_at_utc=instant,
                                     local_date=local_day, temperature_c=10.0))
    result = gold.build_gold_forecasts(spark, _inputs(spark, daily=forecasts), _dimensions(spark, observations))
    errors = result["fact_forecast_daily_error"].orderBy("target_local_date").collect()
    assert [(row.expected_hours, row.observed_hours) for row in errors] == [(23, 23), (24, 24), (25, 25)]
    for error in errors:
        assert error.forecast_temperature_min_c is None
        assert error.temperature_min_error_c is None
        assert error.observed_temperature_min_c == 10.0
        assert error.temperature_max_error_c == 10.0
        assert error.temperature_avg_error_c == 5.0
    daily = result["fact_forecast_daily"].collect()
    assert len(daily) == 6
    assert all(row.is_day is True for row in daily)
    assert any(row.lead_days == -1 for row in daily)


def test_empty_sources_keep_four_explicit_contracts(spark):
    result = gold.build_gold_forecasts(spark, _inputs(spark), _dimensions(spark))
    assert set(result) == {
        "fact_forecast_hourly", "fact_forecast_hourly_error",
        "fact_forecast_daily", "fact_forecast_daily_error",
    }
    for name, frame in result.items():
        assert [(field.name, field.dataType) for field in frame.schema] == [
            (field.name, field.dataType) for field in gold.GOLD_SCHEMAS[name]]
    assert result["fact_forecast_daily_error"].count() == 0
