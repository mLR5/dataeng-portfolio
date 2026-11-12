# Databricks notebook source
# MAGIC %md
# MAGIC # Silver WeatherAPI : règles
# MAGIC
# MAGIC Contrat des deux tables, registre des villes, contrôles de qualité, transformation
# MAGIC d'un JSON Bronze et contrôles de cohérence. Python pur, sans Spark ni I/O :
# MAGIC ce notebook est chargé par `%run` et testé en CI.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Contrat Bronze lu par Silver

# COMMAND ----------

import re
from datetime import datetime, timezone

# Bronze path contract, owned by the Function App (functions/shared/claim_check.py
# and functions/shared/config.py). Silver only reads it; a change there must be mirrored here.
BRONZE_PATH_PREFIX = "weather/weatherapi"
# Old IDs "YYYYMMDD_HHMMSS", new IDs "YYYYMMDD_HHMMSS_<uuid4 hex>".
INGESTION_ID_PATTERN = re.compile(r"^\d{8}_\d{6}(_[0-9a-f]{32})?$")


def parse_timestamp(value):
    """ISO timestamp in UTC; old timestamps without offset are already UTC."""
    timestamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def bronze_blob_name(ingestion_id, ingestion_timestamp):
    """Deterministic Bronze path: one ingestion always maps to the same blob."""
    partition = f"{parse_timestamp(ingestion_timestamp):%Y/%m/%d/%H}"
    return f"{BRONZE_PATH_PREFIX}/{partition}/{ingestion_id}_weather_data.json"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Contrat Silver
# MAGIC
# MAGIC Versioned, I/O-free contracts shared by Silver transformation and Delta storage.
# MAGIC
# MAGIC Every row carries schema_version and transform_version. Changing produced values
# MAGIC requires a new TRANSFORM_VERSION and a separate output root (the tables are
# MAGIC insert-only). Delta commit versions are storage versions, not transformation versions.

# COMMAND ----------

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal

import pyarrow as pa

SCHEMA_VERSION = 1
TRANSFORM_VERSION = "1.0.0"
SOURCE = "weatherapi"
SilverStatus = Literal["success", "partial", "rejected", "empty"]
SILVER_STATUSES = ("success", "partial", "rejected", "empty")
OBSERVATION_TABLE = "observation_snapshots"
FORECAST_TABLE = "forecast_hourly_snapshots"
UTC_TIMESTAMP = pa.timestamp("us", tz="UTC")

# Unpartitioned Delta tables initially: ingestion_date remains useful for filters.
COMMON_FIELDS = [
    pa.field("schema_version", pa.int16(), nullable=False),
    pa.field("transform_version", pa.string(), nullable=False),
    pa.field("source", pa.string(), nullable=False),
    pa.field("ingestion_id", pa.string(), nullable=False),
    pa.field("collected_at_utc", UTC_TIMESTAMP, nullable=False),
    pa.field("ingestion_date", pa.date32(), nullable=False),
    pa.field("city_id", pa.string(), nullable=False),
    pa.field("city_query", pa.string(), nullable=False),
    pa.field("source_location_name", pa.string()),
    pa.field("source_country", pa.string()),
    pa.field("latitude", pa.float64(), nullable=False),
    pa.field("longitude", pa.float64(), nullable=False),
    pa.field("source_timezone", pa.string(), nullable=False),
    pa.field("source_blob", pa.string(), nullable=False),
    pa.field("source_sha256", pa.string(), nullable=False),
]
WEATHER_FIELDS = [
    pa.field("temperature_c", pa.float64(), nullable=False),
    pa.field("feels_like_c", pa.float64()),
    pa.field("humidity_pct", pa.int16()),
    pa.field("pressure_mb", pa.float64()),
    pa.field("wind_kph", pa.float64()),
    pa.field("wind_degree", pa.int16()),
    pa.field("precipitation_mm", pa.float64()),
    pa.field("cloud_pct", pa.int16()),
    pa.field("condition_code", pa.int32()),
    pa.field("condition_text", pa.string()),
    pa.field("is_day", pa.bool_()),
]
OBSERVATION_SCHEMA = pa.schema(COMMON_FIELDS + [
    pa.field("observed_at_utc", UTC_TIMESTAMP, nullable=False),
] + WEATHER_FIELDS)
FORECAST_SCHEMA = pa.schema(COMMON_FIELDS + [
    pa.field("target_at_utc", UTC_TIMESTAMP, nullable=False),
    pa.field("target_local_date", pa.date32(), nullable=False),
    pa.field("lead_time_hours", pa.float64(), nullable=False),
    pa.field("target_is_past_at_collection", pa.bool_(), nullable=False),
] + WEATHER_FIELDS + [
    pa.field("chance_of_rain_pct", pa.int16()),
    pa.field("chance_of_snow_pct", pa.int16()),
])
OBSERVATION_KEYS = ("ingestion_id", "city_id", "observed_at_utc")
FORECAST_KEYS = ("ingestion_id", "city_id", "target_at_utc")
TABLE_SCHEMAS = {OBSERVATION_TABLE: OBSERVATION_SCHEMA, FORECAST_TABLE: FORECAST_SCHEMA}
TABLE_KEYS = {OBSERVATION_TABLE: OBSERVATION_KEYS, FORECAST_TABLE: FORECAST_KEYS}

# Stable quarantine codes; messages remain fixed descriptions, not SDK exceptions.
REJECTION_CODES = frozenset({
    "invalid_json", "invalid_envelope", "invalid_ingestion_id", "invalid_timestamp",
    "source_path_mismatch", "summary_mismatch", "unknown_city", "location_mismatch",
    "collection_failed", "missing_field", "invalid_type", "out_of_range",
    "non_finite", "duplicate_conflict", "invalid_timezone", "invalid_forecast",
    "invalid_date", "ambiguous_local_time", "nonexistent_local_time",
})


@dataclass(frozen=True)
class Rejection:
    """Small JSON-safe diagnostic; the Bronze already retains original values.

    source_blob/source_sha256/ingestion_id are attached by the publication writer
    from TransformResult, avoiding repetition and allowing malformed JSON inputs.
    """
    table: str
    code: str
    city_query: str | None = None
    position: str | None = None
    field: str | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TransformResult:
    observations: pa.Table
    forecasts: pa.Table
    rejections: tuple[Rejection, ...]
    summary: dict[str, Any]
    status: SilverStatus
    ingestion_id: str | None
    collected_at_utc: datetime | None
    source_blob: str
    source_sha256: str

    @property
    def observation_snapshots(self) -> pa.Table:
        return self.observations

    @property
    def forecast_hourly_snapshots(self) -> pa.Table:
        return self.forecasts

    @property
    def tables(self) -> dict[str, pa.Table]:
        return {OBSERVATION_TABLE: self.observations, FORECAST_TABLE: self.forecasts}


def empty_table(table_name: str) -> pa.Table:
    """An empty result still carries the full schema and required-field flags."""
    return pa.Table.from_pylist([], schema=TABLE_SCHEMAS[table_name])

# COMMAND ----------

# MAGIC %md
# MAGIC ## Registre des villes
# MAGIC
# MAGIC Versioned identity registry: labels are aliases, never primary keys.

# COMMAND ----------

from dataclasses import dataclass
import math
import unicodedata
from typing import Any, Mapping

REGISTRY_VERSION = 1


def _normalise(value: str) -> str:
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFKD", value.strip().casefold())
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).split())


@dataclass(frozen=True)
class CityDefinition:
    city_id: str
    timezone: str
    query_aliases: tuple[str, ...]
    country_aliases: tuple[str, ...]
    latitude_bounds: tuple[float, float]
    longitude_bounds: tuple[float, float]


CITIES = (
    CityDefinition("fr_paris", "Europe/Paris", ("Paris",),
                   ("France",), (48.5, 49.2), (1.9, 2.8)),
    CityDefinition("gb_london", "Europe/London", ("Londres", "London"),
                   ("United Kingdom", "UK", "United Kingdom of Great Britain and Northern Ireland"),
                   (51.1, 51.9), (-0.6, 0.3)),
    CityDefinition("be_brussels", "Europe/Brussels", ("Bruxelles", "Brussels", "Brussel"),
                   ("Belgium", "Belgique", "Belgie"), (50.5, 51.2), (3.9, 4.8)),
)
CITY_BY_ID = {city.city_id: city for city in CITIES}
_CITY_BY_QUERY = {_normalise(alias): city for city in CITIES for alias in city.query_aliases}


class CityResolutionError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def city_by_query(query: str) -> CityDefinition | None:
    return _CITY_BY_QUERY.get(_normalise(query))


def resolve_city(query: str, location: Mapping[str, Any]) -> CityDefinition:
    """Validate geography, country and IANA zone independently of source label.

    Bounds intentionally allow provider rounding/nearby metropolitan coordinates.
    Registry changes must be reviewed when adding cities or source aliases.
    """
    city = city_by_query(query)
    if city is None:
        raise CityResolutionError("unknown_city", "Requested city is absent from the versioned registry")
    if not isinstance(location, Mapping):
        raise CityResolutionError("location_mismatch", "Location must be an object")
    if location.get("tz_id") != city.timezone:
        raise CityResolutionError("location_mismatch", "Location timezone differs from the registered city")
    if _normalise(location.get("country")) not in {_normalise(c) for c in city.country_aliases}:
        raise CityResolutionError("location_mismatch", "Location country differs from the registered city")
    for key, bounds in (("lat", city.latitude_bounds), ("lon", city.longitude_bounds)):
        value = location.get(key)
        if isinstance(value, bool):
            raise CityResolutionError("location_mismatch", "Location coordinates are invalid")
        try:
            number = float(value)
        except (ValueError, TypeError):
            raise CityResolutionError("location_mismatch", "Location coordinates are invalid") from None
        if not math.isfinite(number) or not bounds[0] <= number <= bounds[1]:
            raise CityResolutionError("location_mismatch", "Location coordinates differ from the registered city")
    return city

# COMMAND ----------

# MAGIC %md
# MAGIC ## Qualité
# MAGIC
# MAGIC Pure parsing rules used by the WeatherAPI Silver transformation.
# MAGIC
# MAGIC Errors contain stable codes and field names, never the rejected source value.
# MAGIC Missing optional metrics remain null; malformed supplied metrics reject a row.

# COMMAND ----------

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import math
import re
from zoneinfo import ZoneInfo


_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_LOCAL_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}(?::[0-9]{2})?\Z")


class QualityError(ValueError):
    """A deterministic data rejection, distinct from a technical failure."""

    def __init__(self, code, field, message):
        super().__init__(message)
        self.code = code
        self.field = field


def number(value, field, *, required=False, integer=False, minimum=None, maximum=None):
    """Parse finite JSON numbers or unambiguous decimal strings without coercing bools."""
    if value is None:
        if required:
            raise QualityError("missing_field", field, "Required metric is missing")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise QualityError("invalid_type", field, "Metric must be numeric")
    if isinstance(value, str) and not _NUMBER.fullmatch(value):
        raise QualityError("invalid_type", field, "Metric is not an unambiguous number")
    try:
        decimal = Decimal(str(value))
        if not decimal.is_finite():
            raise ValueError
        if integer:
            if decimal != decimal.to_integral_value():
                raise QualityError("invalid_type", field, "Metric must be an integer")
            parsed = int(decimal)
        else:
            parsed = float(decimal)
            if not math.isfinite(parsed):
                raise ValueError
    except (InvalidOperation, ValueError, OverflowError) as error:
        if isinstance(error, QualityError):
            raise
        raise QualityError("invalid_type", field, "Metric must be finite") from None
    if ((minimum is not None and parsed < minimum)
            or (maximum is not None and parsed > maximum)):
        raise QualityError("out_of_range", field, "Metric is outside its allowed domain")
    return parsed


def boolean(value, field):
    """WeatherAPI encodes optional booleans as 0/1, including numeric strings."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return bool(number(value, field, integer=True, minimum=0, maximum=1))


def optional_text(value, field):
    if value is None:
        return None
    if not isinstance(value, str):
        raise QualityError("invalid_type", field, "Text metric must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise QualityError("invalid_type", field, "Text metric must be valid Unicode") from None
    return value


def instant(row, epoch_field, local_field, source_timezone):
    """Prefer the source epoch; resolve local fallback only when it is unambiguous.

    An epoch remains authoritative during DST: two identical wall-clock hours
    can therefore survive with distinct UTC keys. An invalid supplied epoch is
    rejected rather than silently replaced by another field.
    """
    epoch = row.get(epoch_field)
    if epoch is not None:
        value = number(epoch, epoch_field, required=True, integer=True)
        try:
            parsed = datetime.fromtimestamp(value, timezone.utc)
            # The UTC instant must also be representable in its source timezone.
            parsed.astimezone(ZoneInfo(source_timezone))
            return parsed
        except (ValueError, OverflowError, OSError):
            raise QualityError("invalid_timestamp", epoch_field, "Epoch is outside supported dates") from None
    local = row.get(local_field)
    if local is None:
        raise QualityError("missing_field", epoch_field, "Source instant is missing")
    if not isinstance(local, str) or not _LOCAL_TIME.fullmatch(local):
        raise QualityError("invalid_timestamp", local_field, "Local timestamp has an invalid format")
    try:
        wall = datetime.fromisoformat(local)
    except ValueError:
        raise QualityError("invalid_timestamp", local_field, "Local timestamp is invalid") from None
    zone = ZoneInfo(source_timezone)
    candidates = set()
    for fold in (0, 1):
        try:
            candidate = wall.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
            if candidate.astimezone(zone).replace(tzinfo=None) == wall:
                candidates.add(candidate)
        except (ValueError, OverflowError):
            raise QualityError("invalid_timestamp", local_field, "Local timestamp is outside supported dates") from None
    if not candidates:
        raise QualityError("nonexistent_local_time", local_field, "Local timestamp does not exist in this timezone")
    if len(candidates) != 1:
        raise QualityError("ambiguous_local_time", local_field, "Local timestamp needs an epoch during DST overlap")
    return candidates.pop()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Transformation
# MAGIC
# MAGIC Deterministic Bronze JSON -> typed WeatherAPI snapshots, without I/O.
# MAGIC
# MAGIC Only ``current`` produces observations. Forecast snapshots remain forecasts even
# MAGIC when their target time precedes collection. Identity includes the ingestion, so
# MAGIC separate collections never overwrite one another in this layer.

# COMMAND ----------

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import re
from zoneinfo import ZoneInfo

import pyarrow as pa



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

# COMMAND ----------

# MAGIC %md
# MAGIC ## Contrôles de cohérence
# MAGIC
# MAGIC Identity of a Bronze source, consistency of a transformation result, canonical
# MAGIC JSON and quarantine records, applied before anything is written.

# COMMAND ----------

from datetime import date, datetime
import json
import re


_PATH = re.compile(r'^weather/weatherapi/\d{4}/\d{2}/\d{2}/\d{2}/([^/]+)_weather_data\.json$')


class PublicationConflict(ValueError):
    """Published immutable identity or content does not match this ingestion."""


def bronze_identity(source_blob):
    match = _PATH.fullmatch(source_blob)
    if not match or not INGESTION_ID_PATTERN.fullmatch(match.group(1)):
        raise ValueError('Expected a canonical Bronze blob path')
    return match.group(1)


def json_bytes(value):
    def encode(item):
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        raise TypeError('Non JSON-safe publication value')
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False, default=encode).encode('utf-8')


def quarantine_records(result, ingestion_id):
    """Rejections enriched with provenance, in transformer order."""
    return [{
        'ingestion_id': ingestion_id, 'source_blob': result.source_blob,
        'source_sha256': result.source_sha256, **r.to_dict(),
    } for r in result.rejections]


def validate_transform_result(result, *, ingestion_id, source_blob, source_sha256):
    """Refuse a transformation whose identity, status or provenance is inconsistent."""
    if result.source_blob != source_blob or result.source_sha256 != source_sha256:
        raise PublicationConflict('Transformation provenance differs from Bronze')
    terminal_empty = result.status == 'rejected' and not any(t.num_rows for t in result.tables.values())
    if result.ingestion_id not in (None, ingestion_id) and not terminal_empty:
        raise PublicationConflict('Transformation identity differs from Bronze path')
    if result.status not in SILVER_STATUSES:
        raise PublicationConflict('Unknown transformation status')
    has_rows = any(t.num_rows for t in result.tables.values())
    if (result.status in ('success', 'partial')) != has_rows:
        raise PublicationConflict('Transformation status differs from row counts')
    if result.status == 'empty' and not result.summary.get('reason'):
        raise PublicationConflict('Empty transformation requires a collection reason')
    if result.ingestion_id is None and not terminal_empty:
        raise PublicationConflict('Only rejected empty results may lack identity')
    expected = {
        'ingestion_id': ingestion_id, 'source_blob': source_blob,
        'source_sha256': source_sha256, 'schema_version': SCHEMA_VERSION,
        'transform_version': TRANSFORM_VERSION,
    }
    for rows in result.tables.values():
        for row in rows.to_pylist():
            if any(row.get(k) != v for k, v in expected.items()):
                raise PublicationConflict('Row provenance differs from transformation')
