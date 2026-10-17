"""Sonde Bronze : recherche limitée aux partitions couvrant le seuil de fraîcheur."""
import json
import math
from datetime import datetime, timedelta, timezone
from itertools import islice

from azure.core.exceptions import ResourceNotFoundError

from . import config
from .claim_check import INGESTION_ID_PATTERN, bronze_blob_name, parse_timestamp

MAX_BLOBS_PER_PARTITION = 100
MAX_PAYLOADS_PER_CHECK = 100
MAX_CLOCK_SKEW_SECONDS = 60


def check_bronze_health(blobs, poison, *, now=None, max_age_minutes=150):
    if not 60 <= max_age_minutes <= 1440:
        raise ValueError('max_age_minutes must be 60..1440')
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('now must be timezone aware')
    now = now.astimezone(timezone.utc)
    container = blobs.get_container_client(config.BRONZE_CONTAINER)
    candidates = []
    for offset in range(math.ceil(max_age_minutes / 60) + 1):
        hour = now - timedelta(hours=offset)
        prefix = f'{config.BRONZE_PATH_PREFIX}/{hour:%Y/%m/%d/%H}/'
        page = list(islice(container.list_blobs(name_starts_with=prefix), MAX_BLOBS_PER_PARTITION + 1))
        if len(page) > MAX_BLOBS_PER_PARTITION:
            raise RuntimeError('Bronze partition exceeds health check scan limit')
        candidates.extend(page)

    # Noms créés par le pipeline = date UTC + UUID ; on relit le timestamp du
    # contenu pour décider de la fraîcheur, jamais last_modified (rejeu tardif).
    candidates.sort(key=lambda blob: blob.name, reverse=True)
    latest_timestamp = None
    latest_status = None
    for index, candidate in enumerate(candidates):
        if index >= MAX_PAYLOADS_PER_CHECK:
            raise RuntimeError('Bronze health check payload limit exceeded')
        payload = container.get_blob_client(candidate.name).download_blob().readall()
        data = json.loads(payload)
        ingestion_id = data.get('ingestion_id', '')
        if not isinstance(ingestion_id, str) or not INGESTION_ID_PATTERN.fullmatch(ingestion_id):
            raise ValueError('Invalid Bronze ingestion metadata')
        timestamp = parse_timestamp(data['ingestion_timestamp'])
        if candidate.name != bronze_blob_name(ingestion_id, data['ingestion_timestamp']):
            raise ValueError('Bronze partition does not match ingestion timestamp')
        # Une collecte peut démarrer après le snapshot `now`, pendant nos lectures.
        if timestamp > now + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS):
            raise ValueError('Bronze ingestion timestamp is in the future')
        summary = data['summary']
        successful = summary['successful']
        failed = summary['failed']
        counts = (successful, failed, summary['total_cities'])
        if any(type(count) is not int or count < 0 for count in counts):
            raise ValueError('Invalid Bronze summary')
        if successful + failed != summary['total_cities'] or not summary['total_cities']:
            raise ValueError('Invalid Bronze summary')
        status = 'failed' if not successful else ('partial' if failed else 'success')
        if data.get('status', status) != status:
            raise ValueError('Bronze status does not match summary')
        if successful and (latest_timestamp is None or timestamp > latest_timestamp):
            latest_timestamp, latest_status = timestamp, status

    poison_exists = True
    try:
        poison_count = poison.get_queue_properties().approximate_message_count
    except ResourceNotFoundError:
        # Functions ne crée la poison queue qu'au premier message en échec.
        poison_count, poison_exists = 0, False
    if type(poison_count) is not int or poison_count < 0:
        raise ValueError('Invalid poison queue count')
    fresh = latest_timestamp is not None and now - latest_timestamp <= timedelta(minutes=max_age_minutes)
    return {
        'schema_version': 1,
        'checked_at': now.isoformat(),
        'freshness': 'fresh' if fresh else 'stale',
        'latest_ingestion_at': latest_timestamp.isoformat() if latest_timestamp else None,
        'latest_status': latest_status,
        'poison_messages': poison_count,
        'poison_queue_exists': poison_exists,
        'max_age_minutes': max_age_minutes,
    }
