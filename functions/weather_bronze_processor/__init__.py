import azure.functions as func
import logging
import time
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from shared.azure_storage import (
    StagingBlobNotFoundError, bronze_blob_exists, download_staging_blob,
    upload_to_bronze, delete_staging_blob,
)
from shared.claim_check import InvalidMessageError, bronze_blob_name, parse_message, parse_timestamp

logger = logging.getLogger(__name__)


class StagingContentMismatchError(Exception):
    """Le blob staging ne correspond pas à l'ingestion annoncée par le message."""


def _validate_staging_content(bronze_data, message):
    staging_name = message['blob_name']
    if not isinstance(bronze_data, dict) or bronze_data.get('ingestion_id') != message['ingestion_id']:
        raise StagingContentMismatchError(
            f"Staging blob {staging_name} does not match the message ingestion_id"
        )

    timestamp = bronze_data.get('ingestion_timestamp')
    try:
        matches = isinstance(timestamp, str) and (
            parse_timestamp(timestamp) == parse_timestamp(message['ingestion_timestamp'])
        )
    except (ValueError, OverflowError):
        matches = False
    if not matches:
        raise StagingContentMismatchError(
            f"Staging blob {staging_name} does not match the message ingestion_timestamp"
        )


def process_claim_check(message):
    """Promeut un blob staging vers Bronze. Rejouable : un message déjà traité ne produit rien de plus."""
    ingestion_id = message['ingestion_id']
    staging_name = message['blob_name']
    bronze_name = bronze_blob_name(ingestion_id, message['ingestion_timestamp'])
    bronze_created = False

    # Étape 1: Reconnaître un Bronze déjà produit (message rejoué ou dupliqué)
    if bronze_blob_exists(bronze_name):
        logger.info(f"Step 1: Bronze already produced, skipping upload: {bronze_name}")
    else:
        # Étape 2: Télécharger le staging et vérifier qu'il correspond au message
        logger.info("Step 2: Downloading blob from staging")
        try:
            bronze_data = download_staging_blob(staging_name)
        except StagingBlobNotFoundError:
            # Un autre consommateur peut avoir écrit Bronze puis supprimé le staging
            # après notre premier contrôle d'existence.
            if not bronze_blob_exists(bronze_name):
                raise
            logger.info(
                f"Bronze produced by another consumer before staging download: {bronze_name}"
            )
        else:
            _validate_staging_content(bronze_data, message)

            # Étape 3: Upload vers Bronze sans écraser
            logger.info("Step 3: Uploading to bronze layer")
            _, bronze_created = upload_to_bronze(bronze_data, bronze_name)

    # Étape 4: Nettoyer le staging, uniquement une fois Bronze présent.
    # Un échec ici n'invalide pas Bronze : la lifecycle policy purge le staging après 7 jours.
    try:
        staging_deleted = delete_staging_blob(staging_name)
    except Exception as e:
        logger.warning(f"Staging cleanup failed, left to lifecycle policy: {staging_name} ({type(e).__name__})")
        staging_deleted = False

    return {
        "ingestion_id": ingestion_id,
        "bronze_blob": bronze_name,
        "bronze_created": bronze_created,
        "staging_deleted": staging_deleted,
    }


def main(msg: func.QueueMessage) -> None:
    start_time = time.time()
    logger.info("=== Bronze Processor Queue Trigger Started ===")
    logger.info(f"Message ID: {msg.id}, Dequeue count: {msg.dequeue_count}")

    try:
        message = parse_message(msg.get_body())
        logger.info(
            f"Processing ingestion {message['ingestion_id']} "
            f"(status: {message.get('status', 'unknown')}, size: {message['payload_size_bytes']} bytes)"
        )

        result = process_claim_check(message)

        duration_ms = int((time.time() - start_time) * 1000)
        outcome = "Completed" if result['bronze_created'] else "Already Processed"
        logger.info(f"=== Bronze Processing {outcome} ({duration_ms}ms) ===")
        logger.info(f"Bronze blob: {result['bronze_blob']}")

    except InvalidMessageError as e:
        # Rejouer ne corrigera pas le message : il finira dans la poison queue pour analyse.
        logger.error(f"=== Invalid Claim Check Message: {e} ===")
        raise

    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        logger.error(f"=== Bronze Processing Failed ({duration_ms}ms) ===")
        logger.error(f"Error: {str(e)}", exc_info=True)
        logger.error(f"Message will be retried (attempt {msg.dequeue_count}/5)")
        raise
