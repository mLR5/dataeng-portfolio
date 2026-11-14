# Databricks notebook source
# MAGIC %md
# MAGIC # Silver WeatherAPI : ancien format Bronze
# MAGIC
# MAGIC Les 20 premières archives Bronze (19 et 20 janvier 2026) utilisent une ancienne enveloppe :
# MAGIC `ingestion_metadata` (identité, horodatage, source, pipeline, environnement, hôte, villes
# MAGIC et horizon demandés), puis par ville `metadata` (ville, horodatage, endpoint, langue,
# MAGIC durée, statut HTTP, erreur éventuelle) et `raw_data` (la réponse WeatherAPI, ou `null`
# MAGIC en cas d'échec), et un `summary` aux noms de compteurs différents.
# MAGIC
# MAGIC `normalize_envelope` convertit cette enveloppe vers la forme actuelle **sans rien
# MAGIC inventer** : seuls les champs présents sont recopiés, la réponse WeatherAPI est gardée
# MAGIC telle quelle sous `cities[].data`, puis toutes les vérifications habituelles (format de
# MAGIC l'identifiant, horodatage, chemin Bronze, comptes, registre des villes, instants)
# MAGIC s'appliquent sans exception. `Londres` résolu en Argentine reste rejeté.
# MAGIC
# MAGIC | Ancien chemin | Forme actuelle | Décision |
# MAGIC |---|---|---|
# MAGIC | `ingestion_metadata.ingestion_id` | `ingestion_id` | recopié |
# MAGIC | `ingestion_metadata.ingestion_timestamp` | `ingestion_timestamp` | recopié |
# MAGIC | `ingestion_metadata.forecast_days` | `forecast_request.days` | recopié (horizon demandé) |
# MAGIC | `cities[].metadata.city` | `cities[].city` | recopié |
# MAGIC | `cities[].raw_data` | `cities[].data` | recopié à l'identique |
# MAGIC | `cities[].metadata.http_status` (+ `raw_data`, `error`) | `cities[].status` | dérivé, seulement si cohérent |
# MAGIC | `summary.total_cities_requested / _retrieved / _failed` | `summary.total_cities / successful / failed` | recopié |
# MAGIC | `ingestion_metadata.cities_requested` | — | contrôle : doit égaler la liste des villes |
# MAGIC | `ingestion_metadata.source_api`, `pipeline`, `environment` | — | exclu (technique) |
# MAGIC | `ingestion_metadata.source_url`, `host`, `cities[].metadata.endpoint` | — | exclu, jamais lu (URL, hôte) |
# MAGIC | `cities[].metadata.timestamp`, `forecast_days`, `language`, `duration_ms` | — | exclu (technique) |
# MAGIC | `cities[].metadata.error_message` | — | exclu (diagnostic d'échec) |
# MAGIC
# MAGIC Statut d'une ville : `success` si HTTP 200, réponse objet et pas d'`error` ; `error` si
# MAGIC HTTP différent de 200 et réponse `null` ; sinon absent, donc `invalid_envelope`.

# COMMAND ----------

# Legacy path -> decision. Every path observed in the 20 legacy archives is listed;
# other paths are reported by legacy_unknown_paths. Values of the excluded paths are
# never read (some hold URLs or host names).
LEGACY_FIELD_DECISIONS = {
    "ingestion_metadata.ingestion_id": "mapped:ingestion_id",
    "ingestion_metadata.ingestion_timestamp": "mapped:ingestion_timestamp",
    "ingestion_metadata.forecast_days": "mapped:forecast_request.days",
    "ingestion_metadata.cities_requested[]": "control:equals_cities",
    "ingestion_metadata.source_api": "excluded:technical",
    "ingestion_metadata.pipeline": "excluded:technical",
    "ingestion_metadata.environment": "excluded:technical",
    "ingestion_metadata.source_url": "excluded:sensitive",
    "ingestion_metadata.host": "excluded:sensitive",
    "cities[].metadata.city": "mapped:cities[].city",
    "cities[].metadata.http_status": "control:cities[].status",
    "cities[].metadata.error": "control:cities[].status",
    "cities[].metadata.error_message": "excluded:diagnostic",
    "cities[].metadata.endpoint": "excluded:sensitive",
    "cities[].metadata.timestamp": "excluded:technical",
    "cities[].metadata.forecast_days": "excluded:technical",
    "cities[].metadata.language": "excluded:technical",
    "cities[].metadata.duration_ms": "excluded:technical",
    "cities[].raw_data": "mapped:cities[].data",
    "summary.total_cities_requested": "mapped:summary.total_cities",
    "summary.total_cities_retrieved": "mapped:summary.successful",
    "summary.total_cities_failed": "mapped:summary.failed",
}
_LEGACY_CONTAINERS = {"", "ingestion_metadata", "ingestion_metadata.cities_requested",
                      "cities", "cities[]", "cities[].metadata", "summary"}
_LEGACY_SUMMARY = (("total_cities_requested", "total_cities"),
                   ("total_cities_retrieved", "successful"),
                   ("total_cities_failed", "failed"))


def is_legacy_envelope(data):
    """The historical envelope carries its identity under ``ingestion_metadata``."""
    return isinstance(data, dict) and "ingestion_metadata" in data and "ingestion_id" not in data


def _legacy_status(metadata, raw_data):
    """'success' or 'error' only when HTTP status, response and error agree; else None."""
    http_status = metadata.get("http_status")
    if type(http_status) is not int:
        return None
    if http_status == 200 and isinstance(raw_data, dict) and "error" not in metadata:
        return "success"
    if http_status != 200 and raw_data is None:
        return "error"
    return None


def _legacy_city(entry):
    if not isinstance(entry, dict) or not isinstance(entry.get("metadata"), dict):
        return entry  # kept as is: the envelope check rejects it (invalid_envelope)
    metadata = entry["metadata"]
    city = {}
    if "city" in metadata:
        city["city"] = metadata["city"]
    if "raw_data" in entry:
        city["data"] = entry["raw_data"]  # the WeatherAPI response, untouched
    status = _legacy_status(metadata, entry.get("raw_data"))
    if status is not None:
        city["status"] = status
    return city


def normalize_envelope(data):
    """Return ``(envelope in the current shape, bronze_format)``.

    ``bronze_format`` is "current" or "legacy" (BRONZE_FORMATS). A current envelope
    is returned unchanged. Never invents values: a legacy field that cannot be
    mapped stays absent so the usual envelope checks reject it with their code.
    """
    if not is_legacy_envelope(data):
        return data, "current"
    converted = {}
    identity = data["ingestion_metadata"]
    if not isinstance(identity, dict):
        identity = {}
    for key in ("ingestion_id", "ingestion_timestamp"):
        if key in identity:
            converted[key] = identity[key]
    if type(identity.get("forecast_days")) is int:
        converted["forecast_request"] = {"days": identity["forecast_days"]}
    cities = data.get("cities")
    if isinstance(cities, list):
        cities = [_legacy_city(entry) for entry in cities]
        requested = identity.get("cities_requested")
        # A requested list contradicting the results leaves no trustworthy city list.
        if requested is None or requested == [c.get("city") if isinstance(c, dict) else None for c in cities]:
            converted["cities"] = cities
    elif "cities" in data:
        converted["cities"] = cities  # not a list: rejected by the envelope check
    summary = data.get("summary")
    if isinstance(summary, dict):
        converted["summary"] = {new: summary[old] for old, new in _LEGACY_SUMMARY if old in summary}
    return converted, "legacy"


def legacy_unknown_paths(data):
    """Sorted paths of a legacy envelope without a decision (paths only, never values).

    ``cities[].raw_data`` is the WeatherAPI response: after conversion it is
    governed by the current-shape registry (``unknown_paths``).
    """
    found = set()

    def walk(value, path):
        if path in LEGACY_FIELD_DECISIONS:
            return
        if path in _LEGACY_CONTAINERS and isinstance(value, dict):
            for key, child in value.items():
                walk(child, f"{path}.{key}" if path else str(key))
        elif path in _LEGACY_CONTAINERS and isinstance(value, list):
            for child in value:
                walk(child, f"{path}[]")
        else:
            found.add(path)

    if is_legacy_envelope(data):
        walk(data, "")
    return sorted(found)
