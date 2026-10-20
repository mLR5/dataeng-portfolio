#!/usr/bin/env python3
"""Isolated real Flex runtime acceptance; never deploy to the main Function App.

Creates an owned temporary app/plan, two storage prefixes and disabled log rules.
Copies two genuine consecutive-hour archives unchanged to a temporary container.
The accelerated timer opens a daily batch; real queue triggers continue it.
The production schedule remains daily at 02:00 UTC. All owned resources are removed.
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'functions'))

from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import BlobServiceClient
from azure.storage.queue import QueueServiceClient
from deltalake import DeltaTable

from shared import config
from shared.claim_check import parse_timestamp
from shared.silver_contract import FORECAST_TABLE, TABLE_SCHEMAS
from shared.silver_discovery import discover_bronze
from shared.silver_lease import BlobWriterLease, LeaseBusy
from shared.silver_publication import json_bytes, published_read_plan, publication_name
from shared.silver_runner import bronze_identity
from shared.silver_runtime import STATE_NAME, _source_time, batch_cutoff, earliest_bronze, make_runtime, run_tick
from shared.silver_settings import SilverSettings
from shared.silver_queue import wakeup_body
from shared.silver_transform import transform_bronze

BASE = Path(__file__).resolve().parents[1]
REPORT_PATH = Path('silver-s4-report.json')
MANIFEST_PATH = Path('silver-s4-resources.json')
NONCE = os.environ.get('GITHUB_RUN_ID', '') + '-' + os.environ.get('GITHUB_RUN_ATTEMPT', '1')
if not re.fullmatch(r'\d+-\d+', NONCE):
    raise ValidationError('Run this validation from its GitHub workflow')
REPORT = {'result': 'failed', 'commit': os.environ.get('GITHUB_SHA'), 'checks': [], 'cleanup': {}}
OWNED = {'owner': NONCE, 'resource_ids': [], 'containers': [], 'queues': [], 'root': f'silver/validation-s4/{NONCE}'}
SERVICE = None


class ValidationError(RuntimeError):
    pass


def persist():
    REPORT_PATH.write_bytes(json_bytes(REPORT))
    MANIFEST_PATH.write_bytes(json_bytes(OWNED))


def check(name, condition, detail=None):
    REPORT['checks'].append({'name': name, 'passed': bool(condition), 'detail': detail})
    persist()
    print(f"{'PASS' if condition else 'FAIL'} {name}", flush=True)
    if not condition:
        raise AssertionError(name)


def az(args, *, timeout=180, absent_ok=False):
    result = subprocess.run(['az', *args, '--only-show-errors', '--output', 'json'],
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        if absent_ok and any(code in result.stderr for code in ('ResourceNotFound', 'ResourceGroupNotFound', 'NotFound')):
            return None
        codes = re.findall(r'\(([A-Za-z][A-Za-z0-9]+)\)|"code"\s*:\s*"([A-Za-z][A-Za-z0-9]+)"', result.stderr)
        codes = [first or second for first, second in codes]
        REPORT['last_cli_error'] = {'command': args[:2], 'codes': codes[:3]}
        raise ValidationError('Azure CLI command failed; raw output withheld')
    return json.loads(result.stdout) if result.stdout.strip() else None


def arm(method, resource_id, body=None, api='2024-04-01'):
    args = ['rest', '--method', method, '--url', f'{resource_id}?api-version={api}']
    if body is None:
        return az(args, absent_ok=method in ('delete', 'get'))
    with tempfile.TemporaryDirectory() as temporary:
        file = Path(temporary) / 'body.json'
        file.write_bytes(json_bytes(body))
        return az([*args, '--body', '@' + str(file)])


def wait_for(name, predicate, *, seconds=360):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            check(name, True)
            return value
        time.sleep(10)
    check(name, False)


def app_settings(app, rg, values):
    with tempfile.TemporaryDirectory() as temporary:
        file = Path(temporary) / 'settings.json'
        file.write_bytes(json_bytes(values))
        az(['functionapp', 'config', 'appsettings', 'set', '--name', app, '--resource-group', rg,
            '--settings', '@' + str(file)])


def source_snapshot(container, start, end):
    names = []
    cursor = None
    inspected = 0
    for _ in range(20):
        page = discover_bronze(container, start, end, limit=1000, max_pages=48, cursor=cursor)
        names.extend(page.blobs)
        inspected += page.inspected
        cursor = page.continuation
        if cursor is None:
            REPORT['source_objects_scanned'] = inspected
            return sorted(set(names))
    raise ValidationError('Validation window exceeds its 20000-object bound')


def publications(runner):
    prefix = runner.controls.prefix + '/publications/'
    return [json.loads(runner.controls.container.get_blob_client(item.name).download_blob().readall())
            for item in runner.controls.container.list_blobs(name_starts_with=prefix)]


def table_versions(runner):
    return {name: DeltaTable(runner.writer.table_path(name), storage_options=runner.writer.storage_options).version()
            for name in TABLE_SCHEMAS}


def verify_gold(runner, published):
    plan = published_read_plan(published)
    result = {}
    for name, spec in plan['tables'].items():
        rows = DeltaTable(spec['path'], version=spec['version'], storage_options=runner.writer.storage_options).to_pyarrow_table(
            filters=[('ingestion_id', 'in', plan['ingestion_ids'])]).cast(TABLE_SCHEMAS[name]).to_pylist()
        keys = ('ingestion_id', 'city_id', 'observed_at_utc' if name != FORECAST_TABLE else 'target_at_utc')
        check('no_duplicate_keys_' + name, len({tuple(row[key] for key in keys) for row in rows}) == len(rows))
        expected = sum(pub['tables'][name]['row_count'] for pub in published)
        check('published_row_counts_' + name, len(rows) == expected, {'actual': len(rows), 'expected': expected})
        result[name] = len(rows)
    return result


def production_functions(app, rg):
    return sorted(entry['name'].rsplit('/', 1)[-1] for entry in az([
        'functionapp', 'function', 'list', '--name', app, '--resource-group', rg]))


def validate():
    global SERVICE
    recovery_only = os.environ.get('SILVER_VALIDATION_MODE') == 'recovery_only'
    REPORT['validation_mode'] = 'recovery_only' if recovery_only else 'bootstrap_and_recovery'
    project, environment = os.environ['PROJECT_NAME'], os.environ['ENVIRONMENT']
    account = os.environ['AZURE_STORAGE_ACCOUNT']
    location = os.environ.get('AZURE_LOCATION') or 'francecentral'
    rg = f'rg-{project}-{environment}'
    main_app = f'func-{project}-{environment}'
    app = f'vs4-{NONCE}'
    plan = f'asp-vs4-{NONCE}'
    source_container, deployment_container = f'vs4-source-{NONCE}', f'vs4-deploy-{NONCE}'
    subscription = az(['account', 'show'])['id']
    scope = f'/subscriptions/{subscription}/resourceGroups/{rg}'
    app_id, plan_id = scope + '/providers/Microsoft.Web/sites/' + app, scope + '/providers/Microsoft.Web/serverfarms/' + plan
    check('temporary_app_is_distinct', app != main_app)
    before = production_functions(main_app, rg)
    REPORT['production_functions_before'] = before
    connection = az(['storage', 'account', 'show-connection-string', '--name', account,
                     '--resource-group', rg, '--query', 'connectionString'])
    SERVICE = BlobServiceClient.from_connection_string(connection)
    os.environ['STORAGE_CONNECTION_STRING'] = connection
    os.environ['STORAGE_ACCOUNT_NAME'] = account
    originals = SERVICE.get_container_client('bronze')
    REPORT['stage'] = 'inventory_source'
    persist()
    now = datetime.now(timezone.utc)
    # Test-only hour 0 keeps a stable closed-day horizon through the Azure build.
    batch_end = batch_cutoff(now, SilverSettings(batch_hour_utc=0))
    retained = source_snapshot(originals, batch_end - timedelta(hours=48), batch_end)
    check('recent_retained_bronze_window_nonempty', bool(retained))
    cursor = None
    for page_number in range(8):
        first_page = earliest_bronze(originals, cursor=cursor, limit=1 if page_number == 0 else 1000)
        if first_page['earliest'] is not None:
            break
        cursor = first_page['continuation']
        if cursor is None:
            break
    check('real_paginated_earliest_discovery',
          (first_page['earliest'] is not None and first_page['earliest'] <= _source_time(retained[0]))
          or isinstance(first_page['continuation'], str),
          {'pages': page_number + 1, 'has_continuation': first_page['continuation'] is not None})
    # The PR must not run an unbounded business backfill before merge. Exercise
    # every retained archive in a declared <=48h window containing at most ~80
    # ordinary hourly ingestions. Default full-history discovery is tested below
    # on the isolated source, and by the multi-window scheduler regression.
    cutoff = max(_source_time(retained[-1]) - timedelta(hours=48), _source_time(retained[-min(80, len(retained))]))
    snapshot = [name for name in retained if _source_time(name) >= cutoff]
    REPORT['source_scope'] = {'ingestions_in_48h_window': len(retained),
                              'validation_from': cutoff.isoformat(), 'selected_ingestions': 2 if recovery_only else len(snapshot),
                              'inventory_to': batch_end.isoformat(), 'maximum_window_hours': 48}
    check('retained_bronze_snapshot_nonempty', bool(snapshot), {'ingestions': len(snapshot)})
    by_hour = {}
    for name in snapshot:
        by_hour[_source_time(name).replace(minute=0, second=0)] = name
    pair = next(((by_hour[hour - timedelta(hours=1)], by_hour[hour]) for hour in sorted(by_hour, reverse=True)
                 if hour.hour == 0 and hour - timedelta(hours=1) in by_hour), None)
    check('two_genuine_consecutive_hour_archives', pair is not None)
    REPORT['consecutive_sources'] = pair
    marker = SERVICE.get_blob_client('datalake', OWNED['root'] + '/owner.json')
    marker.upload_blob(json_bytes({'owner': NONCE}), overwrite=False)
    OWNED['marker_created'] = True
    for name in (source_container, deployment_container):
        SERVICE.create_container(name)
        OWNED['containers'].append(name)
        persist()
    queue_service = QueueServiceClient.from_connection_string(connection)
    queue_a = queue_b = f'vs4-work-a-{NONCE}'
    for name in (queue_a,):
        OWNED['queues'].append(name)
        persist()
        queue_service.create_queue(name)
    tags = {'purpose': 'silver-s4-validation', 'validation_id': NONCE}
    OWNED['resource_ids'].append(plan_id)
    persist()
    REPORT['stage'] = 'create_flex_plan'
    persist()
    arm('put', plan_id, {'location': location, 'kind': 'functionapp', 'sku': {'name': 'FC1', 'tier': 'FlexConsumption'},
                         'properties': {'reserved': True}, 'tags': tags})
    root_a, root_b = OWNED['root'] + '/bootstrap', OWNED['root'] + '/resume'
    values = {
        'AzureWebJobsStorage': connection, 'DEPLOYMENT_STORAGE_CONNECTION_STRING': connection,
        'AzureWebJobsFeatureFlags': 'EnableWorkerIndexing',
        'STORAGE_CONNECTION_STRING': connection, 'STORAGE_ACCOUNT_NAME': account,
        'QUEUE_NAME': f'vs4-unused-{NONCE}', 'STAGING_CONTAINER': source_container,
        'BRONZE_CONTAINER': 'bronze', 'DATALAKE_CONTAINER': 'datalake',
        'SILVER_ENABLED': 'true', 'SILVER_ROOT': root_a, 'SILVER_TIMER_SCHEDULE': '0 * * * * *',
        'SILVER_START_AT': cutoff.isoformat(), 'SILVER_QUEUE_NAME': queue_a, 'SILVER_BATCH_HOUR_UTC': '0',
        'SILVER_HEALTH_SCHEDULE': '0 * * * * *', 'SILVER_BATCH_SIZE': '5',
        'SILVER_DISCOVERY_LIMIT': '100', 'SILVER_DISCOVERY_PAGES': '168', 'SILVER_BUDGET_SECONDS': '480',
        **{f'AzureWebJobs.{name}.Disabled': 'true' for name in before},
    }
    if recovery_only:
        # A fresh app starts directly on the recovery source/root. No old daily
        # opener or delayed bootstrap wake-up can advance this test's horizon.
        values.update(BRONZE_CONTAINER=source_container, SILVER_ROOT=root_b, SILVER_START_AT='',
                      SILVER_TIMER_SCHEDULE='0 0 0 1 1 *',
                      **{'AzureWebJobs.weather_silver_processor.Disabled': 'true'})
    OWNED['resource_ids'].append(app_id)
    persist()
    REPORT['stage'] = 'create_flex_app'
    persist()
    arm('put', app_id, {
        'location': location, 'kind': 'functionapp,linux', 'identity': {'type': 'SystemAssigned'}, 'tags': tags,
        'properties': {'serverFarmId': plan_id, 'httpsOnly': True,
                       'functionAppConfig': {
                           'runtime': {'name': 'python', 'version': '3.11'},
                           'scaleAndConcurrency': {'maximumInstanceCount': 40, 'instanceMemoryMB': 2048},
                           'deployment': {'storage': {'type': 'blobContainer',
                               'value': f'https://{account}.blob.core.windows.net/{deployment_container}',
                               'authentication': {'type': 'StorageAccountConnectionString',
                                                  'storageAccountConnectionStringName': 'DEPLOYMENT_STORAGE_CONNECTION_STRING'}}}},
                       'siteConfig': {'appSettings': [{'name': k, 'value': v} for k, v in values.items()]}},
    })
    # Write through the app-settings API as well: the build service resolves its
    # deployment connection by setting name, before the Python host is started.
    app_settings(app, rg, values)
    configured_names = az(['functionapp', 'config', 'appsettings', 'list', '--name', app,
                           '--resource-group', rg, '--query', '[].name'])
    check('all_runtime_settings_present', set(values) <= set(configured_names))
    REPORT['stage'] = 'remote_build_and_deploy'
    persist()
    with tempfile.TemporaryDirectory() as temporary:
        archive = Path(temporary) / 'functions.zip'
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as target:
            for file in (BASE / 'functions').rglob('*'):
                if file.is_file() and '__pycache__' not in file.parts:
                    target.write(file, file.relative_to(BASE / 'functions'))
        az(['functionapp', 'deployment', 'source', 'config-zip', '--resource-group', rg, '--name', app,
            '--src', str(archive), '--build-remote', 'true'], timeout=900)
    def indexed_functions():
        result = production_functions(app, rg)
        REPORT['isolated_indexed_functions'] = result
        persist()
        return result if len(result) == 7 else None
    names = wait_for('seven_functions_indexed_with_delta_dependencies', indexed_functions, seconds=180)
    check('silver_functions_indexed', {'weather_silver_processor', 'silver_batch_worker', 'silver_health_monitor'} <= set(names))
    if not recovery_only:
        settings = SilverSettings.from_env({**values, 'SILVER_ROOT': root_a})
        config.BRONZE_CONTAINER = 'bronze'
        runner_a, store_a, _ = make_runtime(settings)
        expected_ids = {bronze_identity(name) for name in snapshot}
        def bootstrapped():
            state = store_a.read()
            if (not state or state['history']['passes'] < 1 or state['pending']
                    or not state.get('last_run') or not state.get('last_finished_at')
                    or parse_timestamp(state['last_finished_at']) < parse_timestamp(state['last_started_at'])):
                return None
            published = publications(runner_a)
            return (state, published) if expected_ids <= {pub['ingestion_id'] for pub in published} else None
        REPORT['stage'] = 'bootstrap'
        persist()
        state_a, published_a = wait_for('automatic_bootstrap_covers_all_retained_archives', bootstrapped, seconds=600)
        REPORT['bootstrap'] = {'source_snapshot': len(snapshot), 'publications': len(published_a),
                               'rows': verify_gold(runner_a, published_a), 'last_run': state_a['last_run'], 'worker_invocations': state_a['invocations']}
        check('queue_continuations_finish_more_than_one_lot', state_a['invocations'] > 1 and state_a['next_wake_at'] is None)
        check('only_closed_collection_days_published', all(_source_time(pub['source_blob']) < batch_end for pub in published_a))
        check('daily_completion_recorded', state_a['daily']['completed_end'] == batch_end.isoformat())
        check('runtime_memory_within_2gb', state_a['last_run']['peak_rss_mb'] < 2048,
              {'peak_rss_mb': state_a['last_run']['peak_rss_mb']})

        # Phase B changes imported source settings. Stop the owned app before
        # switching roots so no old instance can consume/ack a new wake-up while
        # SILVER_ENABLED or the source binding is still propagating. Reuse the same
        # queue binding: this also avoids a scale-controller remapping race.
        az(['functionapp', 'stop', '--name', app, '--resource-group', rg])
        wait_for('owned_app_stopped_before_phase_switch',
                 lambda: (arm('get', app_id) or {}).get('properties', {}).get('state') == 'Stopped', seconds=120)
        queue_service.get_queue_client(queue_a).clear_messages()
        app_settings(app, rg, {'BRONZE_CONTAINER': source_container, 'SILVER_ROOT': root_b,
                              'SILVER_START_AT': '', 'SILVER_ENABLED': 'true',
                              'AzureWebJobs.weather_silver_processor.Disabled': 'true', 'SILVER_QUEUE_NAME': queue_b})
    isolated_source = SERVICE.get_container_client(source_container)
    raws = {name: originals.get_blob_client(name).download_blob().readall() for name in pair}
    isolated_source.get_blob_client(pair[0]).upload_blob(raws[pair[0]], overwrite=False)
    config.BRONZE_CONTAINER = source_container
    settings_b = SilverSettings.from_env({**values, 'SILVER_ROOT': root_b, 'SILVER_START_AT': '', 'SILVER_QUEUE_NAME': queue_b})
    runner_b, store_b, _ = make_runtime(settings_b)
    real_writer = runner_b.writer
    class InjectedInterruption(RuntimeError):
        pass
    class FailSecondTable:
        table_path = real_writer.table_path
        def merge(self, name, rows, lease, **kwargs):
            if name == FORECAST_TABLE:
                raise InjectedInterruption()
            return real_writer.merge(name, rows, lease, **kwargs)
    runner_b.writer = FailSecondTable()
    first_end = _source_time(pair[1]).replace(hour=0, minute=0, second=0, microsecond=0)
    second_end = first_end + timedelta(days=1)
    seeded = run_tick(runner_b, store_b, settings_b, cutoff=first_end)
    runner_b.writer = real_writer
    check('interrupted_ingestion_is_durable_and_unpublished',
          seeded['technical_errors'] == 1 and pair[0] in store_b.read()['pending']
          and runner_b.controls.read(publication_name(bronze_identity(pair[0]))) is None)
    # Start only after the checkpoint and settings are complete. The daily
    # opener stays disabled: only deployed queue continuations can resume.
    if not recovery_only:
        az(['functionapp', 'start', '--name', app, '--resource-group', rg])
        wait_for('owned_app_running_with_resume_configuration',
                 lambda: (arm('get', app_id) or {}).get('properties', {}).get('state') == 'Running', seconds=120)
    queue_service.get_queue_client(queue_b).send_message(wakeup_body(first_end))
    first = wait_for('deployed_queue_resumes_between_table_commits',
                     lambda: runner_b.controls.read(publication_name(bronze_identity(pair[0]))), seconds=300)
    first = json.loads(first)
    check('publication_matches_exact_genuine_source', first['source_sha256'] == transform_bronze(raws[pair[0]], pair[0]).source_sha256)
    with BlobWriterLease(runner_b.controls.blob('writer.lock')):
        isolated_source.get_blob_client(pair[1]).upload_blob(raws[pair[1]], overwrite=False)
        def prepare_second_request():
            try:
                with store_b.session() as session:
                    state = session.read()
                    # A delayed opener from the accelerated bootstrap can have
                    # completed this UTC day before this deliberately late copy.
                    # Make its real reconciliation due rather than pretending
                    # a duplicate daily request should reset a finished window.
                    if parse_timestamp(state['target_end']) >= second_end and state['daily']['window'] is None:
                        state['history']['next_reconcile_at'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
                        session.save(state)
                        REPORT['second_collection_mode'] = 'late_arrival_reconciliation'
                    else:
                        REPORT['second_collection_mode'] = 'next_daily_batch'
                return True
            except LeaseBusy:
                return False
        wait_for('second_collection_request_prepared', prepare_second_request, seconds=60)
        queue_service.get_queue_client(queue_b).send_message(wakeup_body(second_end))
        def busy_pending():
            state = store_b.read()
            job = (state or {}).get('pending', {}).get(pair[1])
            return state if job and job.get('last_error_type') == 'LeaseBusy' and job['attempts'] > 0 else None
        wait_for('deployed_queue_retains_busy_writer_as_pending', busy_pending, seconds=240)
    wait_for('next_daily_batch_processes_second_consecutive_hour',
             lambda: runner_b.controls.read(publication_name(bronze_identity(pair[1]))), seconds=300)
    complete = publications(runner_b)
    check('both_consecutive_ingestions_published', {pub['ingestion_id'] for pub in complete} == {bronze_identity(name) for name in pair})
    REPORT['consecutive_rows'] = verify_gold(runner_b, complete)
    wait_for('second_collection_batch_finished', lambda: (lambda state: state if state and not state['pending'] and state['next_wake_at'] is None else None)(store_b.read()), seconds=180)
    versions = table_versions(runner_b)
    # Make the weekly reconciliation due in this owned test checkpoint. Its
    # execution still runs exclusively through the deployed queue trigger.
    with store_b.session() as session:
        state = session.read()
        state['history']['next_reconcile_at'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        session.save(state)
    last_started = store_b.read()['last_started_at']
    queue_service.get_queue_client(queue_b).send_message(wakeup_body(second_end))
    def replayed():
        state = store_b.read()
        return state if (state and state.get('last_finished_at') and state['last_started_at'] > last_started
                         and parse_timestamp(state['last_finished_at']) >= parse_timestamp(state['last_started_at'])
                         and state['last_run'].get('started_at') == state['last_started_at']
                         and not state['pending'] and state['last_run'].get('already_processed', 0) >= 2) else None
    replay = wait_for('automatic_replay_without_duplicates', replayed, seconds=240)
    check('replay_preserves_delta_versions', table_versions(runner_b) == versions)
    def health_snapshot():
        raw = runner_b.controls.read('runtime/health.json')
        if not raw:
            return None
        health = json.loads(raw)
        return health if health.get('scheduler') == 'fresh' and health.get('latest_source_at') == replay['latest_source_at'] else None
    REPORT['health'] = wait_for('independent_health_timer_publishes_snapshot', health_snapshot, seconds=180)
    REPORT['replay'] = replay['last_run']

    # Validate the actual Azure Monitor API accepts all five KQL rule definitions.
    component = az(['monitor', 'app-insights', 'component', 'show', '--app', f'appi-{project}-{environment}', '--resource-group', rg])
    queries = re.findall(r'(\w+) = \{\s+description = .*?\n\s+query\s*=\s*<<-KQL\n(.*?)\n\s+KQL',
                         (BASE / 'infra/silver_monitoring.tf').read_text(), re.S)
    check('five_monitor_queries_available', len(queries) == 5)
    for key, query in queries:
        resource_id = scope + '/providers/Microsoft.Insights/scheduledQueryRules/' + f'vs4-{NONCE}-{key}'
        OWNED['resource_ids'].append(resource_id)
        persist()
        query = textwrap.dedent(query).replace('func-${var.project_name}-${var.environment}', app)
        arm('put', resource_id, {'location': location, 'tags': tags, 'properties': {
            'displayName': f'S4 isolated validation {key}', 'enabled': False, 'severity': 2,
            'scopes': [component['id']], 'evaluationFrequency': 'PT15M', 'windowSize': 'PT1H',
            'autoMitigate': True, 'skipQueryValidation': False,
            'criteria': {'allOf': [{'query': query, 'timeAggregation': 'Count', 'operator': 'GreaterThan',
                                   'threshold': 0, 'failingPeriods': {'numberOfEvaluationPeriods': 1, 'minFailingPeriodsToAlert': 1}}]},
        }}, api='2023-12-01')
        check('azure_accepts_monitor_rule_' + key, True)
    after = production_functions(main_app, rg)
    check('main_function_app_unchanged', after == before)
    REPORT['production_functions_after'] = after
    REPORT['result'] = 'success'
    persist()


def cleanup():
    global SERVICE
    if OWNED.get('owner') != NONCE or OWNED.get('root') != f'silver/validation-s4/{NONCE}':
        raise ValidationError('Cleanup ownership mismatch')
    failures = []
    for resource_id in reversed(OWNED['resource_ids']):
        try:
            check_owned = resource_id.rsplit('/', 1)[-1]
            if NONCE not in check_owned:
                raise ValidationError('Cleanup resource is not owned')
            api = '2023-12-01' if 'scheduledQueryRules' in resource_id else '2024-04-01'
            arm('delete', resource_id, api=api)
            for attempt in range(18):
                if arm('get', resource_id, api=api) is None:
                    break
                time.sleep(5)
            else:
                raise ValidationError('Resource deletion has not completed')
        except Exception as error:
            failures.append({'kind': 'resource', 'type': type(error).__name__})
    if SERVICE is None:
        account = os.environ['AZURE_STORAGE_ACCOUNT']
        rg = f"rg-{os.environ['PROJECT_NAME']}-{os.environ['ENVIRONMENT']}"
        connection = az(['storage', 'account', 'show-connection-string', '--name', account, '--resource-group', rg, '--query', 'connectionString'])
        SERVICE = BlobServiceClient.from_connection_string(connection)
    for name in OWNED['containers']:
        try:
            if name not in (f'vs4-source-{NONCE}', f'vs4-deploy-{NONCE}'):
                raise ValidationError('Cleanup container is not owned')
            SERVICE.delete_container(name)
        except ResourceNotFoundError:
            pass
        except Exception as error:
            failures.append({'kind': 'container', 'type': type(error).__name__})
    if OWNED.get('marker_created'):
        try:
            container = SERVICE.get_container_client('datalake')
            marker = container.get_blob_client(OWNED['root'] + '/owner.json')
            try:
                owner = json.loads(marker.download_blob().readall())
            except ResourceNotFoundError:
                owner = None
            if owner is not None:
                if owner != {'owner': NONCE}:
                    raise ValidationError('Cleanup prefix owner changed')
                names = [item.name for item in container.list_blobs(name_starts_with=OWNED['root'] + '/')]
                for name in sorted(names, key=lambda n: n.count('/'), reverse=True):
                    if name != marker.blob_name:
                        if name.endswith(('/writer.lock', '/runtime/coordinator.json')):
                            from azure.storage.blob import BlobLeaseClient
                            blob = container.get_blob_client(name)
                            if blob.get_blob_properties().lease.status == 'locked':
                                BlobLeaseClient(blob).break_lease(lease_break_period=0)
                        container.delete_blob(name)
                marker.delete_blob()
            remaining = list(container.list_blobs(name_starts_with=OWNED['root'] + '/'))
            if remaining:
                raise ValidationError('Validation prefix was not completely removed')
            try:
                container.delete_blob(OWNED['root'])
            except ResourceNotFoundError:
                pass
        except Exception as error:
            failures.append({'kind': 'prefix', 'type': type(error).__name__})
    queue_service = QueueServiceClient.from_connection_string(az(['storage', 'account', 'show-connection-string',
        '--name', os.environ['AZURE_STORAGE_ACCOUNT'], '--resource-group', f"rg-{os.environ['PROJECT_NAME']}-{os.environ['ENVIRONMENT']}", '--query', 'connectionString']))
    for name in OWNED.get('queues', []):
        try:
            if name not in (f'vs4-work-a-{NONCE}', f'vs4-work-b-{NONCE}'):
                raise ValidationError('Cleanup queue is not owned')
            queue_service.delete_queue(name)
            try:
                queue_service.delete_queue(name + '-poison')
            except ResourceNotFoundError:
                pass
        except ResourceNotFoundError:
            pass
        except Exception as error:
            failures.append({'kind': 'queue', 'type': type(error).__name__})
    REPORT['cleanup'] = {'result': 'success' if not failures else 'failed', 'errors': failures}
    persist()
    if failures:
        raise ValidationError('Some isolated validation resources remain; see manifest')


if __name__ == '__main__':
    try:
        if '--cleanup' in sys.argv:
            if MANIFEST_PATH.exists():
                OWNED = json.loads(MANIFEST_PATH.read_bytes())
                if REPORT_PATH.exists():
                    REPORT = json.loads(REPORT_PATH.read_bytes())
                cleanup()
        else:
            persist()
            try:
                validate()
            except Exception as error:
                REPORT['validation_error_type'] = type(error).__name__
                if isinstance(error, (ValidationError, AssertionError)):
                    REPORT['validation_error_reason'] = str(error)
                persist()
                raise
            finally:
                cleanup()
    except Exception as error:
        REPORT['error_type'] = type(error).__name__
        persist()
    print(json.dumps({'result': REPORT['result'], 'checks': REPORT['checks'], 'cleanup': REPORT['cleanup'],
                      'error_type': REPORT.get('error_type'), 'last_cli_error': REPORT.get('last_cli_error'),
                      'stage': REPORT.get('stage'), 'reason': REPORT.get('validation_error_reason'),
                      'source_objects_scanned': REPORT.get('source_objects_scanned'),
                      'first_source_object': REPORT.get('first_source_object'),
                      'isolated_indexed_functions': REPORT.get('isolated_indexed_functions'),
                      'source_scope': REPORT.get('source_scope')}), flush=True)
    success = REPORT['cleanup'].get('result') == 'success' and ('--cleanup' in sys.argv or REPORT['result'] == 'success')
    raise SystemExit(0 if success else 1)
