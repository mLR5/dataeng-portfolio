"""Read-only profile of Silver v2 (silver/weatherapi_v2) against the Gold model assumptions.

Prints counts and distributions only: no key, value, path or secret.
Each question is a titled section; a failing query prints its error type and the
script continues, exiting 1 at the end if any section failed.
"""
import os
import sys
import tempfile

import duckdb
from deltalake import DeltaTable

ACCOUNT = os.environ.get("ACCOUNT", "")
KEY = os.environ.get("STORAGE_KEY", "")
DATALAKE = f"abfss://datalake@{ACCOUNT}.dfs.core.windows.net"
# Local override is only for testing the script on synthetic tables.
ROOT = os.environ.get("SILVER_V2_ROOT") or f"{DATALAKE}/silver/weatherapi_v2"
OPTIONS = {"account_name": ACCOUNT, "account_key": KEY} if ACCOUNT else {}
TABLES = {
    "obs": "observation_snapshots",
    "fc": "forecast_hourly_snapshots",
    "daily": "forecast_daily_snapshots",
    "pub": "_control/publications",
    "quar": "_control/quarantine",
}

errors = []
workdir = tempfile.mkdtemp(prefix="gold-align-")
con = duckdb.connect(os.path.join(workdir, "profile.duckdb"))
con.execute("set TimeZone = 'UTC'")  # hours, dates and months are UTC, whatever the runner


def section(title):
    print(f"\n######## {title}", flush=True)


def scalar(sql):
    return con.execute(sql).fetchall()[0][0]


def show(title, sql):
    print(f"-- {title}", flush=True)
    try:
        res = con.execute(sql)
        print(" | ".join(d[0] for d in res.description))
        for row in res.fetchall():
            print(" | ".join("" if v is None else str(v) for v in row))
    except Exception as error:  # noqa: BLE001 - only the type and message of a SQL error
        errors.append(title)
        print(f"ERROR {type(error).__name__}: {str(error).splitlines()[0][:300]}")
    print(flush=True)


# ---------------- Load (copied into a local DuckDB file, read once) ----------------
section("LOAD")
for name, sub in TABLES.items():
    try:
        table = DeltaTable(f"{ROOT}/{sub}", storage_options=OPTIONS)
    except Exception as error:  # noqa: BLE001 - only the type is printed (no path)
        print(f"FAIL load {name}: table not readable ({type(error).__name__})")
        sys.exit(1)
    try:
        source = table.to_pyarrow_dataset()
    except Exception:  # noqa: BLE001 - older readers: whole table in memory
        source = table.to_pyarrow_table()
    con.register("source_dataset", source)
    con.execute(f"create table {name} as select * from source_dataset")
    con.unregister("source_dataset")
    print(f"INFO load {name}: delta version {table.version()}, {scalar(f'select count(*) from {name}')} rows", flush=True)
TZ = "case city_id when 'fr_paris' then 'Europe/Paris' when 'gb_london' then 'Europe/London' when 'be_brussels' then 'Europe/Brussels' else source_timezone end"

# ---------------- 1 ----------------
section("1. FORECAST_DAILY lead_days")
show("overall", "select min(lead_days) min_lead, max(lead_days) max_lead, count(*) n from daily")
show("counts per lead_days", "select lead_days, count(*) n from daily group by 1 order by 1")
show("counts per city and lead_days", "select city_id, lead_days, count(*) n from daily group by all order by all")
show("rows with lead_days < 0 or > 2",
     "select count(*) filter (where lead_days < 0) below_0, count(*) filter (where lead_days > 2) above_2 from daily")
show("out-of-range rows by collection hour UTC and city timezone",
     """select hour(collected_at_utc) collected_hour_utc, source_timezone, lead_days, count(*) n,
               count(distinct ingestion_id) ingestions
        from daily where lead_days < 0 or lead_days > 2 group by all order by all""")
show("days per (ingestion, city): count of rows and min/max lead",
     """select n_rows, min_lead, max_lead, count(*) pairs from (
          select ingestion_id, city_id, count(*) n_rows, min(lead_days) min_lead, max(lead_days) max_lead
          from daily group by 1, 2) group by all order by all""")

# ---------------- 2 ----------------
section("2. DAILY CONDITION CODES")
show("distinct condition_code per table",
     """select 'daily' tbl, count(distinct condition_code) n_codes, count(*) filter (where condition_code is null) null_rows from daily
        union all select 'hourly', count(distinct condition_code), count(*) filter (where condition_code is null) from fc
        union all select 'obs', count(distinct condition_code), count(*) filter (where condition_code is null) from obs""")
show("daily codes never seen in hourly nor obs (code, daily rows)",
     """select condition_code, count(*) daily_rows from daily
        where condition_code is not null
          and condition_code not in (select condition_code from fc where condition_code is not null)
          and condition_code not in (select condition_code from obs where condition_code is not null)
        group by 1 order by 1""")
show("daily codes not seen in hourly (code, daily rows)",
     """select condition_code, count(*) daily_rows from daily where condition_code is not null
          and condition_code not in (select condition_code from fc where condition_code is not null)
        group by 1 order by 1""")
show("daily codes without a (code, is_day=true) pair in hourly+obs",
     """with pairs as (select distinct condition_code from fc where is_day
                       union select distinct condition_code from obs where is_day)
        select d.condition_code, count(*) daily_rows from daily d
        where d.condition_code is not null and d.condition_code not in (select condition_code from pairs where condition_code is not null)
        group by 1 order by 1""")
show("coverage summary",
     """with pairs as (select distinct condition_code from fc where is_day
                       union select distinct condition_code from obs where is_day)
        select count(distinct condition_code) daily_codes,
               count(distinct condition_code) filter (where condition_code in (select condition_code from pairs)) covered_with_is_day_true
        from daily where condition_code is not null""")
show("(condition_code, is_day) distinct pairs in hourly+obs",
     """select count(*) pairs, count(*) filter (where is_day) day_pairs, count(*) filter (where not is_day) night_pairs from (
          select distinct condition_code, is_day from fc union select distinct condition_code, is_day from obs)""")
show("condition_text variants per code (codes with >1 text, all tables)",
     """select count(*) codes_with_multiple_texts from (
          select condition_code from (select condition_code, condition_text from daily union all
                                      select condition_code, condition_text from fc union all
                                      select condition_code, condition_text from obs)
          group by 1 having count(distinct lower(trim(condition_text))) > 1)""")

# ---------------- 3 ----------------
section("3. OBSERVATION TIMING")
show("minute(observed_at_utc) distribution", "select minute(observed_at_utc) minute_utc, count(*) n from obs group by 1 order by 1")
con.execute("""create table obs_h as select city_id, observed_at_utc, collected_at_utc, ingestion_id,
                 date_trunc('hour', observed_at_utc + interval 30 minute) hour_utc,
                 abs(epoch(observed_at_utc) - epoch(date_trunc('hour', observed_at_utc + interval 30 minute))) dist_s
               from obs""")
show("per city: observations, distinct observed_at, distinct hour_utc",
     """select city_id, count(*) obs_rows, count(distinct observed_at_utc) distinct_observed_at,
               count(distinct hour_utc) distinct_hour_utc from obs_h group by 1 order by 1""")
show("observations per hour_utc (rows per (city, hour_utc) -> number of hours)",
     """select city_id, rows_per_hour, count(*) n_hours from (select city_id, hour_utc, count(*) rows_per_hour from obs_h group by 1, 2)
        group by all order by all""")
show("distance of the kept observation to the round hour (minutes, after Gold rule)",
     """select city_id, round(dist_s / 60) dist_min, count(*) n_hours from (
          select *, row_number() over (partition by city_id, hour_utc order by dist_s, collected_at_utc desc) rn from obs_h)
        where rn = 1 group by all order by all""")
show("hour coverage between first and last observation per city",
     """select city_id, min(hour_utc) first_hour, max(hour_utc) last_hour,
               date_diff('hour', min(hour_utc), max(hour_utc)) + 1 hours_in_span,
               count(distinct hour_utc) hours_with_obs,
               round(100.0 * count(distinct hour_utc) / (date_diff('hour', min(hour_utc), max(hour_utc)) + 1), 2) pct
        from obs_h group by 1 order by 1""")
show("hour coverage per city and month (span clipped to first/last obs)",
     """with b as (select city_id, min(hour_utc) f, max(hour_utc) l from obs_h group by 1),
             g as (select b.city_id, unnest(generate_series(b.f, b.l, interval 1 hour)) h from b),
             s as (select distinct city_id, hour_utc from obs_h)
        select g.city_id, strftime(g.h, '%Y-%m') month_utc, count(*) hours_in_span, count(s.hour_utc) hours_with_obs,
               round(100.0 * count(s.hour_utc) / count(*), 2) pct
        from g left join s on s.city_id = g.city_id and s.hour_utc = g.h group by all order by all""")
show("longest gaps (hours without obs) per city, top 5 each",
     """select city_id, gap_hours, count(*) n from (
          select city_id, date_diff('hour', lag(hour_utc) over (partition by city_id order by hour_utc), hour_utc) - 1 gap_hours
          from (select distinct city_id, hour_utc from obs_h)) where gap_hours > 0 group by city_id, gap_hours
        qualify row_number() over (partition by city_id order by gap_hours desc) <= 6 order by city_id, gap_hours desc""")

# ---------------- 4 ----------------
section("4. FORECAST HOURLY KEYS")
show("lead_time_hours <= 0 vs > 0",
     """select count(*) filter (where lead_time_hours <= 0) lead_le_0, count(*) filter (where lead_time_hours > 0) lead_gt_0,
               count(*) filter (where target_is_past_at_collection) past_flag, min(lead_time_hours) min_lead, max(lead_time_hours) max_lead from fc""")
show("duplicates of (city_id, target_at_utc, collected_at_utc)",
     """select count(*) dup_keys, coalesce(sum(n), 0) rows_in_dups, count(*) filter (where n_ing > 1) across_ingestions from (
          select city_id, target_at_utc, collected_at_utc, count(*) n, count(distinct ingestion_id) n_ing from fc group by all having count(*) > 1)""")
show("publications: collected_at_utc shared by several ingestion_id",
     """select count(*) dup_instants, coalesce(sum(n), 0) publications_in_dups from (
          select collected_at_utc, count(distinct ingestion_id) n from pub where collected_at_utc is not null group by 1 having n > 1)""")
show("publications: duplicated collected_at_utc by status",
     """select status, count(*) n from pub where collected_at_utc in (
          select collected_at_utc from pub group by 1 having count(distinct ingestion_id) > 1) group by 1 order by 1""")
show("observations: (city_id, collected_at_utc) across different ingestion_id",
     """select count(*) dup_keys from (select city_id, collected_at_utc from obs group by all having count(distinct ingestion_id) > 1)""")
show("daily: (city_id, target_local_date, collected_at_utc) duplicates",
     """select count(*) dup_keys from (select city_id, target_local_date, collected_at_utc from daily group by all having count(*) > 1)""")
show("observations: (city_id, observed_at_utc, collected_at_utc) duplicates",
     """select count(*) dup_keys from (select city_id, observed_at_utc, collected_at_utc from obs group by all having count(*) > 1)""")
show("publications with null collected_at_utc by status",
     "select status, count(*) n from pub where collected_at_utc is null group by 1 order by 1")

# ---------------- 5 ----------------
section("5. DAILY SUNRISE / SUNSET / WET BULB")
con.execute("""create table sun_null as select d.*, p.bronze_format from daily d left join pub p using (ingestion_id)
               where d.sunrise_utc is null or d.sunset_utc is null""")
show("total null rows", """select count(*) rows, count(*) filter (where sunrise_utc is null) sunrise_null,
                           count(*) filter (where sunset_utc is null) sunset_null,
                           count(*) filter (where sunrise_local is null) sunrise_local_null,
                           count(*) filter (where sunset_local is null) sunset_local_null from sun_null""")
show("by city", "select city_id, count(*) n from sun_null group by 1 order by 1")
show("by target month", "select strftime(target_local_date, '%Y-%m') target_month, count(*) n from sun_null group by 1 order by 1")
show("by bronze_format", "select coalesce(bronze_format, '(no publication)') bronze_format, count(*) n from sun_null group by 1 order by 1")
show("by lead_days and city", "select city_id, lead_days, count(*) n from sun_null group by all order by all")
show("local value shape when utc is null (sunrise_local / sunset_local)",
     """select coalesce(regexp_replace(sunrise_local, '[0-9]', '9', 'g'), '(null)') sunrise_local_shape,
               coalesce(regexp_replace(sunset_local, '[0-9]', '9', 'g'), '(null)') sunset_local_shape, count(*) n
        from sun_null group by all order by n desc""")
show("quarantine field rejections on sunrise/sunset",
     """select field, code, count(*) n from quar where table_name = 'forecast_daily_snapshots'
        and (field ilike '%sunrise%' or field ilike '%sunset%' or field ilike '%astro%') group by all order by all""")
show("wet_bulb_avg_c null share by target month",
     """select strftime(target_local_date, '%Y-%m') target_month, count(*) rows,
               count(*) filter (where wet_bulb_avg_c is null) wet_bulb_avg_null,
               round(100.0 * count(*) filter (where wet_bulb_avg_c is null) / count(*), 2) pct_null,
               count(*) filter (where wet_bulb_max_c is null) wet_bulb_max_null
        from daily group by 1 order by 1""")
show("wet_bulb_avg_c null by bronze_format",
     """select p.bronze_format, count(*) rows, count(*) filter (where d.wet_bulb_avg_c is null) wet_bulb_avg_null
        from daily d left join pub p using (ingestion_id) group by 1 order by 1""")

# ---------------- 6 ----------------
section("6. SUMMARY_JSON STRUCTURE")
for status in ("success", "partial"):
    con.execute(f"""create or replace temp table s as select summary_json::json j from pub where status = '{status}'
                    order by collected_at_utc desc nulls last limit 1""")
    if not scalar("select count(*) from s"):
        print(f"-- no '{status}' publication\n")
        continue
    show(f"[{status}] top-level keys", "select unnest(json_keys(j)) key from s")
    show(f"[{status}] cities keys (list lengths)",
         "select k key, json_type(j -> ('$.cities.' || k)) type, json_array_length(j -> ('$.cities.' || k)) len from (select j, unnest(json_keys(j -> '$.cities')) k from s)")
    show(f"[{status}] city_tables first-level keys (city ids)", "select unnest(json_keys(j -> '$.city_tables')) key from s")
    show(f"[{status}] city_tables.<city> keys (tables)",
         "select distinct unnest(json_keys(j -> ('$.city_tables.' || k))) key from (select j, unnest(json_keys(j -> '$.city_tables')) k from s)")
    show(f"[{status}] city_tables.<city>.<table> keys (counters)",
         """select distinct unnest(json_keys(j -> ('$.city_tables.' || k || '.observation_snapshots'))) key
            from (select j, unnest(json_keys(j -> '$.city_tables')) k from s)""")
    show(f"[{status}] horizons first-level keys (city ids)", "select unnest(json_keys(j -> '$.horizons')) key from s")
    show(f"[{status}] horizons.<city> keys",
         "select distinct unnest(json_keys(j -> ('$.horizons.' || k))) key from (select j, unnest(json_keys(j -> '$.horizons')) k from s)")
    show(f"[{status}] horizons.<city>.daily_coverage[0] keys",
         "select distinct unnest(json_keys(j -> ('$.horizons.' || k || '.daily_coverage[0]'))) key from (select j, unnest(json_keys(j -> '$.horizons')) k from s)")
    show(f"[{status}] tables / collection keys",
         """select 'tables' parent, unnest(json_keys(j -> '$.tables')) key from s
            union all select 'collection', unnest(json_keys(j -> '$.collection')) from s""")
show("top-level keys across all publications (publications holding each)",
     "select key, count(*) publications from (select unnest(json_keys(summary_json::json)) key from pub) group by 1 order by 1")
show("publications with nonempty city_tables / horizons, by status",
     """select status, count(*) pubs,
               count(*) filter (where json_array_length(json_keys(summary_json::json -> '$.city_tables')) > 0) with_city_tables,
               count(*) filter (where json_array_length(json_keys(summary_json::json -> '$.horizons')) > 0) with_horizons,
               count(*) filter (where json_array_length(summary_json::json -> '$.cities.requested') > 0) with_requested
        from pub group by 1 order by 1""")
show("collection_failed cities by count (from summaries)",
     """select json_array_length(summary_json::json -> '$.cities.collection_failed') failed, status, count(*) pubs
        from pub group by all order by all""")
show("expected hourly slots per local day (from horizons daily_coverage)",
     """select expected, count(*) n from (
          select unnest(json_extract(c, '$[*].expected_hour_slots'))::int expected from (
            select summary_json::json -> ('$.horizons.' || k || '.daily_coverage') c from
              (select summary_json, unnest(json_keys(summary_json::json -> '$.horizons')) k from pub)))
        group by 1 order by 1""")

# ---------------- 7 ----------------
section("7. LONDON / CITY FIRST COLLECTION")
show("first and last observed_at_utc per city",
     "select city_id, min(observed_at_utc) first_observed, max(observed_at_utc) last_observed, count(distinct ingestion_id) ingestions from obs group by 1 order by 1")
show("first and last collected_at_utc with valid rows per city (any table)",
     """select city_id, min(collected_at_utc) first_collected, max(collected_at_utc) last_collected, count(distinct ingestion_id) ingestions
        from (select city_id, collected_at_utc, ingestion_id from obs union all select city_id, collected_at_utc, ingestion_id from fc
              union all select city_id, collected_at_utc, ingestion_id from daily) group by 1 order by 1""")
show("gb_london ingestions with valid rows per table",
     """select 'obs' tbl, count(distinct ingestion_id) n, min(ingestion_date) first_date from obs where city_id = 'gb_london'
        union all select 'hourly', count(distinct ingestion_id), min(ingestion_date) from fc where city_id = 'gb_london'
        union all select 'daily', count(distinct ingestion_id), min(ingestion_date) from daily where city_id = 'gb_london'""")
show("publications by month: total vs with gb_london valid observations (summary)",
     """select strftime(collected_at_utc, '%Y-%m') month_utc, count(*) pubs,
               count(*) filter (where list_contains(json_extract_string(summary_json, '$.cities.requested[*]'), 'London')
                                   or list_contains(json_extract_string(summary_json, '$.cities.requested[*]'), 'Londres')) london_requested,
               count(*) filter (where list_contains(json_extract_string(summary_json, '$.cities.with_valid_observations[*]'), 'gb_london')) london_valid_obs
        from pub group by 1 order by 1""")
show("requested city labels (all publications)",
     "select city, count(*) pubs from (select unnest(json_extract_string(summary_json, '$.cities.requested[*]')) city from pub) group by 1 order by 1")

# ---------------- 8 ----------------
section("8. VOLUMES (last 3 full UTC months, by collected_at_utc)")
window = """collected_at_utc >= date_trunc('month', current_date) - interval 3 month
            and collected_at_utc < date_trunc('month', current_date)"""
show("rows per table, city, month",
     f"""select tbl, city_id, strftime(collected_at_utc, '%Y-%m') month_utc, count(*) n_rows, count(distinct ingestion_id) ingestions from (
           select 'obs' tbl, city_id, collected_at_utc, ingestion_id from obs union all
           select 'hourly', city_id, collected_at_utc, ingestion_id from fc union all
           select 'daily', city_id, collected_at_utc, ingestion_id from daily)
         where {window} group by all order by all""")
show("publications and quarantine per month",
     f"""select 'publications' tbl, strftime(collected_at_utc, '%Y-%m') month_utc, count(*) n from pub where {window} group by all
         union all select 'quarantine', strftime(p.collected_at_utc, '%Y-%m'), count(*) from quar q join pub p using (ingestion_id)
         where {window.replace('collected_at_utc', 'p.collected_at_utc')} group by all order by all""")
show("total rows per table", """select 'obs' tbl, count(*) n from obs union all select 'hourly', count(*) from fc
                               union all select 'daily', count(*) from daily union all select 'pub', count(*) from pub
                               union all select 'quar', count(*) from quar""")

section("RESULT")
if errors:
    print(f"ERRORS in {len(errors)} queries: {errors}")
    sys.exit(1)
print("DONE")
