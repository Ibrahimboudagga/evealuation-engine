"""Owner-approved policies are immutable inputs to official release checks."""

from fastapi.testclient import TestClient

from app.api.main import app
from app.database.connection import get_db
from app.database.models import EvaluationRunDB, ProjectReleasePolicyRevisionDB


def _setup(client: TestClient):
    owner = client.post("/auth/bootstrap", json={
        "email": "owner@example.com", "display_name": "Owner", "workspace_name": "Agency",
    })
    assert owner.status_code == 201, owner.text
    headers = {"Authorization": "Bearer " + owner.json()["api_token"]}
    project = client.post("/projects", headers=headers, json={
        "name": "Release gate", "client_name": "Client",
    })
    assert project.status_code == 201, project.text
    return owner.json(), headers, project.json()["id"]


def test_owner_approval_and_run_binding_prevent_threshold_shopping():
    with TestClient(app) as client:
        owner, headers, project_id = _setup(client)
        draft = client.post(
            f"/projects/{project_id}/release-policy-revisions",
            headers=headers,
            json={
                "policy_type": "model",
                "change_note": "Pilot release contract",
                "rules": {"coverage_minimum": 0.98, "minimum_valid_cases": 7,
                          "evaluators": {"exact_match": {"pass_rate_minimum": 0.9}}},
            },
        )
        assert draft.status_code == 201, draft.text
        revision = draft.json()
        assert revision["approved_at"] is None

        editor = client.post("/workspace/members", headers=headers, json={
            "email": "editor@example.com", "display_name": "Editor", "role": "editor",
        })
        assert editor.status_code == 201, editor.text
        editor_headers = {"Authorization": "Bearer " + editor.json()["api_token"]}
        forbidden = client.post(
            f"/projects/{project_id}/release-policy-revisions/{revision['id']}/approve",
            headers=editor_headers,
        )
        assert forbidden.status_code == 403

        approved = client.post(
            f"/projects/{project_id}/release-policy-revisions/{revision['id']}/approve",
            headers=headers,
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["approved_by_user_id"] == owner["user_id"]

        connection = client.post("/provider-connections", headers=headers, json={
            "name": "Governed mock", "provider": "mock", "default_model": "mock",
            "allow_unauthenticated": True,
        })
        assert connection.status_code == 201, connection.text
        dataset = client.post("/datasets", headers=headers, json={
            "name": "Release cases", "project_id": project_id,
            "content": '{"id":"one","input":"hello","expected_output":"hello"}',
        })
        assert dataset.status_code == 201, dataset.text
        launched = client.post("/runs", headers=headers, json={
            "dataset_id": dataset.json()["id"],
            "candidate_connection_id": connection.json()["id"],
            "evaluator_connection_id": connection.json()["id"],
            # A caller cannot weaken the approved project contract.
            "release_rules": {"coverage_minimum": 0, "minimum_valid_cases": 1},
        })
        assert launched.status_code == 200, launched.text
        run_id = launched.json()["run_id"]
        status = client.get(f"/runs/{run_id}", headers=headers)
        assert status.status_code == 200, status.text
        request_config = status.json()["run_configuration"]["request"]
        assert request_config["release_rules"]["minimum_valid_cases"] == 7
        assert request_config["release_policy"]["id"] == revision["id"]
        assert request_config["release_policy"]["sha256"] == revision["rules_sha256"]
        with get_db() as db:
            assert db.get(EvaluationRunDB, run_id).release_policy_revision_id == revision["id"]

        with get_db() as db:
            db.get(EvaluationRunDB, run_id).status = "failed"
            db.commit()
        retried = client.post(f"/runs/{run_id}/retry", headers=headers)
        assert retried.status_code == 200, retried.text
        with get_db() as db:
            retry = db.get(EvaluationRunDB, retried.json()["run_id"])
            assert retry.parent_run_id == run_id
            assert retry.release_policy_revision_id == revision["id"]

        unsafe_delete = client.delete(f"/projects/{project_id}", headers=headers)
        assert unsafe_delete.status_code == 409
        assert "customer-data deletion" in unsafe_delete.json()["detail"]

        override = client.get(
            f"/runs/{run_id}/comparison",
            headers=headers,
            params={"baseline_run_id": run_id + "-other", "coverage_minimum": 0},
        )
        assert override.status_code == 422
        assert "not allowed" in override.json()["detail"]

        events = client.get("/audit-events", headers=headers).json()["events"]
        actions = {event["action"] for event in events}
        assert {"release_policy.revision_created", "release_policy.approved"} <= actions

        with get_db() as db:
            db.get(ProjectReleasePolicyRevisionDB, revision["id"]).rules_sha256 = "0" * 64
            db.commit()
        rejected = client.post("/runs", headers=headers, json={
            "dataset_id": dataset.json()["id"],
            "candidate_connection_id": connection.json()["id"],
            "evaluator_connection_id": connection.json()["id"],
        })
        assert rejected.status_code == 409
        assert "integrity" in rejected.json()["detail"]
