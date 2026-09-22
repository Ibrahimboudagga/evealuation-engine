from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.api.main import app
from app.database.connection import get_db
from app.database.models import EvaluationResultDB, EvaluationRunDB
from app.schemas.outcomes import EvaluationOutcome, RunStatus


def _owner(client):
    response = client.post("/auth/bootstrap", json={
        "email": "owner@example.com", "display_name": "Owner", "workspace_name": "Agency",
    })
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['api_token']}"}


def _project_and_dataset(client, headers):
    project = client.post("/projects", json={"name": "Copilot", "client_name": "Client"}, headers=headers).json()
    dataset = client.post("/datasets", json={
        "name": "Cases", "project_id": project["id"],
        "content": '{"id":"case-1","input":"hello","expected_output":"hello"}',
    }, headers=headers).json()
    return project, dataset


def _completed_run(project_id, dataset_id, run_id="completed-run"):
    with get_db() as db:
        db.add(EvaluationRunDB(
            id=run_id, project_id=project_id, dataset_id=dataset_id, model_name="model",
            status=RunStatus.COMPLETED.value, completed_at=datetime.now(timezone.utc),
        ))
        db.add(EvaluationResultDB(
            run_id=run_id, example_id="case-1", prompt="hello", prediction="hello", expected_output="hello",
            evaluator_name="exact_match", score=1.0, outcome=EvaluationOutcome.EVALUATED.value,
        ))
        db.commit()


def test_template_launches_a_workspace_run_with_saved_connections():
    with TestClient(app) as client:
        headers = _owner(client)
        project, dataset = _project_and_dataset(client, headers)
        connection = client.post("/provider-connections", json={
            "name": "Mock", "provider": "mock", "default_model": "mock", "allow_unauthenticated": True,
        }, headers=headers).json()
        template = client.post("/evaluation-templates", json={
            "name": "Client standard", "candidate_connection_id": connection["id"],
            "evaluator_connection_id": connection["id"], "concurrency": 2, "timeout_seconds": 20,
            "coverage_minimum": 0.95, "exact_match_pass_rate_max_drop": 0.05,
            "report_preferences": {"agency_name": "Agency"},
        }, headers=headers)
        assert template.status_code == 201, template.text
        launched = client.post(f"/evaluation-templates/{template.json()['id']}/launch", json={
            "dataset_id": dataset["id"], "dataset_version_id": dataset["active_version"]["id"],
        }, headers=headers)
        assert launched.status_code == 200, launched.text
        assert client.get("/evaluation-templates", headers=headers).json()[0]["name"] == "Client standard"


def test_client_share_is_public_read_only_and_revocable_and_dashboard_is_scoped():
    with TestClient(app) as client:
        headers = _owner(client)
        project, dataset = _project_and_dataset(client, headers)
        _completed_run(project["id"], dataset["id"])
        dashboard = client.get(f"/projects/{project['id']}/dashboard", headers=headers)
        assert dashboard.status_code == 200
        assert dashboard.json()["latest_run"]["run_id"] == "completed-run"
        assert dashboard.json()["quality_trend"][0]["average_score"] == 1.0

        share = client.post("/report-shares", json={
            "run_id": "completed-run", "expires_in_hours": 1,
            "branding": {"agency_name": "Agency Studio", "report_title": "Client Health"},
        }, headers=headers)
        assert share.status_code == 201, share.text
        url = share.json()["url"]
        public = client.get(url)
        assert public.status_code == 200
        assert "Agency Studio" in public.text
        assert "Client / Copilot" in public.text
        assert client.delete(f"/report-shares/{share.json()['id']}", headers=headers).status_code == 204
        assert client.get(url).status_code == 404
