from unittest.mock import Mock

import pytest

from shared import azure_storage


@pytest.mark.parametrize("timestamp, partition", [
    ("2026-10-17T12:34:56+00:00", "2026/10/17/12"),
    ("2026-10-17T00:30:00+02:00", "2026/10/16/22"),
    ("2026-10-17T12:34:56", "2026/10/17/12"),
    ("2026-10-17T12:34:56Z", "2026/10/17/12"),
])
def test_bronze_partition_uses_utc_timestamp_not_id(monkeypatch, timestamp, partition):
    client = Mock()
    client.get_blob_client.return_value.url = "https://test/bronze/result.json"
    monkeypatch.setattr(azure_storage.BlobServiceClient, "from_connection_string", Mock(return_value=client))
    data = {"ingestion_timestamp": timestamp, "cities": []}
    azure_storage.upload_to_bronze(data, "opaque-unique-id")
    client.get_blob_client.assert_called_once_with(
        container="bronze",
        blob=f"weather/weatherapi/{partition}/opaque-unique-id_weather_data.json",
    )


def test_distinct_ingestions_have_distinct_staging_paths(monkeypatch):
    client = Mock()
    monkeypatch.setattr(azure_storage.BlobServiceClient, "from_connection_string", Mock(return_value=client))
    for suffix in ("first", "second"):
        azure_storage.upload_to_staging({"ingestion_id": f"20261017_120000_{suffix}"})
    paths = [call.kwargs["blob"] for call in client.get_blob_client.call_args_list]
    assert len(set(paths)) == 2
