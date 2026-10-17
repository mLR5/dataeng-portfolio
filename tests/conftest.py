from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from shared import azure_storage, claim_check, config, weather_api
import weather_ingestion_trigger


@pytest.fixture(autouse=True)
def valid_configuration(monkeypatch):
    for name, value in {
        "WEATHERAPI_KEY": "test-only-api-key",
        "STORAGE_CONNECTION_STRING": "test-only-storage-connection",
        "QUEUE_NAME": "ingestion-weatherapi",
        "STAGING_CONTAINER": "staging",
        "BRONZE_CONTAINER": "bronze",
        "CITIES": ["Paris", "Londres", "Bruxelles"],
    }.items():
        monkeypatch.setattr(config, name, value)


@pytest.fixture(autouse=True)
def http_get(monkeypatch):
    get = Mock(side_effect=AssertionError("HTTP must be mocked in tests"))
    monkeypatch.setattr(weather_api.requests, "get", get)
    return get


@pytest.fixture(autouse=True)
def no_external_storage(monkeypatch):
    for client in (azure_storage.BlobServiceClient, azure_storage.QueueClient):
        monkeypatch.setattr(
            client, "from_connection_string",
            Mock(side_effect=AssertionError("Storage must be mocked in tests")),
        )


@pytest.fixture(autouse=True)
def retry_sleep(monkeypatch):
    sleep = Mock()
    monkeypatch.setattr(weather_api._call_api.retry, "sleep", sleep)
    return sleep


@pytest.fixture
def response():
    def make(status=200, headers=None, payload=None):
        result = Mock(spec=requests.Response)
        result.status_code = status
        result.headers = headers or {}
        result.json.return_value = payload if payload is not None else {"current": {"temp_c": 12}}
        return result
    return make


@pytest.fixture
def ingestion_io(monkeypatch):
    def staged(data):
        blob_name = claim_check.staging_blob_name(data["ingestion_id"])
        return {
            "container": "staging",
            "blob_name": blob_name,
            "url": f"https://test.blob.core.windows.net/staging/{blob_name}",
            "size_bytes": 1234,
        }
    upload = Mock(side_effect=staged)
    send = Mock(return_value="test-message")
    monkeypatch.setattr(weather_ingestion_trigger, "upload_to_staging", upload)
    monkeypatch.setattr(weather_ingestion_trigger, "send_queue_message", send)
    return SimpleNamespace(upload=upload, send=send)
