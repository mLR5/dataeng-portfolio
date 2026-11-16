#!/usr/bin/env python3
"""Validate Gold through the existing Azure deployment identity; no secrets in reports."""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REPO = Path(__file__).resolve().parents[1]
TERMINAL = {"TERMINATED", "SKIPPED", "INTERNAL_ERROR", "BLOCKED"}


class CloudError(RuntimeError):
    pass


def az(*args):
    result = subprocess.run(["az", *args, "--only-show-errors", "-o", "json"],
                            capture_output=True, text=True)
    if result.returncode:
        raise CloudError("Azure CLI command failed: " + args[0])
    return json.loads(result.stdout)


def mask(value):
    if value and os.environ.get("GITHUB_ACTIONS") == "true":
        print("::add-mask::" + str(value), flush=True)


def sanitize(value):
    """Keep counters/statuses, redact infrastructure locations and credentials."""
    if isinstance(value, dict):
        return {str(k): sanitize(v) for k, v in value.items()
                if not re.search(r"token|secret|credential|source_ids|fingerprints", str(k), re.I)}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r"(?:https?://|abfss://)[^\s\"'<>]+", "<location>", value)
        value = re.sub(r"/subscriptions/[^\s\"'<>]+", "<azure-resource>", value)
        for key in ("PROJECT", "ENVIRONMENT", "ACCOUNT"):
            item = os.environ.get(key)
            if item and len(item) > 3:
                value = value.replace(item, "<configured>")
        return value[:8000]
    return value


class Databricks:
    def __init__(self, project, environment):
        expected = f"dbw-{project}-{environment}"
        resources = az("resource", "list", "--resource-type", "Microsoft.Databricks/workspaces",
                       "--resource-group", f"rg-{project}-{environment}")
        matches = [r for r in resources if r.get("name") == expected]
        if len(matches) != 1:
            raise CloudError("Expected exactly one configured Databricks workspace")
        resource = az("resource", "show", "--ids", matches[0]["id"], "--api-version", "2026-01-01")
        self.resource_id = resource["id"]
        self.host = "https://" + resource["properties"]["workspaceUrl"].removeprefix("https://")
        mask(self.host)
        mask(self.host.removeprefix("https://"))
        mask(self.resource_id)
        mask(self.resource_id.split("/")[2])
        self.refreshed = 0
        self.headers = {}

    def refresh(self):
        if self.headers and time.monotonic() - self.refreshed < 2400:
            return
        token = az("account", "get-access-token", "--resource", "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d")["accessToken"]
        management = az("account", "get-access-token", "--resource", "https://management.core.windows.net/")["accessToken"]
        mask(token)
        mask(management)
        self.headers = {"Authorization": "Bearer " + token,
                        "X-Databricks-Azure-SP-Management-Token": management,
                        "X-Databricks-Azure-Workspace-Resource-Id": self.resource_id,
                        "Content-Type": "application/json"}
        self.refreshed = time.monotonic()

    def request(self, method, path, body=None):
        self.refresh()
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.host + path, data=data, headers=self.headers, method=method)
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as error:
                if method == "GET" and error.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(min(30, 5 * (attempt + 1)))
                    continue
                # Keep the API diagnosis while removing locations and known credentials.
                try:
                    detail = json.loads(error.read())
                    message = str(detail.get("message", ""))
                    for header in ("Authorization", "X-Databricks-Azure-SP-Management-Token"):
                        secret = self.headers.get(header, "").removeprefix("Bearer ")
                        if secret:
                            message = message.replace(secret, "<credential>")
                    diagnostic = sanitize({"error_code": str(detail.get("error_code", "")),
                                           "message": message})
                except (ValueError, AttributeError):
                    diagnostic = {"error_code": "unavailable"}
                raise CloudError(f"Databricks API {path.split('?')[0]} failed (HTTP {error.code}): "
                                 + json.dumps(diagnostic)[:4000]) from None
            except urllib.error.URLError:
                if method == "GET" and attempt < 3:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise CloudError("Databricks API transport failure") from None

    def wait(self, run_id, timeout):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            run = self.request("GET", "/api/2.1/jobs/runs/get?run_id=" + str(run_id))
            state = run.get("state", {})
            life = state.get("life_cycle_state")
            if life != last:
                print(json.dumps({"run_id": run_id, "life_cycle_state": life}), flush=True)
                last = life
            if life in TERMINAL:
                tasks = [{"task_key": t.get("task_key"), "run_id": t.get("run_id"),
                          "result_state": t.get("state", {}).get("result_state")}
                         for t in run.get("tasks", [])]
                report = {"run_id": run_id, "result_state": state.get("result_state"), "tasks": tasks}
                if state.get("result_state") != "SUCCESS":
                    failures = []
                    for task in tasks:
                        if task.get("run_id") and task.get("result_state") != "SUCCESS":
                            try:
                                output = self.request("GET", "/api/2.1/jobs/runs/get-output?run_id=" + str(task["run_id"]))
                                failures.append({"task_key": task["task_key"],
                                                 "error": sanitize(str(output.get("error", "")))[:4000],
                                                 "error_trace": sanitize(str(output.get("error_trace", "")))[:8000]})
                            except CloudError:
                                failures.append({"task_key": task["task_key"], "error": "Task output unavailable"})
                    report["failures"] = failures
                    raise CloudError("Databricks run failed: " + json.dumps(report, sort_keys=True))
                return run
            time.sleep(30)
        # The timeout applies to this run only; do not leave a validation run spending compute.
        self.request("POST", "/api/2.1/jobs/runs/cancel", {"run_id": run_id})
        raise CloudError("Databricks run exceeded runner timeout and was cancelled")

    def import_validation(self, folder):
        self.request("POST", "/api/2.0/workspace/mkdirs", {"path": folder})
        notebooks = sorted((REPO / "databricks/notebooks").glob("*.py"))
        notebooks.append(REPO / "scripts/gold_validate_notebook.py")
        for source in notebooks:
            raw = source.read_bytes()
            if not raw.startswith(b"# Databricks notebook source"):
                raise CloudError("Expected Databricks SOURCE notebook: " + source.name)
            name = "gold_validate" if source.name == "gold_validate_notebook.py" else source.stem
            self.request("POST", "/api/2.0/workspace/import", {
                "path": folder + "/" + name, "format": "SOURCE", "language": "PYTHON",
                "content": base64.b64encode(raw).decode(), "overwrite": True})
        return len(notebooks)

    def submit_validation(self, folder, parameters, tag, timeout):
        result = self.request("POST", "/api/2.1/jobs/runs/submit", {
            "run_name": "weather-gold-stage4-" + tag,
            "timeout_seconds": timeout,
            "idempotency_token": hashlib.sha256((tag + ":" + parameters.get("operation", "validation")).encode()).hexdigest(),
            "tasks": [{"task_key": "gold_validate", "timeout_seconds": timeout,
                       "notebook_task": {"notebook_path": folder + "/gold_validate",
                                         "base_parameters": parameters}}]})
        run = self.wait(result["run_id"], timeout + 60)
        task_id = run.get("tasks", [{}])[0].get("run_id", result["run_id"])
        output = self.request("GET", "/api/2.1/jobs/runs/get-output?run_id=" + str(task_id))
        if output.get("notebook_output", {}).get("truncated"):
            raise CloudError("Validation report truncated")
        raw = output.get("notebook_output", {}).get("result")
        if not raw:
            raise CloudError("Validation notebook returned no JSON report")
        report = json.loads(raw)
        return {"run_id": result["run_id"], "checks": report}

    def production_run(self, name, timeout):
        result = self.request("GET", "/api/2.1/jobs/list?" + urllib.parse.urlencode({"name": name}))
        matches = [j for j in result.get("jobs", []) if j.get("settings", {}).get("name") == name]
        if len(matches) != 1:
            raise CloudError("Expected exactly one deployed production job")
        job_id = matches[0]["job_id"]
        settings = self.request("GET", "/api/2.1/jobs/get?job_id=" + str(job_id))["settings"]
        if not any(t.get("task_key") == "gold_build" for t in settings.get("tasks", [])):
            raise CloudError("Production job does not yet contain gold_build")
        result = self.request("POST", "/api/2.1/jobs/run-now", {"job_id": job_id})
        run = self.wait(result["run_id"], timeout)
        return {"job_id": job_id, "run_id": result["run_id"],
                "tasks": [{"task_key": t["task_key"], "result_state": t.get("state", {}).get("result_state")}
                          for t in run.get("tasks", [])]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-name", default=os.getenv("PROJECT"))
    parser.add_argument("--environment", default=os.getenv("ENVIRONMENT"))
    parser.add_argument("--account", default=os.getenv("ACCOUNT"))
    parser.add_argument("--silver-path", default=os.getenv("SILVER_PATH", "silver/weatherapi_v2"))
    parser.add_argument("--run-tag", default=os.getenv("GOLD_RUN_TAG", datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")))
    parser.add_argument("--cut-date", default=os.getenv("GOLD_CUT_DATE", ""))
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=6600)
    parser.add_argument("--report", type=Path, default=Path("/tmp/gold-stage4-result.json"))
    args = parser.parse_args(argv)
    report = {"operation": "production" if args.production else "validation", "success": False}
    try:
        for value in (args.project_name, args.environment, args.account):
            if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
                raise CloudError("Missing or invalid configured Azure resource name")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,36}", args.run_tag):
            raise CloudError("Invalid run tag")
        if not re.fullmatch(r"silver/[A-Za-z0-9_/=-]+", args.silver_path) or ".." in args.silver_path:
            raise CloudError("Invalid Silver path")
        client = Databricks(args.project_name, args.environment)
        root = f"abfss://datalake@{args.account}.dfs.core.windows.net"
        folder = "/Shared/weather-gold-validation-" + args.run_tag
        report["notebooks_imported"] = client.import_validation(folder)
        validation_root = root + "/gold/_validation/stage4/" + args.run_tag
        params = {"silver_root": root + "/" + args.silver_path,
                  "validation_root": validation_root, "reference_root": validation_root + "/reference",
                  "incremental_root": validation_root + "/incremental", "cut_date": args.cut_date,
                  "operation": "validation"}
        if args.production:
            report["production_run"] = client.production_run("silver-weatherapi", args.timeout_seconds)
            params.update(operation="production_verify", production_root=root + "/gold/weatherapi_v2",
                          uc_schema=re.sub(r"[^a-z0-9_]", "_", (args.project_name + "_" + args.environment).lower()) + ".gold")
        report["validation"] = client.submit_validation(folder, params, args.run_tag, args.timeout_seconds)
        report["success"] = True
    except Exception as error:
        report["error_type"] = type(error).__name__
        report["error"] = str(error) if isinstance(error, CloudError) else "Runner failed before a complete validation report"
    safe = sanitize(report)
    payload = json.dumps(safe, indent=2, sort_keys=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(payload + "\n")
    print(payload, flush=True)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write("### Gold stage 4\n\n```json\n" + payload + "\n```\n")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
