"""Run the Silver notebook rules on the legacy Bronze archives (read-only evidence).

Downloads every archive of January 2026 under bronze/weather/weatherapi/, keeps the
legacy envelopes (top-level ``ingestion_metadata``), and runs ``transform_bronze``
on the exact bytes. Prints only paths, statuses, counts and rejection codes.
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json, logging, os, sys, types
from pathlib import Path

from azure.storage.blob import BlobServiceClient

NOTEBOOKS, TARGET = Path(sys.argv[1]), Path(sys.argv[2])
CELL = "# COMMAND ----------"


def _execute(name, namespace, stack=()):
    """Same loader as databricks/tests/conftest.py: cells, markdown and %run."""
    path = NOTEBOOKS / f"{name}.py"
    if name in stack:
        raise RecursionError(f"%run cycle through {name}")
    source = path.read_text(encoding="utf-8")
    if not source.startswith("# Databricks notebook source\n"):
        raise ValueError(f"{path} is not a Databricks notebook source file")
    for index, cell in enumerate(source.split(CELL)):
        lines = [line for line in cell.strip("\n").splitlines() if line.strip()]
        if lines and lines[0] == "# Databricks notebook source":
            lines = lines[1:]
        if not lines:
            continue
        if all(line.startswith("# MAGIC") for line in lines):
            command = lines[0].removeprefix("# MAGIC").strip()
            if command.startswith("%run "):
                _execute(command.removeprefix("%run ").strip().removeprefix("./"), namespace, (*stack, name))
            continue
        exec(compile("\n".join(lines) + "\n", f"{path}#cell{index}", "exec"), namespace)


def load_notebook(name):
    module = types.ModuleType(name)
    module.__file__ = str(NOTEBOOKS / f"{name}.py")
    sys.modules[name] = module
    _execute(name, module.__dict__)
    return module


rules = load_notebook("silver_rules")
logging.disable(logging.CRITICAL)
service = BlobServiceClient(f"https://{os.environ['ACCOUNT']}.blob.core.windows.net", credential=os.environ["STORAGE_KEY"])
bronze = service.get_container_client("bronze")
names = sorted(b.name for b in bronze.list_blobs(name_starts_with="weather/weatherapi/2026/01/") if b.name.endswith(".json"))


def fetch(name):
    raw = bronze.download_blob(name).readall()
    try:
        legacy = "ingestion_metadata" in json.loads(raw)
    except ValueError:
        legacy = False
    if legacy:
        target = TARGET / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    return name, legacy


with ThreadPoolExecutor(16) as pool:
    legacy = sorted(name for name, is_legacy in pool.map(fetch, names) if is_legacy)
print(f"January 2026 archives scanned: {len(names)} | legacy envelopes: {len(legacy)}")

totals = Counter()
codes_total = Counter()
statuses = Counter()
city_rows = Counter()
for name in legacy:
    raw = (TARGET / name).read_bytes()
    result = rules.transform_bronze(raw, name)
    codes = Counter(r.code for r in result.rejections)
    scopes = Counter(getattr(r, "scope", "row") for r in result.rejections)
    statuses[result.status] += 1
    codes_total.update(codes)
    totals["observations"] += result.observations.num_rows
    totals["forecasts_hourly"] += result.forecasts.num_rows
    totals["forecasts_daily"] += result.forecasts_daily.num_rows
    for table in (result.observations, result.forecasts):
        city_rows.update(f"{table is result.observations and 'obs' or 'fc'}:{c}" for c in table.column("city_id").to_pylist())
    print(f"{name.removeprefix('weather/weatherapi/')} status={result.status} format={result.summary.get('bronze_format')}"
          f" obs={result.observations.num_rows} fc_hourly={result.forecasts.num_rows}"
          f" fc_daily={result.forecasts_daily.num_rows} reason={result.summary.get('reason')}"
          f" codes={dict(sorted(codes.items()))} scopes={dict(scopes)}"
          f" unknown_paths={len(result.summary.get('unknown_paths') or [])}"
          f" legacy_unknown={rules.legacy_unknown_paths(json.loads(raw))}")

print("\nTOTALS", dict(totals))
print("statuses", dict(statuses))
print("rejection codes", dict(sorted(codes_total.items())))
print("rows per table:city", dict(sorted(city_rows.items())))
