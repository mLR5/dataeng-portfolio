#!/usr/bin/env python3
"""Inspect ARM or probe a temporary true-serverless workspace, then remove it.

The default is read-only. --execute-probe is required for a temporary workspace
PUT. This helper never creates a job, starts compute, changes roles or deletes a
resource group. Azure CLI must already be authenticated by the runner.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
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
import uuid


ARM = "https://management.azure.com"
ARM_API = "2026-01-01"
ARM_AUDIENCE = "https://management.azure.com/"
DATABRICKS_AUDIENCE = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"
PURPOSE = "serverless-cost-probe"
MAX_BODY = 1024 * 1024
PROVISION_TIMEOUT = 12 * 60
CLEANUP_TIMEOUT = 10 * 60
POLL_SECONDS = 15
TERMINAL_FAILURES = {"failed", "canceled", "cancelled", "deleted"}


class ProbeError(RuntimeError):
    """Only fixed codes and status numbers may leave the credential boundary."""

    def __init__(self, stage, code, status=None):
        super().__init__(code)
        self.stage, self.code, self.status = stage, code, status


@dataclass(frozen=True)
class Response:
    status: int
    document: dict
    retry_after: float = 0


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        # Never forward Azure or Databricks authorization headers to a redirect.
        return None


def _document(raw):
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


class Http:
    """No response bodies, URLs or token-bearing headers are logged."""

    def __init__(self):
        self.opener = urllib.request.build_opener(NoRedirect())

    @staticmethod
    def _retry_after(headers):
        try:
            return max(0, min(60, float(headers.get("Retry-After", "0"))))
        except (TypeError, ValueError):
            return 0

    def request(self, method, url, *, headers=None, body=None, timeout=25):
        raw = None if body is None else json.dumps(body).encode("utf-8")
        request_headers = {"Accept": "application/json", **(headers or {})}
        if raw is not None:
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=raw, headers=request_headers, method=method)
        try:
            with self.opener.open(request, timeout=timeout) as result:
                content = result.read(MAX_BODY + 1)
                if len(content) > MAX_BODY:
                    raise ProbeError("http", "response_too_large", result.status)
                return Response(result.status, _document(content), self._retry_after(result.headers))
        except urllib.error.HTTPError as error:
            with error:
                content = error.read(MAX_BODY + 1)
            return Response(error.code, _document(content[:MAX_BODY]), self._retry_after(error.headers))
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ProbeError("http", "request_failed") from None


class AzureCli:
    """Tokens stay inside this object and HTTP headers, never the report."""

    def __init__(self):
        self._tokens = {}

    @staticmethod
    def _json(arguments):
        try:
            result = subprocess.run(
                ["az", *arguments, "--output", "json", "--only-show-errors"],
                capture_output=True, check=False, text=True, timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ProbeError("azure_cli", "azure_cli_unavailable") from None
        if result.returncode:
            raise ProbeError("azure_cli", "azure_cli_failed")
        value = _document(result.stdout)
        if not value:
            raise ProbeError("azure_cli", "azure_cli_invalid_json")
        return value

    def subscription(self):
        account = self._json(["account", "show"])
        value = account.get("id")
        if (not isinstance(value, str)
                or not re.fullmatch(r"[0-9a-fA-F-]{36}", value)
                or account.get("state", "Enabled") != "Enabled"):
            raise ProbeError("azure_cli", "subscription_not_ready")
        return value

    def token(self, audience):
        cached = self._tokens.get(audience)
        if cached and time.monotonic() - cached[0] < 10 * 60:
            return cached[1]
        token = self._json(["account", "get-access-token", "--resource", audience]).get("accessToken")
        if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
            raise ProbeError("azure_cli", "access_token_unavailable")
        self._tokens[audience] = (time.monotonic(), token)
        return token


def _component(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", value):
        raise ProbeError("configuration", f"invalid_{name}")
    return value


def _workspace_host(value):
    if not isinstance(value, str):
        raise ProbeError("workspace", "workspace_host_missing")
    value = value.removeprefix("https://").rstrip("/")
    if not re.fullmatch(r"adb-[a-z0-9-]+(?:\.[a-z0-9-]+)*\.azuredatabricks\.net", value):
        raise ProbeError("workspace", "workspace_host_unexpected")
    return "https://" + value


def _safe_error(error):
    if isinstance(error, ProbeError):
        value = {"stage": error.stage, "code": error.code}
        if error.status is not None:
            value["http_status"] = error.status
        return value
    return {"stage": "internal", "code": "unexpected_failure"}


def _state(document):
    value = document.get("properties", {}).get("provisioningState", "Unknown")
    # Do not publish arbitrary strings from a remote response.
    return value if value in {
        "Succeeded", "Accepted", "Creating", "Updating", "Deleting", "Failed",
        "Canceled", "Cancelled", "Unknown", "Deleted",
    } else "Unknown"


def _mode(document):
    value = document.get("properties", {}).get("computeMode")
    return value if value in {"Serverless", "Hybrid"} else "Unknown"


def _status_label(status):
    if 200 <= status < 300:
        return "allowed"
    return {401: "unauthenticated", 403: "forbidden", 404: "not_found"}.get(status, "unavailable")


class WorkspaceProbe:
    def __init__(self, *, project, environment, run_id, location=None, http=None, cli=None,
                 clock=time.monotonic, sleep=time.sleep, nonce=None):
        self.project = _component(project, "project_name")
        self.environment = _component(environment, "deployment_environment")
        if not isinstance(run_id, str) or not re.fullmatch(r"[0-9]{1,30}", run_id):
            raise ProbeError("configuration", "github_run_id_required")
        if location is not None and not re.fullmatch(r"[a-z0-9]{2,40}", location):
            raise ProbeError("configuration", "invalid_location")
        self.run_id, self.location = run_id, location
        self.nonce = nonce or uuid.uuid4().hex
        if not re.fullmatch(r"[0-9a-f]{32}", self.nonce):
            raise ProbeError("configuration", "invalid_probe_nonce")
        self.name = f"dbw-{self.project}-slprobe-{self.nonce[:8]}"
        self.tags = {"purpose": PURPOSE, "owner_run_id": run_id, "probe_nonce": self.nonce}
        self.http, self.cli = http or Http(), cli or AzureCli()
        self.clock, self.sleep = clock, sleep
        # Leave a margin below a 30-minute workflow timeout so finally can run.
        self.overall_deadline = self.clock() + 27 * 60
        self.resource_id = None
        self.put_attempted = False
        self.put_status = None
        self.resource_observed = False
        self.preexisting_absent = False

    def arm(self, method, resource_id, *, body=None):
        return self.http.request(
            method, ARM + resource_id + "?api-version=" + ARM_API,
            headers={"Authorization": "Bearer " + self.cli.token(ARM_AUDIENCE)}, body=body,
        )

    def setup(self, report):
        subscription = self.cli.subscription()
        group_name = f"rg-{self.project}-{self.environment}"
        group_id = f"/subscriptions/{subscription}/resourceGroups/{group_name}"
        # Resource group API is separate from the Databricks workspace API.
        group = self.http.request(
            "GET", ARM + group_id + "?api-version=2024-03-01",
            headers={"Authorization": "Bearer " + self.cli.token(ARM_AUDIENCE)},
        )
        if group.status != 200:
            raise ProbeError("preflight", "existing_resource_group_unavailable", group.status)
        location = self.location or group.document.get("location")
        if not isinstance(location, str) or not re.fullmatch(r"[a-z0-9]{2,40}", location):
            raise ProbeError("preflight", "resource_group_location_invalid")
        self.location = location
        self.resource_id = group_id + "/providers/Microsoft.Databricks/workspaces/" + self.name
        existing = self.arm("GET", self.resource_id)
        if existing.status != 404:
            raise ProbeError("preflight", "workspace_name_not_confirmed_absent", existing.status)
        self.preexisting_absent = True
        report["preflight"] = {
            "azure_login": "verified", "existing_resource_group": "verified",
            "location": self.location, "temporary_name_absent": True,
            "arm_api": ARM_API,
        }

    def wait_ready(self, report):
        deadline = min(self.clock() + PROVISION_TIMEOUT, self.overall_deadline - CLEANUP_TIMEOUT)
        while self.clock() < deadline:
            response = self.arm("GET", self.resource_id)
            if response.status == 200:
                self.resource_observed = True
                mode, state = _mode(response.document), _state(response.document)
                report["workspace"] = {"requested_mode": "Serverless", "observed_mode": mode,
                                       "provisioning_state": state}
                if mode == "Hybrid":
                    raise ProbeError("provisioning", "mode_mismatch")
                if state.lower() in TERMINAL_FAILURES:
                    raise ProbeError("provisioning", "workspace_provisioning_failed")
                if state == "Succeeded":
                    if mode != "Serverless":
                        raise ProbeError("provisioning", "serverless_mode_not_verified")
                    if not self.owned(response.document):
                        raise ProbeError("provisioning", "workspace_ownership_not_verified")
                    return response.document
            elif response.status not in {404, 408, 429, 500, 502, 503, 504}:
                raise ProbeError("provisioning", "workspace_poll_failed", response.status)
            self.sleep(min(max(POLL_SECONDS, response.retry_after), max(0, deadline - self.clock())))
        raise ProbeError("provisioning", "workspace_provisioning_timeout")

    def owned(self, document):
        actual_id = document.get("id")
        tags = document.get("tags")
        return (
            self.preexisting_absent and self.put_attempted
            and isinstance(actual_id, str) and actual_id.casefold() == self.resource_id.casefold()
            and isinstance(tags, dict) and all(tags.get(key) == value for key, value in self.tags.items())
        )

    def inspect_databricks(self, document, report):
        if _mode(document) != "Serverless" or not self.owned(document):
            raise ProbeError("workspace", "serverless_ownership_gate_failed")
        host = _workspace_host(document.get("properties", {}).get("workspaceUrl"))
        headers = {
            "Authorization": "Bearer " + self.cli.token(DATABRICKS_AUDIENCE),
            "X-Databricks-Azure-SP-Management-Token": self.cli.token(ARM_AUDIENCE),
            "X-Databricks-Azure-Workspace-Resource-Id": self.resource_id,
        }
        identity = self.http.request("GET", host + "/api/2.0/preview/scim/v2/Me", headers=headers)
        details = {"identity_access": _status_label(identity.status), "identity_http_status": identity.status,
                   "account_admin": "not_verified"}
        report["databricks"] = details
        if identity.status != 200:
            raise ProbeError("databricks_auth", "workspace_identity_unavailable", identity.status)
        details["workspace_admin_group_member"] = any(
            isinstance(group, dict) and group.get("display") == "admins"
            for group in identity.document.get("groups", [])
        )
        metastore = self.http.request("GET", host + "/api/2.1/unity-catalog/metastore_summary", headers=headers)
        details["metastore_access"] = _status_label(metastore.status)
        details["metastore_http_status"] = metastore.status
        details["metastore_assigned"] = bool(
            metastore.status == 200 and metastore.document.get("metastore_id")
        )
        credentials = self.http.request(
            "GET", host + "/api/2.1/unity-catalog/credentials?max_results=10", headers=headers,
        )
        details["credential_listing_access"] = _status_label(credentials.status)
        details["credential_listing_http_status"] = credentials.status
        if credentials.status == 200:
            items = credentials.document.get("credentials", [])
            if isinstance(items, list):
                purposes = Counter(
                    item.get("purpose") if item.get("purpose") in {"SERVICE", "STORAGE"} else "UNKNOWN"
                    for item in items if isinstance(item, dict)
                )
                details["credential_page_count"] = len(items)
                details["credential_purposes"] = dict(purposes)
                details["credential_listing_has_more"] = bool(credentials.document.get("next_page_token"))
        details["storage_data_access_tested"] = False
        details["service_credential_access_tested"] = False
        details["compute_started"] = False

    def cleanup(self):
        if not self.put_attempted or not self.preexisting_absent:
            return {"status": "not_needed"}
        deadline = min(self.clock() + CLEANUP_TIMEOUT, self.overall_deadline)
        deleted = False
        while self.clock() < deadline:
            try:
                response = self.arm("GET", self.resource_id)
                if response.status == 404:
                    if deleted or self.resource_observed:
                        return {"status": "removed", "workspace_absent": True}
                    if self.put_status is not None and self.put_status >= 400:
                        return {"status": "not_created", "workspace_absent": True}
                    # A lost/accepted PUT can be invisible briefly. Do not
                    # declare cleanup complete based on the first stale 404.
                    self.sleep(min(POLL_SECONDS, max(0, deadline - self.clock())))
                    continue
                if response.status in {408, 429, 500, 502, 503, 504}:
                    self.sleep(min(max(POLL_SECONDS, response.retry_after), max(0, deadline - self.clock())))
                    continue
                if response.status != 200:
                    return {"status": "verification_failed", "http_status": response.status}
                if not self.owned(response.document):
                    return {"status": "refused", "reason": "ownership_not_verified"}
                mode = _mode(response.document)
                # A mismatched mode blocks ALL dataplane calls, but must not
                # leave our own unexpected Hybrid/NAT resource accumulating cost.
                # Exact ID, pre-create 404, run tag and unpredictable nonce gate
                # this deletion, not a guess about an existing resource's name.
                if not deleted:
                    result = self.arm("DELETE", self.resource_id)
                    if result.status == 404:
                        return {"status": "removed", "workspace_absent": True,
                                "observed_mode_before_delete": mode}
                    if result.status in {200, 202, 204}:
                        deleted = True
                    elif result.status not in {409, 429, 500, 502, 503, 504}:
                        return {"status": "delete_failed", "http_status": result.status,
                                "observed_mode_before_delete": mode}
                self.sleep(min(max(POLL_SECONDS, response.retry_after), max(0, deadline - self.clock())))
            except ProbeError:
                self.sleep(min(POLL_SECONDS, max(0, deadline - self.clock())))
        return {"status": "timeout", "delete_accepted": deleted, "workspace_absent": False}

    def run(self, *, execute=False):
        report = {"schema_version": 1, "execute_probe": execute, "arm_api": ARM_API,
                  "outcome": "not_started", "compute_started": False,
                  "roles_modified": False, "resource_group_deleted": False,
                  "temporary_workspace_name": self.name, "owner_run_id": self.run_id}
        try:
            self.setup(report)
            if not execute:
                report["outcome"] = "read_only_preflight_succeeded"
                return report
            body = {"location": self.location, "properties": {"computeMode": "Serverless"},
                    "sku": {"name": "premium"}, "tags": self.tags}
            # Mark before sending: a timeout may hide an accepted PUT. Finally
            # must still look for and remove only our exact tagged resource.
            self.put_attempted = True
            result = self.arm("PUT", self.resource_id, body=body)
            self.put_status = result.status
            if result.status not in {200, 201, 202}:
                raise ProbeError("provisioning", "workspace_create_rejected", result.status)
            document = self.wait_ready(report)
            self.inspect_databricks(document, report)
            report["outcome"] = "serverless_workspace_auth_verified"
        except BaseException as error:
            report["outcome"] = "blocked"
            report["error"] = _safe_error(error)
        finally:
            try:
                report["cleanup"] = self.cleanup()
            except BaseException:
                report["cleanup"] = {"status": "unexpected_failure", "workspace_absent": False}
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-probe", action="store_true",
                        help="Create and always attempt to remove an owned temporary Serverless workspace.")
    parser.add_argument("--location", default=None, help="Default: region of the existing project resource group.")
    parser.add_argument("--output", type=Path, help="Optional local JSON report, containing no tokens or raw errors.")
    args = parser.parse_args(argv)
    try:
        probe = WorkspaceProbe(
            project=os.environ.get("PROJECT_NAME"), environment=os.environ.get("DEPLOYMENT_ENVIRONMENT"),
            run_id=os.environ.get("GITHUB_RUN_ID"), location=args.location,
        )
        report = probe.run(execute=args.execute_probe)
    except BaseException as error:
        report = {"schema_version": 1, "execute_probe": args.execute_probe, "outcome": "blocked",
                  "error": _safe_error(error), "cleanup": {"status": "not_needed"}, "compute_started": False}
    text = json.dumps(report, sort_keys=True, indent=2, allow_nan=False)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    acceptable = report.get("outcome") in {
        "read_only_preflight_succeeded", "serverless_workspace_auth_verified",
    }
    clean = report.get("cleanup", {}).get("status") in {"not_needed", "removed"}
    return 0 if acceptable and clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
