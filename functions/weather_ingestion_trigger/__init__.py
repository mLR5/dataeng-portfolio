import azure.functions as func
import logging
import time
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from shared.weather_api import get_all_cities_data
from shared.azure_storage import upload_to_staging, send_queue_message
from shared import config

logger = logging.getLogger(__name__)


def execute_ingestion():
    """Exécute le workflow d'ingestion complet: API → Blob → Queue"""
    start_time = time.time()
    config.validate_configuration()

    # Étape 1: Récupérer données API
    logger.info("Step 1: Fetching weather data from API")
    bronze_data = get_all_cities_data(config.WEATHERAPI_KEY)
    ingestion_id = bronze_data['ingestion_id']
    ingestion_timestamp = bronze_data['ingestion_timestamp']
    cities_retrieved = bronze_data['summary']['successful']
    cities_failed = bronze_data['summary']['failed']
    cities_total = bronze_data['summary']['total_cities']

    logger.info(f"Weather data retrieved: {cities_retrieved} cities success, {cities_failed} failed (ID: {ingestion_id})")

    # Étape 2: Upload vers staging blob
    logger.info("Step 2: Uploading to staging blob")
    blob_url = upload_to_staging(bronze_data)

    # Étape 3: Envoyer message queue
    logger.info("Step 3: Sending queue message")
    size_bytes = len(json.dumps(bronze_data, ensure_ascii=False).encode('utf-8'))

    # Parse blob_url to extract container and blob name
    url_parts = blob_url.split('/')
    blob_container = url_parts[3]
    blob_name = '/'.join(url_parts[4:])

    message_id = send_queue_message(
        blob_url=blob_url,
        ingestion_id=ingestion_id,
        size_bytes=size_bytes,
        ingestion_timestamp=ingestion_timestamp,
        cities_count=cities_total,
        blob_container=blob_container,
        blob_name=blob_name,
        summary=bronze_data['summary'],
        queue_name=config.QUEUE_NAME
    )

    duration_ms = int((time.time() - start_time) * 1000)

    return {
        "status": bronze_data['status'],
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
        if summary['status'] == 'failed':
            raise RuntimeError(f"Weather ingestion failed: no cities retrieved (ID: {summary['ingestion_id']})")
        log = logger.warning if summary['status'] == 'partial' else logger.info
        log(f"=== Ingestion Completed: {summary['status']} ({summary['duration_ms']}ms) ===")
        logger.info(f"Summary: {json.dumps(summary, indent=2)}")

    except Exception as e:
        logger.error(f"=== Ingestion Failed ===")
        logger.error(f"Error: {str(e)}", exc_info=True)
        raise
