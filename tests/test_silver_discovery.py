"""Discovery/backfill pagination, durable failures and preview safety."""
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared.silver_discovery import discover_bronze

START = datetime(2026, 10, 18, tzinfo=timezone.utc)


def path(hour, counter):
    return f'weather/weatherapi/2026/10/18/{hour:02d}/20261018_{hour:02d}00{counter:02d}_weather_data.json'


class PagedContainer:
    def __init__(self, names):
        self.names = names
        self.calls = []

    def list_blobs(self, *, name_starts_with, results_per_page):
        self.calls.append((name_starts_with, results_per_page))
        values = sorted(n for n in self.names if n.startswith(name_starts_with))
        class Pages:
            continuation_token = None
            def __init__(self, token):
                self.offset = int(token or 0)
            def __iter__(self):
                return self
            def __next__(self):
                if self.offset >= len(values):
                    raise StopIteration
                part = values[self.offset:self.offset+results_per_page]
                self.offset += len(part)
                self.continuation_token = str(self.offset) if self.offset < len(values) else None
                return iter(SimpleNamespace(name=n) for n in part)
        return SimpleNamespace(by_page=lambda continuation_token=None: Pages(continuation_token))


def load_cli():
    spec = importlib.util.spec_from_file_location('silver_backfill_cli', Path(__file__).parents[1] / 'scripts/silver_backfill.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pagination_resumes_exactly_without_omission():
    names = [path(0, i) for i in range(5)] + [path(1, i) for i in range(3)]
    container = PagedContainer(names)
    cursor, found = None, []
    while True:
        page = discover_bronze(container, START, START+timedelta(hours=2), limit=3, cursor=cursor)
        assert page.inspected <= 3
        found.extend(page.blobs)
        cursor = page.continuation
        if cursor is None:
            break
    assert found == names
    assert len(set(found)) == len(names)


def test_empty_partition_work_is_bounded_and_resumable():
    container = PagedContainer([])
    page = discover_bronze(container, START, START+timedelta(days=7), limit=10, max_pages=3)
    assert len(container.calls) == 3
    assert page.continuation['hour'] == (START+timedelta(hours=3)).isoformat()
    assert page.inspected == 0


def test_invalid_files_count_toward_bound_but_are_not_candidates():
    container = PagedContainer([
        path(0, 0), 'weather/weatherapi/2026/10/18/00/not-bronze.txt',
        'weather/weatherapi/2026/10/18/00/20269999_000000_weather_data.json',
    ])
    page = discover_bronze(container, START, START+timedelta(hours=1), limit=3)
    assert page.inspected == 3
    assert page.blobs == (path(0, 0),)


def test_exact_time_bounds_and_cursor_window_validation():
    container = PagedContainer([path(0, 0), path(0, 1), path(0, 2)])
    page = discover_bronze(container, START+timedelta(seconds=1), START+timedelta(seconds=2), limit=1)
    assert page.blobs == ()
    with pytest.raises(ValueError, match='another window'):
        discover_bronze(container, START, START+timedelta(seconds=2), cursor=page.continuation)
    next_page = discover_bronze(container, START+timedelta(seconds=1), START+timedelta(seconds=2), limit=2, cursor=page.continuation)
    assert next_page.blobs == (path(0, 1),)


def test_backfill_preview_no_writes_and_already_published_visible():
    cli = load_cli()
    runner = SimpleNamespace(
        bronze=PagedContainer([path(0, 0)]),
        preview_ingestion=lambda _: SimpleNamespace(outcome='already_processed', to_dict=lambda: {'outcome':'already_processed'}),
        process_ingestion=lambda _: pytest.fail('Preview must not process'),
    )
    saved = []
    report = cli.run_batch(runner, START, START+timedelta(hours=1), save=saved.append)
    assert report['mode'] == 'preview'
    assert report['results'][0]['outcome'] == 'already_processed'
    assert saved == []


def test_backfill_persists_page_before_process_and_retains_failed_pending():
    cli = load_cli()
    seen_states = []
    def save(state):
        import copy
        seen_states.append(copy.deepcopy(state))
    attempts = []
    def process(name):
        assert seen_states  # durable pending exists before first mutation
        attempts.append(name)
        if name == path(0, 1):
            raise OSError('secret_url')
        return SimpleNamespace(outcome='published', to_dict=lambda: {'outcome':'published'})
    runner = SimpleNamespace(bronze=PagedContainer([path(0, i) for i in range(3)]), process_ingestion=process)
    report = cli.run_batch(runner, START, START+timedelta(hours=1), execute=True, save=save)
    assert len(seen_states[0]['pending']) == 3
    assert report['state']['pending'] == [path(0, 1)]
    assert report['state']['scan_complete'] is True
    assert report['more_work'] is True
    assert report['results'][1]['reason'] == 'OSError'
    assert 'secret_url' not in str(report)
    runner.process_ingestion = lambda _: SimpleNamespace(outcome='already_processed', to_dict=lambda: {'outcome':'already_processed'})
    retried = cli.run_batch(runner, START, START+timedelta(hours=1), state=report['state'], execute=True, save=save)
    assert retried['state']['pending'] == []
    assert retried['more_work'] is False
    assert len(runner.bronze.calls) == 1  # completion never restarts the scan


def test_backfill_crash_after_discovery_preserves_all_candidates():
    cli = load_cli()
    stored = []
    class SimulatedTermination(BaseException):
        pass
    def process(_):
        raise SimulatedTermination()
    runner = SimpleNamespace(bronze=PagedContainer([path(0, i) for i in range(2)]), process_ingestion=process)
    with pytest.raises(SimulatedTermination):
        cli.run_batch(runner, START, START+timedelta(hours=1), execute=True,
                      save=lambda value: stored.append(value.copy()))
    assert stored[0]['pending'] == [path(0, 0), path(0, 1)]


def test_execute_requires_state_and_cli_redacts_errors(capsys):
    cli = load_cli()
    with pytest.raises(SystemExit):
        cli.main(['--from', '2026-10-18', '--to', '2026-10-19', '--execute'])
    def fail():
        raise OSError('sas-secret')
    assert cli.main(['--from','2026-10-18','--to','2026-10-19'], runner_factory=fail) == 1
    captured = capsys.readouterr()
    assert 'sas-secret' not in captured.err
    assert 'OSError' in captured.err
