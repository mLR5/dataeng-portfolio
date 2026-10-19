"""Versioned, I/O-free contracts shared by Silver transformation and Delta storage.

Changing produced values requires a new TRANSFORM_VERSION and a separate output
root. Delta commit versions are storage versions, not transformation versions.
"""
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
