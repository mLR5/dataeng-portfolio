"""Validated bounds for the automatic Silver worker; no credentials in errors."""
from dataclasses import dataclass
import os

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
    batch_size: int = 20
    discovery_limit: int = 40
    discovery_pages: int = 48
    pending_limit: int = 500
    lookback_hours: int = 6
    budget_seconds: int = 240
    max_age_minutes: int = 150
    stalled_minutes: int = 20
    pending_age_minutes: int = 60
    expected_forecast_days: int = 3

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        enabled = env.get('SILVER_ENABLED', 'true').lower()
        if enabled not in ('true', 'false'):
            raise ValueError('SILVER_ENABLED must be true or false')
        root = _relative_silver_root(env.get('SILVER_ROOT', silver_root()))
        start = env.get('SILVER_START_AT') or None
        if start:
            try:
                start = parse_timestamp(start).isoformat()
            except (ValueError, TypeError):
                raise ValueError('SILVER_START_AT must be an ISO UTC timestamp') from None
        return cls(
            enabled=enabled == 'true', root=root, start_at=start,
            batch_size=_integer(env, 'SILVER_BATCH_SIZE', 20, 1, 100),
            discovery_limit=_integer(env, 'SILVER_DISCOVERY_LIMIT', 40, 1, 100),
            discovery_pages=_integer(env, 'SILVER_DISCOVERY_PAGES', 48, 1, 168),
            pending_limit=_integer(env, 'SILVER_PENDING_LIMIT', 500, 4, 1000),
            lookback_hours=_integer(env, 'SILVER_LOOKBACK_HOURS', 6, 1, 168),
            budget_seconds=_integer(env, 'SILVER_BUDGET_SECONDS', 240, 10, 480),
            max_age_minutes=_integer(env, 'SILVER_MAX_AGE_MINUTES', 150, 60, 1440),
            stalled_minutes=_integer(env, 'SILVER_STALLED_MINUTES', 20, 5, 120),
            pending_age_minutes=_integer(env, 'SILVER_PENDING_AGE_MINUTES', 60, 5, 1440),
            expected_forecast_days=_integer(env, 'SILVER_EXPECTED_FORECAST_DAYS', 3, 1, 14),
        )
