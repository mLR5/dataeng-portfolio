"""Daily completion deadlines, source quality and durable continuation health."""
from datetime import datetime, timedelta, timezone

from .silver_runtime import _utc, batch_cutoff, validate_state


def _age(now, value):
    if value is not None and _utc(value) > now + timedelta(seconds=60):
        raise ValueError('Silver health timestamp is in the future')
    return None if value is None else max(0, int((now - _utc(value)).total_seconds()))


def check_silver_health(store, settings, *, now=None):
    now = _utc(now or datetime.now(timezone.utc))
    report = {'schema_version': 2, 'checked_at': now.isoformat(),
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
    # Before 04:00 UTC by default, yesterday's batch still has its completion grace.
    required_end = batch_cutoff(now - timedelta(minutes=settings.completion_grace_minutes), settings)
    completed = state['daily']['completed_end']
    daily_ready = completed is not None and _utc(completed) >= required_end
    wake = state['next_wake_at']
    continuation_fresh = wake is None or now <= _utc(wake) + timedelta(minutes=settings.stalled_minutes)
    scheduler_fresh = continuation_fresh and (daily_ready or tick_age is not None and tick_age <= settings.stalled_minutes * 60)
    data_fresh = state['latest_usable_at'] is not None and _utc(state['latest_usable_at']) >= required_end - timedelta(minutes=settings.max_age_minutes)
    report.update(
        freshness='fresh' if scheduler_fresh and daily_ready and data_fresh else 'stale',
        scheduler='fresh' if scheduler_fresh else 'stale',
        expected_end_utc=required_end.isoformat(), completed_end_utc=completed,
        daily_complete=daily_ready, next_wake_at=wake,
        latest_source_at=state['latest_source_at'], latest_usable_at=state['latest_usable_at'],
        source_age_seconds=source_age, tick_age_seconds=tick_age,
        latest_status=state['latest_publication_status'], quality_degraded=state['latest_quality_degraded'],
        pending=len(state['pending']), oldest_pending_seconds=oldest_pending,
        backlog_stale=oldest_pending > settings.pending_age_minutes * 60,
        history_passes=state['history']['passes'], max_age_minutes=settings.max_age_minutes,
    )
    return report
