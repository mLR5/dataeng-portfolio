"""Domain boundaries and time changes, independent of Delta or Azure."""
from datetime import datetime, timezone

import pytest

from shared.silver_quality import QualityError, boolean, instant, number


@pytest.mark.parametrize("raw,expected", [("12.5", 12.5), ("-1.25e1", -12.5), (0, 0.0)])
def test_unambiguous_decimal_metrics(raw, expected):
    assert number(raw, "temp_c", required=True) == expected


@pytest.mark.parametrize("raw", [True, "", " 12", "12,5", "NaN", float("inf"), [], {}])
def test_no_silent_boolean_zero_locale_or_nonfinite_coercion(raw):
    with pytest.raises(QualityError, match="Metric"):
        number(raw, "temp_c", required=True)


def test_optional_absence_differs_from_invalid_supplied_metric():
    assert number(None, "humidity", integer=True, minimum=0, maximum=100) is None
    assert number("100.0", "humidity", integer=True, minimum=0, maximum=100) == 100
    for raw, code in [(101, "out_of_range"), (-1, "out_of_range"), (50.5, "invalid_type")]:
        with pytest.raises(QualityError) as failure:
            number(raw, "humidity", integer=True, minimum=0, maximum=100)
        assert failure.value.code == code
        assert failure.value.field == "humidity"
    with pytest.raises(QualityError) as failure:
        number(None, "temp_c", required=True)
    assert failure.value.code == "missing_field"


def test_weather_booleans_are_explicit():
    assert boolean("1", "is_day") is True
    assert boolean(0, "is_day") is False
    assert boolean(None, "is_day") is None
    with pytest.raises(QualityError):
        boolean(2, "is_day")


def test_fall_back_two_wall_clock_hours_keep_different_epoch_instants():
    # Paris repeats 02:00 on 25 October 2026; the UTC instants differ by 1h.
    first = datetime(2026, 10, 25, 0, tzinfo=timezone.utc)
    second = datetime(2026, 10, 25, 1, tzinfo=timezone.utc)
    assert instant({"time_epoch": int(first.timestamp()), "time": "2026-10-25 02:00"},
                   "time_epoch", "time", "Europe/Paris") == first
    assert instant({"time_epoch": int(second.timestamp()), "time": "2026-10-25 02:00"},
                   "time_epoch", "time", "Europe/Paris") == second


@pytest.mark.parametrize("local,code", [
    ("2026-10-25 02:00", "ambiguous_local_time"),
    ("2026-03-29 02:00", "nonexistent_local_time"),
    ("2026-02-30 02:00", "invalid_timestamp"),
    ("2026-03-29", "invalid_timestamp"),
])
def test_local_fallback_does_not_invent_dst_instants(local, code):
    with pytest.raises(QualityError) as failure:
        instant({"time": local}, "time_epoch", "time", "Europe/Paris")
    assert failure.value.code == code


def test_local_fallback_uses_actual_iana_timezone():
    assert instant({"time": "2026-07-01 00:00"}, "time_epoch", "time", "Europe/Paris") == (
        datetime(2026, 6, 30, 22, tzinfo=timezone.utc)
    )


def test_invalid_epoch_is_not_hidden_by_valid_local_fallback():
    with pytest.raises(QualityError):
        instant({"time_epoch": "bad", "time": "2026-07-01 00:00"},
                "time_epoch", "time", "Europe/Paris")


def test_epoch_that_overflows_source_timezone_is_a_data_error():
    with pytest.raises(QualityError) as failure:
        instant({"time_epoch": 253402300799}, "time_epoch", "time", "Europe/Paris")
    assert failure.value.code == "invalid_timestamp"
