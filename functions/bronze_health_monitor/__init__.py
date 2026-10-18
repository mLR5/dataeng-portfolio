"""Timer de surveillance indépendant du succès du collecteur."""
import json
import logging

import azure.functions as func
from azure.storage.blob import BlobServiceClient
from azure.storage.queue import QueueClient

from shared import config
from shared.bronze_health import check_bronze_health

logger = logging.getLogger(__name__)


def main(timer: func.TimerRequest) -> None:
    try:
        blobs = BlobServiceClient.from_connection_string(config.STORAGE_CONNECTION_STRING)
        poison = QueueClient.from_connection_string(
            config.STORAGE_CONNECTION_STRING, config.QUEUE_NAME + '-poison',
        )
        health = check_bronze_health(blobs, poison)
    except Exception as error:
        # Message SDK exclu : les URLs de stockage peuvent contenir un SAS.
        logger.error('BRONZE_HEALTH %s', json.dumps({
            'schema_version': 1, 'freshness': 'error', 'error_type': type(error).__name__,
        }))
        raise RuntimeError('Bronze health check failed; see BRONZE_HEALTH event') from None
    logger.info('BRONZE_HEALTH %s', json.dumps(health))
