# Databricks notebook source
# MAGIC %md
# MAGIC # Silver WeatherAPI : contrat Bronze lu par Silver

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
