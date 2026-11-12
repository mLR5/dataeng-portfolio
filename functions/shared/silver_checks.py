"""Engine-independent Silver checks shared by the delta-rs runner and Spark.

No Azure SDK import: this module ships in the Databricks wheel. Changing a check
changes both engines at once, which keeps their published results comparable.
"""
from datetime import date, datetime
import json
import re

from .claim_check import INGESTION_ID_PATTERN
from .silver_contract import SCHEMA_VERSION, SILVER_STATUSES, TRANSFORM_VERSION

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
