"""Process one immutable Bronze blob and publish only after both Delta commits."""
from dataclasses import dataclass
import hashlib
import re

from . import config
from .claim_check import INGESTION_ID_PATTERN
from .silver_contract import SCHEMA_VERSION, TRANSFORM_VERSION, TABLE_SCHEMAS, SILVER_STATUSES
from .silver_lease import LeaseBusy
from .silver_publication import (
    PublicationConflict, build_publication, json_bytes,
    publication_name, read_publication,
)

_PATH = re.compile(r'^weather/weatherapi/\d{4}/\d{2}/\d{2}/\d{2}/([^/]+)_weather_data\.json$')


@dataclass(frozen=True)
class ProcessResult:
    outcome: str
    ingestion_id: str
    publication: dict | None = None

    def to_dict(self):
        return {'outcome': self.outcome, 'ingestion_id': self.ingestion_id,
                'status': self.publication['status'] if self.publication else None}


def bronze_identity(source_blob):
    match = _PATH.fullmatch(source_blob)
    if not match or not INGESTION_ID_PATTERN.fullmatch(match.group(1)):
        raise ValueError('Expected a canonical Bronze blob path')
    return match.group(1)


def silver_root():
    return f'silver/weatherapi/schema=v{SCHEMA_VERSION}/transform={TRANSFORM_VERSION}'


class SilverRunner:
    """Injected storage, transformer and lease factory keep execution testable.

    Errors remain visible and retryable to the orchestrator; only malformed
    business inputs returned as rejected by the transformer are terminal.
    """
    def __init__(self, bronze_container, controls, writer, lease_factory, transform, *, prepare=None):
        self.bronze = bronze_container
        self.controls = controls
        self.writer = writer
        self.lease_factory = lease_factory
        self.transform = transform
        self.prepare = prepare or (lambda: None)

    def preview_ingestion(self, source_blob):
        ingestion_id = bronze_identity(source_blob)
        self.prepare()
        raw = self.bronze.get_blob_client(source_blob).download_blob().readall()
        source_hash = hashlib.sha256(raw).hexdigest()
        paths = {name: self.writer.table_path(name) for name in TABLE_SCHEMAS}
        previous = read_publication(self.controls, ingestion_id, source_blob, source_hash, paths)
        return ProcessResult('already_processed' if previous else 'candidate', ingestion_id, previous)

    def process_ingestion(self, source_blob):
        ingestion_id = bronze_identity(source_blob)
        self.prepare()
        raw = self.bronze.get_blob_client(source_blob).download_blob().readall()
        source_hash = hashlib.sha256(raw).hexdigest()
        paths = {name: self.writer.table_path(name) for name in TABLE_SCHEMAS}
        previous = read_publication(self.controls, ingestion_id, source_blob, source_hash, paths)
        if previous:
            return ProcessResult('already_processed', ingestion_id, previous)
        result = self.transform(raw, source_blob)
        if result.source_blob != source_blob or result.source_sha256 != source_hash:
            raise PublicationConflict('Transformation provenance differs from Bronze')
        terminal_empty = result.status == 'rejected' and not any(t.num_rows for t in result.tables.values())
        if result.ingestion_id not in (None, ingestion_id) and not terminal_empty:
            raise PublicationConflict('Transformation identity differs from Bronze path')
        if result.status not in SILVER_STATUSES:
            raise PublicationConflict('Unknown transformation status')
        has_rows = any(t.num_rows for t in result.tables.values())
        if (result.status in ('success', 'partial')) != has_rows:
            raise PublicationConflict('Transformation status differs from row counts')
        if result.status == 'empty' and not result.summary.get('reason'):
            raise PublicationConflict('Empty transformation requires a collection reason')
        if result.ingestion_id is None and not terminal_empty:
            raise PublicationConflict('Only rejected empty results may lack identity')
        for rows in result.tables.values():
            for row in rows.to_pylist():
                expected = {
                    'ingestion_id': ingestion_id, 'source_blob': source_blob,
                    'source_sha256': source_hash, 'schema_version': SCHEMA_VERSION,
                    'transform_version': TRANSFORM_VERSION,
                }
                if any(row.get(k) != v for k, v in expected.items()):
                    raise PublicationConflict('Row provenance differs from transformation')
        try:
            with self.lease_factory() as lease:
                lease.assert_held()
                # Another process could have finished while this one transformed.
                previous = read_publication(self.controls, ingestion_id, source_blob, source_hash, paths)
                if previous:
                    return ProcessResult('already_processed', ingestion_id, previous)
                commits = {}
                for name in TABLE_SCHEMAS:
                    commits[name] = self.writer.merge(
                        name, result.tables[name], lease, ingestion_id=ingestion_id,
                    )
                publication, quarantine = build_publication(result, ingestion_id, commits)
                lease.assert_held()
                self.controls.create(f'quarantine/{ingestion_id}.jsonl', quarantine)
                lease.assert_held()
                self.controls.create(publication_name(ingestion_id), json_bytes(publication))
                lease.assert_held()
                return ProcessResult('published', ingestion_id, publication)
        except LeaseBusy:
            return ProcessResult('busy', ingestion_id)


def _relative_silver_root(value):
    if not isinstance(value, str) or len(value) > 512 or not re.fullmatch(
        r'silver/(?:[A-Za-z0-9_][A-Za-z0-9_.=-]*)(?:/[A-Za-z0-9_][A-Za-z0-9_.=-]*)*', value,
    ) or any(part in ('.', '..') for part in value.split('/')):
        raise ValueError('Expected a relative Silver root without traversal or URL components')
    return value


def adls_table_root(account, container, root):
    """Canonical HNS URI; the short az:// alias fails creation with Delta 1.2.1."""
    if not isinstance(account, str) or not re.fullmatch(r'[a-z0-9]{3,24}', account):
        raise ValueError('Invalid Azure storage account name')
    if not isinstance(container, str) or not re.fullmatch(r'[a-z0-9](?:[a-z0-9]|-(?!-)){1,61}[a-z0-9]', container):
        raise ValueError('Invalid Azure storage container name')
    root = _relative_silver_root(root)
    return f'abfss://{container}@{account}.dfs.core.windows.net/{root}'


def make_azure_runner(*, transform=None, root=None):
    """Use existing Azure connection or the default Azure credential chain.

    Credentials stay in SDK/config objects, never CLI arguments or reports.
    Create this adapter for a bounded batch; bearer tokens refresh per ingestion.
    """
    root = _relative_silver_root(silver_root() if root is None else root)
    import os
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient
    from .silver_delta import DeltaSnapshotWriter
    from .silver_lease import BlobWriterLease
    from .silver_publication import AzureControlStore
    if transform is None:
        from .silver_transform import transform_bronze
        transform = transform_bronze
    credential = None
    connection = os.getenv('STORAGE_CONNECTION_STRING')
    if connection:
        values = dict(part.split('=', 1) for part in connection.split(';') if '=' in part)
        account = values.get('AccountName', '')
        if not re.fullmatch('[a-z0-9]{3,24}', account) or not values.get('AccountKey'):
            raise ValueError('Silver requires an Azure AccountName/AccountKey connection or default identity')
        if values.get('EndpointSuffix', 'core.windows.net') != 'core.windows.net':
            raise ValueError('This Silver adapter supports Azure public cloud endpoints')
        service = BlobServiceClient.from_connection_string(connection)
        options = {'azure_storage_account_name': account,
                   'azure_storage_account_key': values['AccountKey']}
    else:
        account = os.getenv('STORAGE_ACCOUNT_NAME', '')
        if not re.fullmatch('[a-z0-9]{3,24}', account):
            raise ValueError('Set STORAGE_ACCOUNT_NAME or STORAGE_CONNECTION_STRING')
        credential = DefaultAzureCredential()
        service = BlobServiceClient(f'https://{account}.blob.core.windows.net', credential=credential)
        token = credential.get_token('https://storage.azure.com/.default')
        options = {'azure_storage_account_name': account, 'azure_storage_token': token.token}
    container = service.get_container_client(config.DATALAKE_CONTAINER)
    controls = AzureControlStore(container, f'{root}/_control')
    writer = DeltaSnapshotWriter(
        adls_table_root(account, config.DATALAKE_CONTAINER, root), storage_options=options,
    )
    def prepare():
        if credential is not None:
            # Refresh at each ingestion; SDK caching handles still-valid tokens.
            token = credential.get_token('https://storage.azure.com/.default')
            writer.storage_options['azure_storage_token'] = token.token
    return SilverRunner(
        service.get_container_client(config.BRONZE_CONTAINER), controls, writer,
        lambda: BlobWriterLease(controls.blob('writer.lock')), transform, prepare=prepare,
    )
