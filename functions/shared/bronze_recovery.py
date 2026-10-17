"""Reprise bornée de Claim Checks ; aucune mutation sans execute=True."""
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from itertools import islice

from azure.core.exceptions import AzureError, ResourceNotFoundError

from . import config
from .claim_check import (
    INGESTION_ID_PATTERN, InvalidMessageError, bronze_blob_name, build_message,
    parse_message, parse_timestamp, staging_blob_name,
)


@dataclass
class RecoveryResult:
    outcome: str
    ingestion_id: str | None = None
    reason: str | None = None
    poison_message_id: str | None = None

    def as_dict(self):
        return {key: value for key, value in asdict(self).items() if value is not None}


class RecoveryService:
    def __init__(self, blobs, queue, poison):
        self.blobs = blobs
        self.queue = queue
        self.poison = poison

    def _bronze_exists(self, message):
        name = bronze_blob_name(message['ingestion_id'], message['ingestion_timestamp'])
        return self.blobs.get_blob_client(config.BRONZE_CONTAINER, name).exists()

    def _read_staging(self, ingestion_id):
        name = staging_blob_name(ingestion_id)
        blob = self.blobs.get_blob_client(config.STAGING_CONTAINER, name)
        payload = blob.download_blob().readall()
        data = json.loads(payload)
        if not isinstance(data, dict) or data.get('ingestion_id') != ingestion_id:
            raise InvalidMessageError('Staging ingestion mismatch')
        try:
            # Le statut n'était pas présent dans toutes les anciennes collectes.
            summary = data['summary']
            successful, failed = summary['successful'], summary['failed']
            counts = (successful, failed, summary['total_cities'])
            if any(type(value) is not int or value < 0 for value in counts):
                raise ValueError('Invalid counts')
            if successful + failed != summary['total_cities'] or not summary['total_cities']:
                raise ValueError('Invalid summary')
            status = 'failed' if not successful else ('partial' if failed else 'success')
            if data.get('status', status) != status:
                raise ValueError('Status mismatch')
            data = {**data, 'status': status}
            message = build_message(data, {
                'container': config.STAGING_CONTAINER, 'blob_name': name,
                'url': blob.url.split('?', 1)[0], 'size_bytes': len(payload),
            })
            return parse_message(json.dumps(message))
        except (KeyError, TypeError, ValueError) as error:
            raise InvalidMessageError('Invalid staging metadata') from error

    def _plan_message(self, body):
        message = parse_message(body)
        ingestion_id = message['ingestion_id']
        if self._bronze_exists(message):
            return RecoveryResult('already_bronze', ingestion_id), None
        try:
            canonical = self._read_staging(ingestion_id)
        except ResourceNotFoundError:
            # Le consommateur peut avoir fini entre les deux lectures.
            if self._bronze_exists(message):
                return RecoveryResult('already_bronze', ingestion_id), None
            return RecoveryResult('blocked', ingestion_id, 'staging_missing_or_expired'), None
        if parse_timestamp(message['ingestion_timestamp']) != parse_timestamp(canonical['ingestion_timestamp']):
            return RecoveryResult('blocked', ingestion_id, 'staging_timestamp_mismatch'), None
        return RecoveryResult('preview', ingestion_id), canonical

    def _publish(self, message):
        response = self.queue.send_message(
            json.dumps(message, ensure_ascii=False),
            time_to_live=config.QUEUE_MESSAGE_TTL_SECONDS,
        )
        if not response.id:
            raise RuntimeError('Publication not confirmed')

    def recover_staging(self, ingestion_id, *, execute=False):
        if not isinstance(ingestion_id, str) or not INGESTION_ID_PATTERN.fullmatch(ingestion_id):
            return RecoveryResult('blocked', reason='invalid_ingestion_id')
        try:
            message = self._read_staging(ingestion_id)
            if self._bronze_exists(message):
                return RecoveryResult('already_bronze', ingestion_id)
            if not execute:
                return RecoveryResult('preview', ingestion_id)
            self._publish(message)
            return RecoveryResult('published', ingestion_id)
        except ResourceNotFoundError:
            return RecoveryResult('blocked', ingestion_id, 'staging_missing_or_expired')
        except (InvalidMessageError, ValueError, TypeError):
            return RecoveryResult('blocked', ingestion_id, 'invalid_staging')
        except (AzureError, RuntimeError) as error:
            # Ne jamais afficher les messages SDK : une URL peut contenir un SAS.
            return RecoveryResult('error', ingestion_id, type(error).__name__)

    def recover_poison(self, *, limit=1, execute=False):
        if type(limit) is not int or not 1 <= limit <= 32:
            raise ValueError('limit must be between 1 and 32')
        results = []
        try:
            if execute:
                # Une réception à la fois limite le vieillissement des pop receipts.
                messages = self.poison.receive_messages(
                    messages_per_page=1, max_messages=limit, visibility_timeout=120,
                )
            else:
                messages = self.poison.peek_messages(max_messages=limit)
            for item in messages:
                try:
                    result, canonical = self._plan_message(item.content)
                    if execute and result.outcome == 'preview':
                        self._publish(canonical)
                        result.outcome = 'published'
                    if execute and result.outcome in ('published', 'already_bronze'):
                        # Un échec/timeout de publication laisse TOUJOURS le poison intact.
                        try:
                            self.poison.delete_message(item.id, item.pop_receipt)
                        except AzureError as error:
                            result.reason = type(error).__name__
                            result.outcome += '_unacknowledged'
                        else:
                            result.outcome += '_acknowledged'
                except (InvalidMessageError, ValueError, TypeError):
                    result = RecoveryResult('blocked', reason='invalid_message_or_staging')
                except (AzureError, RuntimeError) as error:
                    result = RecoveryResult('error', reason=type(error).__name__)
                result.poison_message_id = item.id
                results.append(result)
        except ResourceNotFoundError:
            # Queue absente : état normal tant qu'aucun message n'a fini en poison.
            results.append(RecoveryResult('poison_queue_absent', reason='no_poison_queue_yet'))
        return results

    def scan_staging(self, *, older_than_minutes=30, limit=20, now=None):
        """Liste des candidats ; leur absence de la queue n'est pas démontrée."""
        if not 1 <= limit <= 100 or older_than_minutes < 0:
            raise ValueError('Invalid scan bounds')
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=older_than_minutes)
        container = self.blobs.get_container_client(config.STAGING_CONTAINER)
        results = []
        candidates = container.list_blobs(name_starts_with=config.STAGING_PATH_PREFIX)
        for inspected, blob in enumerate(islice(candidates, 1001)):
            if inspected == 1000:
                results.append(RecoveryResult('scan_truncated', reason='inspected_1000_blobs'))
                break
            if blob.last_modified > cutoff or not blob.name.endswith('.json'):
                continue
            ingestion_id = blob.name[len(config.STAGING_PATH_PREFIX):-5]
            results.append(self.recover_staging(ingestion_id))
            if len(results) >= limit:
                break
        return results
