"""Explicit GitHub Actions acceptance on a unique, disposable Silver prefix.

Never runs WeatherAPI, writes Bronze or touches a production queue. Connection
strings and SDK exception messages remain in memory and are never reported.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback
import uuid

sys.path.insert(0, str(Path.cwd() / 'functions'))

import pyarrow as pa
from azure.storage.blob import BlobServiceClient
from deltalake import CommitProperties, DeltaTable, write_deltalake
from deltalake.exceptions import CommitFailedError

from shared import config
from shared.silver_contract import OBSERVATION_TABLE, FORECAST_TABLE, TABLE_SCHEMAS, TABLE_KEYS, empty_table
from shared.silver_delta import DeltaSnapshotWriter, SnapshotConflict
from shared.silver_lease import LeaseBusy, LeaseLost
from shared.silver_publication import publication_name, published_read_plan
from shared.silver_runner import bronze_identity, make_azure_runner
from shared.silver_transform import transform_bronze

SOURCE_A = 'weather/weatherapi/2026/10/18/14/20261018_140443_a832e2104ae947e1a909f1dff8c2a518_weather_data.json'
SOURCE_B = 'weather/weatherapi/2026/10/17/23/20261017_230126_82664d4ec7054e89a29dad74b7147012_weather_data.json'
_PREFIX = re.compile(r'^silver/_validation/s1-s3-[0-9a-f]{32}/$')


class InjectedInterruption(RuntimeError):
    pass


def require(condition):
    if not condition:
        raise AssertionError('Acceptance condition failed')


def connection_in_memory():
    account = os.environ['AZURE_STORAGE_ACCOUNT']
    require(re.fullmatch(r'[a-z0-9]{3,24}', account) is not None)
    group = os.environ.get('AZURE_RESOURCE_GROUP')
    if not group:
        project = os.environ.get('PROJECT_NAME') or os.environ.get('TF_VAR_project_name') or os.environ.get('TF_VAR_PROJECT_NAME')
        environment = os.environ.get('ENVIRONMENT') or os.environ.get('TF_VAR_environment') or os.environ.get('TF_VAR_ENVIRONMENT')
        require(project and environment)
        require(re.fullmatch(r'[A-Za-z0-9_-]{1,64}', project) is not None)
        require(re.fullmatch(r'[A-Za-z0-9_-]{1,32}', environment) is not None)
        group = f'rg-{project}-{environment}'
    process = subprocess.run([
        'az', 'storage', 'account', 'show-connection-string',
        '--name', account, '--resource-group', group, '--only-show-errors', '--output', 'json',
    ], capture_output=True, text=True, timeout=90)
    if process.returncode:
        raise RuntimeError('Azure CLI connection lookup failed')
    value = json.loads(process.stdout)['connectionString']
    require(isinstance(value, str) and 'AccountKey=' in value)
    return value


def table_counts(runner):
    return {name: DeltaTable(runner.writer.table_path(name),
                             storage_options=runner.writer.storage_options).to_pyarrow_table().num_rows
            for name in TABLE_SCHEMAS}


def table_versions(runner):
    return {name: DeltaTable(runner.writer.table_path(name),
                             storage_options=runner.writer.storage_options).version()
            for name in TABLE_SCHEMAS}


def gold_counts(publications, runner):
    plan = published_read_plan(publications)
    counts = {}
    for name, specification in plan['tables'].items():
        table = DeltaTable(specification['path'], version=specification['version'],
                           storage_options=runner.writer.storage_options).to_pyarrow_table(
                               filters=[('ingestion_id', 'in', plan['ingestion_ids'])])
        require(set(table.column('ingestion_id').to_pylist()) <= set(plan['ingestion_ids']))
        counts[name] = table.num_rows
    return counts


def main():
    root = 'silver/_validation/s1-s3-' + uuid.uuid4().hex
    prefix = root + '/'
    require(_PREFIX.fullmatch(prefix) is not None)
    output = Path('silver-delta-validation')
    output.mkdir(exist_ok=True)
    report = {'started_at_utc':datetime.now(timezone.utc).isoformat(),
              'prefix':prefix, 'checks':[], 'cleanup':{'attempted':False}}
    service, container, owned = None, None, False
    previous_connection = os.environ.get('STORAGE_CONNECTION_STRING')
    connection = None
    stage = 'configuration'
    try:
        require(config.DATALAKE_CONTAINER == 'datalake' and config.BRONZE_CONTAINER == 'bronze')
        connection = connection_in_memory()
        os.environ['STORAGE_CONNECTION_STRING'] = connection
        service = BlobServiceClient.from_connection_string(connection)
        container = service.get_container_client('datalake')
        # A unique root must be empty before ownership is established. If UUID
        # collision or operator configuration violates this, do not delete it.
        require(next(iter(container.list_blobs(name_starts_with=prefix)), None) is None)
        require(not container.get_blob_client(root).exists())
        owned = True
        report['namespace_owned'] = True
        (output / 'validation-report.json').write_text(json.dumps(report, indent=2) + '\n')
        runner = make_azure_runner(root=root)
        require(runner.controls.prefix == root + '/_control')
        account = os.environ['AZURE_STORAGE_ACCOUNT']
        require(runner.writer.root == f'abfss://datalake@{account}.dfs.core.windows.net/{root}')
        report['checks'].append({'name':'isolated_adapter_root', 'passed':True})

        stage = 'read_existing_sources'
        raw_a = runner.bronze.get_blob_client(SOURCE_A).download_blob().readall()
        raw_b = runner.bronze.get_blob_client(SOURCE_B).download_blob().readall()
        require(len(raw_a) < 20_000_000 and len(raw_b) < 20_000_000)
        result_a, result_b = transform_bronze(raw_a, SOURCE_A), transform_bronze(raw_b, SOURCE_B)
        require(result_a.status == 'partial')
        require(result_a.observations.num_rows == 2 and result_a.forecasts.num_rows == 144)
        require(result_b.observations.num_rows > 0 and result_b.forecasts.num_rows > 0)
        report['checks'].append({'name':'real_bronze_transform', 'passed':True,
                                 'source_a_status':result_a.status,
                                 'source_a_rows':{name:table.num_rows for name, table in result_a.tables.items()},
                                 'source_b_rows':{name:table.num_rows for name, table in result_b.tables.items()}})

        stage = 'publish_real_ingestion'
        first = runner.process_ingestion(SOURCE_A)
        require(first.outcome == 'published' and first.publication['status'] == 'partial')
        expected_a = {name:table.num_rows for name, table in result_a.tables.items()}
        require(table_counts(runner) == expected_a)
        require(first.publication['source_sha256'] == hashlib.sha256(raw_a).hexdigest())
        for name, expected in result_a.tables.items():
            actual = DeltaTable(runner.writer.table_path(name),
                                storage_options=runner.writer.storage_options).to_pyarrow_table().cast(TABLE_SCHEMAS[name])
            keys = TABLE_KEYS[name]
            keyed = lambda table: {tuple(row[key] for key in keys):row for row in table.to_pylist()}
            require(keyed(actual) == keyed(expected))
        report['checks'].append({'name':'real_delta_publication_values', 'passed':True,
                                 'rows':expected_a, 'versions':table_versions(runner)})

        stage = 'exact_replay'
        before = table_versions(runner)
        replay = runner.process_ingestion(SOURCE_A)
        require(replay.outcome == 'already_processed')
        require(table_versions(runner) == before)
        report['checks'].append({'name':'replay_versions_unchanged', 'passed':True})

        stage = 'azure_lease_contention'
        with runner.lease_factory() as held:
            held.assert_held()
            require(runner.process_ingestion(SOURCE_B).outcome == 'busy')
            busy = False
            try:
                with runner.lease_factory():
                    pass
            except LeaseBusy:
                busy = True
            require(busy)
        require(table_versions(runner) == before)
        report['checks'].append({'name':'azure_lease_contention_and_renewal', 'passed':True})

        stage = 'interruption_between_tables'
        original = runner.writer.merge
        def interrupt(name, *args, **kwargs):
            if name == FORECAST_TABLE:
                raise InjectedInterruption()
            return original(name, *args, **kwargs)
        runner.writer.merge = interrupt
        interrupted = False
        try:
            runner.process_ingestion(SOURCE_B)
        except InjectedInterruption:
            interrupted = True
        finally:
            runner.writer.merge = original
        require(interrupted)
        require(runner.controls.read(publication_name(bronze_identity(SOURCE_B))) is None)
        partial_counts = table_counts(runner)
        require(partial_counts[OBSERVATION_TABLE] == expected_a[OBSERVATION_TABLE] + result_b.observations.num_rows)
        require(partial_counts[FORECAST_TABLE] == expected_a[FORECAST_TABLE])
        require(gold_counts([first.publication], runner) == expected_a)
        # Even a newer observations snapshot remains safe when constrained to
        # the frozen completed-ID set, proving the publication barrier explicitly.
        latest = DeltaTable(runner.writer.table_path(OBSERVATION_TABLE),
                            storage_options=runner.writer.storage_options).to_pyarrow_table(
                                filters=[('ingestion_id', '=', bronze_identity(SOURCE_A))])
        require(latest.num_rows == expected_a[OBSERVATION_TABLE])
        report['checks'].append({'name':'incomplete_ingestion_not_consumable_by_gold', 'passed':True})

        stage = 'resume_between_tables'
        resumed = runner.process_ingestion(SOURCE_B)
        require(resumed.outcome == 'published')
        expected_both = {name:expected_a[name]+result_b.tables[name].num_rows for name in TABLE_SCHEMAS}
        require(table_counts(runner) == expected_both)
        require(gold_counts([first.publication, resumed.publication], runner) == expected_both)
        report['checks'].append({'name':'interrupted_resume_no_duplicates', 'passed':True, 'rows':expected_both})

        stage = 'immutable_content_conflict'
        changed = result_a.observations.to_pylist()
        changed[0]['temperature_c'] += 1
        changed = pa.Table.from_pylist(changed, schema=TABLE_SCHEMAS[OBSERVATION_TABLE])
        before_conflict = table_versions(runner)
        conflicted = False
        with runner.lease_factory() as held:
            try:
                runner.writer.merge(OBSERVATION_TABLE, changed, held,
                                    ingestion_id=bronze_identity(SOURCE_A))
            except SnapshotConflict:
                conflicted = True
        require(conflicted and table_versions(runner) == before_conflict)
        report['checks'].append({'name':'immutable_contents_conflict_visible', 'passed':True})

        stage = 'azure_lease_loss_during_ingestion'
        loss_runner = make_azure_runner(root=root + '/lease-loss')
        original_loss_merge = loss_runner.writer.merge
        def lose_after_first(name, *args, **kwargs):
            commit = original_loss_merge(name, *args, **kwargs)
            loss_runner.controls.blob('writer.lock').break_lease(lease_break_period=0)
            args[1].assert_held()  # actual server rejects the broken lease ID
            return commit
        loss_runner.writer.merge = lose_after_first
        lost = False
        try:
            loss_runner.process_ingestion(SOURCE_A)
        except LeaseLost:
            lost = True
        finally:
            loss_runner.writer.merge = original_loss_merge
        require(lost)
        require(loss_runner.controls.read(publication_name(bronze_identity(SOURCE_A))) is None)
        require(loss_runner.process_ingestion(SOURCE_A).outcome == 'published')
        require(table_counts(loss_runner) == expected_a)
        report['checks'].append({'name':'broken_azure_lease_stops_publication_then_replays', 'passed':True})

        stage = 'optimistic_stale_snapshot_conflict'
        optimistic = DeltaSnapshotWriter(runner.writer.root + '/optimistic',
                                         storage_options=runner.writer.storage_options)
        one_row = result_a.observations.slice(0, 1)
        with runner.lease_factory() as held:
            optimistic.merge(OBSERVATION_TABLE, empty_table(OBSERVATION_TABLE), held,
                             ingestion_id=bronze_identity(SOURCE_A))
            one = DeltaTable(optimistic.table_path(OBSERVATION_TABLE), storage_options=optimistic.storage_options)
            two = DeltaTable(optimistic.table_path(OBSERVATION_TABLE), storage_options=optimistic.storage_options)
            predicate = ' AND '.join(f'target.{key} = source.{key}' for key in TABLE_KEYS[OBSERVATION_TABLE])
            def merger(target):
                return target.merge(one_row, predicate=predicate,
                                    source_alias='source', target_alias='target',
                                    commit_properties=CommitProperties(max_commit_retries=0)
                                    ).when_not_matched_insert_all()
            pending_one, pending_two = merger(one), merger(two)
            pending_one.execute()
            conflict = False
            try:
                pending_two.execute()
            except CommitFailedError:
                conflict = True
            require(conflict)
            held.assert_held()
            require(optimistic.merge(OBSERVATION_TABLE, one_row, held,
                                     ingestion_id=bronze_identity(SOURCE_A)).inserted == 0)
        require(DeltaTable(optimistic.table_path(OBSERVATION_TABLE),
                           storage_options=optimistic.storage_options).to_pyarrow_table().num_rows == 1)
        report['checks'].append({'name':'adls_delta_stale_snapshot_conflict', 'passed':True})
    except Exception as error:
        failure = {'name':stage, 'passed':False, 'error_type':type(error).__name__}
        report['checks'].append(failure)
        if owned and container is not None and type(error).__name__ == 'TableNotFoundError':
            # Bounded probes only under the unique root owned by this run.
            report['diagnostic_objects'] = [item.name.removeprefix(prefix)
                for item in container.list_blobs(name_starts_with=prefix)][:100]
            original_zero = root + '/' + OBSERVATION_TABLE + '/_delta_log/00000000000000000000.json'
            report['original_commit_zero_exists'] = container.get_blob_client(original_zero).exists()
            account = os.environ['AZURE_STORAGE_ACCOUNT']
            options = runner.writer.storage_options
            probes = [
                ('canonical_abfss', f'abfss://datalake@{account}.dfs.core.windows.net/{root}/probe-canonical', root + '/probe-canonical'),
                ('empty_write_short_uri', 'az://datalake/' + root + '/probe-write', root + '/probe-write'),
                ('create_without_config', 'az://datalake/' + root + '/probe-no-config', root + '/probe-no-config'),
            ]
            report['creation_probes'] = []
            for label, uri, relative in probes:
                probe = {'name':label}
                try:
                    if label == 'empty_write_short_uri':
                        write_deltalake(uri, empty_table(OBSERVATION_TABLE), mode='error',
                                        storage_options=options,
                                        configuration={'delta.isolationLevel':'Serializable'})
                        table = DeltaTable(uri, storage_options=options)
                    else:
                        table = DeltaTable.create(uri, TABLE_SCHEMAS[OBSERVATION_TABLE],
                                                  mode='error', storage_options=options)
                    probe.update(passed=True, version=table.version())
                except Exception as probe_error:
                    message = str(probe_error)
                    probe.update(passed=False, error_type=type(probe_error).__name__,
                                 no_log_segment='No files in log segment' in message)
                probe['commit_zero_exists'] = container.get_blob_client(relative + '/_delta_log/00000000000000000000.json').exists()
                report['creation_probes'].append(probe)
        detail = str(error)
        if connection:
            detail = detail.replace(connection, '<connection>')
            for item in connection.split(';'):
                if item.startswith(('AccountKey=', 'SharedAccessSignature=')):
                    detail = detail.replace(item.split('=', 1)[1], '<credential>')
        detail = re.sub(r'https?://[^\s\"\'<>]+', '<storage-url>', detail)
        detail = re.sub(r'(?i)(authorization|accountkey|sharedaccesssignature|sig)[=:][^\s,;}]+', r'\1=<redacted>', detail)
        detail = re.sub(r'(?i)SharedKey\s+[^\s]+', 'SharedKey <redacted>', detail)
        failure.update(detail=detail[:2500],
                       locations=[f'{Path(frame.filename).name}:{frame.lineno}'
                                  for frame in traceback.extract_tb(error.__traceback__)])
    finally:
        if container is not None and owned:
            report['cleanup']['attempted'] = True
            try:
                require(_PREFIX.fullmatch(prefix) is not None)
                blobs = []
                for blob in container.list_blobs(name_starts_with=prefix):
                    require(blob.name.startswith(prefix))
                    blobs.append(blob.name)
                    require(len(blobs) <= 10000)
                blobs.sort(key=lambda name: (name.count('/'), name), reverse=True)
                deleted, errors = 0, []
                for name in blobs:
                    try:
                        client = container.get_blob_client(name)
                        properties = client.get_blob_properties()
                        if properties.lease.status == 'locked':
                            client.break_lease(lease_break_period=0)
                        client.delete_blob(delete_snapshots='include')
                        deleted += 1
                    except Exception as error:
                        errors.append({'error_type':type(error).__name__})
                remaining = sum(1 for _ in container.list_blobs(name_starts_with=prefix))
                # HNS may expose an explicit directory marker for the root,
                # whose name does not end in '/'. It is ours only because both
                # it and its children were absent before this run.
                root_blob = container.get_blob_client(root)
                if remaining == 0 and root_blob.exists():
                    root_blob.delete_blob()
                root_remaining = root_blob.exists()
                report['cleanup'].update(passed=remaining == 0 and not root_remaining and not errors,
                                         deleted=deleted, remaining=remaining,
                                         root_remaining=root_remaining, errors=errors)
            except Exception as error:
                report['cleanup'].update(passed=False, error_type=type(error).__name__)
        else:
            report['cleanup'].update(passed=not owned, skipped='namespace_not_owned')
        if service is not None:
            try:
                service.close()
            except Exception as error:
                report['checks'].append({'name':'close_storage_client', 'passed':False, 'error_type':type(error).__name__})
        if previous_connection is None:
            os.environ.pop('STORAGE_CONNECTION_STRING', None)
        else:
            os.environ['STORAGE_CONNECTION_STRING'] = previous_connection
        report['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        report['status'] = 'success' if report['checks'] and all(check['passed'] for check in report['checks']) and report['cleanup'].get('passed') else 'failed'
        (output / 'validation-report.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, separators=(',', ':')), flush=True)
    return 0 if report['status'] == 'success' else 1


if __name__ == '__main__':
    raise SystemExit(main())
