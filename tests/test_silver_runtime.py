"""Durable scheduler acceptance: crashes, late files, bounded retries and health."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from shared.silver_health import check_silver_health
from shared.silver_lease import BlobWriterLease, LeaseBusy
from shared.silver_runtime import (
    AzureRuntimeStore, STATE_MAX_BYTES, StatePersistenceError,
    earliest_bronze, new_state, run_tick,
)
from shared.silver_settings import SilverSettings
from shared.silver_runner import ProcessResult
from test_silver_discovery import PagedContainer, START, path

NOW = START + timedelta(hours=3)
SETTINGS = SilverSettings(root='silver/test/runtime', discovery_limit=10, discovery_pages=48)


class MemoryStore:
    def __init__(self):
        self.state = None
        self.saves = []
        self.fail_after = None
    def read(self):
        return deepcopy(self.state)
    @contextmanager
    def session(self):
        yield self
    def save(self, state):
        if self.fail_after is not None and len(self.saves) >= self.fail_after:
            raise OSError('secret-SAS-must-not-appear')
        self.state = deepcopy(state)
        self.saves.append(deepcopy(state))


class RuntimeContainer(PagedContainer):
    def list_blobs(self, *, name_starts_with, results_per_page):
        paged = super().list_blobs(name_starts_with=name_starts_with, results_per_page=results_per_page)
        class Result:
            def __iter__(inner):
                return iter(SimpleNamespace(name=n) for n in sorted(self.names) if n.startswith(name_starts_with))
            def by_page(inner, continuation_token=None):
                return paged.by_page(continuation_token=continuation_token)
        return Result()


def publication(name, *, status='success', received_days=3):
    stamp = datetime.strptime(name.rsplit('/', 1)[-1][:15], '%Y%m%d_%H%M%S').replace(tzinfo=timezone.utc)
    return {'collected_at_utc': stamp.isoformat(), 'status': status,
            'tables': {'observations': {'row_count': 3}, 'forecasts': {'row_count': 216}},
            'summary': {'horizons': {city: {'received_days': received_days}
                                   for city in ('fr_paris', 'gb_london', 'be_brussels')}}}


def runner(names, process=None):
    return SimpleNamespace(
        bronze=RuntimeContainer(names),
        process_ingestion=process or (lambda name: ProcessResult('published', name, publication(name))),
    )


def test_candidates_and_cursor_are_durable_before_processing():
    store = MemoryStore()
    names = [path(1, i) for i in range(3)]
    def process(name):
        assert name in store.read()['pending']
        return ProcessResult('published', name, publication(name))
    report = run_tick(runner(names, process), store, SETTINGS, now=NOW)
    assert report['published'] == 3 and report['pending'] == 0
    assert store.state['history']['passes'] == 1
    assert store.state['latest_usable_at'] == (START + timedelta(hours=1, seconds=2)).isoformat()


def test_crash_after_discovery_restarts_all_pending_jobs():
    class Killed(BaseException):
        pass
    store = MemoryStore()
    names = [path(1, i) for i in range(2)]
    def die(name):
        raise Killed()
    with pytest.raises(Killed):
        run_tick(runner(names, die), store, SETTINGS, now=NOW)
    assert set(store.state['pending']) == set(names)
    resumed = run_tick(runner(names), store, SETTINGS, now=NOW + timedelta(minutes=5))
    assert resumed['published'] == 2 and not store.state['pending']


def test_crash_after_publication_replays_and_clears_pending_without_duplicate_commit():
    store = MemoryStore()
    name = path(1, 0)
    commits = set()
    def process(value):
        if value in commits:
            return ProcessResult('already_processed', value, publication(value))
        commits.add(value)
        store.fail_after = len(store.saves)
        return ProcessResult('published', value, publication(value))
    with pytest.raises(StatePersistenceError):
        run_tick(runner([name], process), store, SETTINGS, now=NOW)
    assert name in store.read()['pending'] and commits == {name}
    store.fail_after = None
    resumed = run_tick(runner([name], process), store, SETTINGS, now=NOW + timedelta(minutes=5))
    assert resumed['already_processed'] == 1 and not store.state['pending']
    assert commits == {name}


def test_checkpoint_failure_stops_before_any_delta_mutation():
    store = MemoryStore()
    store.fail_after = 1  # start checkpoint succeeds; discovered candidates cannot be saved
    process = Mock()
    with pytest.raises(StatePersistenceError):
        run_tick(runner([path(1, 0)], process), store, SETTINGS, now=NOW)
    process.assert_not_called()
    assert not store.state['pending']


def test_technical_error_is_retained_with_backoff_and_new_work_continues():
    store = MemoryStore()
    broken, good = path(1, 0), path(1, 1)
    def process(name):
        if name == broken:
            raise OSError('https://storage/?sig=secret')
        return ProcessResult('published', name, publication(name))
    report = run_tick(runner([broken, good], process), store, SETTINGS, now=NOW)
    assert report['outcome'] == 'partial' and report['published'] == 1
    assert list(store.state['pending']) == [broken]
    assert store.state['pending'][broken]['attempts'] == 1
    assert store.state['pending'][broken]['last_error_type'] == 'OSError'
    assert 'sig=' not in json.dumps(store.state) + json.dumps(report)
    process = Mock(side_effect=AssertionError('Backoff not elapsed'))
    run_tick(runner([], process), store, SETTINGS, now=NOW + timedelta(seconds=10))
    process.assert_not_called()
    run_tick(runner([], lambda n: ProcessResult('already_processed', n, publication(n))),
             store, SETTINGS, now=NOW + timedelta(minutes=1))
    assert not store.state['pending']


def test_busy_writer_is_retryable_without_being_a_technical_failure():
    store = MemoryStore()
    report = run_tick(runner([path(1, 0)], lambda n: ProcessResult('busy', n)), store, SETTINGS, now=NOW)
    assert report['busy'] == 1 and report['technical_errors'] == 0
    assert len(store.state['pending']) == 1


def test_discovery_failure_does_not_block_durable_pending():
    store = MemoryStore()
    state = new_state(SETTINGS)
    name = path(1, 0)
    state['pending'][name] = {'lane': 'history', 'attempts': 1, 'first_seen_at': START.isoformat(),
                            'next_attempt_at': START.isoformat(), 'last_error_type': 'OSError'}
    store.state = state
    instance = runner([])
    instance.bronze.list_blobs = Mock(side_effect=OSError('secret'))
    report = run_tick(instance, store, SETTINGS, now=NOW)
    assert report['published'] == 1 and report['discovery_errors'] == 2
    assert not store.state['pending'] and store.state['history']['horizon'] is None


def test_full_reconciliation_finds_a_late_file_older_than_recent_window():
    store = MemoryStore()
    instance = runner([path(0, 0)])
    settings = replace(SETTINGS, lookback_hours=1)
    run_tick(instance, store, settings, now=NOW)
    instance.bronze.names.append(path(0, 1))
    report = run_tick(instance, store, settings, now=NOW + timedelta(minutes=5))
    assert report['published'] == 2  # fake runner also republishes the original; real runner no-ops
    assert not store.state['pending']


def test_reconciliation_expands_origin_for_an_even_earlier_late_archive():
    store = MemoryStore()
    instance = runner([path(1, 0)])
    run_tick(instance, store, SETTINGS, now=NOW)
    instance.bronze.names.append(path(0, 0))
    run_tick(instance, store, SETTINGS, now=NOW + timedelta(minutes=5))
    assert store.state['history']['origin'] == START.isoformat()


def test_full_history_scan_crosses_seven_day_windows_and_empty_weeks():
    store = MemoryStore()
    old, later = path(0, 0), 'weather/weatherapi/2026/11/02/00/20261102_000000_weather_data.json'
    committed = set()
    def process(name):
        committed.add(name)
        return ProcessResult('published', name, publication(name))
    instance = runner([old, later], process)
    settings = replace(SETTINGS, discovery_pages=168, lookback_hours=1)
    now = datetime(2026, 11, 3, tzinfo=timezone.utc)
    for tick in range(3):
        run_tick(instance, store, settings, now=now + timedelta(minutes=5 * tick))
    assert committed == {old, later}
    assert store.state['history']['passes'] == 1


def test_pagination_and_batch_limit_do_not_omit_candidates():
    store = MemoryStore()
    names = [path(1, i) for i in range(8)]
    settings = replace(SETTINGS, batch_size=2, discovery_limit=3)
    committed = set()
    def process(name):
        already = name in committed
        committed.add(name)
        return ProcessResult('already_processed' if already else 'published', name, publication(name))
    instance = runner(names, process)
    for offset in range(12):
        result = run_tick(instance, store, settings, now=NOW + timedelta(minutes=5 * offset))
        assert result['published'] + result['already_processed'] <= 2
        assert len(store.state['pending']) <= settings.pending_limit
    assert committed == set(names)


def test_history_pending_backpressure_preserves_recent_lane_capacity():
    store = MemoryStore()
    settings = replace(SETTINGS, pending_limit=4)
    state = new_state(settings)
    for i in range(2):
        state['pending'][path(0, i)] = {'lane': 'history', 'attempts': 2, 'first_seen_at': START.isoformat(),
                                      'next_attempt_at': (NOW + timedelta(days=1)).isoformat(), 'last_error_type': 'OSError'}
    store.state = state
    result = run_tick(runner([path(2, 0)]), store, settings, now=NOW)
    assert result['published'] == 1 and result['backpressure'] is True
    assert len(store.state['pending']) == 2


def test_coordinator_contention_performs_no_discovery():
    class BusyStore(MemoryStore):
        @contextmanager
        def session(self):
            raise LeaseBusy('busy')
            yield
    instance = runner([])
    instance.bronze.list_blobs = Mock()
    assert run_tick(instance, BusyStore(), SETTINGS, now=NOW)['outcome'] == 'busy'
    instance.bronze.list_blobs.assert_not_called()


def test_disabled_worker_needs_no_clients_or_state():
    assert run_tick(None, None, replace(SETTINGS, enabled=False)) == {'outcome': 'disabled'}


def test_changed_root_start_or_source_state_is_never_silently_reset():
    store = MemoryStore()
    store.state = new_state(SETTINGS)
    with pytest.raises(ValueError, match='another source/root/version/start'):
        run_tick(runner([]), store, replace(SETTINGS, start_at=START.isoformat()), now=NOW)


def test_health_uses_source_time_and_detects_stopped_scheduler_and_backlog():
    store = MemoryStore()
    run_tick(runner([path(2, 0)]), store, SETTINGS, now=NOW)
    assert check_silver_health(store, SETTINGS, now=NOW)['freshness'] == 'fresh'
    late = check_silver_health(store, SETTINGS, now=NOW + timedelta(minutes=25))
    assert late['scheduler'] == 'stale' and late['freshness'] == 'stale'
    store.state['pending'][path(0, 0)] = {'lane': 'history', 'attempts': 5, 'first_seen_at': START.isoformat(),
                                       'next_attempt_at': NOW.isoformat()}
    assert check_silver_health(store, SETTINGS, now=NOW)['backlog_stale'] is True


def test_recent_replay_of_old_data_never_makes_silver_fresh():
    store = MemoryStore()
    run_tick(runner([path(0, 0)]), store, SETTINGS, now=NOW)
    assert check_silver_health(store, SETTINGS, now=NOW)['freshness'] == 'stale'


@pytest.mark.parametrize('status,days', [('partial', 3), ('success', 1), ('empty', 3)])
def test_health_reports_bad_latest_quality_or_short_horizon(status, days):
    store = MemoryStore()
    run_tick(runner([path(2, 0)], lambda n: ProcessResult('published', n, publication(n, status=status, received_days=days))),
             store, SETTINGS, now=NOW)
    assert check_silver_health(store, SETTINGS, now=NOW)['quality_degraded'] is True


def test_older_partial_publication_does_not_replace_latest_good_quality():
    store = MemoryStore()
    def process(name):
        return ProcessResult('published', name, publication(name, status='partial' if name == path(0, 0) else 'success'))
    run_tick(runner([path(0, 0), path(2, 0)], process), store, SETTINGS, now=NOW)
    assert check_silver_health(store, SETTINGS, now=NOW)['quality_degraded'] is False


def test_new_malformed_archive_is_visible_in_quality_without_freshening_data():
    store = MemoryStore()
    valid, malformed = path(1, 0), path(2, 0)
    def process(name):
        value = publication(name)
        if name == malformed:
            value.update(collected_at_utc=None, source_blob=name, status='rejected', summary={})
            value['tables'] = {'observations': {'row_count': 0}, 'forecasts': {'row_count': 0}}
        return ProcessResult('published', name, value)
    run_tick(runner([valid, malformed], process), store, SETTINGS, now=NOW)
    health = check_silver_health(store, SETTINGS, now=NOW)
    assert health['latest_status'] == 'rejected' and health['quality_degraded'] is True
    assert health['latest_source_at'] == (START + timedelta(hours=2)).isoformat()
    assert health['latest_usable_at'] == (START + timedelta(hours=1)).isoformat()


def test_future_health_timestamp_is_an_error_not_permanent_freshness():
    store = MemoryStore()
    run_tick(runner([path(2, 0)]), store, SETTINGS, now=NOW)
    store.state['latest_usable_at'] = (NOW + timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match='future'):
        check_silver_health(store, SETTINGS, now=NOW)


def test_health_before_first_run_and_disabled_are_explicit():
    assert check_silver_health(MemoryStore(), SETTINGS, now=NOW)['reason'] == 'not_started'
    assert check_silver_health(None, replace(SETTINGS, enabled=False), now=NOW)['freshness'] == 'disabled'


@pytest.mark.parametrize('update', [
    {'SILVER_ENABLED': 'yes'}, {'SILVER_BATCH_SIZE': '0'}, {'SILVER_PENDING_LIMIT': '2'},
    {'SILVER_BUDGET_SECONDS': '999'}, {'SILVER_ROOT': 'https://secret'}, {'SILVER_START_AT': 'invalid'},
])
def test_invalid_settings_fail_before_clients(update):
    with pytest.raises(ValueError):
        SilverSettings.from_env(update)


def test_earliest_discovery_ignores_noncanonical_objects_and_is_bounded():
    container = RuntimeContainer(['weather/weatherapi/0000/invalid', path(1, 0)])
    assert earliest_bronze(container)['earliest'] == START + timedelta(hours=1)
    container.names = [f'weather/weatherapi/0000/{i:04d}' for i in range(1001)] + [path(1, 0)]
    first = earliest_bronze(container)
    assert first['earliest'] is None and first['continuation'] is not None and first['inspected'] == 1000
    assert earliest_bronze(container, cursor=first['continuation'])['earliest'] == START + timedelta(hours=1)


def test_bootstrap_cursor_survives_more_than_one_thousand_legacy_objects():
    store = MemoryStore()
    names = [f'weather/weatherapi/0000/{i:04d}' for i in range(1001)] + [path(1, 0)]
    instance = runner(names)
    settings = replace(SETTINGS, lookback_hours=1)
    first = run_tick(instance, store, settings, now=NOW)
    assert first['technical_errors'] == first['discovery_errors'] == 0
    assert store.state['history']['bootstrap_cursor'] is not None
    second = run_tick(instance, store, settings, now=NOW + timedelta(minutes=5))
    assert second['published'] == 1 and store.state['history']['passes'] == 1


def test_lease_fenced_checkpoint_upload_carries_actual_lease(monkeypatch):
    blob = Mock()
    lease = BlobWriterLease(blob)
    lease._lease = Mock()
    monkeypatch.setattr(lease, 'assert_held', Mock())
    lease.replace_blob(b'checkpoint')
    blob.upload_blob.assert_called_once_with(b'checkpoint', overwrite=True, lease=lease._lease)


def test_oversized_checkpoint_is_rejected():
    blob = Mock()
    blob.download_blob.return_value.readall.return_value = b'x' * (STATE_MAX_BYTES + 1)
    with pytest.raises(ValueError, match='Oversized'):
        AzureRuntimeStore(blob).read()


def test_worker_timer_redacts_technical_exception(monkeypatch, caplog):
    import weather_silver_processor
    monkeypatch.setattr(weather_silver_processor, 'make_runtime', Mock(side_effect=OSError('secret-SAS')))
    with pytest.raises(RuntimeError, match='Silver batch failed'):
        weather_silver_processor.main(None)
    assert 'OSError' in caplog.text and 'secret-SAS' not in caplog.text


def test_health_timer_redacts_technical_exception(monkeypatch, caplog):
    import silver_health_monitor
    monkeypatch.setattr(silver_health_monitor, 'make_runtime', Mock(side_effect=OSError('secret-SAS')))
    with pytest.raises(RuntimeError, match='Silver health check failed'):
        silver_health_monitor.main(None)
    assert 'OSError' in caplog.text and 'secret-SAS' not in caplog.text


def test_real_delta_partial_commit_resumes_and_two_ingestions_replay_without_duplicates(tmp_path):
    from deltalake import DeltaTable
    from shared.claim_check import bronze_blob_name
    from shared.silver_contract import TABLE_SCHEMAS, FORECAST_TABLE
    from shared.silver_delta import DeltaSnapshotWriter
    from shared.silver_runner import SilverRunner
    from shared.silver_transform import transform_bronze
    from test_silver_pipeline import source_bytes, ControlStore, TestLease

    raw, name = source_bytes()
    sources = {name: raw}
    container = RuntimeContainer(list(sources))
    container.get_blob_client = lambda key: SimpleNamespace(download_blob=lambda: SimpleNamespace(readall=lambda: sources[key]))
    real_writer = DeltaSnapshotWriter(tmp_path)
    class FailBetweenTables:
        failed = False
        table_path = real_writer.table_path
        def merge(self, table_name, rows, lease, **kwargs):
            if table_name == FORECAST_TABLE and not self.failed:
                self.failed = True
                raise OSError('simulated interruption between commits')
            return real_writer.merge(table_name, rows, lease, **kwargs)
    instance = SilverRunner(container, ControlStore(), FailBetweenTables(), TestLease, transform_bronze)
    store = MemoryStore()
    now = START + timedelta(hours=12)
    first = run_tick(instance, store, SETTINGS, now=now)
    assert first['technical_errors'] == 1 and name in store.state['pending']
    assert not any(key.startswith('publications/') for key in instance.controls.values)
    resumed = run_tick(instance, store, SETTINGS, now=now + timedelta(minutes=1))
    assert resumed['published'] == 1 and not store.state['pending']

    second = json.loads(raw)
    second.update(ingestion_id='20261018_110000', ingestion_timestamp='2026-10-18T11:00:00+00:00')
    second_name = bronze_blob_name(second['ingestion_id'], second['ingestion_timestamp'])
    sources[second_name] = json.dumps(second).encode()
    container.names.append(second_name)
    run_tick(instance, store, SETTINGS, now=now + timedelta(minutes=5))
    versions = {table: DeltaTable(real_writer.table_path(table)).version() for table in TABLE_SCHEMAS}
    assert sorted(DeltaTable(real_writer.table_path(table)).to_pyarrow_table().num_rows
                  for table in TABLE_SCHEMAS) == [2, 4]
    run_tick(instance, store, SETTINGS, now=now + timedelta(minutes=10))
    assert {table: DeltaTable(real_writer.table_path(table)).version() for table in TABLE_SCHEMAS} == versions
