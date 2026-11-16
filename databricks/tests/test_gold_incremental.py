"""Incremental Gold building blocks without Delta: window, SCD 2, SCD 1 counts, modes."""
from datetime import date, datetime, timedelta, timezone
import re
from types import SimpleNamespace

import pytest

import gold_pipeline as gold
from test_gold_dimensions import _inputs, _row


T0 = datetime(2026, 10, 17, 22, 10, tzinfo=timezone.utc)
ORIGIN = gold.GOLD_CITY_VALID_FROM_ORIGIN


def _naive(value):
    return value.astimezone(timezone.utc).replace(tzinfo=None)


# --- Affected window ---------------------------------------------------------

def _window_inputs(spark):
    later = T0 + timedelta(hours=5)
    return _inputs(spark,
        observation_snapshots=[
            _row(gold.OBSERVATION_TABLE, collected_at_utc=T0, observed_at_utc=T0 - timedelta(minutes=5), temperature_c=1.0),
            _row(gold.OBSERVATION_TABLE, ingestion_id="b", collected_at_utc=later,
                 observed_at_utc=later - timedelta(minutes=5), temperature_c=1.0),
        ],
        forecast_hourly_snapshots=[_row(gold.FORECAST_TABLE, collected_at_utc=T0,
            target_at_utc=T0 + timedelta(hours=70), target_local_date=date(2026, 10, 20), lead_time_hours=70.0)],
        forecast_daily_snapshots=[_row(gold.FORECAST_DAILY_TABLE, collected_at_utc=T0,
            target_local_date=date(2026, 10, 20), lead_days=2)])


def test_window_spans_new_collections_minus_one_day_plus_three_days(spark):
    specs = gold.gold_window_specs(gold.gold_affected_windows(_window_inputs(spark)))
    assert specs == [{
        "city_id": "fr_paris",
        "start": "2026-10-16 22:10:00.000000",            # min(collected) - 1 day
        "end": "2026-10-21 03:10:00.000000",              # max(collected) + 3 days
        "first_local_date": "2026-10-17", "last_local_date": "2026-10-21",
    }]
    condition = gold.gold_window_condition(specs, "hour_utc")
    assert condition == ("(target.`city_id` = 'fr_paris' AND target.`hour_utc` BETWEEN "
                         "TIMESTAMP'2026-10-16 22:10:00.000000' AND TIMESTAMP'2026-10-21 03:10:00.000000')")
    assert "DATE'2026-10-17' AND DATE'2026-10-21'" in gold.gold_window_condition(specs, "local_date", kind="date")
    assert gold.gold_window_condition([], "hour_utc") == "false"


def test_window_widens_to_every_target_carried_by_the_new_rows(spark):
    # An unusually late daily target: its whole local day must stay inside the window.
    inputs = _inputs(spark,
        observation_snapshots=[_row(gold.OBSERVATION_TABLE, collected_at_utc=T0,
                                    observed_at_utc=T0 - timedelta(days=2), temperature_c=1.0)],
        forecast_daily_snapshots=[_row(gold.FORECAST_DAILY_TABLE, collected_at_utc=T0,
                                       target_local_date=date(2026, 10, 25), lead_days=2)])
    spec = gold.gold_window_specs(gold.gold_affected_windows(inputs))[0]
    assert spec["start"] == "2026-10-15 22:00:00.000000"   # hour of the old observation
    # 25 October (DST end, 25 h): last local hour 23:00 CET = 22:00 UTC.
    assert spec["end"] == "2026-10-25 22:00:00.000000"
    assert spec["last_local_date"] == "2026-10-25"


def test_window_only_for_cities_with_new_rows(spark):
    assert gold.gold_window_specs(gold.gold_affected_windows(_inputs(spark))) == []


def test_window_literals_refuse_unexpected_text():
    with pytest.raises(ValueError):
        gold.gold_window_condition([{"city_id": "x' OR 1=1 --", "start": "2026-01-01 00:00:00.000000",
                                     "end": "2026-01-02 00:00:00.000000"}], "hour_utc")


# --- dim_city SCD 2 ---------------------------------------------------------------

def _locations(spark, rows):
    return spark.createDataFrame(
        [(f"i{index:03d}", city, at, lat, lon) for index, (city, at, lat, lon) in enumerate(rows)],
        gold.GOLD_CONTROL_SCHEMAS["city_locations"])


def _collections(spark, instants):
    return spark.createDataFrame([(at,) for at in instants], "collected_at_utc timestamp")


def _history(spark, rows, instants=()):
    frame = gold.gold_city_history(spark, _locations(spark, rows),
                                   _collections(spark, [at for _, at, _, _ in rows] + list(instants)))
    return frame.orderBy("city_id", "valid_from_utc").collect()


def _paris(rows):
    return [(r.valid_from_utc, r.valid_to_utc, r.is_current, r.latitude, r.longitude)
            for r in rows if r.city_id == "fr_paris"]


def test_scd2_unchanged_attributes_create_no_version(spark):
    instants = [T0 + timedelta(hours=h) for h in range(4)]
    rows = _history(spark, [("fr_paris", at, 48.8667, 2.3333) for at in instants])
    assert _paris(rows) == [
        (_naive(ORIGIN), _naive(T0), False, None, None),
        (_naive(T0), None, True, 48.8667, 2.3333),
    ]
    london = [r for r in rows if r.city_id == "gb_london"]
    assert [(r.valid_from_utc, r.is_current, r.latitude) for r in london] == [(_naive(ORIGIN), True, None)]


def test_scd2_coordinate_change_and_return_create_versions_with_distinct_keys(spark):
    at = [T0 + timedelta(hours=h) for h in range(5)]
    rows = _history(spark, [
        ("fr_paris", at[0], 48.8667, 2.3333), ("fr_paris", at[1], 48.8667, 2.3333),
        ("fr_paris", at[2], 48.8567, 2.3333), ("fr_paris", at[3], 48.8667, 2.3333),
    ], instants=[at[4]])  # A collection without a valid Paris row keeps the coordinates.
    assert _paris(rows) == [
        (_naive(ORIGIN), _naive(at[0]), False, None, None),
        (_naive(at[0]), _naive(at[2]), False, 48.8667, 2.3333),
        (_naive(at[2]), _naive(at[3]), False, 48.8567, 2.3333),
        (_naive(at[3]), None, True, 48.8667, 2.3333),
    ]
    paris = [r for r in rows if r.city_id == "fr_paris"]
    assert [r.city_sk for r in paris] == [gold.gold_city_sk("fr_paris", v) for v in (ORIGIN, at[0], at[2], at[3])]
    assert len({r.city_sk for r in rows}) == len(rows)
    assert {r.first_collected_at_utc for r in paris} == {_naive(at[0])}


def test_scd2_is_deterministic_whatever_the_input_order(spark):
    at = [T0 + timedelta(hours=h) for h in range(3)]
    data = [("fr_paris", at[0], 48.8667, 2.3333), ("fr_paris", at[1], 48.8567, 2.3333),
            ("be_brussels", at[2], 50.8333, 4.3333)]
    assert _history(spark, data) == _history(spark, list(reversed(data)))


def test_scd2_rounds_coordinates_to_four_decimals(spark):
    noisy = [
        _row(gold.OBSERVATION_TABLE, ingestion_id="a", collected_at_utc=T0, observed_at_utc=T0,
             latitude=48.86670000001, longitude=2.33329999999, temperature_c=1.0),
        _row(gold.OBSERVATION_TABLE, ingestion_id="b", collected_at_utc=T0 + timedelta(hours=1),
             observed_at_utc=T0, latitude=48.8667, longitude=2.3333, temperature_c=1.0),
    ]
    inputs = _inputs(spark, observation_snapshots=noisy)
    assert {(r.latitude, r.longitude) for r in gold._gold_city_locations(inputs).collect()} == {(48.8667, 2.3333)}
    paris = [r for r in gold._gold_dim_city(spark, inputs).collect() if r.city_id == "fr_paris"]
    assert len(paris) == 2  # origin + one version, the noise is not a change


def test_scd2_registry_change_versions_at_first_collection_after_its_effective_instant(spark, monkeypatch):
    at = [T0 + timedelta(hours=h) for h in range(3)]
    monkeypatch.setattr(gold, "GOLD_CITY_REGISTRY_CHANGES", (
        ("fr_paris", T0 + timedelta(minutes=30), {"is_active": False}),))
    rows = _history(spark, [("fr_paris", a, 48.8667, 2.3333) for a in at])
    paris = [r for r in rows if r.city_id == "fr_paris"]
    assert [(r.valid_from_utc, r.is_active) for r in paris] == [
        (_naive(ORIGIN), True), (_naive(at[0]), True), (_naive(at[1]), False)]
    assert paris[-1].is_current and paris[1].valid_to_utc == _naive(at[1])


def test_scd2_history_compatibility_allows_appending_only(spark):
    at = [T0 + timedelta(hours=h) for h in range(3)]
    base = [("fr_paris", at[0], 48.8667, 2.3333), ("fr_paris", at[1], 48.8667, 2.3333)]
    before = gold.gold_city_history(spark, _locations(spark, base), _collections(spark, at[:2]))
    consumed = _collections(spark, at[:2])
    appended = base + [("fr_paris", at[2], 48.8567, 2.3333)]
    after = gold.gold_city_history(spark, _locations(spark, appended), _collections(spark, at))
    assert gold.gold_city_history_compatible(before, after, consumed)
    # A late collection between two consumed ones with other coordinates rewrites history.
    late = base + [("fr_paris", T0 + timedelta(minutes=30), 48.8567, 2.3333)]
    rewritten = gold.gold_city_history(spark, _locations(spark, late),
                                       _collections(spark, at[:2] + [T0 + timedelta(minutes=30)]))
    assert not gold.gold_city_history_compatible(before, rewritten, consumed)


def test_city_sk_resolves_the_version_valid_at_the_instant(spark):
    at = [T0, T0 + timedelta(hours=2)]
    history = gold.gold_city_history(spark, _locations(spark, [
        ("fr_paris", at[0], 48.8667, 2.3333), ("fr_paris", at[1], 48.8567, 2.3333)]), _collections(spark, at))
    facts = gold.gold_canonical_observations(_inputs(spark, observation_snapshots=[
        _row(gold.OBSERVATION_TABLE, ingestion_id=f"i{h}", collected_at_utc=T0 + timedelta(hours=h),
             observed_at_utc=T0 + timedelta(hours=h), temperature_c=1.0) for h in range(4)])[gold.OBSERVATION_TABLE])
    resolved = gold.gold_resolve_city_sk(facts, history, "collected_at_utc", "fact_observation_hourly")
    keys = {r.ingestion_id: r.city_sk for r in resolved.collect()}
    assert keys == {"i0": gold.gold_city_sk("fr_paris", at[0]), "i1": gold.gold_city_sk("fr_paris", at[0]),
                    "i2": gold.gold_city_sk("fr_paris", at[1]), "i3": gold.gold_city_sk("fr_paris", at[1])}


# --- dim_condition SCD 1 ------------------------------------------------------------

def _observation_labels(spark, labels):
    return _inputs(spark, observation_snapshots=[
        _row(gold.OBSERVATION_TABLE, ingestion_id=f"i{index}", observed_at_utc=T0 + timedelta(hours=index),
             temperature_c=1.0, condition_code=1000, is_day=True, condition_text=label)
        for index, label in enumerate(labels)])


def _key(row):
    return tuple("" if value is None else str(value) for value in row)


def _sorted(frame):
    return sorted((tuple(r) for r in frame.collect()), key=_key)


def test_condition_counts_accumulate_to_the_full_counts(spark):
    first, second = ["Ensoleillé", "Soleil", "Soleil"], ["Ensoleillé", "Ensoleillé", None]
    old = gold.gold_condition_counts(_observation_labels(spark, first))
    new = gold.gold_condition_counts(_observation_labels(spark, second))
    total = gold.gold_add_condition_counts(old, new)
    assert _sorted(total) == _sorted(gold.gold_condition_counts(_observation_labels(spark, first + second)))
    assert _sorted(total) == sorted([(1000, True, None, 1), (1000, True, "Ensoleillé", 3),
                                     (1000, True, "Soleil", 2)], key=_key)
    # SCD 1: the most frequent label replaces the previous one in place.
    assert gold.gold_dim_condition_from_counts(old).first().label_fr == "Soleil"
    assert gold.gold_dim_condition_from_counts(total).first().label_fr == "Ensoleillé"


def test_condition_label_tie_keeps_the_lexical_order(spark):
    counts = gold.gold_condition_counts(_observation_labels(spark, ["Z", "A", None]))
    assert gold.gold_dim_condition_from_counts(counts).first().label_fr == "A"


# --- Modes and weekly safety net -------------------------------------------------------

SUNDAY = datetime(2026, 11, 15, 2, tzinfo=timezone.utc)


@pytest.mark.parametrize("weekday, now, expected", [
    ("6", SUNDAY, ("full", "weekly_full_rebuild")),
    ("6", SUNDAY + timedelta(days=1), ("incremental", "requested")),
    ("", SUNDAY, ("incremental", "requested")),
    (None, SUNDAY, ("incremental", "requested")),
    (0, SUNDAY + timedelta(days=1), ("full", "weekly_full_rebuild")),
])
def test_weekly_full_rebuild_overrides_incremental_on_its_utc_weekday(weekday, now, expected):
    assert gold.gold_effective_mode("incremental", weekday, now) == expected
    assert gold.gold_effective_mode("full", weekday, now)[0] == "full"


@pytest.mark.parametrize("value", ["7", "-1", "sunday", True, 1.5])
def test_invalid_full_rebuild_weekday_is_refused(value):
    with pytest.raises(ValueError, match="full_rebuild_weekday"):
        gold.gold_full_rebuild_weekday(value)


def test_invalid_weekday_fails_before_reading_storage(tmp_path):
    with pytest.raises(ValueError, match="full_rebuild_weekday"):
        gold.run_gold(None, silver_root=f"file://{tmp_path}/s", gold_root=f"file://{tmp_path}/g",
                      full_rebuild_weekday="8")


def test_every_table_declares_a_load_strategy():
    assert set(gold.GOLD_LOAD_STRATEGIES) == set(gold.GOLD_SCHEMAS)
    assert set(gold.GOLD_CITY_SK_INSTANTS) == {n for n in gold.GOLD_SCHEMAS if n.startswith("fact_")}
    for name in gold.GOLD_CITY_SK_INSTANTS:
        assert "city_sk" in gold.GOLD_SCHEMAS[name].fieldNames()


# --- Unity Catalog relocation ----------------------------------------------------------

ROOT = "abfss://datalake@acct.dfs.core.windows.net/gold/weatherapi_v2"
OLD = "abfss://datalake@acct.dfs.core.windows.net/gold/weatherapi"


class FakeCatalogSpark:
    def __init__(self, tables):
        self.tables = dict(tables)
        self.statements = []
        self.catalog = SimpleNamespace(tableExists=lambda name: name in self.tables,
                                       getTable=lambda name: SimpleNamespace(tableType=self.tables[name][1]))

    def sql(self, statement):
        self.statements.append(statement)
        if match := re.fullmatch(r"DESCRIBE DETAIL (\S+)", statement):
            return SimpleNamespace(collect=lambda: [{"location": self.tables[match.group(1)][0]}])
        if match := re.fullmatch(r"DROP TABLE (\S+)", statement):
            del self.tables[match.group(1)]
        elif match := re.fullmatch(r"CREATE TABLE (\S+) USING DELTA LOCATION '([^']+)'", statement):
            assert match.group(1) not in self.tables
            self.tables[match.group(1)] = (match.group(2), "EXTERNAL")
        else:
            raise AssertionError(statement)


def _uc(name):
    return f"`weather`.`gold`.`{name}`"


def test_registration_relocates_v1_external_names_without_touching_data(monkeypatch):
    monkeypatch.setattr(gold, "latest_gold_build", lambda spark, paths: {"build_id": "b"})
    spark = FakeCatalogSpark({_uc(name): (f"{OLD}/{name}", "EXTERNAL") for name in gold.GOLD_SCHEMAS})
    paths = gold.GoldPaths(ROOT)
    assert set(gold.register_gold_tables(spark, paths, "weather.gold").values()) == {"relocated"}
    assert all(spark.tables[_uc(name)][0] == paths.table(name) for name in gold.GOLD_SCHEMAS)
    assert not any(s.startswith(("DELETE", "TRUNCATE", "VACUUM")) for s in spark.statements)
    spark.statements.clear()
    assert set(gold.register_gold_tables(spark, paths, "weather.gold").values()) == {"unchanged"}
    assert [s for s in spark.statements if not s.startswith("DESCRIBE")] == []


def test_registration_refuses_to_drop_a_managed_gold_table(monkeypatch):
    monkeypatch.setattr(gold, "latest_gold_build", lambda spark, paths: {"build_id": "b"})
    spark = FakeCatalogSpark({_uc("dim_city"): ("abfss://elsewhere/dim_city", "MANAGED")})
    with pytest.raises(gold.CatalogConflict, match="non-external"):
        gold.register_gold_tables(spark, gold.GoldPaths(ROOT), "weather.gold")
    assert not any(s.startswith("DROP") for s in spark.statements)


def test_real_captures_carry_identical_four_decimal_coordinates():
    """Measured on the two real-derived captures: no coordinate drift between them."""
    from conftest import real_sources, silver_rules
    seen = {}
    for blob, raw in real_sources():
        result = silver_rules.transform_bronze(raw, blob)
        for name in gold.SILVER_DATA_TABLES:
            for row in result.tables[name].to_pylist():
                seen.setdefault(row["city_id"], set()).add((row["latitude"], row["longitude"]))
    assert seen == {"fr_paris": {(48.8667, 2.3333)}, "be_brussels": {(50.8333, 4.3333)}}
