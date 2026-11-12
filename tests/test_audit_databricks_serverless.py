import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_databricks_serverless.py"
spec = importlib.util.spec_from_file_location("audit_databricks_serverless", SCRIPT)
audit_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_mod)

SUB = "11111111-2222-3333-4444-555555555555"
ENV = {
    "PROJECT_NAME": "weatherproj",
    "DEPLOYMENT_ENVIRONMENT": "dev",
    "STORAGE_ACCOUNT_NAME": "stweatherdev01",
    "EXISTING_IDENTITY_OUTCOME": "success",
    "OIDC_OUTCOME": "skipped",
}
ARM_FAILURE = (
    "ERROR: (InvalidTemplateDeployment) The template deployment 'x' is not valid.\n"
    "Code: InvalidTemplateDeployment\n"
    "Message: Workspace dbw-weatherproj-serverless-bench in rg-weatherproj-dev for "
    f"/subscriptions/{SUB}/resourceGroups/rg-weatherproj-dev is not allowed. See https://aka.ms/x\n"
    "Exception Details:\t(ServerlessNotSupported) Serverless is not available in region francecentral.\n"
    "\tCode: ServerlessNotSupported\n"
)


def done(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


BLOB_AUTH_FAILURE = (
    "ERROR: \nYou do not have the required permissions needed to perform this operation.\n"
    "Depending on your operation, you may need to be assigned one of the following roles:\n"
)


def fake_runner(*, serverless_ok=False, no_rbac_ok=False, classic_ok=True, permissions=None,
                blob_ok=True, data_actions=()):
    perms = permissions if permissions is not None else [{"actions": ["*"], "notActions": [
        "Microsoft.Authorization/*/Delete", "Microsoft.Authorization/*/Write"]}]

    def run(args):
        joined = " ".join(args)
        if args[:1] == ["version"]:
            return done(json.dumps({"azure-cli": "2.79.0"}))
        if args[:2] == ["account", "show"]:
            return done(json.dumps({"id": SUB, "state": "Enabled", "principal_type": "servicePrincipal"}))
        if args[:2] == ["group", "show"]:
            return done(json.dumps({"location": "francecentral"}))
        if args[:2] == ["provider", "show"]:
            return done(json.dumps({"state": "Registered", "types": [
                {"v": ["2026-01-01", "2025-10-01-preview"], "l": ["France Central", "West Europe"]}]}))
        if args[:1] == ["rest"]:
            if "storageAccounts" in joined:
                return done(json.dumps({"value": [*perms, {"actions": [], "dataActions": list(data_actions)}]}))
            return done(json.dumps({"value": perms}))
        if args[:3] == ["policy", "assignment", "list"]:
            return done(json.dumps([{"name": "Allowed locations for rg-weatherproj-dev", "mode": "Default"}]))
        if args[:3] == ["deployment", "group", "validate"]:
            template = json.loads(Path(args[args.index("--template-file") + 1]).read_text())
            mode = template["resources"][0]["properties"]["computeMode"]
            level = args[args.index("--validation-level") + 1]
            ok = classic_ok if mode == "Hybrid" else (no_rbac_ok if level == "ProviderNoRbac" else serverless_ok)
            return done(json.dumps({"properties": {}})) if ok else done(returncode=1, stderr=ARM_FAILURE)
        if args[:3] == ["storage", "blob", "list"]:
            if not blob_ok:
                return done(returncode=1, stderr=BLOB_AUTH_FAILURE)
            return done(json.dumps([{"size": 1200}, {"size": 1300}]))
        raise AssertionError(f"unexpected az call: {joined}")

    return run


def run_audit(tmp_path, **kwargs):
    return audit_mod.Audit(ENV, runner=fake_runner(**kwargs), workdir=tmp_path).run()


def test_sanitize_removes_identifiers_urls_and_guids():
    text = f"rg-weatherproj-dev at https://x.y/z for /subscriptions/{SUB}/rg mail a@b.com id {SUB}"
    clean = audit_mod.sanitize(text, ("rg-weatherproj-dev",))
    assert "weatherproj" not in clean and SUB not in clean and "https" not in clean and "a@b.com" not in clean


def test_observed_failure_is_diagnosed_without_leaking_identifiers(tmp_path):
    report = run_audit(tmp_path, serverless_ok=False, no_rbac_ok=False, classic_ok=True)
    validation = report["checks"]["template_validation"]
    assert validation["serverless_provider"]["codes"] == ["InvalidTemplateDeployment", "ServerlessNotSupported"]
    assert validation["classic_provider"]["passed"] is True
    assert report["diagnosis"] == "serverless_rejected_classic_accepted"
    assert report["complete"] is False
    assert [b["check"] for b in report["blocked"]] == ["serverless_template_validation"]
    dumped = json.dumps(report) + "\n".join(audit_mod.summary_lines(report))
    for secret in (SUB, "weatherproj", "stweatherdev01", "rg-weatherproj-dev"):
        assert secret not in dumped
    assert any("Serverless is not available" in m for m in validation["serverless_provider"]["messages"])


def test_rbac_only_failure_is_identified(tmp_path):
    report = run_audit(tmp_path, serverless_ok=False, no_rbac_ok=True)
    assert report["diagnosis"] == "rbac_missing_for_serverless_workspace"


def test_missing_actions_are_reported(tmp_path):
    perms = [{"actions": ["*/read"], "notActions": []}]
    report = run_audit(tmp_path, classic_ok=False, permissions=perms)
    allowed = report["diagnostics"]["effective_permissions"]["allowed"]
    assert allowed["Microsoft.Databricks/workspaces/write"] is False
    assert report["diagnosis"] == "identity_lacks_required_actions"


def test_contributor_permissions_allow_required_actions():
    perms = [{"actions": ["*"], "notActions": ["Microsoft.Authorization/*/Write"]}]
    assert all(audit_mod.action_allowed(a, perms) for a in audit_mod.REQUIRED_ACTIONS)
    assert not audit_mod.action_allowed("Microsoft.Authorization/roleAssignments/write", perms)


def test_success_path_is_complete_and_creates_nothing(tmp_path):
    report = run_audit(tmp_path, serverless_ok=True, no_rbac_ok=True)
    assert report["complete"] is True
    assert report["diagnosis"] == "serverless_validation_passed"
    assert report["resources_created"] is False
    assert report["checks"]["bronze_metadata"] == {
        "auth_mode": "login", "count": 2, "contents_read": False, "all_sizes_positive": True}


def test_classic_template_carries_managed_group_but_serverless_does_not():
    serverless = audit_mod.workspace_template("w", "Serverless", "/subscriptions/x/resourceGroups/m")
    classic = audit_mod.workspace_template("w", "Hybrid", "/subscriptions/x/resourceGroups/m")
    assert "managedResourceGroupId" not in serverless["resources"][0]["properties"]
    assert classic["resources"][0]["properties"]["managedResourceGroupId"].endswith("/m")


@pytest.mark.parametrize("outcome", [{}, {"EXISTING_IDENTITY_OUTCOME": "failure"}])
def test_no_azure_call_without_authentication(tmp_path, outcome):
    env = {**ENV, "EXISTING_IDENTITY_OUTCOME": "skipped", **outcome}

    def forbidden(args):
        raise AssertionError("az must not be called")

    report = audit_mod.Audit(env, runner=forbidden, workdir=tmp_path).run()
    assert report["blocked"] == [{"check": "authentication", "code": "azure_login_failed"}]


def test_contributor_wildcard_does_not_grant_blob_data_read():
    perms = [{"actions": ["*"], "notActions": [], "dataActions": []}]
    assert not audit_mod.action_allowed(audit_mod.BLOB_READ_DATA_ACTION, perms, data=True)
    reader = [{"dataActions": ["Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read"]}]
    assert audit_mod.action_allowed(audit_mod.BLOB_READ_DATA_ACTION, reader, data=True)


def test_second_run_shape_is_a_finding_not_a_blocker(tmp_path):
    report = run_audit(tmp_path, serverless_ok=True, no_rbac_ok=True, blob_ok=False)
    assert report["blocked"] == []
    assert report["complete"] is True
    assert report["diagnostics"]["storage_data_permissions"] == {"read": True, "blob_read_data_action": False}
    finding = report["findings"][0]
    assert finding["code"] == "deployment_identity_lacks_blob_data_read"
    assert finding["messages"][0].startswith("You do not have the required permissions")


def test_blob_failure_with_data_role_stays_blocking(tmp_path):
    report = run_audit(tmp_path, serverless_ok=True, no_rbac_ok=True, blob_ok=False,
                       data_actions=[audit_mod.BLOB_READ_DATA_ACTION])
    assert [b["check"] for b in report["blocked"]] == ["bronze_metadata"]
    assert report["findings"] == []
