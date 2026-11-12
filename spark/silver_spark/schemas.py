"""Spark schemas derived from the single Arrow contract, plus control tables."""
from dataclasses import dataclass
import re

import pyarrow as pa
from pyspark.sql import types as T

from shared.silver_contract import FORECAST_TABLE, OBSERVATION_TABLE, TABLE_KEYS, TABLE_SCHEMAS

PUBLICATIONS = "publications"
QUARANTINE = "quarantine"

_ARROW_TO_SPARK = (
    (pa.types.is_int16, T.ShortType()),
    (pa.types.is_int32, T.IntegerType()),
    (pa.types.is_int64, T.LongType()),
    (pa.types.is_float64, T.DoubleType()),
    (pa.types.is_boolean, T.BooleanType()),
    (pa.types.is_date32, T.DateType()),
    (pa.types.is_string, T.StringType()),
    (pa.types.is_large_string, T.StringType()),
)


def spark_type(arrow_type):
    if pa.types.is_timestamp(arrow_type):
        # The contract only uses UTC instants in microseconds; Spark TIMESTAMP is an instant.
        if arrow_type.unit != "us" or arrow_type.tz != "UTC":
            raise ValueError("Only timestamp(us, UTC) is supported")
        return T.TimestampType()
    for predicate, result in _ARROW_TO_SPARK:
        if predicate(arrow_type):
            return result
    raise ValueError(f"Unsupported Arrow type {arrow_type}")


def spark_schema(arrow_schema):
    return T.StructType([
        T.StructField(f.name, spark_type(f.type), nullable=f.nullable) for f in arrow_schema
    ])


PUBLICATION_SCHEMA = T.StructType([
    T.StructField("ingestion_id", T.StringType(), False),
    T.StructField("complete", T.BooleanType(), False),
    T.StructField("engine", T.StringType(), False),
    T.StructField("schema_version", T.ShortType(), False),
    T.StructField("transform_version", T.StringType(), False),
    T.StructField("source", T.StringType(), False),
    T.StructField("source_blob", T.StringType(), False),
    T.StructField("source_sha256", T.StringType(), False),
    T.StructField("collected_at_utc", T.TimestampType(), True),
    T.StructField("published_at_utc", T.TimestampType(), False),
    T.StructField("status", T.StringType(), False),
    T.StructField("summary_json", T.StringType(), False),
    T.StructField("observation_version", T.LongType(), False),
    T.StructField("observation_row_count", T.IntegerType(), False),
    T.StructField("forecast_version", T.LongType(), False),
    T.StructField("forecast_row_count", T.IntegerType(), False),
    T.StructField("quarantine_count", T.IntegerType(), False),
    T.StructField("quarantine_sha256", T.StringType(), False),
])

QUARANTINE_SCHEMA = T.StructType([
    T.StructField("ingestion_id", T.StringType(), False),
    T.StructField("rejection_index", T.IntegerType(), False),
    T.StructField("source_blob", T.StringType(), False),
    T.StructField("source_sha256", T.StringType(), False),
    T.StructField("table_name", T.StringType(), False),
    T.StructField("code", T.StringType(), False),
    T.StructField("city_query", T.StringType(), True),
    T.StructField("position", T.StringType(), True),
    T.StructField("field", T.StringType(), True),
    T.StructField("message", T.StringType(), False),
])

SPARK_SCHEMAS = {
    OBSERVATION_TABLE: spark_schema(TABLE_SCHEMAS[OBSERVATION_TABLE]),
    FORECAST_TABLE: spark_schema(TABLE_SCHEMAS[FORECAST_TABLE]),
    PUBLICATIONS: PUBLICATION_SCHEMA,
    QUARANTINE: QUARANTINE_SCHEMA,
}
KEYS = {**TABLE_KEYS, PUBLICATIONS: ("ingestion_id",), QUARANTINE: ("ingestion_id", "rejection_index")}

# Insert-only tables. Deletion vectors stay off so delta-rs readers keep working.
TABLE_PROPERTIES = {
    "delta.appendOnly": "true",
    "delta.enableDeletionVectors": "false",
}

_SAFE_ROOT = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:/{1,3}[^`'\s]+$|^/[^`'\s]+$")


@dataclass(frozen=True)
class SilverPaths:
    """One root per schema/transform version; control tables live under _control."""
    root: str

    def __post_init__(self):
        root = self.root.rstrip("/")
        if not _SAFE_ROOT.fullmatch(root) or "/../" in f"{root}/":
            raise ValueError("Silver root must be an absolute path or URI without quotes")
        object.__setattr__(self, "root", root)

    def table(self, name):
        if name in (OBSERVATION_TABLE, FORECAST_TABLE):
            return f"{self.root}/{name}"
        if name in (PUBLICATIONS, QUARANTINE):
            return f"{self.root}/_control/{name}"
        raise ValueError("Unknown Silver table")


def ddl(schema):
    names = {
        T.ShortType: "SMALLINT", T.IntegerType: "INT", T.LongType: "BIGINT",
        T.DoubleType: "DOUBLE", T.BooleanType: "BOOLEAN", T.DateType: "DATE",
        T.StringType: "STRING", T.TimestampType: "TIMESTAMP",
    }
    return ", ".join(
        f"`{f.name}` {names[type(f.dataType)]}{'' if f.nullable else ' NOT NULL'}" for f in schema
    )
