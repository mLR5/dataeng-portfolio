# Module pour les opérations Azure Storage
import json
import logging
from azure.storage.blob import BlobServiceClient, BlobClient
from azure.storage.queue import QueueClient
from azure.core.exceptions import AzureError
from tenacity import retry, stop_after_attempt, wait_exponential, before_sleep_log
from .config import STORAGE_CONNECTION_STRING, STAGING_CONTAINER, BRONZE_CONTAINER, QUEUE_NAME, QUEUE_MESSAGE_TTL_SECONDS, BRONZE_PATH_PREFIX

logger = logging.getLogger(__name__)


# Upload vers staging blob, retourne blob_url
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    before_sleep=before_sleep_log(logger, logging.WARNING)
)
def upload_to_staging(bronze_data):
    ingestion_id = bronze_data['ingestion_id']
    blob_name = f"raw-ingestion/{ingestion_id}.json"

    logger.info(f"Uploading to staging: {blob_name}")

    try:
        blob_service_client = BlobServiceClient.from_connection_string(STORAGE_CONNECTION_STRING)

        blob_client = blob_service_client.get_blob_client(
            container=STAGING_CONTAINER,
            blob=blob_name
        )

        json_data = json.dumps(bronze_data, ensure_ascii=False, indent=2)
        blob_client.upload_blob(json_data, overwrite=True)

        blob_url = blob_client.url
        logger.info(f"Uploaded {len(json_data.encode('utf-8'))} bytes to {blob_url}")

        return blob_url

    except AzureError as e:
        logger.error(f"Azure Storage error uploading blob {blob_name}: {str(e)}")
        raise


# Envoie message Claim Check vers la queue, retourne message_id
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    before_sleep=before_sleep_log(logger, logging.WARNING)
)
def send_queue_message(blob_url, ingestion_id, size_bytes, ingestion_timestamp, cities_count, blob_container, blob_name, summary, queue_name=QUEUE_NAME):
    logger.info(f"Sending message to queue {queue_name} for ingestion {ingestion_id}")

    try:
        message = {
            "type": "claim_check",
            "blob_container": blob_container,
            "blob_name": blob_name,
            "blob_url": blob_url,
            "ingestion_id": ingestion_id,
            "ingestion_timestamp": ingestion_timestamp,
            "cities_count": cities_count,
            "payload_size_bytes": size_bytes,
            "summary": summary
        }

        queue_client = QueueClient.from_connection_string(STORAGE_CONNECTION_STRING, queue_name)

        message_content = json.dumps(message, ensure_ascii=False)
        response = queue_client.send_message(message_content, time_to_live=QUEUE_MESSAGE_TTL_SECONDS)

        message_id = response.id
        logger.info(f"Message sent to queue: {message_id} ({len(message_content.encode('utf-8'))} bytes)")

        return message_id

    except AzureError as e:
        # Log blob_url pour cleanup manuel si queue échoue
        logger.error(f"Azure Storage error sending message to queue. Blob orphaned at: {blob_url}")
        logger.error(f"Queue error details: {str(e)}")
        raise


# Upload vers bronze layer avec partitionnement temporel
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    before_sleep=before_sleep_log(logger, logging.WARNING)
)
def upload_to_bronze(bronze_data, ingestion_id):
    # Parser ingestion_id "20251202_150000" → YYYY/MM/DD/HH
    year = ingestion_id[0:4]
    month = ingestion_id[4:6]
    day = ingestion_id[6:8]
    hour = ingestion_id[9:11]

    directory_path = f"{BRONZE_PATH_PREFIX}/{year}/{month}/{day}/{hour}"
    file_name = f"{ingestion_id}_weather_data.json"
    blob_name = f"{directory_path}/{file_name}"

    logger.info(f"Uploading to bronze: {blob_name}")

    try:
        blob_service_client = BlobServiceClient.from_connection_string(STORAGE_CONNECTION_STRING)

        blob_client = blob_service_client.get_blob_client(
            container=BRONZE_CONTAINER,
            blob=blob_name
        )

        json_data = json.dumps(bronze_data, ensure_ascii=False, indent=2)
        blob_client.upload_blob(json_data, overwrite=True)

        bronze_url = blob_client.url
        logger.info(f"Uploaded {len(json_data.encode('utf-8'))} bytes to {bronze_url}")

        return bronze_url

    except AzureError as e:
        logger.error(f"Azure Storage error uploading blob {blob_name}: {str(e)}")
        raise


# Télécharge blob depuis staging
def download_from_staging(blob_url):
    logger.info(f"Downloading blob from staging: {blob_url}")

    try:
        # Extraire container et blob_name depuis l'URL
        # Format: https://<account>.blob.core.windows.net/<container>/<blob_name>
        from urllib.parse import urlparse
        parsed = urlparse(blob_url)
        path_parts = parsed.path.lstrip('/').split('/', 1)
        container_name = path_parts[0]
        blob_name = path_parts[1] if len(path_parts) > 1 else ''

        blob_service_client = BlobServiceClient.from_connection_string(STORAGE_CONNECTION_STRING)
        blob_client = blob_service_client.get_blob_client(container=container_name, blob=blob_name)

        download_stream = blob_client.download_blob()
        json_data = download_stream.readall().decode('utf-8')
        bronze_data = json.loads(json_data)

        logger.info(f"Downloaded {len(json_data.encode('utf-8'))} bytes from staging")

        return bronze_data

    except AzureError as e:
        logger.error(f"Azure Storage error downloading blob from {blob_url}: {str(e)}")
        raise


# Supprime blob staging
def delete_staging_blob(blob_url):
    logger.info(f"Deleting staging blob: {blob_url}")

    try:
        # Extraire container et blob_name depuis l'URL
        from urllib.parse import urlparse
        parsed = urlparse(blob_url)
        path_parts = parsed.path.lstrip('/').split('/', 1)
        container_name = path_parts[0]
        blob_name = path_parts[1] if len(path_parts) > 1 else ''

        blob_service_client = BlobServiceClient.from_connection_string(STORAGE_CONNECTION_STRING)
        blob_client = blob_service_client.get_blob_client(container=container_name, blob=blob_name)

        blob_client.delete_blob()

        logger.info(f"Deleted staging blob: {blob_url}")

    except AzureError as e:
        logger.error(f"Azure Storage error deleting blob {blob_url}: {str(e)}")
        raise
