from datetime import datetime, timezone

import gold_pipeline as gold
import pytest


def test_coverage_counts_utc_slots_and_temperature_order_is_an_alert(spark):
    observations = spark.createDataFrame([
        ("fr_paris", datetime(2026, 10, 25, hour, tzinfo=timezone.utc)) for hour in (0, 2)
    ], "city_id string, hour_utc timestamp")
    daily = spark.createDataFrame([
        (2.0, 4.0, 6.0), (5.0, 4.0, 6.0), (None, None, None),
    ], "temperature_min_c double, temperature_avg_c double, temperature_max_c double")
    report = gold.gold_quality({"fact_observation_hourly": observations, "fact_forecast_daily": daily})
    city = report["observation_coverage"][0]
    assert city["observed_hours"] == 2 and city["expected_hours"] == 3
    assert city["coverage_pct"] == pytest.approx(200 / 3)
    assert report["daily_temperature_order_alerts"] == 1


@pytest.mark.parametrize("threshold", [-1, 101, float("nan")])
def test_invalid_threshold_fails_before_reading_storage(threshold):
    with pytest.raises(ValueError, match="minimum_coverage_pct"):
        gold.run_gold(None, silver_root="file:///unused/silver", gold_root="file:///unused/gold",
                      minimum_coverage_pct=threshold)


def test_empty_observation_build_cannot_pass_production_coverage():
    with pytest.raises(ValueError, match="no observations"):
        gold._enforce_gold_coverage({"observation_coverage": []}, 98)
    gold._enforce_gold_coverage({"observation_coverage": []}, 0)


def test_missing_required_city_cannot_pass_production_coverage():
    quality = {"observation_coverage": [{"city_id": "fr_paris", "coverage_pct": 100.0}]}
    with pytest.raises(ValueError, match="required city"):
        gold._enforce_gold_coverage(quality, 98)
