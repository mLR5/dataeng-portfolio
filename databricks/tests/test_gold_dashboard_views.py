"""Stable dashboard SQL reads published versions with an explicit query clock."""
from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from pyspark.sql import types as T

import gold_pipeline as gold


AS_OF = datetime(2026, 11, 15, 23, 30, tzinfo=timezone.utc)
SOURCE_TABLES = ("dim_city", "fact_observation_hourly", "fact_forecast_hourly", "fact_forecast_daily")


def _instant(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc)


def _row(table, **values):
    defaults = {
        T.StringType: "fixture", T.BooleanType: True, T.ShortType: 0,
        T.IntegerType: 0, T.LongType: 0, T.DoubleType: 0.0,
        T.DateType: date(2026, 11, 15), T.TimestampType: AS_OF,
    }
    result = {field.name: None if field.nullable else defaults[type(field.dataType)]
              for field in gold.GOLD_SCHEMAS[table]}
    result.update(values)
    return result


@pytest.mark.parametrize("schema", [None, "gold", "cat.gold.extra", "cat.gold;DROP TABLE x",
                                       "cat.`gold`", "cat.gold\n", "cat.gold --", "cat.bad-name"])
def test_dashboard_source_schema_is_an_identifier(schema):
    with pytest.raises(ValueError, match="catalog.schema"):
        gold.gold_dashboard_view_queries(schema)


@pytest.mark.parametrize("bad_version", [-1, True, 1.5, "0 UNION SELECT 1", None])
def test_dashboard_snapshot_versions_are_nonnegative_integers(bad_version):
    versions = dict.fromkeys(SOURCE_TABLES, 0)
    versions["dim_city"] = bad_version
    with pytest.raises(ValueError, match="non-negative integers"):
        gold.gold_dashboard_view_queries("project.gold", source_versions=versions)


def test_dashboard_rejects_partial_manifest_and_arbitrary_clock_sql():
    with pytest.raises(ValueError, match="all dashboard source tables"):
        gold.gold_dashboard_view_queries("project.gold", source_versions={"dim_city": 0})
    with pytest.raises(ValueError, match="unknown Gold table"):
        gold.gold_dashboard_view_queries("project.gold", source_versions={**dict.fromkeys(SOURCE_TABLES, 0), "typo": 0})
    with pytest.raises(ValueError, match="mapping"):
        gold.gold_dashboard_view_queries("project.gold", source_versions=[])
    for clock in (datetime(2026, 11, 15), "current_timestamp(); DROP TABLE x"):
        with pytest.raises(ValueError, match="timezone-aware datetime"):
            gold.gold_dashboard_view_queries("project.gold", as_of=clock)
    bodies = gold.gold_dashboard_view_queries("project.gold")
    assert set(bodies) == {"v_current_weather", "v_latest_forecast_hourly", "v_latest_forecast_daily"}
    assert all("current_timestamp()" in body for body in bodies.values())


def test_persistent_dashboard_views_read_frozen_delta_versions_and_local_dates(delta_spark, tmp_path):
    spark = delta_spark
    spark.conf.set("spark.sql.session.timeZone", "UTC")
    database = "dashboard_views_" + uuid4().hex
    qualified = f"spark_catalog.{database}"
    spark.sql(f"CREATE DATABASE {database} LOCATION '{tmp_path.as_uri()}/tables'")
    try:
        cities = [_row("dim_city", city_id=city, city_name=name, country_name=country, timezone=zone)
                  for city, name, country, zone in (
                      ("fr_paris", "Paris", "France", "Europe/Paris"),
                      ("gb_london", "Londres", "United Kingdom", "Europe/London"),
                      ("be_brussels", "Bruxelles", "Belgium", "Europe/Brussels"))]
        observations = [
            _row("fact_observation_hourly", city_id="fr_paris", ingestion_id="paris-old",
                 observed_at_utc=_instant(15, 22, 15), collected_at_utc=_instant(15, 22, 20),
                 hour_utc=_instant(15, 22), temperature_c=10.0),
            _row("fact_observation_hourly", city_id="fr_paris", ingestion_id="paris-new",
                 observed_at_utc=_instant(15, 23, 15), collected_at_utc=_instant(15, 23, 20),
                 hour_utc=_instant(15, 23), temperature_c=12.0, uv_index=2.5),
            _row("fact_observation_hourly", city_id="fr_paris", ingestion_id="not-yet-collected",
                 observed_at_utc=_instant(15, 23, 25), collected_at_utc=_instant(16, 0),
                 hour_utc=_instant(16, 0), temperature_c=99.0),
            _row("fact_observation_hourly", city_id="gb_london", ingestion_id="london-old",
                 observed_at_utc=_instant(14, 20), collected_at_utc=_instant(14, 20, 5),
                 hour_utc=_instant(14, 20), temperature_c=8.0),
        ]
        hourly = [
            _row("fact_forecast_hourly", city_id="fr_paris", target_hour_utc=_instant(16, 0),
                 target_local_date=date(2026, 11, 16), issued_at_utc=_instant(15, 22),
                 ingestion_id="older", temperature_c=13.0),
            _row("fact_forecast_hourly", city_id="fr_paris", target_hour_utc=_instant(16, 0),
                 target_local_date=date(2026, 11, 16), issued_at_utc=_instant(15, 23, 20),
                 ingestion_id="latest", temperature_c=15.0),
            _row("fact_forecast_hourly", city_id="fr_paris", target_hour_utc=_instant(16, 0),
                 target_local_date=date(2026, 11, 16), issued_at_utc=_instant(15, 23, 40),
                 ingestion_id="future-issued", temperature_c=99.0),
            _row("fact_forecast_hourly", city_id="fr_paris", target_hour_utc=_instant(15, 23),
                 target_local_date=date(2026, 11, 15), issued_at_utc=_instant(15, 22),
                 ingestion_id="past-target", temperature_c=99.0),
        ]
        daily = [
            _row("fact_forecast_daily", city_id="fr_paris", target_local_date=date(2026, 11, 15),
                 issued_at_utc=_instant(15, 22), ingestion_id="paris-local-yesterday", lead_days=0),
            _row("fact_forecast_daily", city_id="fr_paris", target_local_date=date(2026, 11, 16),
                 issued_at_utc=_instant(15, 22), ingestion_id="paris-daily-old", lead_days=1),
            _row("fact_forecast_daily", city_id="fr_paris", target_local_date=date(2026, 11, 16),
                 issued_at_utc=_instant(15, 23, 20), ingestion_id="paris-daily-latest", lead_days=1),
            _row("fact_forecast_daily", city_id="fr_paris", target_local_date=date(2026, 11, 16),
                 issued_at_utc=_instant(15, 23, 25), ingestion_id="paris-daily-negative", lead_days=-1),
            _row("fact_forecast_daily", city_id="fr_paris", target_local_date=date(2026, 11, 16),
                 issued_at_utc=_instant(16, 0), ingestion_id="paris-daily-future-issued", lead_days=0),
            _row("fact_forecast_daily", city_id="gb_london", target_local_date=date(2026, 11, 15),
                 issued_at_utc=_instant(15, 22), ingestion_id="london-local-today", lead_days=0),
        ]
        data = {"dim_city": cities, "fact_observation_hourly": observations,
                "fact_forecast_hourly": hourly, "fact_forecast_daily": daily}
        for name, rows in data.items():
            (spark.createDataFrame(rows, gold.GOLD_SCHEMAS[name]).write.format("delta")
             .option("delta.enableDeletionVectors", "false").saveAsTable(f"{qualified}.{name}"))
        versions = dict.fromkeys(SOURCE_TABLES, 0)
        definitions = gold.gold_dashboard_view_queries(qualified, source_versions=versions, as_of=AS_OF)
        for name, body in definitions.items():
            spark.sql(f"CREATE VIEW {qualified}.{name} AS {body}")

        weather = {row.city_id: row for row in spark.table(f"{qualified}.v_current_weather").collect()}
        assert set(weather) == {"fr_paris", "gb_london"}  # No invented observation for Brussels.
        assert weather["fr_paris"].ingestion_id == "paris-new"
        assert weather["fr_paris"].city_name == "Paris"
        assert weather["fr_paris"].timezone == "Europe/Paris"
        assert weather["fr_paris"].uv_index == 2.5
        assert weather["fr_paris"].observation_age_hours == pytest.approx(0.25)
        assert weather["fr_paris"].is_stale is False
        assert weather["gb_london"].observation_age_hours == pytest.approx(27.5)
        assert weather["gb_london"].is_stale is True

        latest = spark.table(f"{qualified}.v_latest_forecast_hourly").collect()
        assert len(latest) == 1
        assert latest[0].ingestion_id == "latest"
        assert latest[0].temperature_c == 15.0
        assert latest[0].forecast_age_hours == pytest.approx(1 / 6)
        latest_daily = {row.city_id: row for row in spark.table(f"{qualified}.v_latest_forecast_daily").collect()}
        assert latest_daily["fr_paris"].ingestion_id == "paris-daily-latest"
        assert latest_daily["gb_london"].ingestion_id == "london-local-today"

        # Persisted views must retain their creation-time UTC SQL configuration,
        # including city-local day boundaries, when a client's session uses another zone.
        spark.conf.set("spark.sql.session.timeZone", "America/New_York")
        try:
            other_weather = {row.city_id: row for row in spark.table(f"{qualified}.v_current_weather").collect()}
            other_hourly = spark.table(f"{qualified}.v_latest_forecast_hourly").collect()
            other_daily = {row.city_id: row for row in spark.table(f"{qualified}.v_latest_forecast_daily").collect()}
            assert other_weather == weather
            assert other_hourly == latest
            assert other_daily == latest_daily
        finally:
            spark.conf.set("spark.sql.session.timeZone", "UTC")

        # A newer physical version must not leak through a view published at version 0.
        later = _row("fact_forecast_hourly", city_id="fr_paris", target_hour_utc=_instant(16, 0),
                     target_local_date=date(2026, 11, 16), issued_at_utc=_instant(15, 23, 25),
                     ingestion_id="unpublished", temperature_c=22.0)
        (spark.createDataFrame([later], gold.GOLD_SCHEMAS["fact_forecast_hourly"]).write
         .format("delta").mode("append").saveAsTable(f"{qualified}.fact_forecast_hourly"))
        assert spark.table(f"{qualified}.v_latest_forecast_hourly").collect()[0].temperature_c == 15.0
        unfrozen = gold.gold_dashboard_view_queries(qualified, as_of=AS_OF)
        assert spark.sql(unfrozen["v_latest_forecast_hourly"]).collect()[0].temperature_c == 22.0
    finally:
        spark.sql(f"DROP DATABASE IF EXISTS {database} CASCADE")
