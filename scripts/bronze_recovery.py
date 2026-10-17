#!/usr/bin/env python3
"""CLI Bronze : preview par défaut, sans appel WeatherAPI ni suppression de blob."""
import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'functions'))

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from azure.storage.queue import QueueServiceClient

from shared import config
from shared.bronze_recovery import RecoveryService


def make_service():
    if os.getenv('STORAGE_CONNECTION_STRING'):
        blobs = BlobServiceClient.from_connection_string(os.environ['STORAGE_CONNECTION_STRING'])
        queues = QueueServiceClient.from_connection_string(os.environ['STORAGE_CONNECTION_STRING'])
    else:
        account = os.getenv('STORAGE_ACCOUNT_NAME', '')
        if not re.fullmatch(r'[a-z0-9]{3,24}', account):
            raise ValueError('Set STORAGE_ACCOUNT_NAME or STORAGE_CONNECTION_STRING')
        credential = DefaultAzureCredential()
        blobs = BlobServiceClient(f'https://{account}.blob.core.windows.net', credential)
        queues = QueueServiceClient(f'https://{account}.queue.core.windows.net', credential)
    return RecoveryService(
        blobs, queues.get_queue_client(config.QUEUE_NAME),
        queues.get_queue_client(config.QUEUE_NAME + '-poison'),
    )


def bounded_int(minimum, maximum):
    def parse(value):
        value = int(value)
        if not minimum <= value <= maximum:
            raise argparse.ArgumentTypeError(f'value must be {minimum}..{maximum}')
        return value
    return parse


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    staging = commands.add_parser('staging', help='Reprendre une ingestion staging précise')
    staging.add_argument('--ingestion-id', required=True)
    staging.add_argument('--execute', action='store_true')
    poison = commands.add_parser('poison', help='Inspecter/reprendre les premiers messages visibles')
    poison.add_argument('--limit', type=bounded_int(1, 32), default=1)
    poison.add_argument('--execute', action='store_true')
    scan = commands.add_parser('scan-staging', help='Lister les staging anciens, sans mutation')
    scan.add_argument('--older-than-minutes', type=bounded_int(0, 10080), default=30)
    scan.add_argument('--limit', type=bounded_int(1, 100), default=20)
    args = parser.parse_args(argv)
    try:
        service = make_service()
        if args.command == 'staging':
            results = [service.recover_staging(args.ingestion_id, execute=args.execute)]
        elif args.command == 'poison':
            results = service.recover_poison(limit=args.limit, execute=args.execute)
        else:
            results = service.scan_staging(older_than_minutes=args.older_than_minutes, limit=args.limit)
        for result in results:
            print(json.dumps(result.as_dict()))
        return int(any(result.outcome in ('blocked', 'error', 'scan_truncated') or 'unacknowledged' in result.outcome for result in results))
    except Exception as error:
        print(json.dumps({'outcome': 'error', 'reason': type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
