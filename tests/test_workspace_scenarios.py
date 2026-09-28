"""Workspace scenario workflow and isolation using synthetic uploaded evidence."""
import json
from fastapi.testclient import TestClient
from app.api.main import app

CASE = {"id": "case-1", "task": "Return a supported answer", "assertions": [
    {"name": "correct", "path": "/answer", "value": 4}]}

def setup(client):
    response = client.post("/auth/bootstrap", json={"email": "scenario-owner@example.com",
        "display_name": "Owner", "workspace_name": "Scenario Agency"})
    assert response.status_code == 201, response.text
    headers = {"Authorization": "Bearer " + response.json()["api_token"]}
    project = client.post("/projects", headers=headers, json={"name": "Agent", "client_name": "Acme"}).json()
    return headers, project["id"]

def suite(client, headers, project):
    response = client.post("/scenario-suites", headers=headers, json={"project_id": project,
        "name": "Golden synthetic suite", "content": json.dumps(CASE)})
    assert response.status_code == 201, response.text
    return response.json()

def run(client, headers, suite_id, answer=4):
    response = client.post("/scenario-runs", headers=headers, json={"suite_id": suite_id,
        "target_build": "staging-v2", "evidence": {"case-1": {"output": {"answer": answer},
        "simulated": True}}})
    assert response.status_code == 201, response.text
    return response.json()

def test_scenario_upload_review_comparison_export_share_and_revoke():
    with TestClient(app) as client:
        headers, project = setup(client)
        version = suite(client, headers, project)
        baseline = run(client, headers, version["id"])
        regression = run(client, headers, version["id"], answer=3)
        comparison = client.get(f"/scenario-runs/{regression['id']}/compare",
            params={"baseline_run_id": baseline["id"]}, headers=headers)
        assert comparison.json()["decision"] == "regressed", comparison.text
        corrected = run(client, headers, version["id"])
        assert client.get(f"/scenario-runs/{corrected['id']}/compare",
            params={"baseline_run_id": baseline["id"]}, headers=headers).json()["decision"] == "passed"
        report = client.get(f"/scenario-runs/{regression['id']}/export", headers=headers)
        assert report.status_code == 200 and "SIMULATED" in report.text and "correct" in report.text
        shared = client.post(f"/scenario-runs/{regression['id']}/shares", headers=headers,
            json={"expires_in_hours": 1})
        assert shared.status_code == 201, shared.text
        public_path = shared.json()["url"]
        assert client.get(public_path).status_code == 200
        assert client.get(f"/scenario-runs/{regression['id']}").status_code == 401
        assert client.delete(f"/scenario-shares/{shared.json()['id']}", headers=headers).status_code == 204
        assert client.get(public_path).status_code == 404
        usage = client.get("/workspace/usage", headers=headers)
        assert usage.status_code == 200, usage.text
        assert usage.json()["usage"]["runs"] == 3
        assert usage.json()["usage"]["provider_calls"] == 0

def test_missing_evidence_cannot_pass_and_unknown_cases_are_rejected():
    with TestClient(app) as client:
        headers, project = setup(client)
        version = suite(client, headers, project)
        response = client.post("/scenario-runs", headers=headers, json={"suite_id": version["id"],
            "target_build": "build-1", "evidence": {}})
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["metrics"]["decision"] == "inconclusive"
        assert data["metrics"]["coverage"] == 0
        assert data["results"][0]["score"] is None
        unknown = client.post("/scenario-runs", headers=headers, json={"suite_id": version["id"],
            "target_build": "build-1", "evidence": {"unexpected": {}}})
        assert unknown.status_code == 422

def test_client_viewer_cannot_enumerate_other_project_scenario_evidence():
    with TestClient(app) as client:
        headers, project = setup(client)
        version = suite(client, headers, project)
        recorded = run(client, headers, version["id"])
        member = client.post("/workspace/members", headers=headers, json={"email": "client@example.com",
            "display_name": "Client", "role": "client_viewer"})
        assert member.status_code == 201, member.text
        client_headers = {"Authorization": "Bearer " + member.json()["api_token"]}
        assert client.get("/scenario-suites", headers=client_headers).json() == []
        assert client.get("/scenario-runs", headers=client_headers).json() == []
        for path in (f"/scenario-suites/{version['id']}", f"/scenario-runs/{recorded['id']}",
                     f"/scenario-runs/{recorded['id']}/export"):
            assert client.get(path, headers=client_headers).status_code == 404
        assert client.post("/scenario-runs", headers=client_headers, json={"suite_id": version["id"],
            "target_build": "x", "evidence": {}}).status_code in (403, 404)

def test_owner_deletion_revokes_shares_and_project_history_is_not_orphaned():
    with TestClient(app) as client:
        headers, project = setup(client)
        version = suite(client, headers, project)
        recorded = run(client, headers, version["id"])
        shared = client.post(f"/scenario-runs/{recorded['id']}/shares", headers=headers, json={}).json()
        assert client.delete(f"/projects/{project}", headers=headers).status_code == 409
        assert client.delete(f"/scenario-suites/{version['id']}", headers=headers).status_code == 409
        assert client.delete(f"/scenario-runs/{recorded['id']}", headers=headers).status_code == 204
        assert client.get(shared["url"]).status_code == 404
        assert client.delete(f"/scenario-suites/{version['id']}", headers=headers).status_code == 204
        assert client.delete(f"/projects/{project}", headers=headers).status_code == 200


def test_imported_evidence_obeys_limits_and_preserves_version_identity():
    from app.services.identity_service import IdentityService
    from app.services.usage_service import UsageService
    with TestClient(app) as client:
        headers, project = setup(client)
        version1 = suite(client, headers, project)
        version2 = suite(client, headers, project)
        assert version2["version"] == 2
        assert version1["id"] != version2["id"]
        context = IdentityService().authenticate(headers["Authorization"].removeprefix("Bearer "))
        UsageService().set_limits(context.workspace_id, {"runs": 0})
        rejected = client.post("/scenario-runs", headers=headers, json={
            "suite_id": version2["id"], "target_build": "staging", "evidence": {}})
        assert rejected.status_code == 429
        assert client.get("/scenario-runs", headers=headers).json() == []


def test_scenario_report_redacts_obvious_credentials_and_escapes_markup():
    with TestClient(app) as client:
        headers, project = setup(client)
        version = suite(client, headers, project)
        response = client.post("/scenario-runs", headers=headers, json={"suite_id": version["id"],
            "target_build": "<script>alert(1)</script>", "evidence": {"case-1": {
                "simulated": True, "output": {"answer": 4, "password": "DO-NOT-EXPORT",
                                             "extra": "<script>alert(1)</script>"}}}})
        assert response.status_code == 201
        assert "DO-NOT-EXPORT" not in response.text
        report = client.get(f"/scenario-runs/{response.json()['id']}/export", headers=headers)
        assert "DO-NOT-EXPORT" not in report.text
        assert "<script>" not in report.text
        assert "&lt;script&gt;" in report.text


def test_shared_scenario_snapshot_contains_selected_baseline_decision():
    with TestClient(app) as client:
        headers, project = setup(client)
        version = suite(client, headers, project)
        baseline = run(client, headers, version["id"])
        regression = run(client, headers, version["id"], answer=2)
        shared = client.post(f"/scenario-runs/{regression['id']}/shares", headers=headers,
            json={"baseline_run_id": baseline["id"]}).json()
        document = client.get(shared["url"])
        assert document.status_code == 200
        assert "Baseline release decision" in document.text and "regressed" in document.text
        assert baseline["id"] in document.text
        # Deleting the baseline must not silently change an already approved share.
        assert client.delete(f"/scenario-runs/{baseline['id']}", headers=headers).status_code == 204
        assert client.get(shared["url"]).text == document.text
