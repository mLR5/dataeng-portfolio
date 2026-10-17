import json

import azure.functions as func
import pytest

from shared import claim_check, config
import manual_weather_trigger
import weather_ingestion_trigger


@pytest.mark.parametrize("codes, expected, successful", [
    ([200, 200, 200], "success", 3),
    ([200, 401, 200], "partial", 2),
    ([401, 401, 401], "failed", 0),
])
def test_ingestion_archives_every_outcome(http_get, response, ingestion_io, codes, expected, successful):
    http_get.side_effect = [response(code) for code in codes]
    result = weather_ingestion_trigger.execute_ingestion()
    assert result["status"] == expected
    assert result["cities_retrieved"] == successful
    assert result["cities_failed"] == 3 - successful
    archived = ingestion_io.upload.call_args.args[0]
    assert archived["status"] == expected
    assert len(archived["cities"]) == 3
    ingestion_io.send.assert_called_once()
    message = ingestion_io.send.call_args.args[0]
    assert message["ingestion_id"] == result["ingestion_id"]
    assert message["status"] == expected
    assert message["payload_size_bytes"] == 1234


def test_published_message_is_accepted_by_bronze_consumer(http_get, response, ingestion_io):
    http_get.side_effect = [response() for _ in range(3)]
    weather_ingestion_trigger.execute_ingestion()
    published = json.dumps(ingestion_io.send.call_args.args[0])
    message = claim_check.parse_message(published.encode("utf-8"))
    assert message["schema_version"] == claim_check.SCHEMA_VERSION
    assert message["blob_container"] == "staging"
    assert message["blob_name"] == f"raw-ingestion/{message['ingestion_id']}.json"


def test_invalid_configuration_stops_before_external_calls(monkeypatch, http_get, ingestion_io):
    monkeypatch.setattr(config, "WEATHERAPI_KEY", None)
    with pytest.raises(config.ConfigurationError, match="WEATHERAPI_KEY"):
        weather_ingestion_trigger.execute_ingestion()
    http_get.assert_not_called()
    ingestion_io.upload.assert_not_called()
    ingestion_io.send.assert_not_called()


@pytest.mark.parametrize("codes, expected, status_code", [
    ([200, 200, 200], "success", 200),
    ([200, 401, 200], "partial", 200),
    ([401, 401, 401], "failed", 503),
])
def test_http_exposes_collection_outcome(http_get, response, ingestion_io, codes, expected, status_code):
    http_get.side_effect = [response(code) for code in codes]
    req = func.HttpRequest(method="POST", url="http://localhost/api/weather/ingest", body=b"")
    result = manual_weather_trigger.main(req)
    assert result.status_code == status_code
    assert json.loads(result.get_body())["status"] == expected
    ingestion_io.send.assert_called_once()


def test_http_reports_invalid_configuration(monkeypatch, http_get, ingestion_io):
    monkeypatch.setattr(config, "STORAGE_CONNECTION_STRING", "")
    req = func.HttpRequest(method="POST", url="http://localhost/api/weather/ingest", body=b"")
    result = manual_weather_trigger.main(req)
    assert result.status_code == 500
    assert json.loads(result.get_body())["status"] == "failed"
    http_get.assert_not_called()


def test_timer_raises_for_total_collection_failure(http_get, response, ingestion_io):
    http_get.side_effect = [response(401) for _ in range(3)]
    with pytest.raises(RuntimeError, match="no cities retrieved"):
        weather_ingestion_trigger.main(None)
    ingestion_io.send.assert_called_once()


@pytest.mark.parametrize("codes, level", [([200, 200, 200], "INFO"), ([200, 401, 200], "WARNING")])
def test_timer_logs_complete_or_partial_result(http_get, response, ingestion_io, caplog, codes, level):
    http_get.side_effect = [response(code) for code in codes]
    with caplog.at_level("INFO"):
        weather_ingestion_trigger.main(None)
    records = [record for record in caplog.records if "Ingestion Completed:" in record.message]
    assert len(records) == 1
    assert records[0].levelname == level


def test_staging_failure_prevents_queue_publication(http_get, response, ingestion_io):
    http_get.side_effect = [response() for _ in range(3)]
    ingestion_io.upload.side_effect = RuntimeError("Storage unavailable")
    with pytest.raises(RuntimeError, match="Storage unavailable"):
        weather_ingestion_trigger.execute_ingestion()
    ingestion_io.send.assert_not_called()
