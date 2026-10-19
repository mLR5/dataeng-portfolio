"""Immutable control objects and the Gold publication barrier."""
import hashlib
import json
from datetime import date, datetime, timezone

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

from .silver_contract import SCHEMA_VERSION, SOURCE, TRANSFORM_VERSION, TABLE_SCHEMAS


class PublicationConflict(ValueError):
    """Published immutable identity or content does not match this ingestion."""


def json_bytes(value):
    def encode(item):
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        raise TypeError('Non JSON-safe publication value')
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False, default=encode).encode('utf-8')


class AzureControlStore:
    def __init__(self, container, prefix):
        self.container = container
        self.prefix = prefix.strip('/')

    def blob(self, name):
        return self.container.get_blob_client(f'{self.prefix}/{name}')

    def read(self, name):
        try:
            return self.blob(name).download_blob().readall()
        except ResourceNotFoundError:
            return None

    def create(self, name, content):
        try:
            self.blob(name).upload_blob(content, overwrite=False)
        except ResourceExistsError:
            existing = self.read(name)
            if existing != content:
                raise PublicationConflict('Immutable control object differs') from None


def publication_name(ingestion_id):
    # Path ID was parsed from a strict canonical Bronze filename by the runner.
    from .claim_check import INGESTION_ID_PATTERN
    if not INGESTION_ID_PATTERN.fullmatch(ingestion_id):
        raise ValueError('Invalid ingestion identity')
    return f'publications/{ingestion_id}.json'


def read_publication(store, ingestion_id, source_blob, source_sha256, table_paths):
    payload = store.read(publication_name(ingestion_id))
    if payload is None:
        return None
    try:
        publication = json.loads(payload)
        expected = {
            'complete': True, 'schema_version': SCHEMA_VERSION,
            'transform_version': TRANSFORM_VERSION, 'source': SOURCE,
            'ingestion_id': ingestion_id, 'source_blob': source_blob,
            'source_sha256': source_sha256,
        }
        if any(publication.get(k) != v for k, v in expected.items()):
            raise ValueError()
        if set(publication['tables']) != set(TABLE_SCHEMAS):
            raise ValueError()
        for name, path in table_paths.items():
            table = publication['tables'][name]
            if table['path'] != path or type(table['version']) is not int or table['version'] < 0:
                raise ValueError()
        if publication['status'] not in ('success', 'partial', 'empty', 'rejected'):
            raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError):
        raise PublicationConflict('Invalid or incompatible publication') from None
    return publication


def build_publication(result, ingestion_id, commits):
    rejections = [{
        'ingestion_id': ingestion_id, 'source_blob': result.source_blob,
        'source_sha256': result.source_sha256, **r.to_dict(),
    } for r in result.rejections]
    quarantine = b''.join(json_bytes(r) + b'\n' for r in rejections)
    publication = {
        'complete': True, 'schema_version': SCHEMA_VERSION,
        'transform_version': TRANSFORM_VERSION, 'source': SOURCE,
        'ingestion_id': ingestion_id,
        'collected_at_utc': result.collected_at_utc.isoformat() if result.collected_at_utc else None,
        'published_at_utc': datetime.now(timezone.utc).isoformat(),
        'source_blob': result.source_blob, 'source_sha256': result.source_sha256,
        'status': result.status, 'summary': result.summary,
        'tables': {name: commit.to_dict() for name, commit in commits.items()},
        'quarantine': {
            'path': f'quarantine/{ingestion_id}.jsonl', 'count': len(rejections),
            'sha256': hashlib.sha256(quarantine).hexdigest(),
        },
    }
    return publication, quarantine


def published_read_plan(publications):
    """Freeze complete IDs and sufficient Delta versions for a Gold batch.

    Read those exact table versions via Delta and filter ingestion_id to this ID
    set. With insert-only snapshots max(version) contains each prior publication.
    Never glob the underlying Parquet files. Concurrent late publications wait
    for the next Gold batch.
    """
    ids, versions, paths = set(), {}, {}
    for publication in publications:
        if publication.get('complete') is not True:
            raise PublicationConflict('Gold requires completed publications')
        if publication.get('transform_version') != TRANSFORM_VERSION or publication.get('schema_version') != SCHEMA_VERSION:
            raise PublicationConflict('Gold cannot mix transformation versions')
        if publication['ingestion_id'] in ids:
            raise PublicationConflict('Duplicate publication identity')
        ids.add(publication['ingestion_id'])
        for name in TABLE_SCHEMAS:
            table = publication['tables'][name]
            if name in paths and paths[name] != table['path']:
                raise PublicationConflict('Gold cannot mix table roots')
            paths[name] = table['path']
            versions[name] = max(versions.get(name, -1), table['version'])
    return {'ingestion_ids': sorted(ids), 'tables': {
        name: {'path': paths[name], 'version': version} for name, version in versions.items()
    }}
