"""Read-only profile of Bronze (JSON structure on a sample) and Silver (Delta tables).

Prints structure and statistics only: key paths, types, counts, ranges, codes.
"""
import collections, json, os, re, sys
from datetime import datetime, timezone

import duckdb
from azure.storage.blob import BlobServiceClient
from deltalake import DeltaTable

account, key = os.environ["ACCOUNT"], os.environ["STORAGE_KEY"]
service = BlobServiceClient(f"https://{account}.blob.core.windows.net", credential=key)
CANON = re.compile(r"^weather/weatherapi/(\d{4})/(\d{2})/\d{2}/\d{2}/\d{8}_\d{6}(_[0-9a-f]{32})?_weather_data\.json$")


def section(title):
    print(f"\n######## {title}", flush=True)


# ---------------- Bronze ----------------
section("BRONZE LISTING")
bronze = service.get_container_client("bronze")
blobs = [b for b in bronze.list_blobs(name_starts_with="weather/weatherapi/") if CANON.match(b.name)]
blobs.sort(key=lambda b: b.name)
months = collections.Counter("/".join(b.name.split("/")[2:4]) for b in blobs)
sizes = [b.size for b in blobs]
print("canonical archives:", len(blobs), "| per month:", dict(sorted(months.items())))
print("size bytes min/median/max:", min(sizes), sorted(sizes)[len(sizes) // 2], max(sizes))
print("legacy ids (no uuid):", sum(1 for b in blobs if not CANON.match(b.name).group(3)))
other = [b.name for b in bronze.list_blobs() if b.name.endswith(".json") and not CANON.match(b.name)]
print("other json blobs:", len(other), "| prefixes:", collections.Counter("/".join(n.split("/")[:2]) for n in other).most_common(5))

# Sample: first and last of each month, plus an even spread (max 60 files).
sample = {}
by_month = collections.defaultdict(list)
for b in blobs:
    by_month["/".join(b.name.split("/")[2:4])].append(b)
for items in by_month.values():
    sample[items[0].name] = items[0]; sample[items[-1].name] = items[-1]
step = max(1, len(blobs) // 30)
for b in blobs[::step]:
    sample[b.name] = b

paths = collections.defaultdict(lambda: collections.Counter())
docs = 0
facts = collections.defaultdict(collections.Counter)


def walk(value, path):
    kind = type(value).__name__
    paths[path][kind] += 1
    if isinstance(value, dict):
        for k, v in value.items():
            walk(v, f"{path}.{k}" if path else k)
    elif isinstance(value, list):
        for item in value:
            walk(item, f"{path}[]")


for name in sorted(sample):
    data = json.loads(bronze.download_blob(name).readall())
    docs += 1
    walk(data, "")
    facts["status"][data.get("status")] += 1
    for city in data.get("cities", []):
        facts["city.city"][city.get("city")] += 1
        facts["city.status"][city.get("status")] += 1
        d = city.get("data") or {}
        loc = d.get("location") or {}
        facts["location.name|country|tz"][f"{loc.get('name')}|{loc.get('country')}|{loc.get('tz_id')}"] += 1
        days = (d.get("forecast") or {}).get("forecastday") or []
        facts["forecastday count"][len(days)] += 1
        for day in days:
            facts["hours per day"][len(day.get("hour") or [])] += 1
        cur = d.get("current") or {}
        if cur.get("last_updated"):
            facts["current.last_updated minute"][cur["last_updated"][-2:]] += 1

section(f"BRONZE JSON STRUCTURE ({docs} sampled files)")
for path in sorted(paths):
    kinds = paths[path]
    print(f"{path:80s} {dict(kinds)}")
section("BRONZE SAMPLE FACTS")
for k, c in facts.items():
    print(k, dict(c.most_common(12)))

# ---------------- Silver ----------------
root = f"abfss://datalake@{account}.dfs.core.windows.net/silver/weatherapi"
opts = {"account_name": account, "account_key": key}
con = duckdb.connect()
for name, sub in (("obs", "observation_snapshots"), ("fc", "forecast_hourly_snapshots"),
                  ("pub", "_control/publications"), ("quar", "_control/quarantine")):
    table = DeltaTable(f"{root}/{sub}", storage_options=opts)
    arrow = table.to_pyarrow_table()
    con.register(name, arrow)
    section(f"SILVER SCHEMA {sub} (version {table.version()}, {arrow.num_rows} rows)")
    for field in arrow.schema:
        nulls = arrow.column(field.name).null_count
        print(f"{field.name:32s} {str(field.type):28s} nullable={field.nullable!s:5s} nulls={nulls} ({100 * nulls / max(arrow.num_rows, 1):.2f}%)")


def q(title, sql):
    print(f"\n-- {title}")
    res = con.execute(sql)
    cols = [d[0] for d in res.description]
    print(" | ".join(cols))
    for row in res.fetchall():
        print(" | ".join(str(v) for v in row))


section("SILVER OBSERVATIONS")
q("range per city", "select city_id, count(*) n, min(observed_at_utc) first_obs, max(observed_at_utc) last_obs, count(distinct ingestion_id) ingestions from obs group by 1 order by 1")
q("same observed_at repeated across collections (rows per city+observed_at)", "select copies, count(*) n from (select city_id, observed_at_utc, count(*) copies from obs group by 1,2) group by 1 order by 1")
q("distinct observed_at per city and UTC hour", "select k, count(*) n from (select city_id, date_trunc('hour', observed_at_utc) h, count(distinct observed_at_utc) k from obs group by 1,2) group by 1 order by 1")
q("minute of observed_at", "select minute(observed_at_utc) m, count(*) n from obs group by 1 order by n desc limit 8")
q("collection minus observation (minutes) quantiles", "select quantile_cont(epoch(collected_at_utc - observed_at_utc)/60, [0, 0.05, 0.5, 0.95, 1]) from obs")
q("hours without observation per city (between first and last)", "with h as (select city_id, date_trunc('hour', observed_at_utc) h from obs group by 1,2), b as (select city_id, min(h) a, max(h) z, count(*) n from h group by 1) select city_id, n hours_with_obs, (epoch(z - a)/3600 + 1)::int hours_span, (epoch(z - a)/3600 + 1)::int - n missing from b order by 1")
q("observations per city and NEAREST hour (rounded)", "select k, count(*) n from (select city_id, time_bucket(interval 1 hour, observed_at_utc + interval 30 minute) h, count(distinct observed_at_utc) k from obs group by 1,2) group by 1 order by 1")
q("hours without observation per city, nearest-hour rounding", "with h as (select city_id, time_bucket(interval 1 hour, observed_at_utc + interval 30 minute) h from obs group by 1,2), b as (select city_id, min(h) a, max(h) z, count(*) n from h group by 1) select city_id, n hours_with_obs, (epoch(z - a)/3600 + 1)::int hours_span, (epoch(z - a)/3600 + 1)::int - n missing from b order by 1")
q("ingestions without observation per city (collection or rejection gaps)", "select c.city_id, (select count(*) from pub where status in ('success','partial')) usable_pub, count(distinct o.ingestion_id) with_obs from (select distinct city_id from obs) c join obs o using (city_id) group by 1 order by 1")
q("collections per UTC hour of day", "select hour(collected_at_utc) h, minute(collected_at_utc) m, count(distinct ingestion_id) from obs group by 1,2 order by 3 desc limit 6")
q("observation numeric ranges", "select min(temperature_c), max(temperature_c), min(feels_like_c), max(feels_like_c), min(humidity_pct), max(humidity_pct), min(pressure_mb), max(pressure_mb), min(wind_kph), max(wind_kph), min(wind_degree), max(wind_degree), min(precipitation_mm), max(precipitation_mm), min(cloud_pct), max(cloud_pct) from obs")
q("source location per city", "select city_id, city_query, source_location_name, source_country, source_timezone, count(*) from obs group by all order by 1")

section("SILVER FORECASTS")
q("per city", "select city_id, count(*) n, min(target_at_utc), max(target_at_utc), count(distinct ingestion_id) ingestions from fc group by 1 order by 1")
q("targets per ingestion and city", "select k, count(*) n from (select ingestion_id, city_id, count(*) k from fc group by 1,2) group by 1 order by n desc limit 10")
q("local days per ingestion and city", "select k, count(*) n from (select ingestion_id, city_id, count(distinct target_local_date) k from fc group by 1,2) group by 1 order by 1")
q("lead_time_hours quantiles", "select min(lead_time_hours), quantile_cont(lead_time_hours, [0.05, 0.5, 0.95]), max(lead_time_hours) from fc")
q("past targets at collection", "select target_is_past_at_collection, min(lead_time_hours), max(lead_time_hours), count(*) from fc group by 1")
q("forecast versions per city and target hour", "select k, count(*) n from (select city_id, target_at_utc, count(*) k from fc group by 1,2) group by 1 order by 1")
q("minute of target_at", "select minute(target_at_utc) m, count(*) from fc group by 1 order by 2 desc limit 5")
q("DST: local days with 23 or 25 forecast hours", "select k, count(*) from (select ingestion_id, city_id, target_local_date, count(*) k from fc group by 1,2,3) group by 1 order by 1")
q("forecast numeric ranges", "select min(temperature_c), max(temperature_c), min(chance_of_rain_pct), max(chance_of_rain_pct), min(chance_of_snow_pct), max(chance_of_snow_pct), min(precipitation_mm), max(precipitation_mm) from fc")
q("collections per UTC day and city (completeness)", "select k, count(*) n_days from (select city_id, cast(collected_at_utc as date) d, count(distinct ingestion_id) k from obs group by 1,2) group by 1 order by 1")

section("SILVER CONDITIONS")
q("codes observed and forecast", "select condition_code, any_value(condition_text) text_example, count(distinct condition_text) texts, sum(o) obs_rows, sum(f) fc_rows, any_value(is_day) from (select condition_code, condition_text, is_day, 1 o, 0 f from obs union all select condition_code, condition_text, is_day, 0, 1 from fc) group by 1 order by 1")
q("texts per code and day/night", "select condition_code, is_day, count(distinct condition_text) from (select condition_code, condition_text, is_day from obs union all select condition_code, condition_text, is_day from fc) group by 1,2 having count(distinct condition_text) > 1 order by 1 limit 20")

section("SILVER CONTROL")
q("publications per month and status", "select strftime(collected_at_utc, '%Y-%m') mon, status, count(*) from pub group by all order by 1, 2")
q("quarantine by table, code, city", "select table_name, code, city_query, count(*) from quar group by all order by 4 desc limit 25")
q("summary keys sample", "select summary_json from pub where status = 'success' order by collected_at_utc desc limit 1")
