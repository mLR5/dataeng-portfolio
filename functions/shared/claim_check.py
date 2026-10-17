# Contrat des messages Claim Check entre la collecte et le traitement Bronze
import json
import re
from datetime import datetime, timezone

from . import config

MESSAGE_TYPE = "claim_check"
SCHEMA_VERSION = 1

# Anciens identifiants "YYYYMMDD_HHMMSS", nouveaux "YYYYMMDD_HHMMSS_<uuid4 hex>".
INGESTION_ID_PATTERN = re.compile(r"^\d{8}_\d{6}(_[0-9a-f]{32})?$")


class InvalidMessageError(ValueError):
    """Message inexploitable : le rejouer ne changera rien."""


def parse_timestamp(value):
    """Horodatage ISO en UTC ; les anciens horodatages sans fuseau sont déjà UTC."""
    timestamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def build_message(bronze_data, staged_blob):
    """Construit le message v1 à partir des données collectées et du blob staging."""
    return {
        "type": MESSAGE_TYPE,
        "schema_version": SCHEMA_VERSION,
        "ingestion_id": bronze_data['ingestion_id'],
        "ingestion_timestamp": bronze_data['ingestion_timestamp'],
        "status": bronze_data['status'],
        "blob_container": staged_blob['container'],
        "blob_name": staged_blob['blob_name'],
        "blob_url": staged_blob['url'],
        "payload_size_bytes": staged_blob['size_bytes'],
        "cities_count": bronze_data['summary']['total_cities'],
        "summary": bronze_data['summary'],
    }


def parse_message(body):
    """Valide un message reçu et le normalise au format v1.

    Les messages sans schema_version, publiés avant la v1, ont les mêmes champs
    obligatoires et sont acceptés ; leur taille peut s'appeler size_bytes.
    """
    try:
        message = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        raise InvalidMessageError("Message body is not valid JSON")
    if not isinstance(message, dict):
        raise InvalidMessageError("Message body must be a JSON object")

    if message.get("type") != MESSAGE_TYPE:
        raise InvalidMessageError(f"Unsupported message type: {message.get('type')!r}")
    version = message.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise InvalidMessageError(f"Unsupported schema_version: {version!r}")

    for field in ("ingestion_id", "ingestion_timestamp", "blob_container", "blob_name"):
        if not isinstance(message.get(field), str) or not message[field].strip():
            raise InvalidMessageError(f"Missing or invalid field: {field}")

    # Ces champs servent à construire des chemins de blobs : aucun écart n'est toléré.
    if not INGESTION_ID_PATTERN.match(message["ingestion_id"]):
        raise InvalidMessageError("Invalid ingestion_id format")
    try:
        parse_timestamp(message["ingestion_timestamp"])
    except ValueError:
        raise InvalidMessageError("Invalid ingestion_timestamp")
    expected_blob_name = staging_blob_name(message["ingestion_id"])
    if message["blob_container"] != config.STAGING_CONTAINER or message["blob_name"] != expected_blob_name:
        raise InvalidMessageError(
            f"Blob reference must be {config.STAGING_CONTAINER}/{expected_blob_name}"
        )

    return {
        **message,
        "schema_version": SCHEMA_VERSION,
        "payload_size_bytes": message.get("payload_size_bytes", message.get("size_bytes")),
    }


def staging_blob_name(ingestion_id):
    return f"{config.STAGING_PATH_PREFIX}{ingestion_id}.json"


def bronze_blob_name(ingestion_id, ingestion_timestamp):
    """Chemin Bronze déterministe : un même message produit toujours le même blob."""
    partition = f"{parse_timestamp(ingestion_timestamp):%Y/%m/%d/%H}"
    return f"{config.BRONZE_PATH_PREFIX}/{partition}/{ingestion_id}_weather_data.json"
