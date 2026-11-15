#!/usr/bin/env python3
"""Read-only Azure actual-cost, storage metadata and Databricks usage audit."""
import base64
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gold_cloud_client", ROOT / "scripts/gold_cloud_run.py")
cloud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud)
PERIODS = (("november", "2026-11-01", "2026-11-16"), ("october", "2026-10-01", "2026-11-01"))
RUNS = {"production": 205033046212982, "stage": 658348901026035,
        "verification": 859710188956004, "failed_validation": 360314293511069}


class AuditError(RuntimeError):
    def __init__(self, code, status=None):
        self.code, self.status = code, status
        super().__init__(code)


def alias(value):
    return "resource-" + hashlib.sha256(value.lower().encode()).hexdigest()[:12]


def error_report(error):
    return {"available": False, "error": error.code if isinstance(error, AuditError) else type(error).__name__,
            **({"http_status": error.status} if isinstance(error, AuditError) and error.status else {})}


class Costs:
    def __init__(self):
        self.subscription = cloud.az("account", "show")["id"]
        cloud.mask(self.subscription)
        self.scope = "/subscriptions/" + self.subscription
        self.access = cloud.az("account", "get-access-token", "--resource", "https://management.azure.com/")["accessToken"]
        cloud.mask(self.access)

    def post(self, url, body):
        if not url.startswith("https://management.azure.com/"):
            raise AuditError("invalid_cost_continuation_host")
        for attempt in range(5):
            request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                      headers={"Authorization": "Bearer " + self.access, "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    return json.loads(response.read(), parse_float=Decimal)
            except urllib.error.HTTPError as error:
                if error.code in (429, 500, 502, 503, 504) and attempt < 4:
                    delays = [error.headers.get(k, "0") for k in
                              ("Retry-After", "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after",
                               "x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after")]
                    delay = max([15 * (attempt + 1)] + [int(x) for x in delays if str(x).isdigit()])
                    if delay > 60:
                        raise AuditError("cost_throttle_retry_exceeds_audit_budget", error.code) from None
                    time.sleep(delay)
                    continue
                try:
                    code = json.loads(error.read()).get("error", {}).get("code", "cost_api_error")
                except (ValueError, AttributeError):
                    code = "cost_api_error"
                raise AuditError(str(code), error.code) from None
            except urllib.error.URLError:
                raise AuditError("cost_api_transport_failure") from None

    def query(self, scope, start, end, dimensions):
        body = {"type": "ActualCost", "timeframe": "Custom", "timePeriod": {
            "from": start + "T00:00:00Z", "to": end + "T00:00:00Z"}, "dataset": {
            "granularity": "Daily", "aggregation": {"cost": {"name": "Cost", "function": "Sum"}},
            "grouping": [{"type": "Dimension", "name": name} for name in dimensions]}}
        url = "https://management.azure.com" + scope + "/providers/Microsoft.CostManagement/query?api-version=2023-11-01"
        rows, pages, seen = [], 0, set()
        while url:
            if url in seen or pages >= 200:
                raise AuditError("cost_pagination_not_complete")
            seen.add(url)
            result = self.post(url, body)
            properties = result["properties"]
            names = [c["name"] for c in properties["columns"]]
            for values in properties.get("rows", []):
                row = dict(zip(names, values))
                date = str(row.get("UsageDate", ""))
                if not re.fullmatch(r"\d{8}", date):
                    raise AuditError("missing_daily_usage_date")
                date = date[:4] + "-" + date[4:6] + "-" + date[6:]
                if start <= date < end:
                    row["date"] = date
                    rows.append(row)
            pages += 1
            url = properties.get("nextLink")
        return rows, pages


def money(value):
    return str(Decimal(str(value)))


def summarize_rows(rows, group, linked_ids=()):
    details, other, totals, project_totals = [], defaultdict(Decimal), defaultdict(Decimal), defaultdict(Decimal)
    groups = set()
    for row in rows:
        resource = str(row.get("ResourceId") or "")
        group_name = str(row.get("ResourceGroupName") or row.get("ResourceGroup") or "")
        if not group_name and "/resourcegroups/" in resource.lower():
            group_name = resource.lower().split("/resourcegroups/", 1)[1].split("/", 1)[0]
        cost, currency = Decimal(str(row["Cost"])), str(row["Currency"])
        totals[currency] += cost
        if group_name.lower() == group.lower():
            project_totals[currency] += cost
            details.append({"date": row["date"], "scope": "project", "resource": alias(resource) if resource else "unassigned",
                            "service": row.get("ServiceName", "unassigned"), "cost": money(cost), "currency": currency})
        else:
            other[(row["date"], currency)] += cost
            groups.add(group_name.lower())
    return {"project_daily_resources": details,
            "other_daily_total": [{"date": date, "scope": "other", "cost": money(cost), "currency": currency}
                                  for (date, currency), cost in sorted(other.items())],
            "total_by_currency": {k: money(v) for k, v in totals.items()},
            "project_total_by_currency": {k: money(v) for k, v in project_totals.items()},
            "other_total_by_currency": {c: money(sum((v for (d, cur), v in other.items() if cur == c), Decimal(0)))
                                        for c in {cur for _, cur in other}},
            "other_resource_groups_count": len(groups)}


def collect_costs(costs, group):
    result = {}
    for label, start, end in PERIODS:
        report = {"period_start": start, "period_end_exclusive": end}
        scope = costs.scope
        try:
            try:
                rows, pages = costs.query(scope, start, end, ["ResourceGroupName", "ServiceName", "ResourceId"])
            except AuditError as error:
                if error.status == 400:
                    rows, pages = costs.query(scope, start, end, ["ResourceGroup", "ServiceName", "ResourceId"])
                else:
                    raise
            report.update(available=True, scope="subscription", pages=pages, **summarize_rows(rows, group))
        except AuditError as error:
            report["subscription_query"] = error_report(error)
            scope += "/resourceGroups/" + group
            try:
                rows, pages = costs.query(scope, start, end, ["ResourceId", "ServiceName"])
                for row in rows:
                    row["ResourceGroupName"] = group
                report.update(available=True, scope="project_only_fallback", pages=pages, **summarize_rows(rows, group))
            except Exception as fallback_error:
                report.update(error_report(fallback_error))
        try:
            meter_rows, meter_pages = costs.query(costs.scope + "/resourceGroups/" + group,
                start, end, ["ResourceType", "MeterSubCategory", "Meter"])
            report["project_meters"] = {"available": True, "pages": meter_pages, "rows": [
                {"date": r["date"], "resource_type": r.get("ResourceType"),
                 "meter_subcategory": r.get("MeterSubCategory"), "meter": r.get("Meter"),
                 "cost": money(r["Cost"]), "currency": r["Currency"]} for r in meter_rows]}
        except Exception as error:
            report["project_meters"] = error_report(error)
        result[label] = report
    return result


def linked_workspaces(group):
    report = []
    try:
        resources = cloud.az("resource", "list", "--resource-group", group,
                             "--resource-type", "Microsoft.Insights/components")
        for item in resources:
            details = cloud.az("resource", "show", "--ids", item["id"], "--api-version", "2020-02-02")
            resource_id = details.get("properties", {}).get("WorkspaceResourceId")
            if resource_id:
                parts = resource_id.lower().split("/")
                rg = parts[parts.index("resourcegroups") + 1] if "resourcegroups" in parts else ""
                report.append({"resource": alias(resource_id), "scope": "project" if rg == group.lower() else "other",
                               "attribution": "linked_workspace_cost_may_be_shared_not_wholly_allocated_to_project"})
        return {"available": True, "links": report}
    except Exception as error:
        return error_report(error)


def cli_blob_page(account, container, marker, key=None):
    command = ["az", "storage", "blob", "list", "--account-name", account, "--container-name", container,
               "--auth-mode", "key" if key else "login", "--num-results", "5000", "--show-next-marker",
               "--only-show-errors", "-o", "json"]
    if marker:
        command += ["--marker", marker]
    env = dict(os.environ)
    if key:
        env["AZURE_STORAGE_KEY"] = key
    process = subprocess.run(command, capture_output=True, text=True, env=env)
    if process.returncode:
        raise AuditError("blob_metadata_listing_unavailable")
    result = json.loads(process.stdout)
    if isinstance(result, dict):
        return result.get("items", result.get("value", [])), result.get("nextMarker", "")
    return [r for r in result if "name" in r], next((r.get("nextMarker", "") for r in result if "nextMarker" in r), "")


def storage_footprint(account, group):
    categories = (("bronze", "weather/", "bronze/weather"),
                  ("datalake", "silver/weatherapi/", "silver/weatherapi_v1"),
                  ("datalake", "silver/weatherapi_v2/", "silver/weatherapi_v2"),
                  ("datalake", "silver/_validation/", "silver/_validation"),
                  ("datalake", "gold/weatherapi/", "gold/weatherapi"),
                  ("datalake", "gold/_validation/", "gold/_validation"))
    totals = {label: {"blobs": 0, "active_blob_bytes": 0} for _, _, label in categories}
    result = {"definition": "active_listed_blob_contentLength_only_not_physical_billed_replicated_bytes", "containers": {}}
    key = None
    for container in ("bronze", "datalake"):
        marker, seen, pages = "", set(), 0
        counts = {"blobs": 0, "active_blob_bytes": 0, "unclassified_blobs": 0, "unclassified_bytes": 0}
        try:
            while True:
                try:
                    rows, next_marker = cli_blob_page(account, container, marker, key)
                except AuditError:
                    if key is not None:
                        raise
                    keys = cloud.az("storage", "account", "keys", "list", "--account-name", account,
                                    "--resource-group", group)
                    key = keys[0]["value"]
                    cloud.mask(key)
                    rows, next_marker = cli_blob_page(account, container, marker, key)
                for row in rows:
                    size = int(row.get("properties", {}).get("contentLength") or 0)
                    counts["blobs"] += 1
                    counts["active_blob_bytes"] += size
                    matches = [label for c, prefix, label in categories if c == container and row["name"].startswith(prefix)]
                    if len(matches) > 1:
                        raise AuditError("storage_prefix_double_count")
                    if matches:
                        totals[matches[0]]["blobs"] += 1
                        totals[matches[0]]["active_blob_bytes"] += size
                    else:
                        counts["unclassified_blobs"] += 1
                        counts["unclassified_bytes"] += size
                pages += 1
                if not next_marker:
                    break
                if next_marker in seen or pages >= 1000:
                    raise AuditError("storage_listing_incomplete")
                seen.add(next_marker)
                marker = next_marker
            result["containers"][container] = {"available": True, "pages": pages, **counts}
        except Exception as error:
            result["containers"][container] = {**error_report(error), "partial": True, **counts}
    for container, _, label in categories:
        totals[label]["complete"] = result["containers"].get(container, {}).get("available", False)
    result["prefixes"] = totals
    return result


def run_metadata(client, run_id):
    try:
        run = client.request("GET", "/api/2.1/jobs/runs/get?run_id=" + str(run_id))
        def task_metrics(t):
            return {key: t.get(key) for key in ("task_key", "run_id", "start_time", "end_time", "setup_duration",
                    "execution_duration", "cleanup_duration", "run_duration", "attempt_number", "state")}
        return {"available": True, "run_id": run_id, "start_time": run.get("start_time"), "end_time": run.get("end_time"),
                "run_duration": run.get("run_duration"), "state": run.get("state"),
                "tasks": [task_metrics(t) for t in run.get("tasks", [])]}
    except Exception as error:
        return error_report(error)


def billing_notebook(client, tag, catalog):
    folder = "/Shared/weather-cost-audit-" + tag
    client.request("POST", "/api/2.0/workspace/mkdirs", {"path": folder})
    raw = (ROOT / "scripts/cost_audit_notebook.py").read_bytes()
    if not raw.startswith(b"# Databricks notebook source"):
        raise AuditError("cost_notebook_is_not_source")
    client.request("POST", "/api/2.0/workspace/import", {"path": folder + "/cost_audit", "format": "SOURCE",
                   "language": "PYTHON", "content": base64.b64encode(raw).decode(), "overwrite": True})
    workspace = cloud.az("resource", "show", "--ids", client.resource_id, "--api-version", "2026-01-01")
    params = {"workspace_id": str(workspace["properties"]["workspaceId"]), "catalog": catalog,
              "period_start": "2026-11-01", "period_end": "2026-11-16"}
    submitted = client.request("POST", "/api/2.1/jobs/runs/submit", {
        "run_name": "weather-cost-audit-" + tag, "timeout_seconds": 600,
        "idempotency_token": hashlib.sha256(("weather-cost-audit:" + tag).encode()).hexdigest(),
        "tasks": [{"task_key": "cost_audit", "timeout_seconds": 600, "notebook_task": {
            "notebook_path": folder + "/cost_audit", "base_parameters": params}}]})
    run = client.wait(submitted["run_id"], 660)
    task_id = run.get("tasks", [{}])[0].get("run_id", submitted["run_id"])
    output = client.request("GET", "/api/2.1/jobs/runs/get-output?run_id=" + str(task_id))
    if output.get("notebook_output", {}).get("truncated"):
        raise AuditError("billing_notebook_report_truncated")
    value = output.get("notebook_output", {}).get("result")
    if not value:
        raise AuditError("billing_notebook_missing_report")
    return {"available": True, "audit_run_id": submitted["run_id"], "result": json.loads(value)}


def main():
    project, environment, account = (os.environ.get(k, "") for k in ("PROJECT", "ENVIRONMENT", "ACCOUNT"))
    for value in (project, environment, account):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise AuditError("missing_configured_resource_name")
    group = f"rg-{project}-{environment}"
    tag = os.getenv("GOLD_RUN_TAG", datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"))
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,36}", tag):
        raise AuditError("invalid_audit_tag")
    report = {"observed_at_utc": datetime.now(timezone.utc).isoformat(), "read_only": True,
              "notes": ["ActualCost is recorded accrued cost; current dates may be incomplete due to billing latency.",
                        "Databricks list-price valuations are separate from Azure ActualCost and may use another currency.",
                        "One serverless read-only billing notebook can itself incur compute usage."]}
    def checkpoint():
        Path("/tmp/cost-audit-result.json").write_text(json.dumps(cloud.sanitize(report), indent=2, sort_keys=True, default=str) + "\n")
    checkpoint()
    try:
        report["azure_actual_costs"] = collect_costs(Costs(), group)
    except Exception as error:
        report["azure_actual_costs"] = error_report(error)
    checkpoint()
    report["linked_log_analytics"] = linked_workspaces(group)
    checkpoint()
    report["storage_footprint"] = storage_footprint(account, group)
    checkpoint()
    try:
        client = cloud.Databricks(project, environment)
        report["known_runs"] = {label: run_metadata(client, run_id) for label, run_id in RUNS.items()}
        catalog = re.sub(r"[^a-z0-9_]", "_", (project + "_" + environment).lower())
        report["databricks_billing"] = billing_notebook(client, tag, catalog)
    except Exception as error:
        report["databricks_billing"] = error_report(error)
    safe = cloud.sanitize(report)
    payload = json.dumps(safe, indent=2, sort_keys=True, default=str)
    Path("/tmp/cost-audit-result.json").write_text(payload + "\n")
    print(payload, flush=True)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        summary = {"observed_at_utc": safe["observed_at_utc"], "read_only": True,
                   "azure": {k: {field: v.get(field) for field in ("available", "scope", "period_start", "period_end_exclusive",
                              "total_by_currency", "project_total_by_currency", "other_total_by_currency")}
                             for k, v in safe.get("azure_actual_costs", {}).items() if isinstance(v, dict)},
                   "databricks_billing_available": safe.get("databricks_billing", {}).get("available")}
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("### Recorded Azure cost audit\n\n```json\n" + json.dumps(summary, indent=2) + "\n```\n")
    return 0 if any(v.get("available") for v in report.get("azure_actual_costs", {}).values() if isinstance(v, dict)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
