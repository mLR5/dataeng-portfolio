"""Read-only health from the durable coordinator; source time defines freshness."""
from datetime import datetime, timedelta, timezone

from .silver_runtime import _utc, validate_state


def _age(now, value):
    if value is not None and _utc(value) > now + timedelta(seconds=60):
        raise ValueError('Silver health timestamp is in the future')
    return None if value is None else max(0, int((now - _utc(value)).total_seconds()))


def check_silver_health(store, settings, *, now=None):
    now = _utc(now or datetime.now(timezone.utc))
    report = {'schema_version': 1, 'checked_at': now.isoformat(),
              'root': settings.root, 'freshness': 'disabled' if not settings.enabled else 'stale'}
    if not settings.enabled:
        return report
    state = store.read()
    if state is None:
        return {**report, 'reason': 'not_started', 'scheduler': 'stale', 'pending': 0}
    validate_state(state, settings)
    last_tick = max((stamp for stamp in (state['last_started_at'], state['last_finished_at']) if stamp), default=None)
    tick_age = _age(now, last_tick)
    source_age = _age(now, state['latest_usable_at'])
    oldest_pending = max((_age(now, job['first_seen_at']) for job in state['pending'].values()), default=0)
    scheduler_fresh = tick_age is not None and tick_age <= settings.stalled_minutes * 60
    data_fresh = source_age is not None and source_age <= settings.max_age_minutes * 60
    report.update(
        freshness='fresh' if scheduler_fresh and data_fresh else 'stale',
        scheduler='fresh' if scheduler_fresh else 'stale',
        latest_source_at=state['latest_source_at'], latest_usable_at=state['latest_usable_at'],
        source_age_seconds=source_age, tick_age_seconds=tick_age,
        latest_status=state['latest_publication_status'],
        quality_degraded=state['latest_quality_degraded'],
        pending=len(state['pending']), oldest_pending_seconds=oldest_pending,
        backlog_stale=oldest_pending > settings.pending_age_minutes * 60,
        history_passes=state['history']['passes'],
        max_age_minutes=settings.max_age_minutes,
    )
    return report
