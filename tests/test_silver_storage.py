"""Real local Delta transactions plus deterministic publication failure injection."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pyarrow as pa
import pytest
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from deltalake import DeltaTable, CommitProperties
from deltalake.exceptions import CommitFailedError

from shared.silver_contract import (
    SCHEMA_VERSION, TRANSFORM_VERSION, SOURCE, TABLE_SCHEMAS, TABLE_KEYS,
    OBSERVATION_TABLE, FORECAST_TABLE, TransformResult, Rejection, empty_table,
)
from shared.silver_delta import DeltaSnapshotWriter, SnapshotConflict
from shared.silver_lease import BlobWriterLease, LeaseBusy, LeaseLost
from shared.silver_publication import (
    AzureControlStore, PublicationConflict, json_bytes, published_read_plan,
)
from shared.silver_runner import SilverRunner, bronze_identity

NOW = datetime(2026, 10, 18, 10, tzinfo=timezone.utc)
ID = '20261018_100000_' + 'a' * 32
BLOB = f'weather/weatherapi/2026/10/18/10/{ID}_weather_data.json'
RAW = b'{"synthetic":true}'
HASH = hashlib.sha256(RAW).hexdigest()


class Guard:
    def __init__(self):
        self.lost = False
        self.calls = 0
    def assert_held(self):
        self.calls += 1
        if self.lost:
            raise LeaseLost('lost')
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False


class Controls:
    def __init__(self):
        self.values = {}
    def read(self, key):
        return self.values.get(key)
    def create(self, key, value):
        if key in self.values and self.values[key] != value:
            raise PublicationConflict('conflict')
        self.values[key] = value


def rows(name, *, ingestion_id=ID, source_blob=BLOB, source_hash=HASH, temperature=12.0):
    row = {field.name: None for field in TABLE_SCHEMAS[name]}
    row.update(
        schema_version=SCHEMA_VERSION, transform_version=TRANSFORM_VERSION,
        source=SOURCE, ingestion_id=ingestion_id, collected_at_utc=NOW,
        ingestion_date=NOW.date(), city_id='fr_paris', city_query='Paris',
        latitude=48.85, longitude=2.35, source_timezone='Europe/Paris',
        source_blob=source_blob, source_sha256=source_hash, temperature_c=temperature,
    )
    if name == OBSERVATION_TABLE:
        row['observed_at_utc'] = NOW
    else:
        row.update(target_at_utc=NOW + timedelta(hours=2), target_local_date=NOW.date(),
                   lead_time_hours=2.0, target_is_past_at_collection=False)
    return pa.Table.from_pylist([row], schema=TABLE_SCHEMAS[name])


def transformed():
    return TransformResult(rows(OBSERVATION_TABLE), rows(FORECAST_TABLE), (),
                           {'valid_observations': 1, 'valid_forecasts': 1}, 'success',
                           ID, NOW, BLOB, HASH)


def make_runner(tmp_path, *, result=None, guard=None, controls=None):
    guard, controls = guard or Guard(), controls or Controls()
    blob = SimpleNamespace(download_blob=lambda: SimpleNamespace(readall=lambda: RAW))
    bronze = SimpleNamespace(get_blob_client=lambda name: blob)
    writer = DeltaSnapshotWriter(tmp_path)
    runner = SilverRunner(bronze, controls, writer, lambda: guard,
                          lambda raw, source_blob: result or transformed())
    return runner, guard, controls


def test_real_delta_tables_publish_and_exact_replay_noop(tmp_path):
    runner, guard, controls = make_runner(tmp_path)
    first = runner.process_ingestion(BLOB)
    assert first.outcome == 'published'
    assert first.publication['complete'] is True
    versions = {}
    for name in TABLE_SCHEMAS:
        target = DeltaTable(runner.writer.table_path(name))
        assert target.to_pyarrow_table().to_pylist() == rows(name).to_pylist()
        versions[name] = target.version()
    runner.transform = Mock(side_effect=AssertionError('published replay must not transform'))
    assert runner.process_ingestion(BLOB).outcome == 'already_processed'
    assert runner.preview_ingestion(BLOB).outcome == 'already_processed'
    assert {n: DeltaTable(runner.writer.table_path(n)).version() for n in TABLE_SCHEMAS} == versions


def test_second_ingestion_keeps_prior_forecast(tmp_path):
    runner, guard, controls = make_runner(tmp_path)
    runner.process_ingestion(BLOB)
    next_id = '20261018_110000_' + 'b' * 32
    next_blob = f'weather/weatherapi/2026/10/18/11/{next_id}_weather_data.json'
    result = transformed()
    runner.transform = lambda *_: replace(
        result, ingestion_id=next_id, source_blob=next_blob,
        observations=rows(OBSERVATION_TABLE, ingestion_id=next_id, source_blob=next_blob, temperature=14),
        forecasts=rows(FORECAST_TABLE, ingestion_id=next_id, source_blob=next_blob, temperature=15),
    )
    runner.process_ingestion(next_blob)
    target = DeltaTable(runner.writer.table_path(FORECAST_TABLE)).to_pyarrow_table()
    assert sorted(target.column('temperature_c').to_pylist()) == [12.0, 15.0]
    assert len(set(target.column('ingestion_id').to_pylist())) == 2


def test_failure_between_tables_is_unpublished_then_replay_completes(tmp_path):
    runner, guard, controls = make_runner(tmp_path)
    original = runner.writer.merge
    def interrupt(name, *args, **kwargs):
        if name == FORECAST_TABLE:
            raise OSError('injected crash')
        return original(name, *args, **kwargs)
    runner.writer.merge = interrupt
    with pytest.raises(OSError):
        runner.process_ingestion(BLOB)
    assert not controls.values
    assert DeltaTable(runner.writer.table_path(OBSERVATION_TABLE)).to_pyarrow_table().num_rows == 1
    runner.writer.merge = original
    assert runner.process_ingestion(BLOB).outcome == 'published'
    for name in TABLE_SCHEMAS:
        assert DeltaTable(runner.writer.table_path(name)).to_pyarrow_table().num_rows == 1


def test_failure_after_quarantine_does_not_publish_and_replays(tmp_path):
    runner, _, controls = make_runner(tmp_path)
    original = controls.create
    def interrupt(name, value):
        if name.startswith('publications/'):
            raise OSError('injected timeout')
        original(name, value)
    controls.create = interrupt
    with pytest.raises(OSError):
        runner.process_ingestion(BLOB)
    assert list(controls.values) == [f'quarantine/{ID}.jsonl']
    controls.create = original
    assert runner.process_ingestion(BLOB).outcome == 'published'


def test_lost_lease_after_first_table_prevents_publication(tmp_path):
    runner, guard, controls = make_runner(tmp_path)
    original = runner.writer.merge
    def lose_after(name, *args, **kwargs):
        result = original(name, *args, **kwargs)
        guard.lost = True
        return result
    runner.writer.merge = lose_after
    with pytest.raises(LeaseLost):
        runner.process_ingestion(BLOB)
    assert not controls.values
    guard.lost = False
    runner.writer.merge = original
    assert runner.process_ingestion(BLOB).outcome == 'published'


def test_content_hash_and_duplicate_key_conflicts_are_visible(tmp_path):
    writer = DeltaSnapshotWriter(tmp_path)
    guard = Guard()
    writer.merge(OBSERVATION_TABLE, rows(OBSERVATION_TABLE), guard, ingestion_id=ID)
    for changed in (
        rows(OBSERVATION_TABLE, temperature=13.0),
        rows(OBSERVATION_TABLE, source_hash='b' * 64),
        pa.concat_tables([rows(OBSERVATION_TABLE), rows(OBSERVATION_TABLE)]),
        empty_table(OBSERVATION_TABLE),
    ):
        with pytest.raises(SnapshotConflict):
            writer.merge(OBSERVATION_TABLE, changed, guard, ingestion_id=ID)
    assert DeltaTable(writer.table_path(OBSERVATION_TABLE)).to_pyarrow_table().num_rows == 1


def test_real_delta_stale_snapshot_insert_conflict(tmp_path):
    writer = DeltaSnapshotWriter(tmp_path)
    writer.merge(OBSERVATION_TABLE, empty_table(OBSERVATION_TABLE), Guard(), ingestion_id=ID)
    one = DeltaTable(writer.table_path(OBSERVATION_TABLE))
    two = DeltaTable(writer.table_path(OBSERVATION_TABLE))
    predicate = ' AND '.join(f'target.{k} = source.{k}' for k in TABLE_KEYS[OBSERVATION_TABLE])
    def merger(target):
        return target.merge(rows(OBSERVATION_TABLE), predicate=predicate,
                            source_alias='source', target_alias='target',
                            commit_properties=CommitProperties(max_commit_retries=0)
                            ).when_not_matched_insert_all()
    pending_one, pending_two = merger(one), merger(two)
    pending_one.execute()
    with pytest.raises(CommitFailedError):
        pending_two.execute()
    assert writer.merge(OBSERVATION_TABLE, rows(OBSERVATION_TABLE), Guard(), ingestion_id=ID).inserted == 0
    assert DeltaTable(writer.table_path(OBSERVATION_TABLE)).to_pyarrow_table().num_rows == 1


def test_gold_read_plan_filters_incomplete_ingestion(tmp_path):
    runner, _, controls = make_runner(tmp_path)
    publication = runner.process_ingestion(BLOB).publication
    plan = published_read_plan([publication])
    assert plan['ingestion_ids'] == [ID]
    for name, spec in plan['tables'].items():
        table = DeltaTable(spec['path'], version=spec['version']).to_pyarrow_table(
            filters=[('ingestion_id', 'in', plan['ingestion_ids'])],
        )
        assert table.num_rows == 1
    with pytest.raises(PublicationConflict):
        published_read_plan([{**publication, 'complete': False}])
    with pytest.raises(PublicationConflict):
        published_read_plan([publication, {**publication, 'transform_version': '2.0.0'}])


def test_invalid_json_result_can_publish_rejected_empty_tables(tmp_path):
    result = replace(transformed(), observations=empty_table(OBSERVATION_TABLE),
                     forecasts=empty_table(FORECAST_TABLE), status='rejected',
                     ingestion_id=None, collected_at_utc=None,
                     rejections=(Rejection('envelope', 'invalid_json'),))
    runner, _, controls = make_runner(tmp_path, result=result)
    publication = runner.process_ingestion(BLOB).publication
    assert publication['status'] == 'rejected'
    assert publication['quarantine']['count'] == 1
    rejection = json.loads(controls.values[f'quarantine/{ID}.jsonl'])
    assert rejection['source_sha256'] == HASH
    for name in TABLE_SCHEMAS:
        assert DeltaTable(runner.writer.table_path(name)).to_pyarrow_table().num_rows == 0


@pytest.mark.parametrize('change', [{'source_sha256':'0' * 64}, {'ingestion_id':'different'}, {'status':'unknown'}])
def test_runner_rejects_incompatible_transformer_identity(tmp_path, change):
    runner, _, controls = make_runner(tmp_path, result=replace(transformed(), **change))
    with pytest.raises(PublicationConflict):
        runner.process_ingestion(BLOB)
    assert not controls.values


def test_published_source_hash_conflict_is_never_skipped(tmp_path):
    runner, _, _ = make_runner(tmp_path)
    runner.process_ingestion(BLOB)
    runner.bronze = SimpleNamespace(get_blob_client=lambda _: SimpleNamespace(
        download_blob=lambda: SimpleNamespace(readall=lambda: b'changed source')))
    with pytest.raises(PublicationConflict):
        runner.process_ingestion(BLOB)


def test_busy_lease_returns_retryable_outcome(tmp_path):
    runner, _, controls = make_runner(tmp_path)
    @contextmanager
    def busy():
        raise LeaseBusy('busy')
        yield
    runner.lease_factory = busy
    assert runner.process_ingestion(BLOB).outcome == 'busy'
    assert not controls.values


@pytest.mark.parametrize('path', ['../id.json', BLOB + '?sig=secret', 'weather/other/2026/10/18/10/x_weather_data.json'])
def test_runner_refuses_noncanonical_paths(path):
    with pytest.raises(ValueError):
        bronze_identity(path)


def test_azure_controls_404_is_absent_but_other_errors_propagate():
    blob = Mock()
    container = Mock()
    container.get_blob_client.return_value = blob
    store = AzureControlStore(container, 'test/_control')
    blob.download_blob.side_effect = ResourceNotFoundError('absent')
    assert store.read('pub') is None
    blob.download_blob.side_effect = PermissionError('denied')
    with pytest.raises(PermissionError):
        store.read('pub')
    blob.upload_blob.side_effect = ResourceExistsError('exists')
    blob.download_blob.side_effect = None
    blob.download_blob.return_value.readall.return_value = b'same'
    store.create('pub', b'same')
    with pytest.raises(PublicationConflict):
        store.create('pub', b'different')


def test_blob_lease_renews_and_permanently_stops_after_failure():
    blob = Mock()
    lease = blob.acquire_lease.return_value
    with BlobWriterLease(blob) as guard:
        guard.assert_held()
        lease.renew.side_effect = OSError('token secret must not escape')
        with pytest.raises(LeaseLost, match='renewal failed'):
            guard.assert_held()
        lease.renew.side_effect = None
        with pytest.raises(LeaseLost):
            guard.assert_held()
    lease.release.assert_called_once()


def test_blob_lease_busy_is_distinct_from_permission_errors():
    from azure.core.exceptions import HttpResponseError
    blob = Mock()
    error = HttpResponseError('busy')
    error.error_code = 'LeaseAlreadyPresent'
    blob.acquire_lease.side_effect = error
    with pytest.raises(LeaseBusy):
        with BlobWriterLease(blob):
            pass
    error.error_code = 'AuthorizationPermissionMismatch'
    with pytest.raises(HttpResponseError):
        with BlobWriterLease(blob):
            pass


@pytest.mark.parametrize('value', [None, float('nan'), float('inf')])
def test_writer_rejects_invalid_required_numeric_before_any_commit(tmp_path, value):
    writer = DeltaSnapshotWriter(tmp_path)
    incoming = rows(OBSERVATION_TABLE).to_pylist()
    incoming[0]['temperature_c'] = value
    table = pa.Table.from_pylist(incoming, schema=TABLE_SCHEMAS[OBSERVATION_TABLE])
    with pytest.raises(SnapshotConflict):
        writer.merge(OBSERVATION_TABLE, table, Guard(), ingestion_id=ID)
    assert not (tmp_path / OBSERVATION_TABLE).exists()


def test_azure_adapter_uses_existing_connection_without_exposing_key(monkeypatch):
    from shared.silver_runner import make_azure_runner, silver_root
    from azure.storage.blob import BlobServiceClient
    service = Mock()
    monkeypatch.setenv('STORAGE_CONNECTION_STRING',
                       'AccountName=testsilveraccount;AccountKey=not-a-real-key;EndpointSuffix=core.windows.net')
    monkeypatch.setattr(BlobServiceClient, 'from_connection_string', Mock(return_value=service))
    runner = make_azure_runner(transform=lambda *_: transformed())
    assert runner.writer.root == f'abfss://datalake@testsilveraccount.dfs.core.windows.net/{silver_root()}'
    assert runner.writer.storage_options['azure_storage_account_name'] == 'testsilveraccount'
    assert runner.writer.storage_options['azure_storage_account_key'] == 'not-a-real-key'
    assert 'not-a-real-key' not in runner.writer.root
    service.get_container_client.assert_any_call('bronze')
    service.get_container_client.assert_any_call('datalake')


def test_azure_default_identity_refreshes_token_before_each_ingestion(monkeypatch):
    from shared.silver_runner import make_azure_runner
    import azure.identity
    import azure.storage.blob
    credential, service = Mock(), Mock()
    credential.get_token.return_value = SimpleNamespace(token='first-test-token')
    monkeypatch.delenv('STORAGE_CONNECTION_STRING', raising=False)
    monkeypatch.setenv('STORAGE_ACCOUNT_NAME', 'testsilveraccount')
    monkeypatch.setattr(azure.identity, 'DefaultAzureCredential', Mock(return_value=credential))
    monkeypatch.setattr(azure.storage.blob, 'BlobServiceClient', Mock(return_value=service))
    runner = make_azure_runner(transform=lambda *_: transformed())
    credential.get_token.return_value = SimpleNamespace(token='refreshed-test-token')
    runner.prepare()
    assert runner.writer.storage_options['azure_storage_token'] == 'refreshed-test-token'
    credential.get_token.assert_called_with('https://storage.azure.com/.default')


def test_gold_pinned_versions_filter_an_incomplete_ingestion_between_publications(tmp_path):
    runner, _, _ = make_runner(tmp_path)
    first = runner.process_ingestion(BLOB).publication
    pending_id = '20261018_110000_' + 'b' * 32
    pending_blob = f'weather/weatherapi/2026/10/18/11/{pending_id}_weather_data.json'
    runner.writer.merge(OBSERVATION_TABLE,
                        rows(OBSERVATION_TABLE, ingestion_id=pending_id, source_blob=pending_blob),
                        Guard(), ingestion_id=pending_id)
    third_id = '20261018_120000_' + 'c' * 32
    third_blob = f'weather/weatherapi/2026/10/18/12/{third_id}_weather_data.json'
    runner.transform = lambda *_: replace(
        transformed(), ingestion_id=third_id, source_blob=third_blob,
        observations=rows(OBSERVATION_TABLE, ingestion_id=third_id, source_blob=third_blob),
        forecasts=rows(FORECAST_TABLE, ingestion_id=third_id, source_blob=third_blob))
    third = runner.process_ingestion(third_blob).publication
    assert DeltaTable(runner.writer.table_path(OBSERVATION_TABLE)).to_pyarrow_table().num_rows == 3
    plan = published_read_plan([first, third])
    assert plan['ingestion_ids'] == [ID, third_id]
    for name, spec in plan['tables'].items():
        target = DeltaTable(spec['path'], version=spec['version']).to_pyarrow_table(
            filters=[('ingestion_id', 'in', plan['ingestion_ids'])])
        assert target.num_rows == 2
        assert pending_id not in target.column('ingestion_id').to_pylist()


def test_two_runner_writers_are_serialized_and_busy_can_replay(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock
    runner, _, _ = make_runner(tmp_path)
    mutex, entered, release = Lock(), Event(), Event()
    class SharedLease(Guard):
        def __enter__(self):
            if not mutex.acquire(blocking=False):
                raise LeaseBusy('busy')
            entered.set()
            if not release.wait(timeout=5):
                mutex.release()
                raise TimeoutError('test coordination')
            return self
        def __exit__(self, *args):
            mutex.release()
            return False
    runner.lease_factory = SharedLease
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(runner.process_ingestion, BLOB)
        assert entered.wait(timeout=5)
        try:
            assert runner.process_ingestion(BLOB).outcome == 'busy'
        finally:
            release.set()
        assert first.result(timeout=10).outcome == 'published'
    assert runner.process_ingestion(BLOB).outcome == 'already_processed'
    for name in TABLE_SCHEMAS:
        assert DeltaTable(runner.writer.table_path(name)).to_pyarrow_table().num_rows == 1


def test_background_lease_loss_is_observed_by_main_thread():
    from threading import Event
    attempted = Event()
    blob = Mock()
    def fail(**kwargs):
        attempted.set()
        raise OSError('simulated background failure')
    blob.acquire_lease.return_value.renew.side_effect = fail
    with BlobWriterLease(blob, duration=15, renew_interval=0.01) as guard:
        assert attempted.wait(timeout=2)
        with pytest.raises(LeaseLost):
            guard.assert_held()


@pytest.mark.parametrize('root', [
    '/silver/test', 'silver/../prod', 'silver/./test', 'silver//test',
    'silver/test/', 'silver/test?sig=secret', 'silver/test#x',
    'silver/test%2fsecret', 'silver/test\\other', 'bronze/test', '', None,
])
def test_azure_adapter_rejects_unsafe_injected_root_before_credentials(monkeypatch, root):
    from shared.silver_runner import make_azure_runner
    import azure.identity
    factory = Mock(side_effect=AssertionError('Credentials must not be inspected'))
    monkeypatch.setattr(azure.identity, 'DefaultAzureCredential', factory)
    monkeypatch.delenv('STORAGE_CONNECTION_STRING', raising=False)
    if root is None:
        root = 'silver/' + 'x' * 513
    with pytest.raises(ValueError, match='relative Silver root'):
        make_azure_runner(root=root, transform=lambda *_: transformed())
    factory.assert_not_called()


def test_azure_adapter_routes_tables_and_control_to_injected_validation_root(monkeypatch):
    from shared.silver_runner import make_azure_runner
    from azure.storage.blob import BlobServiceClient
    service = Mock()
    monkeypatch.setenv('STORAGE_CONNECTION_STRING', 'AccountName=testsilveraccount;AccountKey=test-key')
    monkeypatch.setattr(BlobServiceClient, 'from_connection_string', Mock(return_value=service))
    root = 'silver/_validation/s1-s3-' + 'a' * 32
    runner = make_azure_runner(root=root, transform=lambda *_: transformed())
    assert runner.writer.root == 'abfss://datalake@testsilveraccount.dfs.core.windows.net/' + root
    assert runner.controls.prefix == root + '/_control'


@pytest.mark.parametrize('account,container,root', [
    ('UPPERCASE', 'datalake', 'silver/test'),
    ('a' * 25, 'datalake', 'silver/test'),
    ('testaccount', 'container/other', 'silver/test'),
    ('testaccount', 'bad--name', 'silver/test'),
    ('testaccount', 'bad_name', 'silver/test'),
    ('testaccount', 'a', 'silver/test'),
    ('testaccount', 'datalake', 'silver/../prod'),
])
def test_canonical_adls_root_refuses_url_components_and_invalid_azure_names(account, container, root):
    from shared.silver_runner import adls_table_root
    with pytest.raises(ValueError):
        adls_table_root(account, container, root)


def test_canonical_adls_root_is_shared_by_writer_and_publication_paths(monkeypatch):
    from shared.silver_runner import adls_table_root, make_azure_runner
    from azure.storage.blob import BlobServiceClient
    monkeypatch.setenv('STORAGE_CONNECTION_STRING', 'AccountName=testsilveraccount;AccountKey=test-key')
    monkeypatch.setattr(BlobServiceClient, 'from_connection_string', Mock(return_value=Mock()))
    root = 'silver/_validation/s1-s3-' + 'c' * 32
    runner = make_azure_runner(root=root, transform=lambda *_: transformed())
    expected = adls_table_root('testsilveraccount', 'datalake', root)
    assert runner.writer.root == expected
    assert all(runner.writer.table_path(name) == expected + '/' + name for name in TABLE_SCHEMAS)
