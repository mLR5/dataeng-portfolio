"""Boundary cases of the source notebook's Gold dimension transformations."""
from datetime import date, datetime, timezone
import json

from pyspark.sql import types as T

import gold_pipeline as gold


NOW = datetime(2026, 10, 17, 11, tzinfo=timezone.utc)


def _row(table, **changes):
    values = {}
    for field in gold.SPARK_SCHEMAS[table]:
        if field.nullable:
            value = None
        elif isinstance(field.dataType, T.StringType):
            value = ""
        elif isinstance(field.dataType, T.BooleanType):
            value = False
        elif isinstance(field.dataType, T.TimestampType):
            value = NOW
        elif isinstance(field.dataType, T.DateType):
            value = NOW.date()
        elif isinstance(field.dataType, T.DoubleType):
            value = 0.0
        else:
            value = 0
        values[field.name] = value
    values.update({k: v for k, v in {
        "ingestion_id": "collection-a", "city_id": "fr_paris", "city_query": "Paris",
        "source_timezone": "Europe/Paris", "latitude": 48.85, "longitude": 2.35,
        "source_blob": "source-a", "source_sha256": "a" * 64,
        "schema_version": gold.SCHEMA_VERSION, "transform_version": gold.TRANSFORM_VERSION,
    }.items() if k in values})
    if table == gold.PUBLICATIONS:
        values.update(complete=True, status="success", bronze_format="current", summary_json="{}")
    values.update(changes)
    return values


def _inputs(spark, **rows):
    return {name: spark.createDataFrame(rows.get(name, []), gold.SPARK_SCHEMAS[name])
            for name in (*gold.SILVER_DATA_TABLES, gold.PUBLICATIONS, gold.QUARANTINE)}


def test_gold_canonical_hour_prefers_nearest_then_earliest_collection(spark):
    observations = [
        _row(gold.OBSERVATION_TABLE, ingestion_id="later", temperature_c=20.0,
             observed_at_utc=NOW.replace(minute=15), collected_at_utc=NOW.replace(minute=20)),
        _row(gold.OBSERVATION_TABLE, ingestion_id="earlier", temperature_c=10.0,
             observed_at_utc=NOW.replace(hour=10, minute=45), collected_at_utc=NOW.replace(minute=0)),
    ]
    inputs = _inputs(spark, observation_snapshots=observations)
    rows = gold._gold_observations(inputs).collect()
    assert len(rows) == 1
    assert rows[0].ingestion_id == "earlier"
    assert rows[0].distance_to_hour_min == 15.0
    assert rows[0].hour_utc == NOW.replace(tzinfo=None)
    observations.append(_row(gold.OBSERVATION_TABLE, ingestion_id="nearest", temperature_c=15.0,
                              observed_at_utc=NOW, collected_at_utc=NOW.replace(minute=30)))
    assert gold._gold_observations(_inputs(spark, observation_snapshots=observations)).first().ingestion_id == "nearest"


def test_gold_canonical_hour_changes_local_date_at_midnight(spark):
    row = _row(gold.OBSERVATION_TABLE, observed_at_utc=NOW.replace(hour=21, minute=45), temperature_c=14.0)
    actual = gold._gold_observations(_inputs(spark, observation_snapshots=[row])).first()
    assert actual.hour_utc == NOW.replace(hour=22, minute=0, tzinfo=None)
    assert actual.local_date == date(2026, 10, 18)
    assert actual.local_hour == 0


def test_gold_preserves_publication_with_untrusted_collection_date(spark):
    pub = _row(gold.PUBLICATIONS, collected_at_utc=None, status="rejected")
    frame = _inputs(spark, publications=[pub])
    actual = gold._gold_dim_collection(frame[gold.PUBLICATIONS]).first()
    assert actual.ingestion_id == "collection-a"
    assert actual.collected_at_utc is None and actual.collected_date is None
    assert actual.cities_requested == actual.cities_successful == 0


def test_gold_empty_sources_keep_registry_but_no_fabricated_facts(spark):
    results = gold.build_gold_dimensions(spark, _inputs(spark))
    assert results["dim_city"].count() == 3
    for name, frame in results.items():
        if name != "dim_city":
            assert frame.count() == 0, name


def test_gold_collection_city_distinguishes_wrong_location_and_missing_request(spark):
    summary = {
        "cities": {"requested": ["Paris", "Londres"],
                   "collection_successful": ["Paris", "Londres"], "collection_failed": []},
        "city_tables": {"fr_paris": {gold.OBSERVATION_TABLE: {"rejected_rows": 2}}},
        "horizons": {"fr_paris": {"daily_coverage": [{"expected_hour_slots": 23}, {"expected_hour_slots": 24}]}},
    }
    inputs = _inputs(spark,
        publications=[_row(gold.PUBLICATIONS, status="partial", summary_json=json.dumps(summary))],
        observation_snapshots=[_row(gold.OBSERVATION_TABLE, observed_at_utc=NOW, temperature_c=14.0)],
        quarantine=[_row(gold.QUARANTINE, city_query="Londres", scope="row", code="location_mismatch")])
    actual = {r.city_id: r for r in gold._gold_collection_city(spark, inputs).collect()}
    assert actual["fr_paris"].collection_status == "collected"
    assert actual["fr_paris"].forecast_hourly_expected == 47
    assert actual["fr_paris"].rejected_rows == 2
    assert actual["fr_paris"].observation_valid == 1
    assert actual["gb_london"].collection_status == "location_rejected"
    assert actual["be_brussels"].collection_status == "not_requested"


def test_gold_condition_modal_label_tie_and_daily_day_reference(spark):
    inputs = _inputs(spark, observation_snapshots=[
        _row(gold.OBSERVATION_TABLE, observed_at_utc=NOW, temperature_c=14.0,
             condition_code=1000, is_day=False, condition_text="Z"),
        _row(gold.OBSERVATION_TABLE, observed_at_utc=NOW, temperature_c=14.0,
             condition_code=1000, is_day=False, condition_text="A"),
    ], forecast_daily_snapshots=[_row(gold.FORECAST_DAILY_TABLE, target_local_date=NOW.date(),
                                     condition_code=1000, condition_text="Soleil")])
    actual = {(r.condition_code, r.is_day): r for r in gold._gold_dim_condition(inputs).collect()}
    assert actual[(1000, False)].label_fr == "A"
    assert actual[(1000, True)].label_fr == "Soleil"
    assert actual[(1000, True)].category == "clear"
    assert actual[(1000, True)].is_precipitation is False


def test_gold_unknown_condition_does_not_invent_precipitation_verdict(spark):
    inputs = _inputs(spark, observation_snapshots=[_row(gold.OBSERVATION_TABLE,
        observed_at_utc=NOW, temperature_c=14.0, condition_code=9999, is_day=True)])
    actual = gold._gold_dim_condition(inputs).first()
    assert actual.category == "unknown"
    assert actual.is_precipitation is None


def test_gold_calendar_includes_intermediate_days_and_iso_weekdays(spark):
    inputs = _inputs(spark, publications=[_row(gold.PUBLICATIONS, collected_at_utc=NOW)],
                     forecast_daily_snapshots=[_row(gold.FORECAST_DAILY_TABLE, target_local_date=date(2026, 10, 20))])
    calendar = gold._gold_dim_date(inputs, gold._gold_observations(inputs)).orderBy("date").collect()
    assert [r.date for r in calendar] == [date(2026, 10, d) for d in (17, 18, 19, 20)]
    assert (calendar[0].day_of_week, calendar[0].day_name, calendar[0].is_weekend) == (6, "samedi", True)
    assert (calendar[2].day_of_week, calendar[2].day_name, calendar[2].is_weekend) == (1, "lundi", False)


def test_gold_city_first_collection_comes_from_any_valid_table(spark):
    first = NOW.replace(day=19)
    later = NOW.replace(day=20)
    inputs = _inputs(spark,
        observation_snapshots=[_row(gold.OBSERVATION_TABLE, city_id="gb_london", city_query="Londres",
            ingestion_id="later-collection", source_timezone="Europe/London", collected_at_utc=later, observed_at_utc=later,
            latitude=51.51, longitude=-0.12, temperature_c=14.0)],
        forecast_daily_snapshots=[_row(gold.FORECAST_DAILY_TABLE, city_id="gb_london", city_query="Londres",
            ingestion_id="first-collection", source_timezone="Europe/London", collected_at_utc=first, target_local_date=first.date(),
            latitude=51.50, longitude=-0.11)],
        quarantine=[_row(gold.QUARANTINE, city_query="Londres", scope="row", code="location_mismatch")])
    rows = gold._gold_dim_city(spark, inputs).orderBy("city_id", "valid_from_utc").collect()
    london = [r for r in rows if r.city_id == "gb_london"]
    # Origin version without validated coordinates, then one version per change.
    assert [(r.valid_from_utc, r.latitude, r.longitude) for r in london] == [
        (gold.GOLD_CITY_VALID_FROM_ORIGIN.replace(tzinfo=None), None, None),
        (first.replace(tzinfo=None), 51.50, -0.11),
        (later.replace(tzinfo=None), 51.51, -0.12),
    ]
    assert {r.first_collected_at_utc for r in london} == {first.replace(tzinfo=None)}
    assert all(r.timezone == "Europe/London" for r in london)
    paris = [r for r in rows if r.city_id == "fr_paris"]
    assert len(paris) == 1 and paris[0].first_collected_at_utc is None and paris[0].is_current


def test_gold_collection_failed_is_not_a_geography_rejection(spark):
    summary = {"cities": {"requested": ["London"], "collection_successful": [],
                           "collection_failed": ["London"]}}
    inputs = _inputs(spark,
        publications=[_row(gold.PUBLICATIONS, status="empty", summary_json=json.dumps(summary))],
        quarantine=[_row(gold.QUARANTINE, city_query="Londres", scope="row", code="collection_failed")])
    rows = {r.city_id: r for r in gold._gold_collection_city(spark, inputs).collect()}
    assert rows["gb_london"].collection_status == "collection_failed"
    assert rows["gb_london"].observation_valid == rows["gb_london"].forecast_hourly_valid == 0
