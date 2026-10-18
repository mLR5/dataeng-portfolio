"""Remove only explicitly identified copies created by our delivery validation."""
import json
import os
from pathlib import Path
import sys

import validate_bronze_azure as base

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'functions'))
from azure.core import MatchConditions
from azure.storage.blob import BlobServiceClient
from shared.claim_check import INGESTION_ID_PATTERN, bronze_blob_name, staging_blob_name


def main():
    reference = json.loads(Path('scripts/bronze_cleanup_reference.json').read_text())
    source_id = reference['source_ingestion_id']
    timestamp = reference['ingestion_timestamp']
    ids = reference['controlled_validation_ingestions']
    assert INGESTION_ID_PATTERN.fullmatch(source_id)
    assert len(ids) <= 2 and all(INGESTION_ID_PATTERN.fullmatch(item) and item != source_id for item in ids)
    account = os.environ['AZURE_STORAGE_ACCOUNT']
    rg = f"rg-{os.environ['AZURE_PROJECT_NAME']}-{os.environ['AZURE_ENVIRONMENT']}"
    connection = base.az_json(['storage', 'account', 'show-connection-string',
                               '--resource-group', rg, '--name', account, '--query', 'connectionString'])
    blobs = BlobServiceClient.from_connection_string(connection)
    source = blobs.get_blob_client('bronze', bronze_blob_name(source_id, timestamp))
    assert source.exists(), 'Original real ingestion must remain archived'
    deleted = []
    for ingestion_id in ids:
        for container, name in [('bronze', bronze_blob_name(ingestion_id, timestamp)),
                                ('staging', staging_blob_name(ingestion_id))]:
            blob = blobs.get_blob_client(container, name)
            if not blob.exists():
                continue
            download = blob.download_blob()
            data = json.loads(download.readall())
            marker = data.get('validation', {})
            assert data.get('ingestion_id') == ingestion_id
            assert data.get('ingestion_timestamp') == timestamp
            assert marker.get('source_ingestion_id') == source_id
            assert marker.get('kind') in ('staging-recovery', 'isolated-poison-recovery')
            blob.delete_blob(etag=download.properties.etag, match_condition=MatchConditions.IfNotModified)
            deleted.append({'container': container, 'ingestion_id': ingestion_id})
    assert source.exists()
    report = {'result': 'success', 'deleted_owned_validation_copies': deleted,
              'real_ingestion_retained': source_id}
    Path('validation-cleanup-report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
