"""Validated bounds for a daily UTC Silver batch and its continuations."""
from dataclasses import dataclass
import os
import re

from .claim_check import parse_timestamp
from .silver_runner import _relative_silver_root, silver_root


def _integer(env, name, default, minimum, maximum):
    try:
        value = int(env.get(name, str(default)))
        if not minimum <= value <= maximum:
            raise ValueError
        return value
    except (TypeError, ValueError):
        raise ValueError(f'{name} must be {minimum}..{maximum}') from None


@dataclass(frozen=True)
class SilverSettings:
    enabled: bool = True
    root: str = silver_root()
    start_at: str | None = None
    queue_name: str = 'silver-runtime'
    batch_hour_utc: int = 2
    replay_days: int = 2
    reconcile_days: int = 7
    completion_grace_minutes: int = 120
    batch_size: int = 20
    discovery_limit: int = 40
    discovery_pages: int = 48
    pending_limit: int = 500
    budget_seconds: int = 240
    max_age_minutes: int = 150
    stalled_minutes: int = 20
    pending_age_minutes: int = 180
    expected_forecast_days: int = 3

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        enabled = env.get('SILVER_ENABLED', 'true').lower()
        if enabled not in ('true', 'false'):
            raise ValueError('SILVER_ENABLED must be true or false')
        root = _relative_silver_root(env.get('SILVER_ROOT', silver_root()))
        queue = env.get('SILVER_QUEUE_NAME', 'silver-runtime')
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9]|-(?!-)){1,61}[a-z0-9]', queue):
            raise ValueError('Invalid SILVER_QUEUE_NAME')
        start = env.get('SILVER_START_AT') or None
        if start:
            try:
                start = parse_timestamp(start).isoformat()
            except (ValueError, TypeError):
                raise ValueError('SILVER_START_AT must be an ISO UTC timestamp') from None
        return cls(
            enabled=enabled == 'true', root=root, start_at=start, queue_name=queue,
            batch_hour_utc=_integer(env, 'SILVER_BATCH_HOUR_UTC', 2, 0, 23),
            replay_days=_integer(env, 'SILVER_REPLAY_DAYS', 2, 1, 31),
            reconcile_days=_integer(env, 'SILVER_RECONCILE_DAYS', 7, 1, 365),
            completion_grace_minutes=_integer(env, 'SILVER_COMPLETION_GRACE_MINUTES', 120, 1, 1440),
            batch_size=_integer(env, 'SILVER_BATCH_SIZE', 20, 1, 100),
            discovery_limit=_integer(env, 'SILVER_DISCOVERY_LIMIT', 40, 1, 100),
            discovery_pages=_integer(env, 'SILVER_DISCOVERY_PAGES', 48, 1, 168),
            pending_limit=_integer(env, 'SILVER_PENDING_LIMIT', 500, 4, 1000),
            budget_seconds=_integer(env, 'SILVER_BUDGET_SECONDS', 240, 10, 480),
            max_age_minutes=_integer(env, 'SILVER_MAX_AGE_MINUTES', 150, 1, 1440),
            stalled_minutes=_integer(env, 'SILVER_STALLED_MINUTES', 20, 5, 120),
            pending_age_minutes=_integer(env, 'SILVER_PENDING_AGE_MINUTES', 180, 5, 1440),
            expected_forecast_days=_integer(env, 'SILVER_EXPECTED_FORECAST_DAYS', 3, 1, 14),
        )
