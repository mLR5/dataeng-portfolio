"""Daily J+1 batches, bounded durable continuations and historical catch-up."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import math
import resource
import time

from azure.core.exceptions import ResourceNotFoundError

from . import config
from .claim_check import parse_timestamp
from .silver_contract import TRANSFORM_VERSION
from .silver_discovery import discover_bronze
from .silver_lease import BlobWriterLease, LeaseBusy
from .silver_publication import json_bytes
from .silver_runner import bronze_identity, make_azure_runner
from .silver_settings import SilverSettings

STATE_NAME = 'runtime/coordinator.json'
STATE_MAX_BYTES = 1024 * 1024


def _utc(value):
    result = parse_timestamp(value) if isinstance(value, str) else value
    if result.tzinfo is None:
        raise ValueError('Runtime time must include a timezone')
    return result.astimezone(timezone.utc)


def _source_time(path):
    identity = bronze_identity(path)
    return datetime.strptime(identity[:15], '%Y%m%d_%H%M%S').replace(tzinfo=timezone.utc)


def earliest_bronze(container, *, cursor=None, limit=1000):
    """One lexical page, resumable even through many directory/legacy objects."""
    if not 1 <= limit <= 1000 or (cursor is not None and not isinstance(cursor, str)):
        raise ValueError('Invalid earliest discovery bounds/cursor')
    pages = container.list_blobs(
        name_starts_with=config.BRONZE_PATH_PREFIX + '/', results_per_page=limit,
    ).by_page(continuation_token=cursor)
    try:
        values = list(next(pages))
        exhausted = False
    except StopIteration:
        values = []
        exhausted = True
    if len(values) > limit:
        raise RuntimeError('Storage exceeded earliest discovery page bound')
    for value in values:
        try:
            stamp = _source_time(value.name)
            if value.name.startswith(f'{config.BRONZE_PATH_PREFIX}/{stamp:%Y/%m/%d/%H}/'):
                return {'earliest': stamp, 'continuation': None, 'inspected': len(values)}
        except ValueError:
            continue
    return {'earliest': None, 'continuation': None if exhausted else pages.continuation_token, 'inspected': len(values)}


def new_state(settings):
    return {
        'schema_version': 2, 'transform_version': TRANSFORM_VERSION,
        'root': settings.root, 'bronze_container': config.BRONZE_CONTAINER,
        'configured_start_at': settings.start_at,
        'target_end': None, 'daily': {'start_at': None, 'window': None, 'scanned_end': None, 'completed_end': None},
        'next_wake_at': None,
        'history': {'origin': None, 'horizon': None, 'next_at': None, 'window': None, 'passes': 0,
                    'bootstrap_cursor': None, 'scan_done': False, 'completed_at': None, 'next_reconcile_at': None},
        'pending': {}, 'invocations': 0, 'last_started_at': None, 'last_finished_at': None,
        'latest_source_at': None, 'latest_usable_at': None,
        'latest_publication_status': None, 'latest_quality_degraded': None,
        'last_run': None,
    }


def validate_state(state, settings):
    expected = new_state(settings)
    if (not isinstance(state, dict) or any(state.get(k) != expected[k] for k in (
            'schema_version', 'transform_version', 'root', 'bronze_container', 'configured_start_at'))):
        raise ValueError('Runtime state belongs to another source/root/version/start')
    pending = state.get('pending')
    if not isinstance(pending, dict) or len(pending) > 1000:
        raise ValueError('Invalid runtime pending state')
    for path, job in pending.items():
        _source_time(path)
        if (not isinstance(job, dict) or job.get('lane') not in ('daily', 'history')
                or type(job.get('attempts')) is not int or job['attempts'] < 0):
            raise ValueError('Invalid runtime pending job')
        _utc(job['first_seen_at'])
        _utc(job['next_attempt_at'])
    history = state.get('history')
    if not isinstance(history, dict) or type(history.get('passes')) is not int or history['passes'] < 0:
        raise ValueError('Invalid runtime history state')
    if history.get('bootstrap_cursor') is not None and not isinstance(history['bootstrap_cursor'], str):
        raise ValueError('Invalid runtime bootstrap cursor')
    for stamp in ('origin', 'horizon', 'next_at', 'completed_at', 'next_reconcile_at'):
        if history.get(stamp) is not None:
            _utc(history[stamp])
    daily = state.get('daily')
    if not isinstance(daily, dict) or type(history.get('scan_done')) is not bool:
        raise ValueError('Invalid daily batch state')
    for stamp in ('target_end', 'next_wake_at'):
        if state.get(stamp) is not None:
            _utc(state[stamp])
    for stamp in ('start_at', 'scanned_end', 'completed_end'):
        if daily.get(stamp) is not None:
            _utc(daily[stamp])
    for window in (daily.get('window'), history.get('window')):
        if window is not None:
            if not isinstance(window, dict) or _utc(window['start']) >= _utc(window['end']):
                raise ValueError('Invalid runtime discovery window')
            if window.get('cursor') is not None and not isinstance(window['cursor'], dict):
                raise ValueError('Invalid runtime discovery cursor')
    return state


class AzureRuntimeStore:
    """The JSON checkpoint is itself leased; every replace carries its lease ID."""
    def __init__(self, blob):
        self.blob = blob

    def read(self):
        try:
            raw = self.blob.download_blob().readall()
        except ResourceNotFoundError:
            return None
        if len(raw) > STATE_MAX_BYTES:
            raise ValueError('Oversized runtime state')
        return json.loads(raw) if raw else None

    @contextmanager
    def session(self):
        with BlobWriterLease(self.blob) as lease:
            store = self
            class Session:
                def read(self):
                    lease.assert_held()
                    return store.read()
                def save(self, state):
                    raw = json_bytes(state)
                    if len(raw) > STATE_MAX_BYTES:
                        raise ValueError('Oversized runtime state')
                    lease.replace_blob(raw)
            yield Session()


def _window(start, end):
    return {'start': start.isoformat(), 'end': end.isoformat(), 'cursor': None}


def batch_cutoff(now, settings):
    """Exclusive UTC midnight of the latest batch scheduled at batch_hour_utc."""
    now = _utc(now)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight if now >= midnight + timedelta(hours=settings.batch_hour_utc) else midnight - timedelta(days=1)


def _request_batch(state, cutoff, settings):
    previous = state['target_end']
    if previous is not None and cutoff <= _utc(previous):
        return
    daily = state['daily']
    start = cutoff - timedelta(days=settings.replay_days)
    if previous is not None:
        # Cover every missed day, even after a long shutdown.
        start = min(start, _utc(previous) - timedelta(days=settings.replay_days - 1))
    if daily['window'] is not None:
        start = min(start, _utc(daily['window']['start']))
    if settings.start_at:
        start = max(start, _utc(settings.start_at))
    state['target_end'] = cutoff.isoformat()
    daily.update(start_at=start.isoformat(), window=_window(start, cutoff) if start < cutoff else None,
                 scanned_end=cutoff.isoformat() if start >= cutoff else None)


def _history_due(state, settings, now):
    history = state['history']
    return (history['horizon'] is not None or history['bootstrap_cursor'] is not None or
            history['passes'] == 0 or _utc(history['next_reconcile_at']) <= now)


def _discover(state, lane, runner, settings, now, session, report):
    pending = state['pending']
    capacity = settings.pending_limit // 2 - sum(job['lane'] == lane for job in pending.values())
    if capacity <= 0 or len(pending) >= settings.pending_limit:
        report['backpressure'] = True
        return
    history = state['history']
    if lane == 'daily':
        window = state['daily']['window']
        if window is None:
            return
    else:
        if not _history_due(state, settings, now) or history['scan_done']:
            return
        if history['horizon'] is None:
            if settings.start_at:
                first = _utc(settings.start_at)
            else:
                page = earliest_bronze(runner.bronze, cursor=history.get('bootstrap_cursor'))
                history['bootstrap_cursor'] = page['continuation']
                report['inspected'] += page['inspected']
                first = page['earliest']
                if first is None and page['continuation'] is not None:
                    _save(session, state)
                    return
            cutoff = _utc(state['target_end'])
            if first is None or first >= cutoff:
                history.update(horizon=cutoff.isoformat(), scan_done=True)
                _save(session, state)
                return
            if history['origin'] is not None:
                first = min(first, _utc(history['origin']))
            history.update(origin=first.isoformat(), horizon=cutoff.isoformat(), next_at=first.isoformat())
        if history['window'] is None:
            start = _utc(history['next_at'])
            history['window'] = _window(start, min(start + timedelta(days=7), _utc(history['horizon'])))
        window = history['window']
    page = discover_bronze(
        runner.bronze, _utc(window['start']), _utc(window['end']), cursor=window['cursor'],
        limit=min(capacity, settings.discovery_limit, settings.pending_limit - len(pending)),
        max_pages=settings.discovery_pages,
    )
    report['inspected'] += page.inspected
    for path in page.blobs:
        if path not in pending:
            pending[path] = {'lane': lane, 'attempts': 0, 'first_seen_at': now.isoformat(),
                             'next_attempt_at': now.isoformat(), 'last_error_type': None}
            report['discovered'] += 1
    window['cursor'] = page.continuation
    if page.continuation is None:
        if lane == 'daily':
            state['daily'].update(window=None, scanned_end=window['end'])
        else:
            history['next_at'] = window['end']
            history['window'] = None
            if _utc(history['next_at']) >= _utc(history['horizon']):
                history['scan_done'] = True
    # Persist the candidate list and its cursor together before any Delta write.
    _save(session, state)


def _finish_scans(state, settings, now):
    daily, history = state['daily'], state['history']
    if daily['scanned_end'] is not None and not any(
            job['lane'] == 'daily' or _utc(daily['start_at']) <= _source_time(path) < _utc(daily['scanned_end'])
            for path, job in state['pending'].items()):
        daily['completed_end'] = daily['scanned_end']
    if history['scan_done'] and not any(
            _source_time(path) < _utc(history['horizon']) for path in state['pending']):
        history.update(horizon=None, next_at=None, window=None, bootstrap_cursor=None, scan_done=False,
                       passes=history['passes'] + 1, completed_at=now.isoformat(),
                       next_reconcile_at=(now + timedelta(days=settings.reconcile_days)).isoformat())


def _continuation_delay(state, settings, now, report):
    delays = [max(0, math.ceil((_utc(job['next_attempt_at']) - now).total_seconds()))
              for job in state['pending'].values()]
    for lane in ('daily', 'history'):
        needs_scan = (state['daily']['window'] is not None if lane == 'daily' else
                      _history_due(state, settings, now) and not state['history']['scan_done'])
        capacity = settings.pending_limit // 2 - sum(job['lane'] == lane for job in state['pending'].values())
        if needs_scan and capacity > 0 and len(state['pending']) < settings.pending_limit:
            delays.append(30 if report['discovery_errors'] else 0)
    return min(3600, min(delays)) if delays else None


def _record_publication(state, result, settings):
    publication = result.publication
    if not publication:
        return
    # A malformed JSON has no envelope timestamp. Its canonical source identity
    # still orders quality events, without making any rejected data usable/fresh.
    stamp = (_utc(publication['collected_at_utc']) if publication.get('collected_at_utc')
             else _source_time(publication['source_blob']))
    status = publication['status']
    tables = publication['tables']
    usable = status in ('success', 'partial') and all(table['row_count'] > 0 for table in tables.values())
    if usable and (state['latest_usable_at'] is None or stamp > _utc(state['latest_usable_at'])):
        state['latest_usable_at'] = stamp.isoformat()
    if state['latest_source_at'] is None or stamp >= _utc(state['latest_source_at']):
        horizons = publication['summary'].get('horizons', {})
        complete_horizon = (len(horizons) == len(config.CITIES) and all(
            horizon.get('received_days', 0) >= settings.expected_forecast_days for horizon in horizons.values()))
        state.update(latest_source_at=stamp.isoformat(), latest_publication_status=status,
                     latest_quality_degraded=status != 'success' or not complete_horizon)


def run_tick(runner, store, settings, *, now=None, cutoff=None, monotonic=time.monotonic):
    if not settings.enabled:
        return {'outcome': 'disabled'}
    now = _utc(now or datetime.now(timezone.utc))
    cutoff = batch_cutoff(now, settings) if cutoff is None else _utc(cutoff)
    if cutoff != cutoff.replace(hour=0, minute=0, second=0, microsecond=0) or cutoff > batch_cutoff(now, settings):
        raise ValueError('Batch cutoff must be an eligible UTC midnight')
    began = monotonic()
    report = {'outcome': 'ok', 'root': settings.root, 'started_at': now.isoformat(), 'published': 0, 'already_processed': 0,
              'busy': 0, 'technical_errors': 0, 'discovery_errors': 0, 'discovered': 0,
              'inspected': 0, 'backpressure': False, 'quality': {}, 'error_types': {}}
    try:
        with store.session() as session:
            state = session.read()
            state = new_state(settings) if state is None else validate_state(state, settings)
            _request_batch(state, cutoff, settings)
            state['invocations'] += 1
            state['last_started_at'] = now.isoformat()
            _save(session, state)
            for lane in ('daily', 'history'):
                if monotonic() - began >= settings.budget_seconds:
                    break
                # A discovery error must not starve already durable pending jobs.
                # Work on a copy: an interrupted listing never advances its cursor.
                candidate = json.loads(json_bytes(state))
                try:
                    _discover(candidate, lane, runner, settings, now, session, report)
                    state = candidate
                except Exception as error:
                    # Saving failures are fatal; never process from unpersisted candidates.
                    if isinstance(error, StatePersistenceError):
                        raise
                    report['discovery_errors'] += 1
                    name = type(error).__name__
                    report['error_types'][name] = report['error_types'].get(name, 0) + 1
            due = sorted(
                (path for path, job in state['pending'].items() if _utc(job['next_attempt_at']) <= now),
                key=lambda path: (state['pending'][path]['attempts'] > 0,
                                  state['pending'][path]['lane'] != 'daily',
                                  state['pending'][path]['first_seen_at'], path),
            )
            for path in due[:settings.batch_size]:
                if monotonic() - began >= settings.budget_seconds:
                    break
                job = state['pending'][path]
                try:
                    result = runner.process_ingestion(path)
                    if result.outcome not in ('published', 'already_processed', 'busy'):
                        raise ValueError('Unknown Silver processing outcome')
                    if result.outcome == 'busy':
                        failure = 'LeaseBusy'
                        report['busy'] += 1
                    else:
                        _record_publication(state, result, settings)
                        report[result.outcome] += 1
                        if result.publication:
                            status = result.publication['status']
                            report['quality'][status] = report['quality'].get(status, 0) + 1
                        del state['pending'][path]
                        _save(session, state)
                        continue
                except StatePersistenceError:
                    raise
                except Exception as error:
                    failure = type(error).__name__
                    report['technical_errors'] += 1
                job['attempts'] += 1
                job['last_error_type'] = failure
                delay = min(3600, 30 * 2 ** min(job['attempts'] - 1, 7))
                job['next_attempt_at'] = (now + timedelta(seconds=monotonic() - began + delay)).isoformat()
                report['error_types'][failure] = report['error_types'].get(failure, 0) + 1
                _save(session, state)
            report['pending'] = len(state['pending'])
            report['duration_ms'] = int((monotonic() - began) * 1000)
            report['peak_rss_mb'] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
            if report['technical_errors'] or report['discovery_errors']:
                report['outcome'] = 'partial'
            finished = now + timedelta(milliseconds=report['duration_ms'])
            _finish_scans(state, settings, finished)
            delay = _continuation_delay(state, settings, finished, report)
            state['next_wake_at'] = (finished + timedelta(seconds=delay)).isoformat() if delay is not None else None
            report.update(next_delay_seconds=delay, cutoff_utc=state['target_end'],
                          daily_completed_end=state['daily']['completed_end'], history_passes=state['history']['passes'])
            state['last_finished_at'] = finished.isoformat()
            state['last_run'] = report
            _save(session, state)
            return report
    except LeaseBusy:
        return {'outcome': 'busy', 'reason': 'coordinator_lease_held', 'next_delay_seconds': 30, 'cutoff_utc': cutoff.isoformat()}


class StatePersistenceError(RuntimeError):
    pass


def _save(session, state):
    try:
        session.save(state)
    except Exception:
        raise StatePersistenceError('Silver coordinator checkpoint failed') from None


def make_runtime(settings=None):
    settings = settings or SilverSettings.from_env()
    runner = make_azure_runner(root=settings.root)
    return runner, AzureRuntimeStore(runner.controls.blob(STATE_NAME)), settings
