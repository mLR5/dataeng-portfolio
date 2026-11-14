# Databricks notebook source
# MAGIC %md
# MAGIC # Silver WeatherAPI : prévision quotidienne
# MAGIC
# MAGIC One `forecast_daily_snapshots` row per `forecastday`: the provider's official
# MAGIC daily values (`day`) and sun events (`astro`). They are never replaced by an
# MAGIC aggregate of hourly rows.
# MAGIC
# MAGIC - Toutes les mesures sont optionnelles : une clé absente reste nulle (jamais 0) ;
# MAGIC   une valeur fournie mais invalide devient nulle et est tracée (rejet de portée
# MAGIC   `field`), la ligne est conservée. Un bloc `day` absent laisse toutes les mesures
# MAGIC   nulles ; un bloc `day` qui n'est pas un objet rend le jour inutilisable.
# MAGIC - Probabilités (`daily_chance_of_*`) et humidité moyenne : décimaux acceptés dans
# MAGIC   [0, 100], jamais arrondis.
# MAGIC - `avgwetbulb_c` / `maxwetbulb_c` manquent dans les archives anciennes : nulles sans rejet.
# MAGIC - Lever/coucher du soleil : texte local source (`07:42 AM`) conservé tel quel, et
# MAGIC   instant UTC calculé avec le fuseau IANA de la ville pour `target_local_date`
# MAGIC   (heure d'été gérée ; heure locale inexistante ou ambiguë = rejet de champ sur
# MAGIC   l'instant UTC, le texte source reste). Un événement qui n'a pas lieu
# MAGIC   (`No sunrise`, `No sunset`) est nul, sans rejet.
# MAGIC - Lune (`moonrise`, `moonset`, `moon_phase`, `moon_illumination`, `is_moon_up`,
# MAGIC   `is_sun_up`) : exclue par décision documentée.

# COMMAND ----------

from datetime import datetime, time, timezone
import re
from zoneinfo import ZoneInfo

# (column, key in forecastday["day"], number() options). A missing key stays None.
_DAILY_MEASURES = (
    ("temperature_min_c", "mintemp_c", {}),
    ("temperature_max_c", "maxtemp_c", {}),
    ("temperature_avg_c", "avgtemp_c", {}),
    ("wind_max_kph", "maxwind_kph", {"minimum": 0}),
    ("precipitation_total_mm", "totalprecip_mm", {"minimum": 0}),
    ("snowfall_total_cm", "totalsnow_cm", {"minimum": 0}),
    ("visibility_avg_km", "avgvis_km", {"minimum": 0}),
    ("humidity_avg_pct", "avghumidity", {"minimum": 0, "maximum": 100}),
    ("chance_of_rain_pct", "daily_chance_of_rain", {"minimum": 0, "maximum": 100}),
    ("chance_of_snow_pct", "daily_chance_of_snow", {"minimum": 0, "maximum": 100}),
    ("uv_index", "uv", {"minimum": 0}),
    ("wet_bulb_avg_c", "avgwetbulb_c", {}),
    ("wet_bulb_max_c", "maxwetbulb_c", {}),
)
_DAILY_FLAGS = (("will_it_rain", "daily_will_it_rain"), ("will_it_snow", "daily_will_it_snow"))
# WeatherAPI writes sun events as a 12-hour clock, e.g. "07:42 AM" (12 = noon/midnight).
_DAILY_SUN_CLOCK = re.compile(r"(0?[1-9]|1[0-2]):([0-5][0-9]) (AM|PM)\Z")
# Polar day/night: the event does not occur on this date (NULL, not an error).
_DAILY_SUN_ABSENT = re.compile(r"(?:no sun(?:rise|set)|does not (?:rise|set)(?: today)?)\Z", re.IGNORECASE)


def _daily_object(parent, key, field):
    """Nested source object; absent -> {}, present but not an object -> QualityError."""
    value = parent.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise QualityError("invalid_type", field, "Daily source block must be an object")
    return value


def _daily_sun_instant(local_day, clock, zone, field):
    """Resolve a wall-clock time of ``local_day`` to UTC, refusing DST gaps and overlaps."""
    wall = datetime.combine(local_day, clock)
    candidates = set()
    for fold in (0, 1):
        candidate = wall.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
        if candidate.astimezone(zone).replace(tzinfo=None) == wall:
            candidates.add(candidate)
    if not candidates:
        raise QualityError("nonexistent_local_time", field, "Local sun event does not exist in this timezone")
    if len(candidates) != 1:
        raise QualityError("ambiguous_local_time", field, "Local sun event falls in a DST overlap")
    return candidates.pop()


def _daily_sun_event(astro, key, local_day, zone, nulled):
    """Return (source local text, UTC instant) of astro[key]; invalid values are nulled and recorded."""
    field = f"astro.{key}"
    value = astro.get(key)
    if value is None:
        return None, None
    if not isinstance(value, str):
        nulled.append(QualityError("invalid_type", field, "Sun event must be a string"))
        return None, None
    if _DAILY_SUN_ABSENT.fullmatch(value):
        return None, None
    match = _DAILY_SUN_CLOCK.fullmatch(value)
    if not match:
        nulled.append(QualityError("invalid_time", field, "Sun event is not a 12-hour clock time"))
        return None, None
    hour = int(match.group(1)) % 12 + (12 if match.group(3) == "PM" else 0)
    clock = time(hour, int(match.group(2)))
    # The source text stays available even when its UTC instant cannot be resolved.
    return value, lenient(nulled, lambda: _daily_sun_instant(local_day, clock, zone, field))


def daily_row(forecastday, *, common, city, local_day, collected, nulled):
    """Return the daily row for one ``forecastday`` (FORECAST_DAILY_SCHEMA columns).

    ``common``: the COMMON_FIELDS values of the city; ``city``: its registry entry
    (``city.timezone``); ``local_day``: the validated local date of the forecastday;
    ``collected``: the collection instant (UTC). An invalid OPTIONAL measure is set
    to None and its QualityError appended to ``nulled`` (see ``lenient``). Raise
    QualityError only for a problem that makes the whole day unusable.
    """
    if not isinstance(forecastday, dict):
        raise QualityError("invalid_type", None, "Forecast day must be an object")
    zone = ZoneInfo(city.timezone)
    lead_days = (local_day - collected.astimezone(zone).date()).days
    row = {**common, "target_local_date": local_day, "lead_days": lead_days}
    # A missing "day" leaves every measure null; a malformed one makes the day unusable.
    day = _daily_object(forecastday, "day", "day")
    for column, key, options in _DAILY_MEASURES:
        row[column] = lenient(nulled, lambda: number(day.get(key), f"day.{key}", **options))
    for column, key in _DAILY_FLAGS:
        row[column] = lenient(nulled, lambda: boolean(day.get(key), f"day.{key}"))
    condition = lenient(nulled, lambda: _daily_object(day, "condition", "day.condition")) or {}
    row["condition_code"] = lenient(nulled, lambda: number(
        condition.get("code"), "day.condition.code", integer=True, minimum=0, maximum=2**31 - 1))
    row["condition_text"] = lenient(nulled, lambda: optional_text(condition.get("text"), "day.condition.text"))
    astro = lenient(nulled, lambda: _daily_object(forecastday, "astro", "astro")) or {}
    for event in ("sunrise", "sunset"):
        row[f"{event}_local"], row[f"{event}_utc"] = _daily_sun_event(astro, event, local_day, zone, nulled)
    for field in FORECAST_DAILY_SCHEMA:
        row.setdefault(field.name, None)
    return row
