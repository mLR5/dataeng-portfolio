"""Bounded automatic Bronze discovery, durable pending work and retry scheduling."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
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


def earliest_bronze(container):
    """Azure lists names lexically. Bound malformed objects before the first ID."""
    values = container.list_blobs(name_starts_with=config.BRONZE_PATH_PREFIX + '/', results_per_page=100)
    for index, value in enumerate(values):
        if index >= 1000:
            raise RuntimeError('Earliest Bronze scan exceeded its bound')
        try:
            stamp = _source_time(value.name)
            if value.name.startswith(f'{config.BRONZE_PATH_PREFIX}/{stamp:%Y/%m/%d/%H}/'):
                return stamp
        except ValueError:
            continue
    return None


def new_state(settings):
    return {
        'schema_version': 1, 'transform_version': TRANSFORM_VERSION,
        'root': settings.root, 'bronze_container': config.BRONZE_CONTAINER,
        'configured_start_at': settings.start_at,
        'recent': None,
        'history': {'origin': None, 'horizon': None, 'next_at': None, 'window': None, 'passes': 0},
        'pending': {}, 'last_started_at': None, 'last_finished_at': None,
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
        if (not isinstance(job, dict) or job.get('lane') not in ('recent', 'history')
                or type(job.get('attempts')) is not int or job['attempts'] < 0):
            raise ValueError('Invalid runtime pending job')
        _utc(job['first_seen_at'])
        _utc(job['next_attempt_at'])
    history = state.get('history')
    if not isinstance(history, dict) or type(history.get('passes')) is not int or history['passes'] < 0:
        raise ValueError('Invalid runtime history state')
    for stamp in ('origin', 'horizon', 'next_at'):
        if history.get(stamp) is not None:
            _utc(history[stamp])
    for window in (state.get('recent'), history.get('window')):
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


def _discover(state, lane, runner, settings, now, session, report):
    pending = state['pending']
    # One failing historical batch cannot consume the recent lane's capacity.
    capacity = settings.pending_limit // 2 - sum(job['lane'] == lane for job in pending.values())
    if capacity <= 0 or len(pending) >= settings.pending_limit:
        report['backpressure'] = True
        return
    history = state['history']
    if lane == 'recent':
        if state['recent'] is None:
            start = now - timedelta(hours=settings.lookback_hours)
            lower = settings.start_at or history['origin']
            if lower is not None:
                start = max(start, _utc(lower))
            if start >= now:
                return
            state['recent'] = _window(start, now)
        window = state['recent']
    else:
        if history['horizon'] is None:
            first = _utc(settings.start_at) if settings.start_at else earliest_bronze(runner.bronze)
            if first is None or first >= now:
                return
            # Refresh the earliest name on every pass: older late arrivals are covered.
            if history['origin'] is not None:
                first = min(first, _utc(history['origin']))
            history.update(origin=first.isoformat(), horizon=now.isoformat(), next_at=first.isoformat())
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
        if lane == 'recent':
            state['recent'] = None
        else:
            history['next_at'] = window['end']
            history['window'] = None
            if _utc(history['next_at']) >= _utc(history['horizon']):
                history.update(horizon=None, next_at=None, passes=history['passes'] + 1)
    # Pending and its advanced cursor are one atomic, lease-fenced replacement.
    _save(session, state)


def _record_publication(state, result, settings):
    publication = result.publication
    if not publication or not publication.get('collected_at_utc'):
        return
    stamp = _utc(publication['collected_at_utc'])
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


def run_tick(runner, store, settings, *, now=None, monotonic=time.monotonic):
    if not settings.enabled:
        return {'outcome': 'disabled'}
    now = _utc(now or datetime.now(timezone.utc))
    began = monotonic()
    report = {'outcome': 'ok', 'root': settings.root, 'started_at': now.isoformat(), 'published': 0, 'already_processed': 0,
              'busy': 0, 'technical_errors': 0, 'discovery_errors': 0, 'discovered': 0,
              'inspected': 0, 'backpressure': False, 'quality': {}, 'error_types': {}}
    try:
        with store.session() as session:
            state = session.read()
            state = new_state(settings) if state is None else validate_state(state, settings)
            state['last_started_at'] = now.isoformat()
            _save(session, state)
            for lane in ('recent', 'history'):
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
                                  state['pending'][path]['lane'] != 'recent',
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
            state['last_finished_at'] = (now + timedelta(milliseconds=report['duration_ms'])).isoformat()
            state['last_run'] = report
            _save(session, state)
            return report
    except LeaseBusy:
        return {'outcome': 'busy', 'reason': 'coordinator_lease_held'}


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
