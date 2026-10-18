"""Bounded hourly Bronze enumeration with explicit, resumable cursors."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re

from . import config
from .claim_check import parse_timestamp

_NAME = re.compile(r'^(\d{8}_\d{6}(?:_[0-9a-f]{32})?)_weather_data\.json$')


@dataclass(frozen=True)
class DiscoveryPage:
    blobs: tuple[str, ...]
    inspected: int
    continuation: dict | None


def _utc(value):
    return parse_timestamp(value) if isinstance(value, str) else (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def discover_bronze(container, start, end, *, limit=200, max_pages=48, cursor=None):
    """Inspect at most limit blobs, returning continuation even with no matches.

    The cursor binds its original [start,end) window. Persist it and keep failed
    processing jobs separately; discovery progress is not processing success.
    SDK pages must respect requested results_per_page. Network errors propagate.
    """
    start, end = _utc(start), _utc(end)
    if start >= end or end - start > timedelta(days=366 * 10):
        raise ValueError('Invalid Bronze discovery window')
    if not 1 <= limit <= 1000 or not 1 <= max_pages <= 1000:
        raise ValueError('Discovery bounds must be 1..1000')
    first_hour = start.replace(minute=0, second=0, microsecond=0)
    hour, token = first_hour, None
    identity = {'start': start.isoformat(), 'end': end.isoformat()}
    if cursor is not None:
        if not isinstance(cursor, dict) or any(cursor.get(k) != v for k, v in identity.items()):
            raise ValueError('Discovery cursor belongs to another window')
        hour = parse_timestamp(cursor['hour'])
        token = cursor.get('token')
        if hour < first_hour or hour >= end or hour != hour.replace(minute=0, second=0, microsecond=0):
            raise ValueError('Invalid discovery cursor hour')
        if token is not None and not isinstance(token, str):
            raise ValueError('Invalid discovery cursor token')
    inspected, found, page_count = 0, [], 0
    while hour < end and inspected < limit and page_count < max_pages:
        prefix = f'{config.BRONZE_PATH_PREFIX}/{hour:%Y/%m/%d/%H}/'
        page_count += 1
        pages = container.list_blobs(
            name_starts_with=prefix, results_per_page=limit-inspected,
        ).by_page(continuation_token=token)
        exhausted = False
        try:
            items = list(next(pages))
        except StopIteration:
            items = []
            exhausted = True
        if len(items) > limit-inspected:
            raise RuntimeError('Storage exceeded requested discovery page bound')
        inspected += len(items)
        for item in items:
            name = item.name
            match = _NAME.fullmatch(name.removeprefix(prefix)) if name.startswith(prefix) else None
            if match:
                try:
                    collected = datetime.strptime(match.group(1)[:15], '%Y%m%d_%H%M%S').replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                if start <= collected < end:
                    found.append(name)
        token = None if exhausted else pages.continuation_token
        if token is None:
            hour += timedelta(hours=1)
        elif not items:
            # A service may return an empty page with a continuation. Return it
            # instead of spinning forever while inspecting zero blobs.
            break
    continuation = {**identity, 'hour': hour.isoformat(), 'token': token} if hour < end else None
    return DiscoveryPage(tuple(found), inspected, continuation)
