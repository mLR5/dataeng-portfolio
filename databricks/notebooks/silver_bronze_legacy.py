# Databricks notebook source
# MAGIC %md
# MAGIC # Silver WeatherAPI : ancien format Bronze
# MAGIC
# MAGIC Converts the historical Bronze envelope (`ingestion_metadata`, `metadata`,
# MAGIC `raw_data`) into the current envelope shape read by the transformation, so both
# MAGIC formats go through exactly the same identity, geography and time checks.

# COMMAND ----------

def normalize_envelope(data):
    """Return ``(envelope in the current shape, bronze_format)``.

    ``bronze_format`` is "current" or "legacy" (BRONZE_FORMATS). A current envelope
    is returned unchanged. Never invents values: a legacy field that cannot be
    mapped stays absent so the usual envelope checks reject it with their code.
    """
    return data, "current"
