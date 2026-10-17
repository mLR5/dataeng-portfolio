# Module pour les opérations Azure Storage
import json
import logging
from azure.storage.blob import BlobServiceClient
from azure.storage.queue import QueueClient
from azure.core.exceptions import AzureError, ResourceExistsError, ResourceNotFoundError
from tenacity import retry, stop_after_attempt, wait_exponential, before_sleep_log
from . import config
from .claim_check import staging_blob_name

logger = logging.getLogger(__name__)


class StagingBlobNotFoundError(Exception):
    """Blob staging absent alors que Bronze n'a pas été produit."""


def _blob_client(container, blob_name):
    blob_service_client = BlobServiceClient.from_connection_string(config.STORAGE_CONNECTION_STRING)
    return blob_service_client.get_blob_client(container=container, blob=blob_name)


# Upload vers staging blob, retourne la référence du blob et sa taille
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    before_sleep=before_sleep_log(logger, logging.WARNING)
)
def upload_to_staging(bronze_data):
    blob_name = staging_blob_name(bronze_data['ingestion_id'])

    logger.info(f"Uploading to staging: {blob_name}")

    try:
        blob_client = _blob_client(config.STAGING_CONTAINER, blob_name)

        payload = json.dumps(bronze_data, ensure_ascii=False, indent=2).encode('utf-8')
        blob_client.upload_blob(payload, overwrite=True)

        logger.info(f"Uploaded {len(payload)} bytes to {blob_client.url}")

        return {
            "container": config.STAGING_CONTAINER,
            "blob_name": blob_name,
            "url": blob_client.url,
            "size_bytes": len(payload),
        }

    except AzureError as e:
        logger.error(f"Azure Storage error uploading blob {blob_name}: {str(e)}")
        raise


# Envoie message Claim Check vers la queue, retourne message_id
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    before_sleep=before_sleep_log(logger, logging.WARNING)
)
def send_queue_message(message, queue_name=None):
    queue_name = queue_name or config.QUEUE_NAME
    logger.info(f"Sending message to queue {queue_name} for ingestion {message['ingestion_id']}")

    try:
        queue_client = QueueClient.from_connection_string(config.STORAGE_CONNECTION_STRING, queue_name)

        message_content = json.dumps(message, ensure_ascii=False)
        response = queue_client.send_message(message_content, time_to_live=config.QUEUE_MESSAGE_TTL_SECONDS)

        message_id = response.id
        logger.info(f"Message sent to queue: {message_id} ({len(message_content.encode('utf-8'))} bytes)")

        return message_id

    except AzureError as e:
        # Log blob_url pour cleanup manuel si queue échoue
        logger.error(f"Azure Storage error sending message to queue. Blob orphaned at: {message['blob_url']}")
        logger.error(f"Queue error details: {str(e)}")
        raise


# Indique si le blob Bronze a déjà été produit
def bronze_blob_exists(blob_name):
    return _blob_client(config.BRONZE_CONTAINER, blob_name).exists()


# Upload vers Bronze sans jamais écraser, retourne (url, created)
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    before_sleep=before_sleep_log(logger, logging.WARNING)
)
def upload_to_bronze(bronze_data, blob_name):
    logger.info(f"Uploading to bronze: {blob_name}")

    try:
        blob_client = _blob_client(config.BRONZE_CONTAINER, blob_name)

        payload = json.dumps(bronze_data, ensure_ascii=False, indent=2).encode('utf-8')
        blob_client.upload_blob(payload, overwrite=False)

        logger.info(f"Uploaded {len(payload)} bytes to {blob_client.url}")
        return blob_client.url, True

    except ResourceExistsError:
        # Message dupliqué traité en parallèle, ou tentative précédente aboutie côté serveur.
        logger.info(f"Bronze blob already exists, keeping existing version: {blob_name}")
        return blob_client.url, False

    except AzureError as e:
        logger.error(f"Azure Storage error uploading blob {blob_name}: {str(e)}")
        raise


# Télécharge un blob du conteneur staging
def download_staging_blob(blob_name):
    logger.info(f"Downloading blob from staging: {blob_name}")

    try:
        download_stream = _blob_client(config.STAGING_CONTAINER, blob_name).download_blob()
        payload = download_stream.readall()

    except ResourceNotFoundError:
        raise StagingBlobNotFoundError(f"Staging blob not found: {config.STAGING_CONTAINER}/{blob_name}")

    except AzureError as e:
        logger.error(f"Azure Storage error downloading blob {blob_name}: {str(e)}")
        raise

    logger.info(f"Downloaded {len(payload)} bytes from staging")
    return json.loads(payload.decode('utf-8'))


# Supprime un blob staging, retourne False s'il était déjà supprimé
def delete_staging_blob(blob_name):
    logger.info(f"Deleting staging blob: {blob_name}")

    try:
        _blob_client(config.STAGING_CONTAINER, blob_name).delete_blob()

    except ResourceNotFoundError:
        logger.info(f"Staging blob already deleted: {blob_name}")
        return False

    logger.info(f"Deleted staging blob: {blob_name}")
    return True
