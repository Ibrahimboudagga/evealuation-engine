from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.api.main import app
from app.database.connection import get_db
from app.database.models import AuditEventDB, EvaluationRunDB
from app.schemas.outcomes import RunStatus
from app.services.activation_service import ActivationService


def _owner(client):
    response = client.post("/auth/bootstrap", json={
        "email": "owner@example.com", "display_name": "Owner", "workspace_name": "Agency",
    })
    assert response.status_code == 201
    payload = response.json()
    return payload, {"Authorization": f"Bearer {payload['api_token']}"}


def test_owner_can_operate_manual_pilot_billing_and_see_trial_warnings():
    with TestClient(app) as client:
        _, headers = _owner(client)
        initial = client.get("/workspace/billing", headers=headers)
        assert initial.status_code == 200
        assert initial.json()["billing_status"] == "trial"
        assert "Trial end date is not set." in initial.json()["warnings"]

        trial = client.put("/workspace/billing", json={
            "billing_status": "trial",
            "trial_ends_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
        }, headers=headers)
        assert trial.status_code == 200
        assert "Trial ends in" in trial.json()["warnings"][0]

        updated = client.put("/workspace/billing", json={
            "plan": "agency-pilot", "billing_status": "manual",
            "trial_ends_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
            "invoice_contact_email": "finance@example.com",
        }, headers=headers)
        assert updated.status_code == 200, updated.text
        assert updated.json()["plan"] == "agency-pilot"
        assert updated.json()["billing_status"] == "manual"
        assert updated.json()["invoice_contact_email"] == "finance@example.com"
        assert updated.json()["warnings"] == []

        with get_db() as db:
            assert db.query(AuditEventDB).filter(AuditEventDB.action == "workspace.billing_updated").count() == 2


def test_activation_funnel_records_first_workspace_actions_without_content_data():
    with TestClient(app) as client:
        bootstrap, headers = _owner(client)
        project = client.post("/projects", json={"name": "Copilot", "client_name": "Client"}, headers=headers).json()
        dataset = client.post("/datasets", json={
            "name": "Cases", "project_id": project["id"],
            "content": '{"id":"case-1","input":"hello","expected_output":"hello"}',
        }, headers=headers).json()
        connection = client.post("/provider-connections", json={
            "name": "Mock", "provider": "mock", "default_model": "mock", "allow_unauthenticated": True,
        }, headers=headers).json()
        template = client.post("/evaluation-templates", json={
            "name": "Standard", "candidate_connection_id": connection["id"],
            "evaluator_connection_id": connection["id"],
        }, headers=headers).json()
        schedule = client.post("/schedules", json={
            "template_id": template["id"], "dataset_id": dataset["id"],
            "dataset_version_id": dataset["active_version"]["id"], "frequency": "weekly",
            "next_execution_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        }, headers=headers)
        assert schedule.status_code == 201, schedule.text

        with get_db() as db:
            db.add(EvaluationRunDB(
                id="activation-completed-run", project_id=project["id"], dataset_id=dataset["id"],
                model_name="mock", status=RunStatus.COMPLETED.value,
            ))
            db.commit()
        assert ActivationService().record_completed_run("activation-completed-run")

        share = client.post("/report-shares", json={
            "run_id": "activation-completed-run", "expires_in_hours": 1,
        }, headers=headers)
        assert share.status_code == 201, share.text
        demo = client.post("/demo/seed", headers=headers)
        assert demo.status_code == 200

        funnel = client.get("/workspace/activation", headers=headers)
        assert funnel.status_code == 200
        milestones = {item["name"]: item for item in funnel.json()["milestones"]}
        expected = {
            "workspace_created", "demo_seeded", "first_project", "first_dataset", "first_template",
            "first_completed_run", "first_shared_report", "first_recurring_schedule",
        }
        assert expected == {name for name, item in milestones.items() if item["completed"]}
        assert all(milestones[name]["count"] == 1 for name in expected)
        assert funnel.json()["completed_milestones"] == len(expected)
        assert funnel.json()["next_milestone"] is None
