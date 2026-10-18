"""Bounded live delivery checks; credentials and raw weather stay in memory."""
import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from urllib.request import Request, urlopen

import validate_bronze_azure as base

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'functions'))
from azure.storage.blob import BlobServiceClient
from azure.storage.queue import QueueServiceClient
from shared import config
from shared.bronze_health import check_bronze_health
from shared.bronze_recovery import RecoveryService
from shared.claim_check import bronze_blob_name, build_message, staging_blob_name

logging.getLogger('azure').setLevel(logging.ERROR)
check = base.check
report = base.REPORT


def wait_for(predicate, *, seconds=240, interval=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    return None


def query_rows(app_id, query):
    response = base.az_json([
        'rest', '--method', 'post', '--url',
        f'https://api.applicationinsights.io/v1/apps/{app_id}/query',
        '--resource', 'https://api.applicationinsights.io',
        '--body', json.dumps({'query': query, 'timespan': 'PT1H'}),
    ])
    return response.get('tables', [{}])[0].get('rows', [])


def delivery():
    base.validate()
    report['result'] = 'failed'
    project = os.environ['AZURE_PROJECT_NAME']
    environment = os.environ['AZURE_ENVIRONMENT']
    account = os.environ['AZURE_STORAGE_ACCOUNT']
    rg = f'rg-{project}-{environment}'
    app = f'func-{project}-{environment}'
    args = ['--resource-group', rg, '--name', app]
    functions = base.az_json(['functionapp', 'function', 'list', *args])
    names = {item['name'].rsplit('/', 1)[-1] for item in functions}
    check('health_function_indexed', 'bronze_health_monitor' in names, 'Fourth Function is indexed')
    connection = base.az_json([
        'storage', 'account', 'show-connection-string', '--resource-group', rg,
        '--name', account, '--query', 'connectionString',
    ])
    blobs = BlobServiceClient.from_connection_string(connection)
    queues = QueueServiceClient.from_connection_string(connection)
    connection = None
    queue = queues.get_queue_client(config.QUEUE_NAME)
    poison = queues.get_queue_client(config.QUEUE_NAME + '-poison')
    service = RecoveryService(blobs, queue, poison)
    path = report['bronze_path'].removeprefix('bronze/')
    blob = blobs.get_blob_client(config.BRONZE_CONTAINER, path)
    raw = blob.download_blob().readall()
    data = json.loads(raw)
    etag = blob.get_blob_properties().etag
    sha = hashlib.sha256(raw).hexdigest()

    # Retain field inventory only, never the raw response or credentials.
    sample = next(city['data'] for city in data['cities'] if city['status'] == 'success')
    forecast_day = sample.get('forecast', {}).get('forecastday', [{}])[0]
    inventory = {
        'envelope': sorted(data), 'city': sorted(data['cities'][0]),
        'location': sorted(sample.get('location', {})),
        'current': sorted(sample.get('current', {})),
        'forecastday': sorted(forecast_day),
        'day': sorted(forecast_day.get('day', {})),
        'hour': sorted((forecast_day.get('hour') or [{}])[0]),
    }
    Path('weather-field-inventory.json').write_text(json.dumps(inventory, indent=2) + '\n')

    # Re-publish an exact existing claim check after its staging was removed.
    message = build_message(data, {
        'container': config.STAGING_CONTAINER, 'blob_name': staging_blob_name(data['ingestion_id']),
        'url': blobs.get_blob_client(config.STAGING_CONTAINER, staging_blob_name(data['ingestion_id'])).url,
        'size_bytes': len(raw),
    })
    replay = queue.send_message(json.dumps(message), time_to_live=600)
    check('replay_publication_confirmed', bool(replay.id), 'One valid duplicate queued')
    report['replay_message_id'] = replay.id

    insights = base.az_json(['resource', 'list', '--resource-group', rg,
                            '--resource-type', 'Microsoft.Insights/components'])
    check('application_insights_found', len(insights) == 1, 'One existing telemetry component')
    component = base.az_json(['resource', 'show', '--ids', insights[0]['id'], '--api-version', '2020-02-02'])
    app_id = component['properties']['AppId']
    replay_query = (
        f"let ops = traces | where message contains '{replay.id}' | distinct operation_Id; "
        "traces | where operation_Id in (ops) "
        "| where message contains 'Bronze Processing Already Processed' | project timestamp"
    )
    check('replay_consumed_without_rewrite', bool(wait_for(lambda: query_rows(app_id, replay_query), seconds=300, interval=15)),
          'Live consumer confirmed Already Processed in telemetry')
    check('bronze_unchanged_after_replay', blob.get_blob_properties().etag == etag
          and hashlib.sha256(blob.download_blob().readall()).hexdigest() == sha,
          'ETag and SHA-256 are unchanged')

    def stage_copy(kind):
        fixture = copy.deepcopy(data)
        fixture['ingestion_id'] = data['ingestion_id'][:15] + '_' + uuid.uuid4().hex
        fixture['validation'] = {'kind': kind, 'source_ingestion_id': data['ingestion_id']}
        payload = json.dumps(fixture, ensure_ascii=False, indent=2).encode()
        staged = blobs.get_blob_client(config.STAGING_CONTAINER, staging_blob_name(fixture['ingestion_id']))
        staged.upload_blob(payload, overwrite=False)
        report.setdefault('controlled_validation_ingestions', []).append(fixture['ingestion_id'])
        return fixture, payload, staged

    def confirm_fixture(fixture, payload, staged, label):
        target = blobs.get_blob_client(config.BRONZE_CONTAINER, bronze_blob_name(fixture['ingestion_id'], fixture['ingestion_timestamp']))
        check(label + '_bronze_created', bool(wait_for(target.exists)), 'Exact controlled ingestion reached Bronze')
        check(label + '_raw_preserved', json.loads(target.download_blob().readall()) == json.loads(payload), 'Payload unchanged')
        check(label + '_staging_removed', bool(wait_for(lambda: not staged.exists(), seconds=60)), 'Only its staging was removed by the consumer')

    fixture, payload, staged = stage_copy('staging-recovery')
    preview = service.recover_staging(fixture['ingestion_id'])
    check('staging_recovery_preview', preview.outcome == 'preview' and staged.exists(), 'Valid orphan is recoverable')
    outcome = service.recover_staging(fixture['ingestion_id'], execute=True)
    check('staging_recovery_publication', outcome.outcome == 'published', 'Normal queue confirmed publication')
    confirm_fixture(fixture, payload, staged, 'staging_recovery')

    # A dedicated short-lived queue isolates poison recovery from real incidents.
    isolated = queues.get_queue_client('validation-bronze-' + uuid.uuid4().hex)
    isolated.create_queue()
    report['isolated_recovery_queue_created'] = True
    try:
        fixture, payload, staged = stage_copy('isolated-poison-recovery')
        message = build_message(fixture, {
            'container': config.STAGING_CONTAINER, 'blob_name': staged.blob_name,
            'url': staged.url, 'size_bytes': len(payload),
        })
        isolated.send_message(json.dumps(message), time_to_live=600)
        isolated_service = RecoveryService(blobs, queue, isolated)
        preview = isolated_service.recover_poison(limit=1)
        check('poison_recovery_preview', len(preview) == 1 and preview[0].outcome == 'preview', 'Isolated valid reference; production poison untouched')
        outcomes = isolated_service.recover_poison(limit=1, execute=True)
        check('poison_recovery_acknowledged', len(outcomes) == 1 and outcomes[0].outcome == 'published_acknowledged', 'Published to normal queue before acknowledgement')
        confirm_fixture(fixture, payload, staged, 'poison_recovery')
        check('isolated_poison_empty', not list(isolated.peek_messages(max_messages=1)), 'No test message remains')
    finally:
        # This exact disposable queue was created by this run; never clear prod queues.
        isolated.delete_queue()
        report['isolated_recovery_queue_removed'] = True

    health = check_bronze_health(blobs, poison)
    report['direct_health'] = health
    check('live_health_fresh', health['freshness'] == 'fresh', 'Business ingestion timestamp is recent')
    check('production_poison_empty', health['poison_messages'] == 0, 'No pending production poison message')
    inventory_staging = service.scan_staging(older_than_minutes=30, limit=100)
    report['old_staging_candidates'] = [item.as_dict() for item in inventory_staging]

    keys = base.az_json(['functionapp', 'keys', 'list', *args])
    host = base.az_json(['functionapp', 'show', *args])
    props = host.get('properties', host)
    requested_at = datetime.now(timezone.utc).isoformat()
    request = Request(f"https://{props['defaultHostName']}/admin/functions/bronze_health_monitor",
                      data=b'{"input":"{}"}', method='POST',
                      headers={'x-functions-key': keys['masterKey'], 'Content-Type': 'application/json'})
    with urlopen(request, timeout=90) as response:
        check('health_timer_invocation_accepted', response.status == 202, 'Admin invocation of the deployed timer')
    keys = None
    health_query = (
        f"traces | where timestamp >= datetime({requested_at}) "
        "| where message startswith 'BRONZE_HEALTH ' "
        "| top 1 by timestamp desc | project message"
    )
    rows = wait_for(lambda: query_rows(app_id, health_query), seconds=300, interval=15)
    check('health_signal_in_azure_monitor', bool(rows), 'Deployed timer emitted its health trace')
    emitted = json.loads(rows[0][0].removeprefix('BRONZE_HEALTH '))
    report['deployed_health'] = emitted
    check('deployed_health_fresh', emitted['freshness'] == 'fresh' and emitted['poison_messages'] == 0,
          'Timer confirms freshness and empty production poison queue')

    rg_info = base.az_json(['group', 'show', '--name', rg])
    rules = base.az_json(['rest', '--method', 'get', '--url',
                         'https://management.azure.com' + rg_info['id']
                         + '/providers/Microsoft.Insights/scheduledQueryRules?api-version=2022-06-15'])
    expected = {f'bronze-{name}-{project}-{environment}' for name in ('freshness', 'poison', 'failures', 'monitor_missing')}
    bronze_rules = [rule for rule in rules['value'] if rule['name'] in expected]
    check('four_alert_rules_deployed', len(bronze_rules) == 4 and all(rule['properties']['enabled'] for rule in bronze_rules), 'All four expected rules are enabled')
    report['alert_rules'] = [{'name': r['name'], 'enabled': r['properties']['enabled'],
                             'action_group_count': len(r['properties'].get('actions', {}).get('actionGroups', []))}
                            for r in bronze_rules]
    report['external_notifications_configured'] = all(r['action_group_count'] > 0 for r in report['alert_rules'])
    report['validated_commit'] = os.environ.get('VALIDATED_COMMIT', '')
    report['result'] = 'success'


if __name__ == '__main__':
    try:
        delivery()
    except Exception as error:
        report['result'] = 'failed'
        report['error'] = str(error) if isinstance(error, base.ValidationError) else type(error).__name__
        report['error_locations'] = [f'{Path(frame.filename).name}:{frame.lineno}'
                                     for frame in traceback.extract_tb(error.__traceback__)]
        print('Validation failed: ' + report['error'], flush=True)
        print('Locations: ' + ', '.join(report['error_locations']), flush=True)
    finally:
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        Path('validation-report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
        if os.getenv('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as output:
                output.write('\n## Bronze delivery\n\nResult: **' + report['result'] + '**\n\n')
                output.write('| Check | Result |\n| --- | --- |\n')
                for item in report['checks']:
                    output.write(f"| {item['check']} | {'PASS' if item['passed'] else 'FAIL'} |\n")
    raise SystemExit(0 if report['result'] == 'success' else 1)
