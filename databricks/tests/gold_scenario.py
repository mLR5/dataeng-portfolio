"""Bronze sources for the Gold incremental proofs, derived from a real capture.

Only identity, collection instant, observation instant, Paris latitude and the
observed temperature change; forecasts and every other value are the real
fixture's. Collections C_k are hourly from 2026-10-17 22:10 UTC (00:10 in Paris)
and observe five minutes earlier, so their canonical hours cover the whole local
day of 18 October. Late collections L_k observe exactly on the hour: they arrive
in a later batch and replace the canonical observation of C_k.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from zoneinfo import ZoneInfo

from conftest import FIXTURES, silver_rules

BASE = FIXTURES / "bronze_success_20261017_derived_real.json"
START = datetime(2026, 10, 17, 22, 10, tzinfo=timezone.utc)
PARIS_LATITUDE, PARIS_MOVED = 48.8667, 48.8567


def bronze_variant(collected, *, observed, paris_latitude=PARIS_LATITUDE, temperature_shift=0.0):
    value = json.loads(BASE.read_bytes())
    tag = f"{collected.isoformat()}|{observed.isoformat()}|{paris_latitude}|{temperature_shift}"
    ingestion_id = f"{collected:%Y%m%d_%H%M%S}_{hashlib.md5(tag.encode()).hexdigest()}"
    value["ingestion_id"] = ingestion_id
    value["ingestion_timestamp"] = collected.isoformat()
    for city in value["cities"]:
        data = city["data"]
        zone = ZoneInfo(data["location"]["tz_id"])
        data["location"]["localtime_epoch"] = int(collected.timestamp())
        data["location"]["localtime"] = collected.astimezone(zone).strftime("%Y-%m-%d %H:%M")
        current = data["current"]
        current["last_updated_epoch"] = int(observed.timestamp())
        current["last_updated"] = observed.astimezone(zone).strftime("%Y-%m-%d %H:%M")
        current["temp_c"] = round(current["temp_c"] + temperature_shift, 1)
        if city["city"] == "Paris":
            data["location"]["lat"] = paris_latitude
    raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
    return silver_rules.bronze_blob_name(ingestion_id, value["ingestion_timestamp"]), raw


def regular(k):
    """C_k: Paris moves at C_12 and comes back at C_18 (two dim_city versions)."""
    collected = START + timedelta(hours=k)
    latitude = PARIS_MOVED if 12 <= k < 18 else PARIS_LATITUDE
    return bronze_variant(collected, observed=collected - timedelta(minutes=5), paris_latitude=latitude)


def late(k):
    """L_k: collected 7 minutes before C_k, observed exactly on C_k's canonical hour."""
    hour = START - timedelta(minutes=10) + timedelta(hours=k)
    latitude = PARIS_MOVED if 12 < k <= 18 else PARIS_LATITUDE  # coordinates in force then
    return bronze_variant(hour + timedelta(minutes=3), observed=hour, paris_latitude=latitude,
                          temperature_shift=1.5)


def batches():
    """Three arrival batches. L_2 (batch 2) improves an hour published by batch 1;
    the canonical-hour truth of C_24 (22:00 UTC on 18 October, batch 3) arrives
    after forecasts of C_0..C_23 targeting it; L_20 then changes a complete day."""
    return [
        [regular(k) for k in range(0, 12)],
        [regular(k) for k in range(12, 24)] + [late(2)],
        [regular(24), late(20)],
    ]
