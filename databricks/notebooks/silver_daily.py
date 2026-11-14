# Databricks notebook source
# MAGIC %md
# MAGIC # Silver WeatherAPI : prévision quotidienne
# MAGIC
# MAGIC One `forecast_daily_snapshots` row per `forecastday`: the provider's official
# MAGIC daily values (`day`) and sun events (`astro`). They are never replaced by an
# MAGIC aggregate of hourly rows.

# COMMAND ----------

from zoneinfo import ZoneInfo


def daily_row(forecastday, *, common, city, local_day, collected, nulled):
    """Return the daily row for one ``forecastday`` (FORECAST_DAILY_SCHEMA columns).

    ``common``: the COMMON_FIELDS values of the city; ``city``: its registry entry
    (``city.timezone``); ``local_day``: the validated local date of the forecastday;
    ``collected``: the collection instant (UTC). An invalid OPTIONAL measure is set
    to None and its QualityError appended to ``nulled`` (see ``lenient``). Raise
    QualityError only for a problem that makes the whole day unusable.
    """
    lead_days = (local_day - collected.astimezone(ZoneInfo(city.timezone)).date()).days
    row = {**common, "target_local_date": local_day, "lead_days": lead_days}
    for field in FORECAST_DAILY_SCHEMA:
        row.setdefault(field.name, None)
    return row
