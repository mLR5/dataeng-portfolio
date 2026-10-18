#!/usr/bin/env python3
"""Bounded Bronze→Silver backfill; preview by default, resumable execution."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'functions'))

from shared.claim_check import parse_timestamp
from shared.silver_contract import TRANSFORM_VERSION
from shared.silver_discovery import discover_bronze
from shared.silver_publication import json_bytes
from shared.silver_runner import bronze_identity, make_azure_runner


def _limit(value):
    value = int(value)
    if not 1 <= value <= 100:
        raise argparse.ArgumentTypeError('limit must be 1..100')
    return value


def _save(path, state):
    target = Path(path)
    temporary = target.with_name(target.name + '.tmp')
    temporary.write_bytes(json_bytes(state))
    temporary.replace(target)


def run_batch(runner, start, end, *, limit=20, state=None, execute=False, save=None):
    """Persist the discovered page before processing, then remove successes.

    The same state is shared by retries; a completed cursor never implies failed
    jobs succeeded. Default preview performs no writes, including local state.
    """
    identity = {'from': start.isoformat(), 'to': end.isoformat(), 'version': TRANSFORM_VERSION}
    if state is None:
        state = {**identity, 'cursor': None, 'pending': [], 'scan_complete': False}
    else:
        state = json.loads(json_bytes(state))  # do not mutate caller's preview state
        if any(state.get(k) != v for k, v in identity.items()):
            raise ValueError('Backfill state belongs to another window/version')
        if not isinstance(state.get('pending'), list) or len(state['pending']) > 100:
            raise ValueError('Invalid backfill pending state')
        if type(state.get('scan_complete')) is not bool or (state.get('cursor') is not None and not isinstance(state['cursor'], dict)):
            raise ValueError('Invalid backfill cursor state')
        if len(set(state['pending'])) != len(state['pending']):
            raise ValueError('Duplicate backfill pending jobs')
        for path in state['pending']:
            ingestion_id = bronze_identity(path)
            collected = datetime.strptime(ingestion_id[:15], '%Y%m%d_%H%M%S').replace(tzinfo=timezone.utc)
            if not start <= collected < end:
                raise ValueError('Pending job outside backfill window')
    if execute and save is None:
        raise ValueError('Execute requires durable state persistence')
    if not state['pending'] and not state['scan_complete']:
        page = discover_bronze(runner.bronze, start, end, limit=limit, cursor=state['cursor'])
        state['pending'] = list(page.blobs)
        state['cursor'] = page.continuation
        state['scan_complete'] = page.continuation is None
    if execute:
        save(state)
    results = []
    for path in state['pending'][:limit]:
        if not execute:
            try:
                result = runner.preview_ingestion(path)
                results.append({'source_blob': path, **result.to_dict()})
                if result.outcome == 'already_processed':
                    state['pending'].remove(path)
            except Exception as error:
                results.append({'source_blob': path, 'outcome': 'error', 'reason': type(error).__name__})
            continue
        try:
            result = runner.process_ingestion(path)
            results.append({'source_blob': path, **result.to_dict()})
            if result.outcome in ('published', 'already_processed'):
                state['pending'].remove(path)
                save(state)
        except Exception as error:
            # SDK error text can contain URLs/tokens. Keep only its class.
            results.append({'source_blob': path, 'outcome': 'error', 'reason': type(error).__name__})
    return {
        'mode': 'execute' if execute else 'preview', 'results': results,
        'state': state, 'more_work': bool(state['pending']) or not state['scan_complete'],
    }


def main(argv=None, *, runner_factory=make_azure_runner):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from', dest='start', required=True, help='UTC ISO start inclusive')
    parser.add_argument('--to', dest='end', required=True, help='UTC ISO end exclusive')
    parser.add_argument('--version', choices=[TRANSFORM_VERSION], default=TRANSFORM_VERSION)
    parser.add_argument('--limit', type=_limit, default=20)
    parser.add_argument('--state-file', help='Local durable pending/cursor state; required for execute')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    if args.execute and not args.state_file:
        parser.error('--execute requires --state-file')
    try:
        start, end = parse_timestamp(args.start), parse_timestamp(args.end)
        if start >= end:
            raise ValueError('Invalid backfill window')
        state = None
        if args.state_file and Path(args.state_file).exists():
            if Path(args.state_file).stat().st_size > 1024 * 1024:
                raise ValueError('Oversized backfill state')
            state = json.loads(Path(args.state_file).read_bytes())
        runner = runner_factory()
        report = run_batch(
            runner, start, end, limit=args.limit, state=state, execute=args.execute,
            save=(lambda value: _save(args.state_file, value)) if args.execute else None,
        )
        print(json_bytes(report).decode('utf-8'))
        return 1 if any(r['outcome'] in ('error', 'busy') for r in report['results']) else (2 if report['more_work'] else 0)
    except Exception as error:
        print(json.dumps({'outcome': 'error', 'reason': type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
