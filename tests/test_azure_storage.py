import json
from unittest.mock import Mock

import pytest
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

from shared import azure_storage, claim_check


@pytest.fixture
def storage(monkeypatch):
    service = Mock()
    service.get_blob_client.return_value.url = "https://test.blob.core.windows.net/container/blob.json"
    monkeypatch.setattr(azure_storage.BlobServiceClient, "from_connection_string", Mock(return_value=service))
    return service


@pytest.mark.parametrize("timestamp, partition", [
    ("2026-10-17T12:34:56+00:00", "2026/10/17/12"),
    ("2026-10-17T00:30:00+02:00", "2026/10/16/22"),
    ("2026-10-17T12:34:56", "2026/10/17/12"),
    ("2026-10-17T12:34:56Z", "2026/10/17/12"),
])
def test_bronze_partition_uses_utc_timestamp_not_id(timestamp, partition):
    assert claim_check.bronze_blob_name("opaque-unique-id", timestamp) == (
        f"weather/weatherapi/{partition}/opaque-unique-id_weather_data.json"
    )


def test_distinct_ingestions_have_distinct_staging_paths(storage):
    for suffix in ("first", "second"):
        azure_storage.upload_to_staging({"ingestion_id": f"20261017_120000_{suffix}"})
    paths = [call.kwargs["blob"] for call in storage.get_blob_client.call_args_list]
    assert len(set(paths)) == 2


def test_staging_upload_reports_exact_uploaded_size(storage):
    data = {"ingestion_id": "20261017_120000", "city": "Liège"}
    staged = azure_storage.upload_to_staging(data)
    uploaded = storage.get_blob_client.return_value.upload_blob.call_args.args[0]
    assert staged["size_bytes"] == len(uploaded)
    assert staged["container"] == "staging"
    assert staged["blob_name"] == "raw-ingestion/20261017_120000.json"


def test_bronze_upload_never_overwrites(storage):
    url, created = azure_storage.upload_to_bronze({"ingestion_id": "x"}, "weather/x.json")
    assert created is True
    storage.get_blob_client.assert_called_once_with(container="bronze", blob="weather/x.json")
    assert storage.get_blob_client.return_value.upload_blob.call_args.kwargs["overwrite"] is False


def test_existing_bronze_blob_is_kept_without_retry(storage):
    blob = storage.get_blob_client.return_value
    blob.upload_blob.side_effect = ResourceExistsError("exists")
    url, created = azure_storage.upload_to_bronze({"ingestion_id": "x"}, "weather/x.json")
    assert created is False
    assert url == blob.url
    blob.upload_blob.assert_called_once()


def test_bronze_existence_check_targets_bronze_container(storage):
    storage.get_blob_client.return_value.exists.return_value = True
    assert azure_storage.bronze_blob_exists("weather/x.json") is True
    storage.get_blob_client.assert_called_once_with(container="bronze", blob="weather/x.json")


def test_missing_staging_blob_raises_dedicated_error(storage):
    storage.get_blob_client.return_value.download_blob.side_effect = ResourceNotFoundError("missing")
    with pytest.raises(azure_storage.StagingBlobNotFoundError, match="staging/raw-ingestion/x.json"):
        azure_storage.download_staging_blob("raw-ingestion/x.json")


def test_staging_download_reads_staging_container(storage):
    payload = json.dumps({"ingestion_id": "x"}).encode("utf-8")
    storage.get_blob_client.return_value.download_blob.return_value.readall.return_value = payload
    assert azure_storage.download_staging_blob("raw-ingestion/x.json") == {"ingestion_id": "x"}
    storage.get_blob_client.assert_called_once_with(container="staging", blob="raw-ingestion/x.json")


def test_staging_delete_is_idempotent(storage):
    blob = storage.get_blob_client.return_value
    assert azure_storage.delete_staging_blob("raw-ingestion/x.json") is True
    blob.delete_blob.side_effect = ResourceNotFoundError("missing")
    assert azure_storage.delete_staging_blob("raw-ingestion/x.json") is False
    for call in storage.get_blob_client.call_args_list:
        assert call.kwargs["container"] == "staging"


def test_queue_message_uses_configured_queue(monkeypatch):
    queue = Mock()
    queue.send_message.return_value.id = "message-id"
    from_connection_string = Mock(return_value=queue)
    monkeypatch.setattr(azure_storage.QueueClient, "from_connection_string", from_connection_string)
    message = {"ingestion_id": "x", "blob_url": "https://test/staging/x.json"}
    assert azure_storage.send_queue_message(message) == "message-id"
    assert from_connection_string.call_args.args[1] == "ingestion-weatherapi"
    assert json.loads(queue.send_message.call_args.args[0]) == message
