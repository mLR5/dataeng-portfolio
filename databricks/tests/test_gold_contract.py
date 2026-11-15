"""The Gold contract is loaded from the deployed notebook, not copied code."""
from datetime import date, datetime, timezone

from pyspark.sql import types as T
import pytest

import gold_pipeline


EXPECTED_KEYS = {
    "dim_city": ("city_id",),
    "dim_date": ("date",),
    "dim_condition": ("condition_code", "is_day"),
    "dim_collection": ("ingestion_id",),
    "fact_collection_city": ("ingestion_id", "city_id"),
    "fact_observation_hourly": ("city_id", "hour_utc"),
    "fact_forecast_hourly": ("city_id", "target_hour_utc", "issued_at_utc"),
    "fact_forecast_hourly_error": ("city_id", "target_hour_utc", "issued_at_utc"),
    "fact_forecast_daily": ("city_id", "target_local_date", "issued_at_utc"),
    "fact_forecast_daily_error": ("city_id", "target_local_date", "issued_at_utc"),
}


@pytest.fixture(scope="module")
def gold_contract():
    return gold_pipeline


def test_step_four_has_exactly_the_ten_dimension_and_fact_tables(gold_contract):
    assert set(gold_contract.GOLD_SCHEMAS) == set(EXPECTED_KEYS)
    assert gold_contract.GOLD_TABLE_KEYS == EXPECTED_KEYS


@pytest.mark.parametrize("name", EXPECTED_KEYS)
def test_primary_keys_exist_and_are_required(gold_contract, name):
    schema = gold_contract.GOLD_SCHEMAS[name]
    assert len(schema.names) == len(set(schema.names))
    for key in EXPECTED_KEYS[name]:
        assert schema[key].nullable is False


def test_gold_retains_v2_observation_measures_and_alignment_metadata(gold_contract):
    schema = gold_contract.GOLD_SCHEMAS["fact_observation_hourly"]
    assert isinstance(schema["hour_utc"].dataType, T.TimestampType)
    assert isinstance(schema["local_date"].dataType, T.DateType)
    for field in ("uv_index", "wind_gust_kph", "visibility_km", "distance_to_hour_min"):
        assert isinstance(schema[field].dataType, T.DoubleType)


def test_daily_fact_preserves_negative_lead_and_sun_nullability(gold_contract):
    schema = gold_contract.GOLD_SCHEMAS["fact_forecast_daily"]
    assert isinstance(schema["lead_days"].dataType, (T.ShortType, T.IntegerType))
    assert schema["is_day"].nullable is False
    for field in ("sunrise_utc", "sunset_utc"):
        assert isinstance(schema[field].dataType, T.TimestampType)
        assert schema[field].nullable is True


def test_fractional_weather_probabilities_remain_double(gold_contract):
    for name in ("fact_forecast_hourly", "fact_forecast_daily"):
        for field in ("chance_of_rain_pct", "chance_of_snow_pct"):
            assert isinstance(gold_contract.GOLD_SCHEMAS[name][field].dataType, T.DoubleType)


_DAY = date(2026, 10, 18)
_ISSUED = datetime(2026, 10, 18, tzinfo=timezone.utc)
_TARGET = datetime(2026, 10, 18, 1, tzinfo=timezone.utc)


def _required_row(contract, name, **changes):
    """Minimal typed data for validator tests; metric values are unimportant."""
    defaults = {
        T.StringType: "value", T.BooleanType: True, T.DoubleType: 1.0,
        T.ShortType: 1, T.IntegerType: 1, T.LongType: 1,
        T.DateType: _DAY, T.TimestampType: _ISSUED,
    }
    row = {
        field.name: None if field.nullable else defaults[type(field.dataType)]
        for field in contract.GOLD_SCHEMAS[name]
    }
    row.update(changes)
    return row


def _candidates(spark, contract, records):
    # Nullable input flags permit deliberately bad required values to reach the
    # validator. All names and physical data types still match the contract.
    return {
        name: spark.createDataFrame(
            records.get(name, []),
            T.StructType([T.StructField(field.name, field.dataType, True) for field in schema]),
        )
        for name, schema in contract.GOLD_SCHEMAS.items()
    }


def _parents(contract):
    return {
        "dim_city": [_required_row(contract, "dim_city", city_id="fr_paris")],
        "dim_date": [_required_row(contract, "dim_date", date=_DAY)],
        "dim_collection": [
            _required_row(contract, "dim_collection", ingestion_id="forecast"),
        ],
    }


def test_validator_accepts_unknown_collection_date_and_absent_daily_condition(spark, gold_contract):
    records = _parents(gold_contract)
    records["fact_forecast_daily"] = [_required_row(
        gold_contract, "fact_forecast_daily", city_id="fr_paris", ingestion_id="forecast",
        target_local_date=_DAY, issued_at_utc=_ISSUED, lead_days=0,
        condition_code=None, is_day=True,
    )]
    counts = gold_contract.validate_gold(spark, _candidates(spark, gold_contract, records))
    assert counts["dim_collection"] == counts["fact_forecast_daily"] == 1


@pytest.mark.parametrize("problem", ["null_key", "duplicate_key"])
def test_validator_blocks_missing_or_duplicate_primary_key(spark, gold_contract, problem):
    row = _required_row(gold_contract, "dim_city", city_id="fr_paris")
    rows = [{**row, "city_id": None}] if problem == "null_key" else [row, row]
    candidates = _candidates(spark, gold_contract, {"dim_city": rows})
    message = "required value missing" if problem == "null_key" else "duplicate key"
    with pytest.raises(ValueError, match=message):
        gold_contract.validate_gold(spark, candidates)


def test_validator_rejects_error_linked_to_another_observation_ingestion(spark, gold_contract):
    records = _parents(gold_contract)
    records["dim_collection"] += [
        _required_row(gold_contract, "dim_collection", ingestion_id=ingestion)
        for ingestion in ("observation", "another-observation")
    ]
    records["fact_observation_hourly"] = [_required_row(
        gold_contract, "fact_observation_hourly", city_id="fr_paris", hour_utc=_TARGET,
        observed_at_utc=_TARGET, ingestion_id="observation", local_date=_DAY,
    )]
    records["fact_forecast_hourly"] = [_required_row(
        gold_contract, "fact_forecast_hourly", city_id="fr_paris", target_hour_utc=_TARGET,
        issued_at_utc=_ISSUED, ingestion_id="forecast", target_local_date=_DAY,
    )]
    records["fact_forecast_hourly_error"] = [_required_row(
        gold_contract, "fact_forecast_hourly_error", city_id="fr_paris", target_hour_utc=_TARGET,
        issued_at_utc=_ISSUED, ingestion_id="forecast", observed_at_utc=_TARGET,
        observation_ingestion_id="another-observation",
    )]
    with pytest.raises(ValueError, match="another canonical observation ingestion"):
        gold_contract.validate_gold(spark, _candidates(spark, gold_contract, records))


@pytest.mark.parametrize("lead_days, observed_hours, message", [
    (-1, 24, "past forecast"),
    (0, 23, "complete local-day"),
])
def test_validator_daily_error_requires_nonpast_forecast_and_complete_truth(
    spark, gold_contract, lead_days, observed_hours, message,
):
    records = _parents(gold_contract)
    common = {
        "city_id": "fr_paris", "target_local_date": _DAY,
        "issued_at_utc": _ISSUED, "ingestion_id": "forecast",
    }
    records["fact_forecast_daily"] = [_required_row(
        gold_contract, "fact_forecast_daily", **common, lead_days=lead_days,
    )]
    records["fact_forecast_daily_error"] = [_required_row(
        gold_contract, "fact_forecast_daily_error", **common,
        expected_hours=24, observed_hours=observed_hours,
    )]
    with pytest.raises(ValueError, match=message):
        gold_contract.validate_gold(spark, _candidates(spark, gold_contract, records))
