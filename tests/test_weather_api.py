from datetime import datetime, timezone

import pytest
import requests

from shared import weather_api


@pytest.mark.parametrize("statuses, expected, successful", [
    ([200, 200, 200], "success", 3),
    ([200, 401, 200], "partial", 2),
    ([401, 401, 401], "failed", 0),
])
def test_collection_completeness(http_get, response, statuses, expected, successful):
    http_get.side_effect = [response(code) for code in statuses]
    data = weather_api.get_all_cities_data("test-key")
    assert data["status"] == expected
    assert data["summary"] == {"total_cities": 3, "successful": successful, "failed": 3 - successful}
    assert len(data["cities"]) == 3
    assert datetime.fromisoformat(data["ingestion_timestamp"]).utcoffset().total_seconds() == 0
    assert data["forecast_request"] == {"days": 3}
    assert [call.kwargs["params"]["days"] for call in http_get.call_args_list] == [3, 3, 3]


@pytest.mark.parametrize("city, query", [
    ("Paris", "48.8566,2.3522"),
    ("Londres", "51.5074,-0.1278"),
    ("Bruxelles", "50.8503,4.3517"),
])
def test_queries_coordinates_and_preserves_city_label_and_raw_payload(http_get, response, city, query):
    payload = {"location": {"name": "Provider label"}, "forecast": {"forecastday": []}}
    http_get.side_effect = None
    http_get.return_value = response(payload=payload)
    result = weather_api.get_weather_data(city, "test-key")
    assert http_get.call_args.kwargs["params"] == {"key": "test-key", "q": query, "days": 3, "lang": "fr"}
    assert result == {"city": city, "data": payload, "status": "success"}
    assert result["data"] is payload


def test_unknown_city_does_not_fall_back_to_an_ambiguous_name(http_get):
    result = weather_api.get_weather_data("Unknown city", "test-key")
    assert result == {"city": "Unknown city", "data": None, "status": "error"}
    http_get.assert_not_called()


def test_explicit_forecast_request_is_recorded_without_inventing_received_days(http_get, response):
    payload = {"forecast": {"forecastday": [{"date": "2026-10-19", "hour": []}]}}
    http_get.side_effect = None
    http_get.return_value = response(payload=payload)
    data = weather_api.get_all_cities_data("test-key", days=2, lang="en")
    assert data["forecast_request"] == {"days": 2}
    assert [item["city"] for item in data["cities"]] == ["Paris", "Londres", "Bruxelles"]
    assert all(len(item["data"]["forecast"]["forecastday"]) == 1 for item in data["cities"])
    assert all(call.kwargs["params"]["days"] == 2 and call.kwargs["params"]["lang"] == "en"
               for call in http_get.call_args_list)


def test_ingestion_ids_differ_even_at_the_same_instant(monkeypatch, http_get, response):
    class FrozenClock:
        @staticmethod
        def now(tz):
            return datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(weather_api, "datetime", FrozenClock)
    http_get.side_effect = None
    http_get.return_value = response()
    first = weather_api.get_all_cities_data("test-key")
    second = weather_api.get_all_cities_data("test-key")
    assert first["ingestion_id"] != second["ingestion_id"]
    assert first["ingestion_id"].startswith("20261017_120000_")
    assert first["ingestion_timestamp"] == second["ingestion_timestamp"]


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_temporary_http_error_is_retried(http_get, response, retry_sleep, code):
    failed = response(code)
    http_get.side_effect = [failed, response()]
    assert weather_api.get_weather_data("Paris", "test-key")["status"] == "success"
    assert http_get.call_count == 2
    failed.close.assert_called_once()
    retry_sleep.assert_called_once_with(1)


@pytest.mark.parametrize("code", [400, 401, 403, 404, 501])
def test_permanent_http_error_is_not_retried(http_get, response, retry_sleep, code):
    http_get.side_effect = None
    http_get.return_value = response(code)
    assert weather_api.get_weather_data("Paris", "test-key")["status"] == "error"
    assert http_get.call_count == 1
    retry_sleep.assert_not_called()


@pytest.mark.parametrize("error_type", [requests.exceptions.Timeout, requests.exceptions.ConnectionError])
def test_network_failure_stops_after_three_attempts_without_logging_key(http_get, retry_sleep, caplog, error_type):
    secret = "do-not-log-this-api-key"
    http_get.side_effect = error_type(f"https://api.weatherapi.com/?key={secret}")
    assert weather_api.get_weather_data("Paris", secret)["status"] == "error"
    assert http_get.call_count == 3
    assert retry_sleep.call_count == 2
    assert secret not in caplog.text


def test_repeated_http_error_stops_after_three_attempts(http_get, response, retry_sleep):
    http_get.side_effect = [response(503), response(503), response(503)]
    assert weather_api.get_weather_data("Paris", "test-key")["status"] == "error"
    assert http_get.call_count == 3
    assert retry_sleep.call_count == 2


def test_retry_after_seconds_is_respected(http_get, response, retry_sleep):
    http_get.side_effect = [response(429, {"Retry-After": "8"}), response()]
    assert weather_api.get_weather_data("Paris", "test-key")["status"] == "success"
    retry_sleep.assert_called_once_with(8)


def test_retry_after_http_date_is_respected(monkeypatch, http_get, response, retry_sleep):
    class FrozenClock:
        @staticmethod
        def now(tz):
            return datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(weather_api, "datetime", FrozenClock)
    http_get.side_effect = [response(503, {"Retry-After": "Sat, 17 Oct 2026 12:00:06 GMT"}), response()]
    assert weather_api.get_weather_data("Paris", "test-key")["status"] == "success"
    retry_sleep.assert_called_once_with(6)


@pytest.mark.parametrize("header", ["invalid", "-10", "Sat, 01 Jan 2000 00:00:00 GMT"])
def test_invalid_or_expired_retry_after_uses_backoff(http_get, response, retry_sleep, header):
    http_get.side_effect = [response(429, {"Retry-After": header}), response()]
    assert weather_api.get_weather_data("Paris", "test-key")["status"] == "success"
    retry_sleep.assert_called_once_with(1)


def test_invalid_json_is_a_city_failure(http_get, response, retry_sleep):
    invalid = response()
    invalid.json.side_effect = ValueError("Invalid JSON")
    http_get.side_effect = None
    http_get.return_value = invalid
    assert weather_api.get_weather_data("Paris", "test-key")["status"] == "error"
    assert http_get.call_count == 1
    retry_sleep.assert_not_called()


def test_retry_after_at_limit_is_respected(http_get, response, retry_sleep):
    http_get.side_effect = [response(429, {"Retry-After": "30"}), response()]
    assert weather_api.get_weather_data("Paris", "test-key")["status"] == "success"
    retry_sleep.assert_called_once_with(30)


@pytest.mark.parametrize("header", ["31", "3600", "Sun, 18 Oct 2026 12:00:00 GMT"])
def test_retry_after_beyond_limit_fails_city_without_waiting(monkeypatch, http_get, response, retry_sleep, caplog, header):
    class FrozenClock:
        @staticmethod
        def now(tz):
            return datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(weather_api, "datetime", FrozenClock)
    http_get.side_effect = None
    http_get.return_value = response(429, {"Retry-After": header})
    assert weather_api.get_weather_data("Paris", "test-key")["status"] == "error"
    assert http_get.call_count == 1
    retry_sleep.assert_not_called()
    assert "not retrying" in caplog.text
