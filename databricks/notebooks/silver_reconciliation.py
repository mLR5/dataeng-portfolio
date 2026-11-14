# Databricks notebook source
# MAGIC %md
# MAGIC # Réconciliation Bronze → Silver (lecture seule)
# MAGIC
# MAGIC Le recomptage des créneaux Bronze est indépendant des compteurs du
# MAGIC transformateur. Les valeurs Silver, les rejets et la publication sont
# MAGIC ensuite comparés au contrat versionné, sans réécrire les données.

# COMMAND ----------

# MAGIC %run ./silver_rules

# COMMAND ----------

from collections import Counter
from datetime import timezone


def _bronze_slots(raw):
    """Count source slots BEFORE city validation; never assume 24 hours/day.

    One observation slot per successful city, including a missing current
    section. Forecast slots are actual entries in hour lists, including invalid
    entries. A failed API call has no weather rows to lose in Silver.
    """
    slots = {name: Counter() for name in TABLE_SCHEMAS}
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return slots
    cities = data.get("cities") if isinstance(data, dict) else None
    for index, city in enumerate(cities if isinstance(cities, list) else []):
        if not isinstance(city, dict) or city.get("status") != "success":
            continue
        prefix = f"cities[{index}]"
        slots[OBSERVATION_TABLE][prefix] += 1
        payload = city.get("data")
        forecast = payload.get("forecast") if isinstance(payload, dict) else None
        days = forecast.get("forecastday") if isinstance(forecast, dict) else None
        for day in days if isinstance(days, list) else []:
            hours = day.get("hour") if isinstance(day, dict) else None
            if isinstance(hours, list):
                slots[FORECAST_TABLE][prefix] += len(hours)
    return slots


def explain_bronze(raw, source_blob):
    """Independently balance all Bronze slots against the versioned rules.

    A city/envelope rejection can exclude many rows, so diagnostic counts must
    never be substituted for rejected row counts. Unparseable JSON is reported
    as uncountable, with its explicit envelope rejection, rather than as success.
    """
    result = transform_bronze(raw, source_blob)
    slots = _bronze_slots(raw)
    errors = []
    tables = {}
    envelope_rejected = any(r.table == "envelope" for r in result.rejections)
    excluded_cities = {r.position for r in result.rejections if r.table == "city"}
    for name, by_city in slots.items():
        counts = result.summary["tables"][name]
        excluded = sum(n for prefix, n in by_city.items()
                       if envelope_rejected or prefix in excluded_cities)
        raw_slots = sum(by_city.values())
        if raw_slots != counts["input_rows"] + excluded:
            errors.append(f"{name}: bronze_inventory_mismatch")
        if counts["input_rows"] != sum(counts[k] for k in (
                "valid_rows", "rejected_rows", "duplicates_removed")):
            errors.append(f"{name}: unaccounted_candidate_rows")
        if counts["valid_rows"] != result.tables[name].num_rows:
            errors.append(f"{name}: valid_count_mismatch")
        if not 0 <= counts["conflicting_rows"] <= counts["rejected_rows"]:
            errors.append(f"{name}: conflict_count_mismatch")
        # A row rejection has a current/hour position; section diagnostics don't.
        row_rejections = [r for r in result.rejections if r.table == name and r.position
                          and (r.position.endswith(".current") or ".hour[" in r.position)]
        if len(row_rejections) != counts["rejected_rows"]:
            errors.append(f"{name}: missing_row_rejection")
        if sum(r.code == "duplicate_conflict" for r in row_rejections) != counts["conflicting_rows"]:
            errors.append(f"{name}: missing_conflict_rejection")
        for key, value in counts.items():
            if sum(city[name][key] for city in result.summary["city_tables"].values()) != value:
                errors.append(f"{name}: city_totals_mismatch:{key}")
        tables[name] = {"bronze_slots": raw_slots, "scope_excluded_rows": excluded, **counts}
    report = {
        "source_blob": source_blob, "source_sha256": result.source_sha256,
        "status": result.status, "tables": tables,
        "rejection_codes": dict(sorted(Counter(r.code for r in result.rejections).items())),
        "rejections": [r.to_dict() for r in result.rejections],
        "collection": result.summary.get("collection"),
        "city_tables": result.summary["city_tables"],
        "horizons": result.summary["horizons"],
        "ignored_fields": result.summary["ignored_fields"],
        "bronze_countable": not any(r.code == "invalid_json" for r in result.rejections),
        "errors": errors,
    }
    return result, report


def _row_difference(expected, actual, columns):
    """Multiset equality: a duplicate cannot compensate for a missing row."""
    left = Counter(tuple(row[col] for col in columns) for row in expected)
    right = Counter(tuple(row[col] for col in columns) for row in actual)
    return {"missing_or_changed_rows": sum((left - right).values()),
            "unexpected_or_changed_rows": sum((right - left).values())}


def reconcile_source(raw, source_blob, *, tables, publications, quarantine):
    """Compare one Bronze with actual stored rows from all four Silver tables.

    ``tables`` maps business table names to Arrow tables with contract schemas.
    Controls are lists of dictionaries, filtered by canonical ingestion ID.
    A missing publication or quarantine diagnostic is an anomaly even if business
    row counts match. This function only reads its arguments.
    """
    result, report = explain_bronze(raw, source_blob)
    ingestion_id = bronze_identity(source_blob)
    errors = report["errors"]
    for name, schema in TABLE_SCHEMAS.items():
        actual = tables[name]
        if not actual.schema.equals(schema, check_metadata=False):
            errors.append(f"{name}: schema_mismatch")
            continue
        expected_rows, actual_rows = result.tables[name].to_pylist(), actual.to_pylist()
        diff = _row_difference(expected_rows, actual_rows, schema.names)
        report["tables"][name].update(stored_rows=actual.num_rows, **diff)
        if any(diff.values()):
            errors.append(f"{name}: stored_rows_differ")
        keys = Counter(tuple(r[k] for k in TABLE_KEYS[name]) for r in actual_rows)
        if any(n > 1 for n in keys.values()):
            errors.append(f"{name}: duplicate_stored_key")

    expected_quarantine = [dict(
        ingestion_id=ingestion_id, rejection_index=i, source_blob=source_blob,
        source_sha256=result.source_sha256, table_name=r.table, code=r.code,
        city_query=r.city_query, position=r.position, field=r.field, message=r.message,
    ) for i, r in enumerate(result.rejections)]
    q_columns = ("ingestion_id", "rejection_index", "source_blob", "source_sha256",
                 "table_name", "code", "city_query", "position", "field", "message")
    report["quarantine"] = _row_difference(expected_quarantine, quarantine, q_columns)
    if any(report["quarantine"].values()):
        errors.append("quarantine: stored_rejections_differ")

    if len(publications) != 1:
        errors.append("publications: missing_or_duplicate_publication")
    else:
        publication = publications[0]
        expected = {
            "ingestion_id": ingestion_id, "complete": True,
            "schema_version": SCHEMA_VERSION, "transform_version": TRANSFORM_VERSION,
            "source": SOURCE, "source_blob": source_blob, "source_sha256": result.source_sha256,
            "status": result.status, "observation_row_count": result.observations.num_rows,
            "forecast_row_count": result.forecasts.num_rows, "quarantine_count": len(result.rejections),
            "quarantine_sha256": hashlib.sha256(b"".join(
                json_bytes(r) + b"\n" for r in quarantine_records(result, ingestion_id))).hexdigest(),
        }
        for key, value in expected.items():
            if publication.get(key) != value:
                errors.append(f"publications: mismatch:{key}")
        collected = publication.get("collected_at_utc")
        if collected is not None and collected.tzinfo is None:
            collected = collected.replace(tzinfo=timezone.utc)
        if collected != result.collected_at_utc:
            errors.append("publications: mismatch:collected_at_utc")
        try:
            summary = json.loads(publication["summary_json"])
        except (KeyError, TypeError, ValueError):
            summary = None
        if summary != result.summary:
            errors.append("publications: mismatch:summary_json")
    report["ok"] = not errors
    return report
