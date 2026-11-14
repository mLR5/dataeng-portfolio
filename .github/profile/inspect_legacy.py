"""Read-only structure of the legacy Bronze archives (old envelope).

Prints only blob paths, key paths with types, and the values of non-sensitive
structural scalars (ids, timestamps, city names, statuses, counts, horizon).
Never prints URLs, hosts, keys, endpoints, query strings or similar values.
"""
import collections, json, logging, os, re

from azure.storage.blob import BlobServiceClient

logging.disable(logging.CRITICAL)  # never echo request URLs from the SDK
account, key = os.environ["ACCOUNT"], os.environ["STORAGE_KEY"]
service = BlobServiceClient(f"https://{account}.blob.core.windows.net", credential=key)
bronze = service.get_container_client("bronze")
ID = re.compile(r"^\d{8}_\d{6}(_[0-9a-f]{32})?$")
SENSITIVE = re.compile(r"url|uri|key|host|endpoint|token|secret|query|param|password|auth|(^|_)ip(_|$|addr)|user|machine|node|path|server|domain", re.I)
SAFE_KEY = re.compile(r"(^|_)(id|timestamp|time|date|city|cities|status|code|count|total|successful|failed|days|horizon|lang|language|format|version|type|duration|ms|seconds|name|country|tz_id|environment|env|pipeline|source|method|provider|forecast_days)($|_)|^(city|status|days|requested_cities)$", re.I)
SAFE_STR = re.compile(r"^[\w .:+\-/]{0,64}$")


def safe_value(path, value):
    plain = re.sub(r"\[\d*\]", "", path)
    leaf = plain.rsplit(".", 1)[-1]
    if any(SENSITIVE.search(p) for p in plain.split(".")):
        return "<redacted>"
    if value is None or isinstance(value, bool):
        return repr(value)
    if isinstance(value, (int, float)):
        return repr(value) if SAFE_KEY.search(leaf) or path.startswith("ingestion_metadata") or ".metadata." in f".{plain}" else "<num>"
    if isinstance(value, str):
        if (SAFE_KEY.search(leaf) and SAFE_STR.fullmatch(value) and "://" not in value
                and "=" not in value and "@" not in value and not re.search(r"\.(com|net|org|io|local|internal)\b", value, re.I)):
            return repr(value)
        return f"<str len={len(value)}>"
    return f"<{type(value).__name__}>"


RAW_KEYS = ("raw_data", "data")


def summarise(raw):
    if not isinstance(raw, dict):
        return f"<{type(raw).__name__}>"
    loc = raw.get("location") if isinstance(raw.get("location"), dict) else {}
    forecast = raw.get("forecast") if isinstance(raw.get("forecast"), dict) else {}
    days = forecast.get("forecastday") if isinstance(forecast.get("forecastday"), list) else []
    cur = raw.get("current") if isinstance(raw.get("current"), dict) else {}
    return (f"keys={sorted(raw)} location={loc.get('name')!r}|{loc.get('country')!r}|{loc.get('tz_id')!r}"
            f" lat={loc.get('lat')!r} lon={loc.get('lon')!r} current.last_updated={cur.get('last_updated')!r}"
            f" forecastday={len(days)} hours={[len(d.get('hour') or []) for d in days if isinstance(d, dict)]}"
            f" dates={[d.get('date') for d in days if isinstance(d, dict)]}")


def walk(value, path, paths, out):
    if paths is not None:
        paths[path][type(value).__name__] += 1
    if isinstance(value, dict):
        for k, v in value.items():
            k = k if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", k) else "<nonidentifier-key>"
            child = f"{path}.{k}" if path else k
            if out is not None and k in RAW_KEYS and path:
                out.append((child, summarise(v)))
            else:
                walk(v, child, paths, out)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            walk(item, f"{path}[{i}]" if out is not None else f"{path}[]", paths, out)
    elif out is not None:
        out.append((path, safe_value(path, value)))


names = sorted(b.name for b in bronze.list_blobs(name_starts_with="weather/weatherapi/") if b.name.endswith(".json"))
print("json blobs under prefix:", len(names), flush=True)
legacy, paths, top_current = [], collections.defaultdict(collections.Counter), collections.Counter()
for name in names:
    try:
        data = json.loads(bronze.download_blob(name).readall())
    except Exception as error:  # report the class only
        print("unreadable:", name, type(error).__name__)
        continue
    if not isinstance(data, dict):
        print("non-object:", name)
        continue
    ingestion_id = data.get("ingestion_id")
    if "ingestion_metadata" in data or not (isinstance(ingestion_id, str) and ID.fullmatch(ingestion_id)):
        legacy.append((name, data))
    else:
        top_current[tuple(sorted(data))] += 1
print("legacy archives:", len(legacy))
print("current top-level key sets:", dict(top_current))

print("\n######## PER ARCHIVE (envelope scalars; WeatherAPI responses summarised)")
for name, data in legacy:
    print(f"\n== {name}")
    out = []
    walk(data, "", None, out)
    for path, value in out:
        print(f"  {path} = {value}")

print("\n######## LEGACY KEY PATHS WITH TYPES (all legacy archives)")
for name, data in legacy:
    walk(data, "", paths, None)
for path in sorted(paths):
    print(f"{path or '<root>':<90} {dict(paths[path])}")
