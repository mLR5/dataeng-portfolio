"""Contract tests use clearly synthetic data; no WeatherAPI requests are made."""
from dataclasses import asdict
from datetime import date, datetime, timezone
import json

from deltalake import DeltaTable, write_deltalake
import pyarrow as pa
import pytest

from shared.silver_cities import CITIES, CityResolutionError, city_by_query, resolve_city
from shared.silver_contract import (
    FORECAST_KEYS, FORECAST_SCHEMA, FORECAST_TABLE, OBSERVATION_KEYS,
    OBSERVATION_SCHEMA, OBSERVATION_TABLE, Rejection, SCHEMA_VERSION,
    SOURCE, TABLE_KEYS, TABLE_SCHEMAS, TRANSFORM_VERSION, TransformResult, empty_table,
)


def synthetic_row(table_name):
    """Artificial weather values exercise types, not real observed conditions."""
    collected = datetime(2026, 10, 18, 14, 0, 0, 123456, tzinfo=timezone.utc)
    row = {
        "schema_version": SCHEMA_VERSION, "transform_version": TRANSFORM_VERSION,
        "source": SOURCE, "ingestion_id": "20261018_140000_" + "a" * 32,
        "collected_at_utc": collected, "ingestion_date": date(2026, 10, 18),
        "city_id": "fr_paris", "city_query": "Paris", "source_location_name": "Paris",
        "source_country": "France", "latitude": 48.85, "longitude": 2.35,
        "source_timezone": "Europe/Paris", "source_blob": "weather/weatherapi/synthetic.json",
        "source_sha256": "a" * 64, "temperature_c": 12.25,
        "feels_like_c": None, "humidity_pct": 82, "pressure_mb": 1012.0,
        "wind_kph": 10.8, "wind_degree": 210, "precipitation_mm": 0.0,
        "cloud_pct": 50, "condition_code": 1003, "condition_text": "Synthetic cloud",
        "is_day": True,
    }
    if table_name == OBSERVATION_TABLE:
        row["observed_at_utc"] = collected
    else:
        row.update(target_at_utc=collected, target_local_date=date(2026, 10, 18),
                   lead_time_hours=0.0, target_is_past_at_collection=False,
                   chance_of_rain_pct=20, chance_of_snow_pct=None)
    return row


@pytest.mark.parametrize("name", list(TABLE_SCHEMAS))
def test_explicit_contract_has_complete_keys_and_utc(name):
    schema = TABLE_SCHEMAS[name]
    assert len(schema.names) == len(set(schema.names))
    for key in TABLE_KEYS[name]:
        assert schema.field(key).nullable is False
    assert schema.field("collected_at_utc").type == pa.timestamp("us", tz="UTC")
    assert schema.field("ingestion_date").type == pa.date32()
    assert schema.field("temperature_c").nullable is False
    assert schema.field("humidity_pct").nullable is True
    assert empty_table(name).schema.equals(schema)
    assert empty_table(name).num_rows == 0


@pytest.mark.parametrize("name", list(TABLE_SCHEMAS))
def test_create_two_real_delta_tables_roundtrip_schema_utc_and_merge(tmp_path, name):
    """Actual delta-rs creates logs/Parquet, then reads and merges; not a mock."""
    schema = TABLE_SCHEMAS[name]
    table_path = tmp_path / name
    delta = DeltaTable.create(table_path, schema=schema)
    assert (table_path / "_delta_log" / "00000000000000000000.json").is_file()
    assert delta.to_pyarrow_table().num_rows == 0
    assert delta.protocol().min_reader_version == 1
    assert delta.protocol().min_writer_version == 2
    data = pa.Table.from_pylist([synthetic_row(name)], schema=schema)
    write_deltalake(table_path, data, mode="append")
    delta = DeltaTable(table_path)
    actual = delta.to_pyarrow_table()
    assert actual.schema.equals(schema, check_metadata=False)
    assert actual.to_pylist() == data.to_pylist()
    predicate = " AND ".join(f"target.{key} = incoming.{key}" for key in TABLE_KEYS[name])
    delta.merge(data, predicate=predicate, source_alias="incoming", target_alias="target").when_not_matched_insert_all().execute()
    assert DeltaTable(table_path).to_pyarrow_table().num_rows == 1
    assert DeltaTable(table_path).to_pyarrow_table().to_pylist() == data.to_pylist()


def test_snapshot_keys_preserve_collection_history():
    assert OBSERVATION_KEYS == ("ingestion_id", "city_id", "observed_at_utc")
    assert FORECAST_KEYS == ("ingestion_id", "city_id", "target_at_utc")
    assert "collected_at_utc" not in FORECAST_KEYS  # ingestion_id identifies the collection.


@pytest.mark.parametrize("query,city_id", [("Paris", "fr_paris"), ("  LONDRES ", "gb_london"),
    ("London", "gb_london"), ("Bruxelles", "be_brussels"), ("Brussel", "be_brussels")])
def test_registry_aliases_are_stable(query, city_id):
    assert city_by_query(query).city_id == city_id


def test_registry_does_not_generate_unknown_city_ids():
    assert city_by_query("Paris, Texas") is None
    with pytest.raises(CityResolutionError) as exc:
        resolve_city("Paris, Texas", {})
    assert exc.value.code == "unknown_city"


@pytest.mark.parametrize("city", CITIES)
def test_registry_accepts_geography_without_matching_translated_source_name(city):
    location = {"name": "Provider label may vary", "tz_id": city.timezone,
                "country": city.country_aliases[0],
                "lat": sum(city.latitude_bounds) / 2, "lon": sum(city.longitude_bounds) / 2}
    assert resolve_city(city.query_aliases[0], location) == city


@pytest.mark.parametrize("update", [{"lat": 0}, {"lon": float("nan")}, {"lat": True},
    {"tz_id": "America/Chicago"}, {"country": "United States"}, {"lon": None}])
def test_registry_rejects_misresolved_city(update):
    location = {"name": "Paris", "tz_id": "Europe/Paris", "country": "France", "lat": 48.85, "lon": 2.35}
    location.update(update)
    with pytest.raises(CityResolutionError) as exc:
        resolve_city("Paris", location)
    assert exc.value.code == "location_mismatch"


def test_rejected_result_retains_provenance_without_business_rows():
    rejection = Rejection(table="envelope", code="invalid_json", message="Invalid JSON")
    result = TransformResult(observations=empty_table(OBSERVATION_TABLE),
        forecasts=empty_table(FORECAST_TABLE), rejections=(rejection,),
        summary={"reason": "invalid_json"}, status="rejected", ingestion_id=None,
        collected_at_utc=None, source_blob="weather/weatherapi/synthetic.json", source_sha256="f" * 64)
    assert result.tables[OBSERVATION_TABLE] is result.observation_snapshots
    assert result.tables[FORECAST_TABLE] is result.forecast_hourly_snapshots
    assert result.source_sha256 == "f" * 64
    assert json.loads(json.dumps(rejection.to_dict())) == asdict(rejection)


def test_versioned_fixture_explicitly_declares_synthetic_provenance():
    from pathlib import Path
    fixtures = Path(__file__).parent / "fixtures" / "weatherapi"
    provenance = json.loads((fixtures / "synthetic_minimal.provenance.json").read_text())
    payload = json.loads((fixtures / "synthetic_minimal.json").read_text())
    assert provenance["kind"] == "synthetic"
    assert len(payload["cities"]) == provenance["expected_cities"] == 3
    for entry in payload["cities"]:
        city = resolve_city(entry["city"], entry["data"]["location"])
        assert city.city_id in {"fr_paris", "gb_london", "be_brussels"}
        assert len(entry["data"]["forecast"]["forecastday"][0]["hour"]) == 2


@pytest.mark.parametrize("day", ["20261017", "20261018"])
def test_real_bronze_derived_fixture_preserves_wrong_city_and_verified_shape(day):
    """Real supplier issue must remain visible, never relabel Argentina as London."""
    from pathlib import Path
    import hashlib
    fixtures = Path(__file__).parent / "fixtures" / "weatherapi"
    stem = f"bronze_success_{day}_derived_real"
    raw = (fixtures / f"{stem}.json").read_bytes()
    provenance = json.loads((fixtures / f"{stem}.provenance.json").read_text())
    payload = json.loads(raw)
    assert provenance["kind"] == "derived_from_real_bronze"
    assert hashlib.sha256(raw).hexdigest() == provenance["derived_sha256"]
    for entry in payload["cities"]:
        days = entry["data"]["forecast"]["forecastday"]
        assert len(days) == 3
        assert [len(item["hour"]) for item in days] == [2, 2, 2]
        location = entry["data"]["location"]
        assert isinstance(entry["data"]["current"]["temp_c"], float)
        assert isinstance(entry["data"]["current"]["condition"]["code"], int)
        if entry["city"] == "Londres":
            assert location["tz_id"] == "America/Argentina/Catamarca"
            assert location["country"] == "Argentina"
            with pytest.raises(CityResolutionError) as exc:
                resolve_city(entry["city"], location)
            assert exc.value.code == "location_mismatch"
        else:
            assert resolve_city(entry["city"], location).city_id in {"fr_paris", "be_brussels"}
    profiles = json.loads((fixtures / "real_bronze_profiles.json").read_text())
    profile = next(p for p in profiles if p["source_sha256"] == provenance["original_source_sha256"])
    assert profile["source_blob"] == provenance["original_source_blob"]
    assert all(city["forecast_days"] == 3 and city["hours_per_day"] == [24, 24, 24] for city in profile["cities"])
    shape = profile["shape"]
    for field in ("$.cities[].data.current.temp_c", "$.cities[].data.forecast.forecastday[].hour[].time_epoch"):
        assert shape[field]["null_count"] == 0
