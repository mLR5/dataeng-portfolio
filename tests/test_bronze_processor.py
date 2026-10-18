import json
from types import SimpleNamespace
from unittest.mock import Mock, call

import azure.functions as func
import pytest

from shared import claim_check
from shared.azure_storage import StagingBlobNotFoundError
import weather_bronze_processor

INGESTION_ID = "20261017_120000_" + "a" * 32
STAGING_NAME = f"raw-ingestion/{INGESTION_ID}.json"
BRONZE_NAME = f"weather/weatherapi/2026/10/17/12/{INGESTION_ID}_weather_data.json"
MESSAGE = {
    "type": "claim_check",
    "schema_version": 1,
    "ingestion_id": INGESTION_ID,
    "ingestion_timestamp": "2026-10-17T12:00:00+00:00",
    "status": "success",
    "blob_container": "staging",
    "blob_name": STAGING_NAME,
    "payload_size_bytes": 1234,
}


@pytest.fixture
def bronze_io(monkeypatch):
    io = SimpleNamespace(
        exists=Mock(return_value=False),
        download=Mock(return_value={
            "ingestion_id": INGESTION_ID,
            "ingestion_timestamp": MESSAGE["ingestion_timestamp"],
            "cities": [],
        }),
        upload=Mock(return_value=("https://test/bronze/blob.json", True)),
        delete=Mock(return_value=True),
    )
    for name, mock in {
        "bronze_blob_exists": io.exists, "download_staging_blob": io.download,
        "upload_to_bronze": io.upload, "delete_staging_blob": io.delete,
    }.items():
        monkeypatch.setattr(weather_bronze_processor, name, mock)
    return io


def queue_message(body):
    msg = Mock(spec=func.QueueMessage, id="message-id", dequeue_count=1)
    msg.get_body.return_value = body
    return msg


def test_first_delivery_promotes_staging_to_bronze(bronze_io):
    result = weather_bronze_processor.process_claim_check(MESSAGE)
    assert result == {
        "ingestion_id": INGESTION_ID, "bronze_blob": BRONZE_NAME,
        "bronze_created": True, "staging_deleted": True,
    }
    bronze_io.exists.assert_called_once_with(BRONZE_NAME)
    bronze_io.download.assert_called_once_with(STAGING_NAME)
    bronze_io.upload.assert_called_once_with(bronze_io.download.return_value, BRONZE_NAME)
    bronze_io.delete.assert_called_once_with(STAGING_NAME)


def test_replay_after_complete_processing_does_nothing(bronze_io):
    bronze_io.exists.return_value = True
    bronze_io.delete.return_value = False
    result = weather_bronze_processor.process_claim_check(MESSAGE)
    assert result["bronze_created"] is False
    assert result["staging_deleted"] is False
    bronze_io.download.assert_not_called()
    bronze_io.upload.assert_not_called()


def test_replay_after_crash_before_cleanup_only_cleans_staging(bronze_io):
    bronze_io.exists.return_value = True
    result = weather_bronze_processor.process_claim_check(MESSAGE)
    assert result["staging_deleted"] is True
    bronze_io.upload.assert_not_called()
    bronze_io.delete.assert_called_once_with(STAGING_NAME)


def test_concurrent_duplicate_keeps_existing_bronze(bronze_io):
    bronze_io.upload.return_value = ("https://test/bronze/blob.json", False)
    result = weather_bronze_processor.process_claim_check(MESSAGE)
    assert result["bronze_created"] is False
    bronze_io.delete.assert_called_once_with(STAGING_NAME)


def test_concurrent_completion_before_staging_read_is_recognized(bronze_io):
    bronze_io.exists.side_effect = [False, True]
    bronze_io.download.side_effect = StagingBlobNotFoundError("already cleaned by another consumer")
    bronze_io.delete.return_value = False

    result = weather_bronze_processor.process_claim_check(MESSAGE)

    assert result["bronze_created"] is False
    assert result["staging_deleted"] is False
    bronze_io.upload.assert_not_called()
    bronze_io.delete.assert_called_once_with(STAGING_NAME)
    assert bronze_io.exists.call_args_list == [call(BRONZE_NAME), call(BRONZE_NAME)]


def test_missing_staging_does_not_hide_a_failed_bronze_recheck(bronze_io):
    bronze_io.exists.side_effect = [False, RuntimeError("Storage unavailable")]
    bronze_io.download.side_effect = StagingBlobNotFoundError("missing")

    with pytest.raises(RuntimeError, match="Storage unavailable"):
        weather_bronze_processor.process_claim_check(MESSAGE)

    bronze_io.upload.assert_not_called()
    bronze_io.delete.assert_not_called()


def test_missing_staging_without_bronze_fails_without_side_effects(bronze_io):
    bronze_io.download.side_effect = StagingBlobNotFoundError("missing")
    with pytest.raises(StagingBlobNotFoundError):
        weather_bronze_processor.process_claim_check(MESSAGE)
    bronze_io.upload.assert_not_called()
    bronze_io.delete.assert_not_called()


def test_staging_content_for_another_ingestion_is_not_promoted(bronze_io):
    bronze_io.download.return_value = {"ingestion_id": "20261017_120000"}
    with pytest.raises(weather_bronze_processor.StagingContentMismatchError):
        weather_bronze_processor.process_claim_check(MESSAGE)
    bronze_io.upload.assert_not_called()
    bronze_io.delete.assert_not_called()


@pytest.mark.parametrize("timestamp", [
    "2026-10-17T13:00:00+00:00",
    "2026-10-17T12:00:00.123+00:00",
])
def test_staging_timestamp_mismatch_is_not_promoted_or_deleted(bronze_io, timestamp):
    bronze_io.download.return_value["ingestion_timestamp"] = timestamp

    with pytest.raises(weather_bronze_processor.StagingContentMismatchError, match="ingestion_timestamp"):
        weather_bronze_processor.process_claim_check(MESSAGE)

    bronze_io.upload.assert_not_called()
    bronze_io.delete.assert_not_called()


@pytest.mark.parametrize("timestamp", [None, "not-a-timestamp"])
def test_invalid_staging_timestamp_is_not_promoted_or_deleted(bronze_io, timestamp):
    bronze_io.download.return_value["ingestion_timestamp"] = timestamp

    with pytest.raises(weather_bronze_processor.StagingContentMismatchError, match="ingestion_timestamp"):
        weather_bronze_processor.process_claim_check(MESSAGE)

    bronze_io.upload.assert_not_called()
    bronze_io.delete.assert_not_called()


def test_missing_staging_timestamp_is_not_promoted_or_deleted(bronze_io):
    del bronze_io.download.return_value["ingestion_timestamp"]

    with pytest.raises(weather_bronze_processor.StagingContentMismatchError, match="ingestion_timestamp"):
        weather_bronze_processor.process_claim_check(MESSAGE)

    bronze_io.upload.assert_not_called()
    bronze_io.delete.assert_not_called()


@pytest.mark.parametrize("timestamp", [
    "2026-10-17T14:00:00+02:00",
    "2026-10-17T12:00:00",
    "2026-10-17T12:00:00Z",
])
def test_equivalent_staging_timestamps_use_the_same_utc_partition(bronze_io, timestamp):
    bronze_io.download.return_value["ingestion_timestamp"] = timestamp

    result = weather_bronze_processor.process_claim_check(MESSAGE)

    assert result["bronze_blob"] == BRONZE_NAME
    assert result["bronze_created"] is True
    bronze_io.upload.assert_called_once_with(bronze_io.download.return_value, BRONZE_NAME)


def test_upload_failure_keeps_staging(bronze_io):
    bronze_io.upload.side_effect = RuntimeError("Storage unavailable")
    with pytest.raises(RuntimeError):
        weather_bronze_processor.process_claim_check(MESSAGE)
    bronze_io.delete.assert_not_called()


def test_cleanup_failure_does_not_fail_processing(bronze_io, caplog):
    bronze_io.delete.side_effect = RuntimeError("Storage unavailable")
    result = weather_bronze_processor.process_claim_check(MESSAGE)
    assert result["bronze_created"] is True
    assert result["staging_deleted"] is False
    assert "left to lifecycle policy" in caplog.text


def test_queue_trigger_processes_valid_message(bronze_io):
    weather_bronze_processor.main(queue_message(json.dumps(MESSAGE).encode("utf-8")))
    bronze_io.upload.assert_called_once()


def test_queue_trigger_rejects_invalid_message_before_storage(bronze_io):
    body = json.dumps({**MESSAGE, "blob_container": "bronze"}).encode("utf-8")
    with pytest.raises(claim_check.InvalidMessageError):
        weather_bronze_processor.main(queue_message(body))
    bronze_io.exists.assert_not_called()
    bronze_io.delete.assert_not_called()


def test_queue_trigger_reraises_storage_failure_for_retry(bronze_io, caplog):
    bronze_io.exists.side_effect = RuntimeError("Storage unavailable")
    with pytest.raises(RuntimeError, match="Storage unavailable"):
        weather_bronze_processor.main(queue_message(json.dumps(MESSAGE).encode("utf-8")))
    assert "Message will be retried (attempt 1/5)" in caplog.text
