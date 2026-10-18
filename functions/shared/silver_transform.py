"""Deterministic Bronze JSON -> typed WeatherAPI snapshots, without I/O.

Only ``current`` produces observations. Forecast snapshots remain forecasts even
when their target time precedes collection. Identity includes the ingestion, so
separate collections never overwrite one another in this layer.
"""
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import re
from zoneinfo import ZoneInfo

import pyarrow as pa

from .claim_check import INGESTION_ID_PATTERN, bronze_blob_name, parse_timestamp
from .silver_cities import CityResolutionError, resolve_city
from .silver_contract import (
    FORECAST_KEYS, FORECAST_SCHEMA, FORECAST_TABLE, OBSERVATION_KEYS,
    OBSERVATION_SCHEMA, OBSERVATION_TABLE, SCHEMA_VERSION, SOURCE,
    TRANSFORM_VERSION, Rejection, TransformResult,
)
from .silver_quality import QualityError, boolean, instant, number, optional_text


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        # Unknown provider keys can later appear in summary.ignored_fields.
        key.encode("utf-8")
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("Non-finite JSON constant")


def _path_id(source_blob):
    filename = source_blob.rsplit("/", 1)[-1]
    candidate = filename.removesuffix("_weather_data.json")
    return candidate if INGESTION_ID_PATTERN.fullmatch(candidate) else None


def _safe_source_blob(source_blob):
    if (not isinstance(source_blob, str) or not source_blob
            or source_blob.startswith("/") or any(c in source_blob for c in ("?", "#", "\\", ":"))
            or any(part in ("", ".", "..") for part in source_blob.split("/"))):
        raise ValueError("source_blob must be a relative blob path without URL credentials")
    try:
        source_blob.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("source_blob must be a valid Unicode relative blob path") from None


def _weather(row):
    if not isinstance(row, dict):
        raise QualityError("invalid_type", None, "Weather row must be an object")
    condition = row.get("condition")
    if condition is None:
        condition = {}
    if not isinstance(condition, dict):
        raise QualityError("invalid_type", "condition", "Condition must be an object")
    metrics = {
        "temperature_c": number(row.get("temp_c"), "temp_c", required=True),
        "feels_like_c": number(row.get("feelslike_c"), "feelslike_c"),
        "humidity_pct": number(row.get("humidity"), "humidity", integer=True, minimum=0, maximum=100),
        "pressure_mb": number(row.get("pressure_mb"), "pressure_mb", minimum=0),
        "wind_kph": number(row.get("wind_kph"), "wind_kph", minimum=0),
        "wind_degree": number(row.get("wind_degree"), "wind_degree", integer=True, minimum=0, maximum=360),
        "precipitation_mm": number(row.get("precip_mm"), "precip_mm", minimum=0),
        "cloud_pct": number(row.get("cloud"), "cloud", integer=True, minimum=0, maximum=100),
        "condition_code": number(condition.get("code"), "condition.code", integer=True, minimum=0, maximum=2**31 - 1),
        "condition_text": optional_text(condition.get("text"), "condition.text"),
        "is_day": boolean(row.get("is_day"), "is_day"),
    }
    if metrics["pressure_mb"] == 0:
        raise QualityError("out_of_range", "pressure_mb", "Pressure must be strictly positive when supplied")
    return metrics


def _envelope(data, source_blob):
    if not isinstance(data, dict):
        raise QualityError("invalid_envelope", None, "Bronze envelope must be an object")
    ingestion_id = data.get("ingestion_id")
    if not isinstance(ingestion_id, str) or not INGESTION_ID_PATTERN.fullmatch(ingestion_id):
        raise QualityError("invalid_ingestion_id", "ingestion_id", "Ingestion ID has an invalid format")
    stamp = data.get("ingestion_timestamp")
    try:
        if not isinstance(stamp, str) or not re.search(r"[T ][0-9]{2}:[0-9]{2}", stamp):
            raise ValueError
        collected = parse_timestamp(stamp)
    except (ValueError, TypeError, OverflowError):
        raise QualityError("invalid_timestamp", "ingestion_timestamp", "Collection timestamp is invalid") from None
    if source_blob != bronze_blob_name(ingestion_id, stamp):
        raise QualityError("source_path_mismatch", "source_blob", "Bronze path differs from its envelope identity or UTC partition")
    cities = data.get("cities")
    if not isinstance(cities, list) or not cities:
        raise QualityError("invalid_envelope", "cities", "Bronze cities must be a nonempty list")
    for city in cities:
        if (not isinstance(city, dict) or not isinstance(city.get("city"), str)
                or not city["city"].strip() or city.get("status") not in ("success", "error")):
            raise QualityError("invalid_envelope", "cities", "Each collection result needs a city and a known status")
        try:
            optional_text(city["city"], "city")
        except QualityError:
            raise QualityError("invalid_envelope", "cities", "Requested city labels must be valid Unicode") from None
    summary = data.get("summary")
    successful = sum(city["status"] == "success" for city in cities)
    expected = {"total_cities": len(cities), "successful": successful, "failed": len(cities) - successful}
    if (not isinstance(summary, dict)
            or any(type(summary.get(k)) is not int or summary[k] != v for k, v in expected.items())):
        raise QualityError("summary_mismatch", "summary", "Collection counts differ from the city results")
    status = "failed" if not successful else ("partial" if expected["failed"] else "success")
    # Historical Bronze envelopes did not all include a top-level status.
    if data.get("status", status) != status:
        raise QualityError("summary_mismatch", "status", "Collection status differs from its city results")
    return ingestion_id, collected, cities, expected


def _deduplicate(candidates, keys, table_name, rejections, counts, city_counts):
    groups = defaultdict(list)
    for row, query, position in candidates:
        groups[tuple(row[key] for key in keys)].append((row, query, position))
    accepted = []
    for key in sorted(groups):
        group = groups[key]
        row = group[0][0]
        per_city = city_counts[row["city_id"]][table_name]
        if any(item[0] != row for item in group[1:]):
            counts["rejected_rows"] += len(group)
            counts["conflicting_rows"] += len(group)
            per_city["rejected_rows"] += len(group)
            per_city["conflicting_rows"] += len(group)
            for _, query, position in group:
                rejections.append(Rejection(table_name, "duplicate_conflict", query, position,
                                            None, "Snapshot key has incompatible source rows"))
        else:
            accepted.append(row)
            counts["duplicates_removed"] += len(group) - 1
            per_city["duplicates_removed"] += len(group) - 1
            per_city["valid_rows"] += 1
    counts["valid_rows"] = len(accepted)
    return accepted


def _coverage(city, days, raw_hours, valid_rows):
    zone = ZoneInfo(city.timezone)
    targets = {row["target_at_utc"] for row in valid_rows}
    coverage = []
    for day in sorted(days):
        start = datetime.combine(day, time(), zone).astimezone(timezone.utc)
        end = datetime.combine(day + timedelta(days=1), time(), zone).astimezone(timezone.utc)
        expected = {start + timedelta(hours=i) for i in range(int((end - start).total_seconds() / 3600))}
        coverage.append({
            "local_date": day.isoformat(), "expected_hour_slots": len(expected),
            "valid_hour_slots": len(expected & targets), "missing_hour_slots": len(expected - targets),
        })
    return {
        "received_days": len(days), "received_hours": raw_hours, "valid_hours": len(valid_rows),
        "first_target_at_utc": min(targets).isoformat() if targets else None,
        "last_target_at_utc": max(targets).isoformat() if targets else None,
        "minimum_lead_time_hours": min((row["lead_time_hours"] for row in valid_rows), default=None),
        "maximum_lead_time_hours": max((row["lead_time_hours"] for row in valid_rows), default=None),
        "daily_coverage": coverage,
    }


def transform_bronze(raw: bytes, source_blob: str) -> TransformResult:
    """Transform one exact Bronze payload; data errors return a terminal result.

    The caller supplies a relative, unsigned blob path. Invalid call arguments
    raise TypeError/ValueError. Technical library failures are not reclassified
    as data rejections; this function never calls an API or storage service.
    """
    if not isinstance(raw, bytes):
        raise TypeError("raw must contain the exact Bronze bytes")
    _safe_source_blob(source_blob)
    source_sha256 = hashlib.sha256(raw).hexdigest()
    ingestion_id = _path_id(source_blob)
    collected = None
    rejections = []
    candidates = {OBSERVATION_TABLE: [], FORECAST_TABLE: []}
    summary = {
        "tables": {table: {"input_rows": 0, "valid_rows": 0, "rejected_rows": 0,
                           "duplicates_removed": 0, "conflicting_rows": 0}
                   for table in candidates},
        "cities": {"requested": [], "collection_successful": [], "collection_failed": [],
                   "with_valid_observations": [], "with_valid_forecasts": []},
        "horizons": {}, "ignored_fields": {}, "city_tables": {},
    }

    def reject(table, error, query=None, position=None):
        rejections.append(Rejection(table, error.code, query, position, error.field, str(error)))

    def result(observations=(), forecasts=(), status="rejected", reason=None):
        summary["rejection_count"] = len(rejections)
        if reason:
            summary["reason"] = reason
        return TransformResult(
            observations=pa.Table.from_pylist(list(observations), schema=OBSERVATION_SCHEMA),
            forecasts=pa.Table.from_pylist(list(forecasts), schema=FORECAST_SCHEMA),
            rejections=tuple(rejections), summary=summary, status=status,
            ingestion_id=ingestion_id, collected_at_utc=collected,
            source_blob=source_blob, source_sha256=source_sha256,
        )

    try:
        data = json.loads(raw, object_pairs_hook=_json_object, parse_constant=_invalid_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        reject("envelope", QualityError("invalid_json", None, "Bronze payload is not valid unique-key JSON"))
        return result(reason="invalid_json")
    # Preserve trustworthy metadata on a rejected envelope without inventing it.
    if isinstance(data, dict):
        possible_id = data.get("ingestion_id")
        if isinstance(possible_id, str) and INGESTION_ID_PATTERN.fullmatch(possible_id):
            ingestion_id = possible_id
        try:
            if (isinstance(data.get("ingestion_timestamp"), str)
                    and re.search(r"[T ][0-9]{2}:[0-9]{2}", data["ingestion_timestamp"])):
                collected = parse_timestamp(data["ingestion_timestamp"])
        except (ValueError, TypeError, OverflowError):
            pass
    try:
        ingestion_id, collected, cities, collection_counts = _envelope(data, source_blob)
    except QualityError as error:
        reject("envelope", error)
        return result(reason=error.code)
    summary["collection"] = collection_counts
    city_horizons = {}
    ignored = defaultdict(set)

    for city_position, entry in enumerate(cities):
        query = entry["city"]
        prefix = f"cities[{city_position}]"
        summary["cities"]["requested"].append(query)
        if entry["status"] == "error":
            summary["cities"]["collection_failed"].append(query)
            reject("collection", QualityError("collection_failed", "status", "City collection failed in Bronze"), query, prefix)
            continue
        summary["cities"]["collection_successful"].append(query)
        payload = entry.get("data")
        if not isinstance(payload, dict):
            reject("city", QualityError("invalid_type", "data", "Successful city data must be an object"), query, prefix)
            continue
        location = payload.get("location")
        try:
            city = resolve_city(query, location)
            location_name = optional_text(location.get("name"), "location.name")
        except CityResolutionError as error:
            reject("city", QualityError(error.code, "location", str(error)), query, prefix)
            continue
        except QualityError as error:
            reject("city", error, query, prefix)
            continue
        common = {
            "schema_version": SCHEMA_VERSION, "transform_version": TRANSFORM_VERSION, "source": SOURCE,
            "ingestion_id": ingestion_id, "collected_at_utc": collected, "ingestion_date": collected.date(),
            "city_id": city.city_id, "city_query": query,
            "source_location_name": location_name,
            "source_country": location["country"],
            "latitude": float(location["lat"]), "longitude": float(location["lon"]),
            "source_timezone": city.timezone, "source_blob": source_blob, "source_sha256": source_sha256,
        }
        city_counts = summary["city_tables"].setdefault(city.city_id, {
            table: {key: 0 for key in counts} for table, counts in summary["tables"].items()
        })
        observation_counts = summary["tables"][OBSERVATION_TABLE]
        observation_counts["input_rows"] += 1
        city_counts[OBSERVATION_TABLE]["input_rows"] += 1
        current = payload.get("current")
        try:
            metrics = _weather(current)
            observed_at = instant(current, "last_updated_epoch", "last_updated", city.timezone)
            candidates[OBSERVATION_TABLE].append((
                {**common, "observed_at_utc": observed_at, **metrics}, query, f"{prefix}.current"))
            ignored["current"].update(set(current) - {
                "last_updated_epoch", "last_updated", "temp_c", "feelslike_c", "humidity", "pressure_mb",
                "wind_kph", "wind_degree", "precip_mm", "cloud", "condition", "is_day"})
        except QualityError as error:
            observation_counts["rejected_rows"] += 1
            city_counts[OBSERVATION_TABLE]["rejected_rows"] += 1
            reject(OBSERVATION_TABLE, error, query, f"{prefix}.current")

        forecast = payload.get("forecast")
        forecastdays = forecast.get("forecastday") if isinstance(forecast, dict) else None
        if not isinstance(forecastdays, list) or not forecastdays:
            reject(FORECAST_TABLE, QualityError("invalid_forecast", "forecast.forecastday",
                                             "Forecast days must be a nonempty list"), query, prefix)
            continue
        horizon = city_horizons.setdefault(city.city_id, {"city": city, "days": set(), "raw_hours": 0})
        for day_position, forecastday in enumerate(forecastdays):
            day_prefix = f"{prefix}.forecast.forecastday[{day_position}]"
            if not isinstance(forecastday, dict) or not isinstance(forecastday.get("hour"), list):
                reject(FORECAST_TABLE, QualityError("invalid_forecast", "hour", "Forecast day must contain an hour list"), query, day_prefix)
                continue
            day_error = None
            try:
                day_value = forecastday.get("date")
                if not isinstance(day_value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day_value):
                    raise ValueError
                local_day = date.fromisoformat(day_value)
                # Coverage needs both midnight boundaries of the local day.
                local_day + timedelta(days=1)
                horizon["days"].add(local_day)
            except (ValueError, OverflowError):
                local_day = None
                day_error = QualityError("invalid_date", "date", "Forecast day needs a valid local calendar date")
            if not forecastday["hour"]:
                reject(FORECAST_TABLE, day_error or QualityError("invalid_forecast", "hour", "Forecast hour list is empty"), query, day_prefix)
            for hour_position, hour in enumerate(forecastday["hour"]):
                position = f"{day_prefix}.hour[{hour_position}]"
                counts = summary["tables"][FORECAST_TABLE]
                counts["input_rows"] += 1
                city_counts[FORECAST_TABLE]["input_rows"] += 1
                horizon["raw_hours"] += 1
                try:
                    if day_error:
                        raise day_error
                    metrics = _weather(hour)
                    target = instant(hour, "time_epoch", "time", city.timezone)
                    target_local_date = target.astimezone(ZoneInfo(city.timezone)).date()
                    if target_local_date != local_day:
                        raise QualityError("invalid_date", "date", "Forecast target differs from its local forecast day")
                    lead = (target - collected).total_seconds() / 3600
                    row = {**common, "target_at_utc": target, "target_local_date": target_local_date,
                           "lead_time_hours": lead, "target_is_past_at_collection": lead < 0, **metrics,
                           "chance_of_rain_pct": number(hour.get("chance_of_rain"), "chance_of_rain", integer=True, minimum=0, maximum=100),
                           "chance_of_snow_pct": number(hour.get("chance_of_snow"), "chance_of_snow", integer=True, minimum=0, maximum=100)}
                    candidates[FORECAST_TABLE].append((row, query, position))
                    ignored["hour"].update(set(hour) - {
                        "time_epoch", "time", "temp_c", "feelslike_c", "humidity", "pressure_mb", "wind_kph",
                        "wind_degree", "precip_mm", "cloud", "condition", "is_day", "chance_of_rain", "chance_of_snow"})
                except QualityError as error:
                    counts["rejected_rows"] += 1
                    city_counts[FORECAST_TABLE]["rejected_rows"] += 1
                    reject(FORECAST_TABLE, error, query, position)

    observations = _deduplicate(candidates[OBSERVATION_TABLE], OBSERVATION_KEYS, OBSERVATION_TABLE,
                                rejections, summary["tables"][OBSERVATION_TABLE], summary["city_tables"])
    forecasts = _deduplicate(candidates[FORECAST_TABLE], FORECAST_KEYS, FORECAST_TABLE,
                             rejections, summary["tables"][FORECAST_TABLE], summary["city_tables"])
    summary["cities"]["with_valid_observations"] = sorted({row["city_id"] for row in observations})
    summary["cities"]["with_valid_forecasts"] = sorted({row["city_id"] for row in forecasts})
    for city_id, horizon in sorted(city_horizons.items()):
        summary["horizons"][city_id] = _coverage(horizon["city"], horizon["days"], horizon["raw_hours"],
                                              [row for row in forecasts if row["city_id"] == city_id])
    summary["ignored_fields"] = {section: sorted(fields) for section, fields in sorted(ignored.items()) if fields}
    if not observations and not forecasts:
        if not collection_counts["successful"]:
            return result(status="empty", reason="collection_failed")
        return result(reason="no_valid_rows")
    return result(observations, forecasts, status="partial" if rejections else "success")
