import azure.functions as func
import logging
import time
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from shared.weather_api import get_all_cities_data
from shared.azure_storage import upload_to_staging, send_queue_message
from shared.config import WEATHERAPI_KEY, STORAGE_ACCOUNT_NAME, QUEUE_NAME

logger = logging.getLogger(__name__)


def execute_ingestion():
    """Exécute le workflow d'ingestion complet: API → Blob → Queue"""
    start_time = time.time()

    # Étape 1: Récupérer données API
    logger.info("Step 1: Fetching weather data from API")
    bronze_data = get_all_cities_data(WEATHERAPI_KEY)
    ingestion_id = bronze_data['ingestion_metadata']['ingestion_id']
    cities_retrieved = bronze_data['summary']['total_cities_retrieved']
    cities_failed = bronze_data['summary']['total_cities_failed']

    logger.info(f"Weather data retrieved: {cities_retrieved} cities success, {cities_failed} failed (ID: {ingestion_id})")

    # Étape 2: Upload vers staging blob
    logger.info("Step 2: Uploading to staging blob")
    blob_url = upload_to_staging(bronze_data, STORAGE_ACCOUNT_NAME)

    # Étape 3: Envoyer message queue
    logger.info("Step 3: Sending queue message")
    size_bytes = len(json.dumps(bronze_data, ensure_ascii=False).encode('utf-8'))
    message_id = send_queue_message(blob_url, ingestion_id, size_bytes, STORAGE_ACCOUNT_NAME, QUEUE_NAME)

    duration_ms = int((time.time() - start_time) * 1000)

    return {
        "status": "success",
        "ingestion_id": ingestion_id,
        "cities_retrieved": cities_retrieved,
        "cities_failed": cities_failed,
        "blob_url": blob_url,
        "queue_message_id": message_id,
        "duration_ms": duration_ms
    }


def main(timer: func.TimerRequest) -> None:
    logger.info("=== Weather Ingestion Timer Trigger Started ===")

    try:
        summary = execute_ingestion()
        logger.info(f"=== Ingestion Completed Successfully ({summary['duration_ms']}ms) ===")
        logger.info(f"Summary: {json.dumps(summary, indent=2)}")

    except Exception as e:
        logger.error(f"=== Ingestion Failed ===")
        logger.error(f"Error: {str(e)}", exc_info=True)
        raise
