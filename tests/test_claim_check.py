import json

import pytest

from shared import claim_check

INGESTION_ID = "20261017_120000_" + "a" * 32


def message(**overrides):
    body = {
        "type": "claim_check",
        "schema_version": 1,
        "ingestion_id": INGESTION_ID,
        "ingestion_timestamp": "2026-10-17T12:00:00+00:00",
        "status": "success",
        "blob_container": "staging",
        "blob_name": f"raw-ingestion/{INGESTION_ID}.json",
        "blob_url": f"https://test.blob.core.windows.net/staging/raw-ingestion/{INGESTION_ID}.json",
        "payload_size_bytes": 1234,
        "cities_count": 3,
        "summary": {"total_cities": 3, "successful": 3, "failed": 0},
    }
    body.update(overrides)
    return json.dumps({k: v for k, v in body.items() if v is not None}).encode("utf-8")


def test_built_message_round_trips():
    data = {
        "ingestion_id": INGESTION_ID,
        "ingestion_timestamp": "2026-10-17T12:00:00+00:00",
        "status": "partial",
        "summary": {"total_cities": 3, "successful": 2, "failed": 1},
    }
    staged = {"container": "staging", "blob_name": f"raw-ingestion/{INGESTION_ID}.json", "url": "https://test", "size_bytes": 99}
    built = claim_check.build_message(data, staged)
    assert built["schema_version"] == claim_check.SCHEMA_VERSION
    assert claim_check.parse_message(json.dumps(built)) == built


def test_message_without_version_published_before_v1_is_accepted():
    parsed = claim_check.parse_message(message(schema_version=None, status=None))
    assert parsed["schema_version"] == 1
    assert parsed["payload_size_bytes"] == 1234


def test_legacy_size_field_is_normalized():
    parsed = claim_check.parse_message(message(schema_version=None, payload_size_bytes=None, size_bytes=42))
    assert parsed["payload_size_bytes"] == 42


def test_legacy_ingestion_id_without_uuid_is_accepted():
    parsed = claim_check.parse_message(message(ingestion_id="20261017_120000", blob_name="raw-ingestion/20261017_120000.json"))
    assert parsed["ingestion_id"] == "20261017_120000"


@pytest.mark.parametrize("body, reason", [
    (b"not json", "not valid JSON"),
    (b"\xff\xfe", "not valid JSON"),
    (b"[]", "JSON object"),
    (message(type="other"), "Unsupported message type"),
    (message(type=None), "Unsupported message type"),
    (message(schema_version=2), "Unsupported schema_version"),
    (message(ingestion_id=None), "ingestion_id"),
    (message(ingestion_timestamp=""), "ingestion_timestamp"),
    (message(blob_name=None), "blob_name"),
    (message(ingestion_id="../../weather/x"), "ingestion_id format"),
    (message(ingestion_timestamp="yesterday"), "ingestion_timestamp"),
])
def test_invalid_message_is_rejected(body, reason):
    with pytest.raises(claim_check.InvalidMessageError, match=reason):
        claim_check.parse_message(body)


@pytest.mark.parametrize("container, blob_name", [
    ("bronze", f"raw-ingestion/{INGESTION_ID}.json"),
    ("staging", "raw-ingestion/20261017_120000.json"),
    ("staging", f"other/{INGESTION_ID}.json"),
])
def test_blob_reference_outside_expected_staging_blob_is_rejected(container, blob_name):
    with pytest.raises(claim_check.InvalidMessageError, match="Blob reference"):
        claim_check.parse_message(message(blob_container=container, blob_name=blob_name))
