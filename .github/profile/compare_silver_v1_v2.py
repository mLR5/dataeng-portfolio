"""Read-only comparison of Silver v1 (silver/weatherapi) and Silver v2 (silver/weatherapi_v2).

Prints PASS/FAIL/WARN/INFO lines with counts only: no key, value, path or secret.
Exits 1 if any check FAILs. Bronze keeps growing: v2 can hold ingestions newer than
v1, so row comparisons are restricted to the ingestions published by v1.

Checks:
  a. every v1 key exists in v2 (observations, hourly forecasts, publications);
  b. on common columns, rows of a common key are equal (chance_of_*_pct compared as
     numbers; a value nulled by v2 is counted apart);
  c. v2 extra keys on v1 ingestions = recovered rows, counted per reason against the
     targets (legacy archives, decimal probabilities);
  d. forecast_daily_snapshots: about 3 rows per valid city and ingestion;
  e. publications: no unknown Bronze path, nulled metrics, statuses per month;
  f. no duplicate key in v2.
"""
import os
import sys
import tempfile

import duckdb
from deltalake import DeltaTable

ACCOUNT = os.environ.get("ACCOUNT", "")
KEY = os.environ.get("STORAGE_KEY", "")
DATALAKE = f"abfss://datalake@{ACCOUNT}.dfs.core.windows.net"
# Local overrides are only for testing the script on synthetic tables.
ROOTS = {
    "v1": os.environ.get("SILVER_V1_ROOT") or f"{DATALAKE}/silver/weatherapi",
    "v2": os.environ.get("SILVER_V2_ROOT") or f"{DATALAKE}/silver/weatherapi_v2",
}
OPTIONS = {"account_name": ACCOUNT, "account_key": KEY} if ACCOUNT else {}
TABLES = {
    "obs": "observation_snapshots",
    "fc": "forecast_hourly_snapshots",
    "daily": "forecast_daily_snapshots",
    "pub": "_control/publications",
    "quar": "_control/quarantine",
}
KEYS = {
    "obs": ("ingestion_id", "city_id", "observed_at_utc"),
    "fc": ("ingestion_id", "city_id", "target_at_utc"),
    "daily": ("ingestion_id", "city_id", "target_local_date"),
    "pub": ("ingestion_id",),
    "quar": ("ingestion_id", "rejection_index"),
}
TABLE_NAMES = {"obs": "observation_snapshots", "fc": "forecast_hourly_snapshots"}
# Rows recovered by v2 on the ingestions already published by v1 (investigation of
# 2026-11-13): 20 legacy archives and 10 forecasts with a decimal probability.
TARGETS = {("obs", "legacy_archive"): 33, ("fc", "legacy_archive"): 2336,
           ("fc", "decimal_probability"): 10, ("obs", "decimal_probability"): 0}
EXPECTED_SCHEMA_VERSION, EXPECTED_TRANSFORM_VERSION = 2, "2.0.0"
STATUS_RANK = {"rejected": 0, "empty": 1, "partial": 2, "success": 3}

failures = []
workdir = tempfile.mkdtemp(prefix="compare-silver-")
con = duckdb.connect(os.path.join(workdir, "compare.duckdb"))
con.execute("set TimeZone = 'UTC'")  # months and dates are UTC, whatever the runner


def report(level, name, detail=""):
    if level == "FAIL":
        failures.append(name)
    print(f"{level} {name}{': ' + detail if detail else ''}", flush=True)


def check(name, ok, detail=""):
    report("PASS" if ok else "FAIL", name, detail)


def section(title):
    print(f"\n######## {title}", flush=True)


def scalar(sql):
    return con.execute(sql).fetchall()[0][0]


def show(title, sql):
    print(f"-- {title}")
    res = con.execute(sql)
    print(" | ".join(d[0] for d in res.description))
    for row in res.fetchall():
        print(" | ".join("" if v is None else str(v) for v in row))


def columns(table):
    return {name: dtype for name, dtype in con.execute(
        "select column_name, data_type from information_schema.columns where table_name = ? order by ordinal_position",
        [table]).fetchall()}


def exists(table):
    return bool(scalar(f"select count(*) from information_schema.tables where table_name = '{table}'"))


def keys_sql(name):
    return ", ".join(KEYS[name])


# ---------------- Load (copied into a local DuckDB file, read once) ----------------
section("LOAD")
for version, root in ROOTS.items():
    for name, sub in TABLES.items():
        if version == "v1" and name == "daily":
            continue
        target = f"{version}_{name}"
        try:
            table = DeltaTable(f"{root}/{sub}", storage_options=OPTIONS)
        except Exception as error:  # noqa: BLE001 - only the type is printed (no path)
            check(f"load {target}", False, f"table not readable ({type(error).__name__})")
            continue
        try:
            source = table.to_pyarrow_dataset()
        except Exception:  # noqa: BLE001 - older readers: whole table in memory
            source = table.to_pyarrow_table()
        con.register("source_dataset", source)
        con.execute(f"create table {target} as select * from source_dataset")
        con.unregister("source_dataset")
        report("INFO", f"load {target}", f"delta version {table.version()}, {scalar(f'select count(*) from {target}')} rows")
if failures:
    sys.exit(1)

# Comparison scope: v2 ingestions already published by v1, matched by ingestion_id or by
# Bronze blob (a legacy archive rejected by v1 may carry another publication ID).
con.execute("""create table v1_ids as select distinct p2.ingestion_id from v2_pub p2
               where p2.ingestion_id in (select ingestion_id from v1_pub)
                  or p2.source_blob in (select source_blob from v1_pub)""")
con.execute("""create table legacy_ids as select ingestion_id from v2_pub
               where json_extract_string(summary_json, '$.bronze_format') = 'legacy'""")
report("INFO", "v1 publications", str(scalar("select count(*) from v1_pub")))
report("INFO", "v2 publications on v1 ingestions", str(scalar("select count(*) from v1_ids")))
report("INFO", "v2 publications newer than v1 (not compared row by row)",
       str(scalar("select count(*) from v2_pub anti join v1_ids using (ingestion_id)")))

# ---------------- f. Unique keys and versions in v2 ----------------
section("F. V2 KEYS AND VERSIONS")
for name in ("obs", "fc", "daily", "pub", "quar"):
    duplicates = scalar(f"select count(*) from (select {keys_sql(name)} from v2_{name} group by all having count(*) > 1)")
    check(f"no duplicate key in v2 {TABLES[name]}", duplicates == 0, f"{duplicates} duplicated keys")
for name in ("obs", "fc", "daily", "pub"):
    wrong = scalar(f"""select count(*) from v2_{name} where schema_version <> {EXPECTED_SCHEMA_VERSION}
                       or transform_version <> '{EXPECTED_TRANSFORM_VERSION}'""")
    check(f"v2 {TABLES[name]} rows carry schema 2 / transform 2.0.0", wrong == 0, f"{wrong} other rows")

# ---------------- a. Every v1 key exists in v2 ----------------
section("A. V1 KEYS IN V2")
missing_pub = scalar("select count(*) from v1_pub anti join v2_pub using (source_blob)")
check("every v1 publication (Bronze blob) is published by v2", missing_pub == 0, f"{missing_pub} missing")
show("v1 publications whose v2 ingestion_id differs (same Bronze blob)",
     """select coalesce(json_extract_string(p2.summary_json, '$.bronze_format'), '(none)') v2_bronze_format,
               count(*) n from v1_pub p1 join v2_pub p2 using (source_blob)
        where p1.ingestion_id <> p2.ingestion_id group by 1 order by 1""")
for name in ("obs", "fc"):
    missing = scalar(f"select count(*) from v1_{name} anti join v2_{name} using ({keys_sql(name)})")
    total = scalar(f"select count(*) from v1_{name}")
    check(f"every v1 {TABLES[name]} key exists in v2", missing == 0, f"{missing} missing of {total}")
    if missing:
        show(f"missing {TABLES[name]} keys per v2 publication status",
             f"""select coalesce(p.status, '(no v2 publication)') v2_status, count(*) n
                 from (select * from v1_{name} anti join v2_{name} using ({keys_sql(name)})) m
                 left join v2_pub p using (ingestion_id) group by 1 order by 1""")

# ---------------- b. Equal values on common keys ----------------
section("B. COMMON KEYS: SAME VALUES")
for name in ("obs", "fc"):
    v1_cols, v2_cols = columns(f"v1_{name}"), columns(f"v2_{name}")
    compared = [c for c in v1_cols if c in v2_cols and c not in KEYS[name]
                and c not in ("schema_version", "transform_version")]
    retyped = [f"{c} {v1_cols[c]}->{v2_cols[c]}" for c in compared if v1_cols[c] != v2_cols[c]]
    report("INFO", f"{TABLES[name]} compared columns", f"{len(compared)} (retyped: {', '.join(retyped) or 'none'})")
    report("INFO", f"{TABLES[name]} columns only in v2", ", ".join(c for c in v2_cols if c not in v1_cols) or "none")
    only_v1 = [c for c in v1_cols if c not in v2_cols]
    check(f"{TABLES[name]} v1 columns kept in v2", not only_v1, ", ".join(only_v1) or "all kept")
    pairs = ", ".join(f"a.\"{c}\" as \"a_{c}\", b.\"{c}\" as \"b_{c}\"" for c in compared)
    con.execute(f"create table j_{name} as select {pairs} from v1_{name} a join v2_{name} b using ({keys_sql(name)})")
    common = scalar(f"select count(*) from j_{name}")
    report("INFO", f"{TABLES[name]} common keys", str(common))
    measures = []
    for c in compared:
        # A v1 integer probability and its v2 double are compared as numbers.
        left = f"cast(\"a_{c}\" as {v2_cols[c]})" if v1_cols[c] != v2_cols[c] else f"\"a_{c}\""
        right = f"\"b_{c}\""
        measures.append(f"count(*) filter (where {left} is distinct from {right} and {right} is not null) as \"diff_{c}\"")
        measures.append(f"count(*) filter (where {right} is null and {left} is not null) as \"nulled_{c}\"")
    row = con.execute(f"select {', '.join(measures)} from j_{name}").fetchall()[0]
    for index, c in enumerate(compared):
        diff, nulled = row[2 * index], row[2 * index + 1]
        check(f"{TABLES[name]}.{c} equal on common keys", diff == 0, f"{diff} different")
        if nulled:
            report("WARN", f"{TABLES[name]}.{c} nulled by v2 (invalid optional value)", f"{nulled} rows")

# ---------------- c. Recovered rows ----------------
section("C. RECOVERED ROWS (V2 EXTRA KEYS ON V1 INGESTIONS)")
v1_quarantine_groups = """select distinct ingestion_id, city_query, table_name from v1_quar"""
for name in ("obs", "fc"):
    decimal = ("(chance_of_rain_pct <> floor(chance_of_rain_pct) or chance_of_snow_pct <> floor(chance_of_snow_pct))"
               if name == "fc" else "false")
    con.execute(f"""create table x_{name} as
        select x.*, case
            when x.ingestion_id in (select ingestion_id from legacy_ids) then 'legacy_archive'
            when {decimal} then 'decimal_probability'
            when exists (select 1 from ({v1_quarantine_groups}) q
                         where q.ingestion_id = x.ingestion_id and q.city_query = x.city_query
                           and q.table_name in ('{TABLE_NAMES[name]}', 'city')) then 'v1_rejected_other'
            else 'unexplained' end as reason
        from (select * from v2_{name} semi join v1_ids using (ingestion_id)) x
        anti join v1_{name} using ({keys_sql(name)})""")
    counts = dict(con.execute(f"select reason, count(*) from x_{name} group by 1").fetchall())
    report("INFO", f"{TABLES[name]} extra keys on v1 ingestions", f"{sum(counts.values())} {counts}")
    for reason in ("legacy_archive", "decimal_probability"):
        if (name, reason) in TARGETS:
            got, want = counts.get(reason, 0), TARGETS[(name, reason)]
            check(f"{TABLES[name]} recovered by {reason}", got == want, f"{got} (target {want})")
    check(f"{TABLES[name]} extra keys all explained", counts.get("unexplained", 0) == 0,
          f"{counts.get('unexplained', 0)} unexplained")
    if counts.get("v1_rejected_other"):
        report("WARN", f"{TABLES[name]} recovered for another v1 rejection", f"{counts['v1_rejected_other']} rows")
        show(f"{TABLES[name]}: v1 quarantine codes of those city ingestions",
             f"""select q.table_name, q.code, q.field, count(*) n from v1_quar q
                 where exists (select 1 from x_{name} x where x.reason = 'v1_rejected_other'
                               and x.ingestion_id = q.ingestion_id and x.city_query = q.city_query)
                 group by all order by n desc limit 20""")
extra_newer = {name: scalar(f"select count(*) from v2_{name} anti join v1_ids using (ingestion_id)") for name in ("obs", "fc")}
report("INFO", "v2 rows of ingestions newer than v1", str(extra_newer))
show("legacy publications in v2: status (v1 status)",
     """select p2.status v2_status, coalesce(p1.status, '(none)') v1_status, count(*) n
        from v2_pub p2 left join v1_pub p1 using (source_blob)
        where p2.ingestion_id in (select ingestion_id from legacy_ids) group by all order by all""")

# ---------------- d. Daily forecasts ----------------
section("D. FORECAST_DAILY_SNAPSHOTS")
con.execute("""create table daily_pairs as
    with h as (select ingestion_id, city_id, count(distinct target_local_date) hourly_days from v2_fc group by 1, 2),
         d as (select ingestion_id, city_id, count(*) daily_rows from v2_daily group by 1, 2)
    select h.ingestion_id, h.city_id, h.hourly_days, coalesce(d.daily_rows, 0) daily_rows
    from h left join d using (ingestion_id, city_id)""")
show("daily rows per (ingestion, city) with hourly forecasts",
     "select daily_rows, count(*) pairs from daily_pairs group by 1 order by 1")
pairs = scalar("select count(*) from daily_pairs")
three = scalar("select count(*) from daily_pairs where daily_rows = 3")
without = scalar("select count(*) from daily_pairs where daily_rows = 0")
beyond = scalar("select count(*) from daily_pairs where daily_rows > hourly_days")
check("every city ingestion with hourly forecasts has daily forecasts", without == 0, f"{without} of {pairs} without")
check("daily rows never exceed the local days of hourly forecasts", beyond == 0, f"{beyond} pairs")
share = three / pairs if pairs else 0
report("PASS" if share >= 0.99 else "WARN", "about 3 daily rows per valid city and ingestion",
       f"{three} of {pairs} pairs ({100 * share:.2f}%)")
orphans = scalar("select count(*) from (select distinct ingestion_id, city_id from v2_daily) anti join v2_fc using (ingestion_id, city_id)")
report("INFO", "daily (ingestion, city) without hourly forecast", str(orphans))
show("daily optional fields: null rows",
     """select count(*) rows, count(*) filter (where sunrise_utc is null) sunrise_null,
               count(*) filter (where sunset_utc is null) sunset_null,
               count(*) filter (where wet_bulb_avg_c is null) wet_bulb_avg_null,
               count(*) filter (where chance_of_rain_pct <> floor(chance_of_rain_pct)) decimal_rain
        from v2_daily""")

# ---------------- e. Publications ----------------
section("E. PUBLICATIONS")
pub_cols = columns("v2_pub")
unknown_expr = "coalesce(json_array_length(json_extract(summary_json, '$.unknown_paths')), 0)"
if "unknown_path_count" in pub_cols:
    unknown_expr = f"greatest({unknown_expr}, coalesce(unknown_path_count, 0))"
with_unknown = scalar(f"select count(*) from v2_pub where {unknown_expr} > 0")
check("unknown_path_count == 0 in every v2 publication", with_unknown == 0, f"{with_unknown} publications with unknown paths")
if with_unknown:
    show("unknown Bronze paths (structure only)",
         """select path, count(*) publications from (select unnest(json_extract_string(summary_json, '$.unknown_paths[*]')) path
            from v2_pub) group by 1 order by 2 desc limit 30""")
report("INFO", "v2 publications without unknown_paths in summary (unparsable JSON)",
       str(scalar("select count(*) from v2_pub where json_extract(summary_json, '$.unknown_paths') is null")))
show("v2 Bronze formats", """select coalesce(json_extract_string(summary_json, '$.bronze_format'), '(none)') bronze_format,
                                    status, count(*) n from v2_pub group by all order by all""")
incomplete = scalar("select count(*) from v2_pub where not complete")
report("INFO" if incomplete == 0 else "WARN", "v2 incomplete publications", str(incomplete))

quar_cols = columns("v2_quar")
if "scope" in quar_cols:
    show("v2 quarantine per scope", "select scope, count(*) n from v2_quar group by 1 order by 1")
    show("nulled metrics (scope = field) per table, field, code",
         "select table_name, field, code, count(*) n from v2_quar where scope = 'field' group by all order by n desc limit 40")
else:
    report("WARN", "v2 quarantine has no scope column", "nulled metrics read from summaries only")
if scalar("select count(*) from v2_pub where json_extract(summary_json, '$.nulled_metrics') is not null"):
    show("nulled_metrics in summaries (most frequent values)",
         """select json_extract(summary_json, '$.nulled_metrics')::varchar nulled_metrics, count(*) publications
            from v2_pub where json_extract(summary_json, '$.nulled_metrics') is not null
            group by 1 order by 2 desc limit 20""")
show("quarantine codes v1 vs v2 (v1 ingestions only)",
     """with a as (select table_name, code, count(*) n from v1_quar group by all),
             b as (select table_name, code, count(*) n from v2_quar semi join v1_ids using (ingestion_id) group by all)
        select coalesce(a.table_name, b.table_name) table_name, coalesce(a.code, b.code) code,
               coalesce(a.n, 0) v1, coalesce(b.n, 0) v2
        from a full join b on a.table_name = b.table_name and a.code = b.code order by 1, 2""")
show("statuses per month: v1 vs v2 (v2 split: on v1 ingestions / newer)",
     """with a as (select strftime(collected_at_utc, '%Y-%m') mon, status, count(*) n from v1_pub group by all),
             b as (select strftime(p.collected_at_utc, '%Y-%m') mon, p.status,
                          count(*) filter (where i.ingestion_id is not null) n_common,
                          count(*) filter (where i.ingestion_id is null) n_newer
                   from v2_pub p left join v1_ids i using (ingestion_id) group by all)
        select coalesce(a.mon, b.mon, '(no time)') month_utc, coalesce(a.status, b.status) status,
               coalesce(a.n, 0) v1, coalesce(b.n_common, 0) v2_common, coalesce(b.n_newer, 0) v2_newer
        from a full join b on a.mon is not distinct from b.mon and a.status = b.status order by 1, 2""")
show("status transitions v1 -> v2",
     """select p1.status v1_status, p2.status v2_status, count(*) n from v1_pub p1 join v2_pub p2 using (source_blob)
        group by all order by all""")
rank = "case {col} " + " ".join(f"when '{s}' then {r}" for s, r in STATUS_RANK.items()) + " else -1 end"
regressions = scalar(f"""select count(*) from v1_pub p1 join v2_pub p2 using (source_blob)
                         where {rank.format(col='p2.status')} < {rank.format(col='p1.status')}""")
check("no publication status regression from v1 to v2", regressions == 0, f"{regressions} regressions")

section("RESULT")
if failures:
    print(f"FAIL {len(failures)} check(s) failed")
    sys.exit(1)
print("PASS all checks")
