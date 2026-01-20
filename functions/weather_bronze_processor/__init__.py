import azure.functions as func
import logging
import json
import time
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from shared.azure_storage import download_from_staging, upload_to_bronze, delete_staging_blob

logger = logging.getLogger(__name__)


def main(msg: func.QueueMessage) -> None:
    start_time = time.time()
    logger.info("=== Bronze Processor Queue Trigger Started ===")

    try:
        # Parser message JSON
        message_body = msg.get_body().decode('utf-8')
        message_data = json.loads(message_body)

        blob_url = message_data['blob_url']
        ingestion_id = message_data['ingestion_id']
        size_bytes = message_data.get('size_bytes', 0)

        logger.info(f"Processing ingestion {ingestion_id} (size: {size_bytes} bytes)")
        logger.info(f"Message ID: {msg.id}, Dequeue count: {msg.dequeue_count}")

        # Étape 1: Télécharger blob depuis staging
        logger.info("Step 1: Downloading blob from staging")
        bronze_data = download_from_staging(blob_url)

        # Étape 2: Upload vers bronze layer
        logger.info("Step 2: Uploading to bronze layer")
        bronze_url = upload_to_bronze(bronze_data, ingestion_id)

        # Étape 3: Supprimer blob staging
        logger.info("Step 3: Deleting staging blob")
        delete_staging_blob(blob_url)

        duration_ms = int((time.time() - start_time) * 1000)

        logger.info(f"=== Bronze Processing Completed Successfully ({duration_ms}ms) ===")
        logger.info(f"Bronze URL: {bronze_url}")

    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        logger.error(f"=== Bronze Processing Failed ({duration_ms}ms) ===")
        logger.error(f"Error: {str(e)}", exc_info=True)
        logger.error(f"Message will be retried (attempt {msg.dequeue_count}/5)")
        raise
