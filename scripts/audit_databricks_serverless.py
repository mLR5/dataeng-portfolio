"""Read-only Databricks serverless readiness diagnostics for the project group.

Nothing is created: ARM templates are only submitted to
``az deployment group validate``. Only error codes, booleans and sanitized,
bounded messages are written to the report and the public job log.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

API_VERSION = "2026-01-01"
LOCATION = "francecentral"
WORKSPACE_TYPE = "Microsoft.Databricks/workspaces"
REQUIRED_ACTIONS = (
    "Microsoft.Resources/deployments/validate/action",
    "Microsoft.Resources/deployments/write",
    "Microsoft.Databricks/workspaces/write",
)
VALIDATION_VARIANTS = (
    # name, compute mode, validation level
    ("serverless_provider", "Serverless", "Provider"),
    ("serverless_provider_no_rbac", "Serverless", "ProviderNoRbac"),
    ("classic_provider", "Hybrid", "Provider"),
)
GUID = re.compile(r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b")
CODE_PATTERNS = (
    re.compile(r"(?:^|\n)\s*(?:ERROR:\s*)?\(([A-Za-z][A-Za-z0-9_.]{0,127})\)"),
    re.compile(r"(?:^|\n)\s*(?:Code|ErrorCode):\s*([A-Za-z][A-Za-z0-9_.]{0,127})"),
    re.compile(r'"code"\s*:\s*"([A-Za-z][A-Za-z0-9_.]{0,127})"'),
)
MESSAGE_PATTERNS = (
    # Inner ARM details usually carry the actionable reason: keep them first.
    re.compile(r"Exception Details:\s*\([A-Za-z][A-Za-z0-9_.]*\)\s*(.+)"),
    re.compile(r"(?:^|\n)\s*Message:\s*(.+)"),
    re.compile(r'"message"\s*:\s*"((?:[^"\\]|\\.){1,2000})"'),
    re.compile(r"(?:^|\n)\s*(?:ERROR:\s*)?\([A-Za-z][A-Za-z0-9_.]*\)\s*(.+)"),
)
MAX_MESSAGE = 300

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def sanitize(text: str, sensitive: tuple[str, ...] = ()) -> str:
    """Remove identifiers before anything reaches a public log."""
    for value in sorted((v for v in sensitive if v and len(v) >= 3), key=len, reverse=True):
        text = re.sub(re.escape(value), "<redacted>", text, flags=re.IGNORECASE)
    text = re.sub(r"https?://\S+", "<url>", text)
    text = re.sub(r"/subscriptions/[^\s\"',;)]+", "<resource-id>", text, flags=re.IGNORECASE)
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "<email>", text)
    text = GUID.sub("<guid>", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_MESSAGE]


def error_codes(text: str) -> list[str]:
    found: list[str] = []
    for pattern in CODE_PATTERNS:
        for code in pattern.findall(text):
            if code not in found:
                found.append(code)
    return found[:10]


def error_messages(text: str, sensitive: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for pattern in MESSAGE_PATTERNS:
        for raw in pattern.findall(text):
            try:
                raw = json.loads(f'"{raw}"')
            except json.JSONDecodeError:
                pass
            message = sanitize(raw, sensitive)
            if message and message not in found:
                found.append(message)
    if not found:
        first = next((line for line in text.splitlines() if line.strip()), "")
        if first:
            found.append(sanitize(first, sensitive))
    return found[:5]


def action_allowed(action: str, permissions: list[dict]) -> bool:
    """Evaluate ARM effective permissions (actions minus notActions)."""
    action = action.lower()
    for entry in permissions:
        allowed = any(fnmatch.fnmatchcase(action, p.lower()) for p in entry.get("actions", []))
        denied = any(fnmatch.fnmatchcase(action, p.lower()) for p in entry.get("notActions", []))
        if allowed and not denied:
            return True
    return False


def workspace_template(name: str, compute_mode: str, managed_group_id: str) -> dict:
    properties: dict = {"computeMode": compute_mode}
    if compute_mode != "Serverless":
        properties["managedResourceGroupId"] = managed_group_id
    return {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
        "contentVersion": "1.0.0.0",
        "resources": [{
            "type": WORKSPACE_TYPE,
            "apiVersion": API_VERSION,
            "name": name,
            "location": LOCATION,
            "sku": {"name": "premium"},
            "properties": properties,
        }],
    }


def default_runner(arguments: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["az", *arguments, "--only-show-errors", "--output", "json"],
        capture_output=True, text=True, timeout=180, check=False,
    )


class Audit:
    def __init__(self, env: dict[str, str], runner: Runner = default_runner, workdir: Path = Path(".")):
        self.runner = runner
        self.workdir = workdir
        self.project = env.get("PROJECT_NAME", "")
        self.environment = env.get("DEPLOYMENT_ENVIRONMENT", "")
        self.account = env.get("STORAGE_ACCOUNT_NAME", "")
        self.resource_group = f"rg-{self.project}-{self.environment}"
        self.workspace = f"dbw-{self.project}-serverless-bench"
        self.authenticated = "success" in (env.get("OIDC_OUTCOME"), env.get("EXISTING_IDENTITY_OUTCOME"))
        self.sensitive: tuple[str, ...] = (
            self.resource_group, self.workspace, self.account, self.project, self.environment,
        )
        self.report: dict = {
            "observed_at_utc": datetime.now(timezone.utc).isoformat(),
            "read_only": True,
            "resources_created": False,
            "requested_api_version": API_VERSION,
            "requested_location": LOCATION,
            "checks": {},
            "diagnostics": {},
            "blocked": [],
        }

    # -- helpers -----------------------------------------------------------
    def blocked(self, check: str, code: str, messages: list[str] | None = None) -> None:
        item: dict = {"check": check, "code": code}
        if messages:
            item["messages"] = messages
        self.report["blocked"].append(item)

    def call(self, arguments: list[str]) -> tuple[object | None, dict | None]:
        """Run az; return (json, None) or (None, failure details)."""
        try:
            result = self.runner(arguments)
        except subprocess.TimeoutExpired:
            return None, {"codes": ["command_timeout"], "messages": []}
        except OSError:
            return None, {"codes": ["command_unavailable"], "messages": []}
        text = (result.stderr or "") + "\n" + (result.stdout or "")
        if result.returncode:
            return None, {
                "codes": error_codes(text) or ["azure_cli_failed"],
                "messages": error_messages(text, self.sensitive),
            }
        try:
            return json.loads(result.stdout or "null"), None
        except json.JSONDecodeError:
            return None, {"codes": ["invalid_json_response"], "messages": []}

    # -- steps -------------------------------------------------------------
    def cli_version(self) -> None:
        value, _ = self.call(["version"])
        if isinstance(value, dict):
            self.report["diagnostics"]["azure_cli_version"] = value.get("azure-cli")

    def subscription(self) -> str | None:
        value, failure = self.call(["account", "show", "--query", "{id:id,state:state,principal_type:user.type}"])
        if failure:
            self.blocked("subscription", failure["codes"][0], failure["messages"])
            return None
        state = value.get("state")
        self.report["checks"]["subscription"] = {"state": state, "principal_type": value.get("principal_type")}
        if state != "Enabled":
            self.blocked("subscription", "subscription_not_enabled")
        return value.get("id")

    def resource_group_ready(self) -> bool:
        value, failure = self.call(["group", "show", "--name", self.resource_group, "--query", "{location:location}"])
        if failure:
            self.blocked("resource_group", failure["codes"][0], failure["messages"])
            return False
        location = (value.get("location") or "").lower()
        self.report["checks"]["resource_group"] = {"location": location}
        if location != LOCATION:
            self.blocked("resource_group", "existing_group_not_francecentral")
            return False
        return True

    def provider_ready(self) -> bool:
        value, failure = self.call([
            "provider", "show", "--namespace", "Microsoft.Databricks", "--query",
            "{state:registrationState,types:resourceTypes[?resourceType=='workspaces'].{v:apiVersions,l:locations}}",
        ])
        if failure:
            self.blocked("databricks_provider", failure["codes"][0], failure["messages"])
            return False
        types = value.get("types") or []
        versions = {v for t in types for v in t.get("v", [])}
        locations = {loc.replace(" ", "").lower() for t in types for loc in t.get("l", [])}
        newest = sorted(versions, reverse=True)[:5]
        self.report["checks"]["databricks_provider"] = {
            "registration_state": value.get("state"),
            "requested_api_supported": API_VERSION in versions,
            "requested_location_supported": LOCATION in locations,
            "newest_api_versions": newest,
        }
        if value.get("state") != "Registered":
            self.blocked("databricks_provider", "provider_not_registered_no_registration_attempted")
        elif API_VERSION not in versions:
            self.blocked("databricks_provider", "requested_api_version_unavailable")
        elif LOCATION not in locations:
            self.blocked("databricks_provider", "francecentral_not_advertised")
        else:
            return True
        return False

    def effective_permissions(self, subscription_id: str) -> None:
        url = (
            f"https://management.azure.com/subscriptions/{subscription_id}/resourceGroups/"
            f"{self.resource_group}/providers/Microsoft.Authorization/permissions?api-version=2022-04-01"
        )
        value, failure = self.call(["rest", "--method", "get", "--url", url])
        if failure:
            self.report["diagnostics"]["effective_permissions"] = {"read": False, "codes": failure["codes"]}
            return
        permissions = (value or {}).get("value", []) if isinstance(value, dict) else []
        self.report["diagnostics"]["effective_permissions"] = {
            "read": True,
            "wildcard_actions": any("*" in e.get("actions", []) for e in permissions),
            "allowed": {action: action_allowed(action, permissions) for action in REQUIRED_ACTIONS},
        }

    def policy_assignments(self) -> None:
        value, failure = self.call([
            "policy", "assignment", "list", "--resource-group", self.resource_group,
            "--disable-scope-strict-match",
            "--query", "[].{name:displayName,mode:enforcementMode}",
        ])
        if failure:
            self.report["diagnostics"]["policy_assignments"] = {"read": False, "codes": failure["codes"]}
            return
        items = value if isinstance(value, list) else []
        self.report["diagnostics"]["policy_assignments"] = {
            "read": True,
            "count": len(items),
            "assignments": [
                {"name": sanitize(str(i.get("name") or "<unnamed>"), self.sensitive), "mode": i.get("mode")}
                for i in items[:20] if isinstance(i, dict)
            ],
        }

    def validate_templates(self, subscription_id: str) -> None:
        managed_group = f"/subscriptions/{subscription_id}/resourceGroups/{self.workspace}-managed"
        results: dict = {}
        for name, compute_mode, level in VALIDATION_VARIANTS:
            path = self.workdir / f"template-{name}.json"
            path.write_text(json.dumps(workspace_template(self.workspace, compute_mode, managed_group)), encoding="utf-8")
            value, failure = self.call([
                "deployment", "group", "validate", "--resource-group", self.resource_group,
                "--name", f"audit-dbx-{name.replace('_', '-')}",
                "--template-file", str(path), "--validation-level", level,
            ])
            if failure is None and isinstance(value, dict) and value.get("error"):
                text = json.dumps(value["error"])
                failure = {"codes": error_codes(text) or ["validation_error"],
                           "messages": error_messages(text, self.sensitive)}
            results[name] = (
                {"passed": True, "compute_mode": compute_mode, "validation_level": level}
                if failure is None else
                {"passed": False, "compute_mode": compute_mode, "validation_level": level, **failure}
            )
        self.report["checks"]["template_validation"] = results
        main = results["serverless_provider"]
        if not main["passed"]:
            self.blocked("serverless_template_validation", main["codes"][0], main.get("messages"))
        self.report["diagnosis"] = diagnose(results, self.report["diagnostics"])

    def bronze_metadata(self) -> None:
        if not re.fullmatch(r"[a-z0-9]{3,24}", self.account):
            self.blocked("bronze_metadata", "missing_or_invalid_storage_account")
            return
        value, failure = self.call([
            "storage", "blob", "list", "--account-name", self.account, "--container-name", "bronze",
            "--prefix", "weather/weatherapi/", "--auth-mode", "login", "--num-results", "5",
            "--query", "[].{size:properties.contentLength}",
        ])
        if failure:
            self.blocked("bronze_metadata", failure["codes"][0], failure["messages"])
            return
        sizes = [i.get("size") for i in value or [] if isinstance(i, dict)]
        self.report["checks"]["bronze_metadata"] = {
            "auth_mode": "login", "count": len(sizes), "contents_read": False,
            "all_sizes_positive": all(isinstance(s, int) and s > 0 for s in sizes),
        }

    def run(self) -> dict:
        if not self.authenticated:
            self.blocked("authentication", "azure_login_failed")
        elif not (self.project and self.environment
                  and re.fullmatch(r"[A-Za-z0-9._()\-]{1,90}", self.resource_group)
                  and re.fullmatch(r"[A-Za-z0-9_-]{3,64}", self.workspace)):
            self.blocked("configuration", "missing_or_invalid_project_scope")
        else:
            self.cli_version()
            subscription_id = self.subscription()
            group_ok = self.resource_group_ready()
            provider_ok = self.provider_ready()
            if subscription_id:
                self.effective_permissions(subscription_id)
            self.policy_assignments()
            if subscription_id and group_ok and provider_ok:
                self.validate_templates(subscription_id)
            else:
                self.blocked("serverless_template_validation", "prerequisite_not_ready")
            self.bronze_metadata()
        self.report["complete"] = not self.report["blocked"]
        self.report["creation_or_execution_authorized_by_this_report"] = False
        return self.report


def diagnose(results: dict, diagnostics: dict) -> str:
    """Translate the variant matrix into a short, factual hypothesis."""
    serverless = results["serverless_provider"]["passed"]
    no_rbac = results["serverless_provider_no_rbac"]["passed"]
    classic = results["classic_provider"]["passed"]
    allowed = (diagnostics.get("effective_permissions") or {}).get("allowed") or {}
    if serverless:
        return "serverless_validation_passed"
    if no_rbac:
        return "rbac_missing_for_serverless_workspace"
    if allowed and not all(allowed.values()):
        return "identity_lacks_required_actions"
    if classic:
        return "serverless_rejected_classic_accepted"
    return "databricks_workspace_rejected_independently_of_compute_mode"


def summary_lines(report: dict) -> list[str]:
    lines = [
        "## Databricks serverless: read-only readiness (v2)",
        "",
        "ARM validate only. No workspace, managed group, network, catalog or connector created.",
        f"Diagnosis: `{report.get('diagnosis', 'not_evaluated')}`.",
        f"Successful checks: {len(report['checks'])}. Blocked checks: {len(report['blocked'])}.",
    ]
    for name, result in (report["checks"].get("template_validation") or {}).items():
        state = "passed" if result["passed"] else "failed " + ", ".join(result.get("codes", []))
        lines.append(f"- validate `{name}`: {state}")
        for message in result.get("messages", []):
            lines.append(f"  - {message}")
    for item in report["blocked"]:
        lines.append(f"- blocked `{item['check']}`: `{item['code']}`")
    return lines


def main() -> int:
    audit = Audit(dict(os.environ))
    report = audit.run()
    Path("databricks-serverless-readiness.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = summary_lines(report)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")
    # The sanitized report holds no identifiers: print it so the log alone is enough.
    print("DATABRICKS_SERVERLESS_READINESS " + json.dumps(report, sort_keys=True))
    print("\n".join(lines))
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
