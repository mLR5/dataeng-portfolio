"""One real ingestion followed by read-only checks of its Bronze output.

Run after azure/login in the existing dev environment. Credentials and raw API
responses are never included in the validation report.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class ValidationError(RuntimeError):
    pass


REPORT = {"result": "failed", "started_at": datetime.now(timezone.utc).isoformat(), "checks": []}
STORAGE_AUTH = "login"


def check(name, condition, detail):
    REPORT["checks"].append({"check": name, "passed": bool(condition), "detail": detail})
    print(f"{'PASS' if condition else 'FAIL'}: {name}: {detail}", flush=True)
    if not condition:
        raise ValidationError(f"Validation failed at {name}")


def run_az(args):
    return subprocess.run(
        ["az", *args, "--only-show-errors", "--output", "json"],
        capture_output=True, text=True, timeout=60, check=False,
    )


def az_json(args):
    result = run_az(args)
    if result.returncode:
        # stdout may contain keys; stderr is intentionally not echoed.
        raise ValidationError("Azure CLI failed for " + " ".join(args[:3]))
    return json.loads(result.stdout)


def storage_json(args, account):
    global STORAGE_AUTH
    result = run_az(["storage", "blob", *args, "--account-name", account, "--auth-mode", STORAGE_AUTH])
    if result.returncode and STORAGE_AUTH == "login":
        # Same deployment identity; Azure CLI resolves an account key through ARM.
        # No key is read into this script or printed.
        STORAGE_AUTH = "key"
        REPORT["storage_authentication"] = "Azure CLI account-key lookup using deployment identity"
        result = run_az(["storage", "blob", *args, "--account-name", account, "--auth-mode", STORAGE_AUTH])
    if result.returncode:
        codes = re.findall(r"(?:ErrorCode:|ERROR: \()\s*([A-Za-z][A-Za-z0-9]+)", result.stderr)
        detail = ", ".join(codes) or "Azure CLI returned a non-zero exit status"
        raise ValidationError(f"Storage verification failed for {args[0]} using {STORAGE_AUTH}: {detail}")
    return json.loads(result.stdout)


def exists(account, container, blob):
    return storage_json(["exists", "--container-name", container, "--name", blob], account)["exists"]


def validate_payload(data, ingestion_id, summary):
    check("bronze_ingestion_id", data.get("ingestion_id") == ingestion_id, "Bronze matches the triggered ingestion")
    check("unique_id_format", bool(re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{32}", ingestion_id)), "Timestamp and UUID identifier")
    timestamp = datetime.fromisoformat(data["ingestion_timestamp"].replace("Z", "+00:00"))
    check("explicit_utc_timestamp", timestamp.utcoffset() is not None and timestamp.utcoffset().total_seconds() == 0, "UTC offset is explicit")
    cities = data.get("cities", [])
    expected = {"Paris", "Londres", "Bruxelles"}
    check("expected_cities", len(cities) == 3 and {item.get("city") for item in cities} == expected, "Paris, Londres, Bruxelles")
    successful = sum(item.get("status") == "success" for item in cities)
    failed = len(cities) - successful
    counts = {"total_cities": 3, "successful": successful, "failed": failed}
    check("consistent_counts", data.get("summary") == counts and summary.get("cities_retrieved") == successful and summary.get("cities_failed") == failed, f"{successful} successful; {failed} failed")
    check("complete_collection", data.get("status") == summary.get("status") == "success" and successful == 3, "All three cities retrieved")
    for item in cities:
        payload = item.get("data")
        check("raw_payload_" + item["city"], isinstance(payload, dict) and all(key in payload for key in ("location", "current", "forecast")), "WeatherAPI location, current and forecast preserved")
    return timestamp


def validate():
    project = os.environ.get("AZURE_PROJECT_NAME", "").strip()
    environment = os.environ.get("AZURE_ENVIRONMENT", "").strip()
    account = os.environ.get("AZURE_STORAGE_ACCOUNT", "").strip()
    check("validation_configuration", bool(project and environment and account), "Deployment resource settings available")
    resource_group = f"rg-{project}-{environment}"
    app = f"func-{project}-{environment}"
    app_args = ["--resource-group", resource_group, "--name", app]

    app_info = az_json(["functionapp", "show", *app_args])
    properties = app_info.get("properties", app_info)
    state = properties.get("state")
    REPORT["function_app_state"] = state
    REPORT["function_app_enabled"] = properties.get("enabled")
    check("function_app_running", isinstance(state, str) and state.lower() == "running", f"Observed Function App state: {state!r}")
    functions = az_json(["functionapp", "function", "list", *app_args])
    indexed = {item["name"].rsplit("/", 1)[-1] for item in functions}
    expected = {"manual_weather_trigger", "weather_ingestion_trigger", "weather_bronze_processor"}
    REPORT["indexed_functions"] = sorted(indexed & expected)
    check("function_indexing", expected <= indexed, f"{len(indexed & expected)}/3 expected functions indexed")

    reference = os.environ.get("VALIDATION_REFERENCE")
    if reference:
        previous = json.loads(Path(reference).read_text(encoding="utf-8"))
        http_status = previous["http_status"]
        summary = previous["summary"]
        summary["blob_url"] = f"https://{account}.blob.core.windows.net/staging/raw-ingestion/{summary['ingestion_id']}.json"
        REPORT["reference_run"] = previous["run_url"]
        print("Resuming verification of the existing ingestion; no HTTP invocation", flush=True)
    else:
        keys = az_json(["functionapp", "function", "keys", "list", *app_args, "--function-name", "manual_weather_trigger"])
        key = keys.get("default")
        check("function_authentication", isinstance(key, str) and bool(key), "Function-scoped default key available; value omitted")
        request = Request(
            f"https://{properties['defaultHostName']}/api/weather/ingest",
            data=b"{}", method="POST",
            headers={"x-functions-key": key, "Content-Type": "application/json"},
        )
        # Exactly one invocation: retrying a POST could create a second ingestion.
        try:
            with urlopen(request, timeout=180) as result:
                http_status, body = result.status, result.read()
        except HTTPError as error:
            http_status, body = error.code, error.read()
        except URLError:
            raise ValidationError("Cannot reach the ingestion endpoint") from None
        finally:
            key = None
            keys = None
        try:
            summary = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise ValidationError(f"Ingestion returned HTTP {http_status} without a JSON result") from None
    REPORT["http_status"] = http_status
    REPORT["collection_status"] = summary.get("status")
    REPORT["cities_retrieved"] = summary.get("cities_retrieved")
    REPORT["cities_failed"] = summary.get("cities_failed")
    ingestion_id = summary.get("ingestion_id", "")
    check("ingestion_response", isinstance(ingestion_id, str) and bool(re.fullmatch(r"\d{8}_\d{6}(?:_[0-9a-f]{32})?", ingestion_id)), f"HTTP {http_status}; ingestion {ingestion_id}")
    REPORT["ingestion_id"] = ingestion_id
    check("queue_publication", bool(summary.get("queue_message_id")), "Ingestion returned a queue message identifier")
    REPORT["queue_message_id"] = summary["queue_message_id"]

    parsed = urlparse(summary.get("blob_url", ""))
    staging_path = parsed.path.lstrip("/").split("/", 1)
    check("staging_reference", parsed.hostname == f"{account}.blob.core.windows.net" and staging_path == ["staging", f"raw-ingestion/{ingestion_id}.json"], "Staging reference matches this account and ingestion")
    event_time = datetime.strptime(ingestion_id[:15], "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
    bronze_path = f"weather/weatherapi/{event_time:%Y/%m/%d/%H}/{ingestion_id}_weather_data.json"
    REPORT["bronze_path"] = "bronze/" + bronze_path
    print("Waiting for the queue consumer to produce this Bronze file", flush=True)
    deadline = time.monotonic() + 180
    found = False
    while time.monotonic() < deadline:
        if exists(account, "bronze", bronze_path):
            found = True
            break
        time.sleep(5)
    check("bronze_created", found, "The exact ingestion file exists in Bronze")

    with tempfile.TemporaryDirectory() as temporary:
        target = Path(temporary) / "bronze.json"
        storage_json(["download", "--container-name", "bronze", "--name", bronze_path, "--file", str(target), "--overwrite", "true"], account)
        data = json.loads(target.read_text(encoding="utf-8"))
    timestamp = validate_payload(data, ingestion_id, summary)
    check("utc_partition", bronze_path.startswith(f"weather/weatherapi/{timestamp:%Y/%m/%d/%H}/"), "Bronze partition matches its UTC ingestion timestamp")

    deadline = time.monotonic() + 60
    cleaned = False
    while time.monotonic() < deadline:
        if not exists(account, "staging", staging_path[1]):
            cleaned = True
            break
        time.sleep(5)
    check("staging_cleaned", cleaned, "The queue consumer removed this staging file")
    check("http_success", http_status == 200, "Successful ingestion HTTP response")
    REPORT.setdefault("storage_authentication", "Azure CLI identity / Azure AD")
    REPORT["result"] = "success"


def main():
    try:
        validate()
    except Exception as error:
        # Only our own controlled error messages are included, never raw CLI/HTTP output.
        REPORT["error"] = str(error) if isinstance(error, ValidationError) else type(error).__name__
        print("Validation failed: " + REPORT["error"], flush=True)
    finally:
        REPORT["finished_at"] = datetime.now(timezone.utc).isoformat()
        Path("validation-report.json").write_text(json.dumps(REPORT, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            lines = ["# Azure Bronze validation", "", f"Result: **{REPORT['result']}**", "", "| Check | Result |", "| --- | --- |"]
            lines.extend(f"| {item['check']} | {'PASS' if item['passed'] else 'FAIL'} |" for item in REPORT["checks"])
            with open(summary, "a", encoding="utf-8") as output:
                output.write("\n".join(lines) + "\n")
    return 0 if REPORT["result"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
