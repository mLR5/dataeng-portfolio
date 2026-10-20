"""Daily acceptance: UTC boundaries, >20 ingestions, late data and queue delivery."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from shared.silver_health import check_silver_health
from shared.silver_queue import read_wakeup, wakeup_body
from shared.silver_runtime import batch_cutoff, run_tick
from shared.silver_runner import ProcessResult
from shared.silver_settings import SilverSettings
from test_silver_runtime import MemoryStore, RuntimeContainer, publication, runner, SETTINGS, START, NOW, path


def source(day, hour=0):
    stamp = START + timedelta(days=day, hours=hour)
    return f'weather/weatherapi/{stamp:%Y/%m/%d/%H}/{stamp:%Y%m%d_%H%M%S}_weather_data.json'


def drain(instance, store, settings=SETTINGS, now=NOW, cutoff=None):
    for step in range(150):
        report = run_tick(instance, store, settings, now=now, cutoff=cutoff)
        if report['next_delay_seconds'] is None:
            return report
        now += timedelta(seconds=max(1, report['next_delay_seconds']))
    pytest.fail('Batch did not drain')


def test_cutoff_before_and_after_daily_start_uses_utc():
    settings = SilverSettings()
    assert batch_cutoff(START + timedelta(days=1, hours=1, minutes=59), settings) == START
    assert batch_cutoff(START + timedelta(days=1, hours=2), settings) == START + timedelta(days=1)
    # The same instant in another offset must produce the same UTC collection day.
    offset = timezone(timedelta(hours=5))
    assert batch_cutoff((START + timedelta(hours=2)).astimezone(offset), settings) == START


def test_day_batch_handles_all_24_collections_in_multiple_lots_and_excludes_today():
    names = [path(hour, 0) for hour in range(24)] + [source(1, 0), source(1, 1)]
    processed = set()
    def process(name):
        repeated = name in processed
        processed.add(name)
        return ProcessResult('already_processed' if repeated else 'published', name, publication(name))
    instance, store = runner(names, process), MemoryStore()
    settings = replace(SETTINGS, discovery_limit=40)
    first = run_tick(instance, store, settings, now=NOW)
    assert first['published'] == 20 and first['next_delay_seconds'] == 0
    assert store.state['daily']['completed_end'] is None
    final = drain(instance, store, settings)
    assert processed == set(names[:24])
    assert store.state['daily']['completed_end'] == (START + timedelta(days=1)).isoformat()
    assert final['next_delay_seconds'] is None and final['history_passes'] == 1


def test_idle_batch_does_not_restart_historical_scan_or_reclean_on_duplicate_wakeup():
    store = MemoryStore()
    process = Mock(side_effect=lambda name: ProcessResult('published', name, publication(name)))
    instance = runner([path(23, 0)], process)
    drain(instance, store)
    process.reset_mock()
    instance.bronze.list_blobs = Mock(side_effect=AssertionError('Idle batch must not list Bronze'))
    report = run_tick(instance, store, SETTINGS, now=NOW + timedelta(hours=1))
    assert report['next_delay_seconds'] is None
    process.assert_not_called()


def test_missed_days_and_late_yesterday_file_are_reconciled():
    store = MemoryStore()
    processed = set()
    def process(name):
        processed.add(name)
        return ProcessResult('published', name, publication(name))
    instance = runner([source(0, 23)], process)
    settings = replace(SETTINGS, replay_days=2)
    drain(instance, store, settings)
    instance.bronze.names.extend([source(0, 22), source(1, 23), source(2, 23), source(3, 23), source(4, 0)])
    drain(instance, store, settings, now=START + timedelta(days=4, hours=5))
    assert {source(0, 22), source(1, 23), source(2, 23), source(3, 23)} <= processed
    assert source(4, 0) not in processed


def test_older_late_file_waits_for_weekly_scan_without_daily_full_history_replays():
    store = MemoryStore()
    instance = runner([source(0, 23)])
    drain(instance, store)
    instance.bronze.names.append(source(0, 22))
    processed = set()
    instance.process_ingestion = lambda name: (processed.add(name) or ProcessResult('published', name, publication(name)))
    drain(instance, store, now=START + timedelta(days=3, hours=5))
    assert source(0, 22) not in processed
    drain(instance, store, now=START + timedelta(days=9, hours=5))
    assert source(0, 22) in processed and store.state['history']['passes'] == 2


def test_retry_backoff_schedules_a_delayed_continuation_instead_of_spinning():
    store = MemoryStore()
    instance = runner([path(23, 0)], lambda name: ProcessResult('busy', name))
    first = run_tick(instance, store, SETTINGS, now=NOW)
    assert 30 <= first['next_delay_seconds'] <= 31
    second = run_tick(instance, store, SETTINGS, now=NOW + timedelta(seconds=5))
    assert second['busy'] == 0 and 25 <= second['next_delay_seconds'] <= 26
    assert store.state['history']['passes'] == 0 and store.state['daily']['completed_end'] is None


def test_no_archives_finishes_discovery_without_a_continuation_loop():
    store = MemoryStore()
    final = drain(runner([]), store)
    assert final['next_delay_seconds'] is None and final['history_passes'] == 1
    assert check_silver_health(store, SETTINGS, now=NOW)['freshness'] == 'stale'


def test_queued_cutoff_does_not_expand_at_midnight_without_the_next_daily_request():
    store = MemoryStore()
    settings = replace(SETTINGS, batch_size=1)
    instance = runner([path(22, 0), path(23, 0), source(1, 23)])
    cutoff = START + timedelta(days=1)
    run_tick(instance, store, settings, now=NOW, cutoff=cutoff)
    processed = set()
    instance.process_ingestion = lambda name: (processed.add(name) or ProcessResult('published', name, publication(name)))
    drain(instance, store, settings, now=NOW + timedelta(days=1), cutoff=cutoff)
    assert source(1, 23) not in processed


@pytest.mark.parametrize('body', [b'{}', b'[]', b'"secret"', b'x'*1025, b'{"schema_version":1,"cutoff_utc":"2026-10-19T00:00:01Z"}'])
def test_invalid_wakeups_fail_before_storage(body):
    with pytest.raises((ValueError, KeyError)):
        read_wakeup(body, SETTINGS, now=NOW)


def test_wakeup_rejects_unfinished_day_and_round_trips_fixed_cutoff():
    assert read_wakeup(wakeup_body(START + timedelta(days=1)).encode(), SETTINGS, now=NOW) == START + timedelta(days=1)
    with pytest.raises(ValueError, match='unfinished'):
        read_wakeup(wakeup_body(START + timedelta(days=2)).encode(), SETTINGS, now=NOW)


def test_queue_worker_enqueues_successor_before_acknowledging_even_partial_work(monkeypatch):
    import silver_batch_worker
    monkeypatch.setattr(silver_batch_worker, 'read_wakeup', lambda *_: START)
    monkeypatch.setattr(silver_batch_worker, 'make_runtime', lambda *_: (None, None, None))
    report = {'outcome': 'partial', 'cutoff_utc': START.isoformat(), 'next_delay_seconds': 30}
    monkeypatch.setattr(silver_batch_worker, 'run_tick', lambda *_ , **kwargs: report)
    enqueue = Mock()
    monkeypatch.setattr(silver_batch_worker, 'enqueue_wakeup', enqueue)
    silver_batch_worker.main(SimpleNamespace(get_body=lambda: b'{}'))
    assert enqueue.call_args.kwargs == {'delay': 30}
    enqueue.side_effect = OSError('secret-SAS')
    with pytest.raises(RuntimeError, match='durable checkpoint retained'):
        silver_batch_worker.main(SimpleNamespace(get_body=lambda: b'{}'))


def test_legacy_continuous_checkpoint_is_rejected_instead_of_silently_reset():
    from shared.silver_runtime import new_state
    store = MemoryStore()
    store.state = new_state(SETTINGS)
    store.state['schema_version'] = 1
    with pytest.raises(ValueError, match='another source/root/version/start'):
        run_tick(runner([]), store, SETTINGS, now=NOW)


def test_new_daily_request_does_not_hide_older_unfinished_daily_work():
    store = MemoryStore()
    bad = source(0, 23)
    instance = runner([bad], lambda name: ProcessResult('busy', name))
    run_tick(instance, store, SETTINGS, now=NOW)
    instance.bronze.names.append(source(1, 23))
    instance.process_ingestion = lambda name: ProcessResult('busy' if name == bad else 'published', name, publication(name))
    run_tick(instance, store, SETTINGS, now=NOW + timedelta(days=1))
    assert bad in store.state['pending'] and store.state['daily']['completed_end'] is None
