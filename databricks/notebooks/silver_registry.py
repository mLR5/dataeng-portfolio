# Databricks notebook source
# MAGIC %md
# MAGIC # Silver WeatherAPI : registre des chemins Bronze
# MAGIC
# MAGIC Every JSON path observed in Bronze has a written decision (kept, added,
# MAGIC derivable, represented otherwise, control only, excluded). Unknown paths are
# MAGIC reported in the publication summary and alert; they never evolve a schema.

# COMMAND ----------

# Path pattern -> decision. Lists are written with [], e.g. "cities[].data.current.uv".
FIELD_DECISIONS = {}


def unknown_paths(data):
    """Sorted scalar paths of ``data`` (a current-shape envelope) without a decision."""
    return []
