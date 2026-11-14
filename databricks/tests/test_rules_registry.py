"""Non-omission registry: every Bronze JSON path has a decision, unknown ones surface."""
import copy
import json
import re

import pytest

from conftest import FIXTURES
from silver_rules import (FIELD_DECISIONS, REGISTRY_DECISIONS, TABLE_SCHEMAS, decisions_table,
                          unknown_paths)

LOCATION = "cities[].data.location."
CURRENT = "cities[].data.current."
FORECASTDAY = "cities[].data.forecast.forecastday[]."
DAY = FORECASTDAY + "day."
ASTRO = FORECASTDAY + "astro."
HOUR = FORECASTDAY + "hour[]."

# Scalar leaves observed on a 52-archive Bronze sample (current envelope shape),
# copied here so the test does not depend on the sample itself.
PROFILE_SECTIONS = (
    ("", """
        cities[].city cities[].status forecast_request.days ingestion_id ingestion_timestamp
        status summary.failed summary.successful summary.total_cities
    """),
    (LOCATION, """
        country lat localtime localtime_epoch lon name region tz_id
    """),
    (CURRENT, """
        chance_of_rain chance_of_snow cloud condition.code condition.icon condition.text
        feelslike_c feelslike_f gust_kph gust_mph humidity is_day last_updated
        last_updated_epoch precip_in precip_mm pressure_in pressure_mb temp_c temp_f uv vis_km
        vis_miles will_it_rain will_it_snow wind_degree wind_dir wind_kph wind_mph
    """),
    (FORECASTDAY, """
        date date_epoch
    """),
    (DAY, """
        avghumidity avgtemp_c avgtemp_f avgvis_km avgvis_miles avgwetbulb_c avgwetbulb_f
        condition.code condition.icon condition.text daily_chance_of_rain daily_chance_of_snow
        daily_will_it_rain daily_will_it_snow maxtemp_c maxtemp_f maxwetbulb_c maxwetbulb_f
        maxwind_kph maxwind_mph mintemp_c mintemp_f totalprecip_in totalprecip_mm totalsnow_cm
        uv
    """),
    (ASTRO, """
        is_moon_up is_sun_up moon_illumination moon_phase moonrise moonset sunrise sunset
    """),
    (HOUR, """
        chance_of_rain chance_of_snow cloud condition.code condition.icon condition.text
        dewpoint_c dewpoint_f feelslike_c feelslike_f gust_kph gust_mph heatindex_c heatindex_f
        humidity is_day precip_in precip_mm pressure_in pressure_mb snow_cm temp_c temp_f time
        time_epoch uv vis_km vis_miles wetbulb_c wetbulb_f will_it_rain will_it_snow wind_degree
        wind_dir wind_kph wind_mph windchill_c windchill_f
    """),
)
PROFILE_PATHS = sorted(prefix + name for prefix, names in PROFILE_SECTIONS for name in names.split())
BRONZE_FIXTURES = sorted(path for path in FIXTURES.glob("*.json")
                         if not path.name.endswith(".provenance.json") and path.name != "real_bronze_profiles.json")


def _scalar_paths(value, path=""):
    """Independent reference walk (recursive) used to check the registry."""
    if isinstance(value, dict):
        return set().union(*(_scalar_paths(v, f"{path}.{k}" if path else k) for k, v in value.items()))
    if isinstance(value, list):
        return set().union(*(_scalar_paths(item, path + "[]") for item in value))
    return {path}


def _fixture(name="bronze_success_20261018_derived_real.json"):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_profile_is_the_expected_sample():
    assert len(PROFILE_PATHS) == len(set(PROFILE_PATHS)) == 120


@pytest.mark.parametrize("pattern", PROFILE_PATHS)
def test_every_profiled_bronze_path_has_a_decision(pattern):
    assert pattern in FIELD_DECISIONS


def test_registry_holds_no_pattern_outside_the_observed_paths():
    observed = set(PROFILE_PATHS)
    for path in BRONZE_FIXTURES:
        observed |= _scalar_paths(json.loads(path.read_text(encoding="utf-8")))
    assert set(FIELD_DECISIONS) == observed


@pytest.mark.parametrize("path", BRONZE_FIXTURES, ids=lambda path: path.name)
def test_repository_fixtures_have_no_unknown_path(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    assert _scalar_paths(data) <= set(FIELD_DECISIONS)
    assert unknown_paths(data) == []


def test_complete_real_response_profiles_are_covered():
    # real_bronze_profiles.json describes the complete original responses (before reduction).
    profiles = json.loads((FIXTURES / "real_bronze_profiles.json").read_text(encoding="utf-8"))
    scalars = {key.removeprefix("$.") for profile in profiles for key, shape in profile["shape"].items()
               if key != "$" and not set(shape["types"]) <= {"dict", "list"}}
    assert scalars and scalars <= set(FIELD_DECISIONS)


def test_invented_field_is_reported_once_across_cities():
    data = _fixture()
    assert len(data["cities"]) > 1
    for position, city in enumerate(data["cities"]):
        city["data"]["current"]["new_metric"] = position
    assert unknown_paths(data) == [CURRENT + "new_metric"]


def test_lists_of_different_lengths_do_not_duplicate_patterns():
    data = _fixture()
    days = data["cities"][0]["data"]["forecast"]["forecastday"]
    days[0]["hour"] = days[0]["hour"][:1]
    days[1]["hour"] = days[1]["hour"] * 3
    for forecastday in days:
        for hour in forecastday["hour"]:
            hour["new_metric"] = 1.0
    data["cities"][1]["data"]["forecast"]["forecastday"] = days[:1]
    assert unknown_paths(data) == [HOUR + "new_metric"]


def test_unknown_paths_are_sorted_and_cover_every_level():
    data = _fixture()
    data["zz_top"] = "x"
    data["summary"]["skipped"] = 0
    data["cities"][0]["data"]["forecast"]["forecastday"][0]["day"]["condition"]["new_code"] = 1
    data["cities"][0]["data"]["alerts"] = {"alert": [{"headline": "storm", "areas": ["a", "b"]}]}
    assert unknown_paths(data) == [
        "cities[].data.alerts.alert[].areas[]", "cities[].data.alerts.alert[].headline",
        DAY + "condition.new_code", "summary.skipped", "zz_top",
    ]


def test_null_unknown_field_is_a_scalar_leaf():
    data = _fixture()
    data["cities"][0]["data"]["current"]["new_metric"] = None
    assert unknown_paths(data) == [CURRENT + "new_metric"]


def test_new_empty_container_is_reported_as_itself():
    data = _fixture()
    data["cities"][0]["data"]["current"]["new_block"] = {}
    data["cities"][1]["data"]["alerts"] = {"alert": []}
    assert unknown_paths(data) == ["cities[].data.alerts.alert", CURRENT + "new_block"]


def test_new_container_with_leaves_reports_only_its_leaves():
    data = _fixture()
    data["cities"][0]["data"]["current"]["new_block"] = {"value": 1}
    data["cities"][1]["data"]["current"]["new_block"] = {}
    assert unknown_paths(data) == [CURRENT + "new_block", CURRENT + "new_block.value"]


def test_known_paths_with_unexpected_types_are_left_to_the_quality_checks():
    data = _fixture()
    city = data["cities"][0]["data"]
    city["current"]["condition"] = None
    city["current"]["uv"] = {}
    city["forecast"]["forecastday"][0]["hour"] = []
    data["cities"][1]["data"] = None  # failed collection
    assert unknown_paths(data) == []


def test_known_scalar_turned_container_surfaces_its_new_leaves():
    data = _fixture()
    data["cities"][0]["data"]["current"]["uv"] = {"value": 2.0, "band": ["low"]}
    assert unknown_paths(data) == [CURRENT + "uv.band[]", CURRENT + "uv.value"]


@pytest.mark.parametrize("value", [None, [], [{"cities": []}], "cities", 42, 1.5, b"{}", ({},)])
def test_non_dict_input_returns_empty_list(value):
    assert unknown_paths(value) == []


def test_odd_input_never_raises():
    cyclic = {"cities": []}
    cyclic["cities"].append(cyclic)
    deep = {}
    node = deep
    for _ in range(3000):
        node["x"] = {}
        node = node["x"]
    node["leaf"] = 1
    assert unknown_paths({1: "a", None: {"b": 2}, ("t",): []}) == ["('t',)", "1", "None.b"]
    assert unknown_paths(cyclic) == []
    assert unknown_paths(deep) == ["x" + ".x" * 2999 + ".leaf"]
    assert unknown_paths({}) == []


def test_input_is_not_modified():
    data = _fixture()
    data["cities"][0]["data"]["current"]["new_metric"] = 1
    before = copy.deepcopy(data)
    unknown_paths(data)
    assert data == before


def test_every_decision_is_known_and_explained():
    for pattern, entry in FIELD_DECISIONS.items():
        assert entry["decision"] in REGISTRY_DECISIONS, pattern
        text_key = "reason" if entry["decision"] in ("derivable", "excluded") else "destination"
        assert set(entry) == {"decision", text_key}, pattern
        assert entry[text_key].strip(), pattern


def test_registry_patterns_are_leaves_only():
    patterns = set(FIELD_DECISIONS)
    for pattern in patterns:
        assert not any(other.startswith((pattern + ".", pattern + "[]")) for other in patterns), pattern


TABLE_COLUMN = re.compile(r"(\*|" + "|".join(TABLE_SCHEMAS) + r")\.([a-z_]+)")


def test_destinations_name_existing_silver_columns():
    for pattern, entry in FIELD_DECISIONS.items():
        references = TABLE_COLUMN.findall(entry.get("destination", ""))
        if entry["decision"] in ("kept", "added", "represented"):
            assert references, pattern
        for table, column in references:
            schemas = TABLE_SCHEMAS.values() if table == "*" else [TABLE_SCHEMAS[table]]
            assert all(column in schema.names for schema in schemas), (pattern, table, column)


def test_every_new_silver_v2_column_is_fed_by_an_added_path():
    fed = {(table, column) for entry in FIELD_DECISIONS.values() if entry["decision"] == "added"
           for table, column in TABLE_COLUMN.findall(entry["destination"])}
    for column in ("uv_index", "wind_gust_kph", "visibility_km"):
        assert ("observation_snapshots", column) in fed
    for column in ("uv_index", "wind_gust_kph", "visibility_km", "dew_point_c", "snowfall_cm",
                   "heat_index_c", "wind_chill_c", "wet_bulb_c"):
        assert ("forecast_hourly_snapshots", column) in fed
    daily = TABLE_SCHEMAS["forecast_daily_snapshots"].names
    own = daily[daily.index("target_local_date"):]
    assert {column for table, column in fed if table == "forecast_daily_snapshots"} == set(own)


@pytest.mark.parametrize(("pattern", "decision"), [
    (CURRENT + "uv", "added"), (CURRENT + "gust_kph", "added"), (CURRENT + "vis_km", "added"),
    (HOUR + "dewpoint_c", "added"), (HOUR + "snow_cm", "added"), (HOUR + "heatindex_c", "added"),
    (HOUR + "windchill_c", "added"), (HOUR + "wetbulb_c", "added"),
    (DAY + "maxwetbulb_c", "added"), (DAY + "daily_will_it_snow", "added"),
    (ASTRO + "sunrise", "added"), (ASTRO + "sunset", "added"),
    (DAY + "maxtemp_f", "derivable"), (DAY + "avgvis_miles", "derivable"), (HOUR + "wetbulb_f", "derivable"),
    (CURRENT + "vis_miles", "derivable"), (HOUR + "wind_dir", "derivable"),
    (CURRENT + "condition.icon", "derivable"), (DAY + "condition.icon", "derivable"),
    (ASTRO + "moon_phase", "excluded"), (ASTRO + "is_sun_up", "excluded"), (ASTRO + "is_moon_up", "excluded"),
    (HOUR + "will_it_rain", "excluded"), (HOUR + "will_it_snow", "excluded"),
    (CURRENT + "chance_of_rain", "excluded"), (CURRENT + "will_it_snow", "excluded"),
    (CURRENT + "last_updated", "represented"), (CURRENT + "last_updated_epoch", "represented"),
    (HOUR + "time", "represented"), (HOUR + "time_epoch", "represented"),
    (FORECASTDAY + "date_epoch", "represented"), (LOCATION + "localtime_epoch", "represented"),
    (LOCATION + "region", "control"), (LOCATION + "localtime", "control"), ("forecast_request.days", "control"),
    ("status", "structural"), ("summary.total_cities", "structural"), ("ingestion_id", "structural"),
    ("ingestion_timestamp", "structural"), ("cities[].status", "structural"),
    (HOUR + "chance_of_rain", "kept"), (CURRENT + "temp_c", "kept"), (LOCATION + "tz_id", "kept"),
])
def test_documented_silver_v2_decisions(pattern, decision):
    assert FIELD_DECISIONS[pattern]["decision"] == decision


def test_imperial_variants_are_all_derivable():
    imperial = [p for p in FIELD_DECISIONS if re.search(r"(_f|_mph|_in|_miles)$", p)]
    assert len(imperial) == 26
    assert all(FIELD_DECISIONS[p]["decision"] == "derivable" for p in imperial)


def test_decisions_table_documents_every_pattern_in_order():
    rows = decisions_table()
    assert [row[0] for row in rows] == sorted(FIELD_DECISIONS)
    assert all(len(row) == 3 and row[1] in REGISTRY_DECISIONS and row[2] for row in rows)
