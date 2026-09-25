import asyncio
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.api.main import app
from app.database.connection import get_db
from app.database.models import EvaluationRunDB, EvaluationScheduleDB, ProjectDB, WorkspaceDB
from app.services.agency_service import AgencyService
from app.services.dataset_service import DatasetService
from app.services.provider_connection_service import ProviderConnectionService
from app.services.queue_worker import QueueWorker
from app.services.schedule_service import ScheduleService


def _owner(client):
    response = client.post("/auth/bootstrap", json={
        "email": "owner@example.com", "display_name": "Owner", "workspace_name": "Agency",
    })
    assert response.status_code == 201
    return response.json(), {"Authorization": f"Bearer {response.json()['api_token']}"}


def test_due_daily_schedule_creates_a_run_and_worker_health_is_persisted():
    workspace = WorkspaceDB(id="workspace-schedule", name="Agency", slug="agency-schedule")
    project = ProjectDB(id="project-schedule", workspace_id=workspace.id, name="Assistant", client_name="Client")
    with get_db() as db:
        db.add_all([workspace, project])
        db.commit()

    dataset = DatasetService().create_dataset(
        "Cases", '{"id":"case-1","input":"hello","expected_output":"hello"}', project_id="project-schedule",
    )
    connection = ProviderConnectionService().create(
        "workspace-schedule", "Mock", "mock", "mock", allow_unauthenticated=True,
    )
    template = AgencyService().create_template("workspace-schedule", {
        "name": "Daily client check",
        "candidate_connection_id": connection.id,
        "evaluator_connection_id": connection.id,
        "concurrency": 1,
        "timeout_seconds": 10,
    })
    schedule = ScheduleService().create(
        "workspace-schedule", template.id, dataset.id, dataset.versions[0].id, "daily",
        next_execution_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    worker = QueueWorker(worker_id="schedule-test-worker", retry_base_seconds=0, retry_cap_seconds=0)
    assert asyncio.run(worker.run_once())

    with get_db() as db:
        stored_schedule = db.query(EvaluationScheduleDB).filter(EvaluationScheduleDB.id == schedule.id).first()
        assert stored_schedule.last_executed_at is not None
        assert stored_schedule.next_execution_at > stored_schedule.last_executed_at
        execution = stored_schedule.executions[0]
        assert execution.run_id
        assert execution.status == "queued"
        assert db.query(EvaluationRunDB).filter(EvaluationRunDB.id == execution.run_id).first()

    health = worker.health()
    assert health["last_heartbeat_at"] is not None
    assert health["queue_depth"] == 0
    assert health["overdue_schedule_count"] == 0


def test_workspace_usage_limits_are_visible_snapshotted_and_block_new_work():
    with TestClient(app) as client:
        _, headers = _owner(client)
        project = client.post("/projects", json={"name": "Copilot", "client_name": "Client"}, headers=headers)
        assert project.status_code == 201
        dataset = client.post("/datasets", json={
            "name": "Cases", "project_id": project.json()["id"],
            "content": '{"id":"case-1","input":"hello","expected_output":"hello"}',
        }, headers=headers)
        assert dataset.status_code == 201

        limited = client.put("/workspace/limits", json={"limits": {
            "active_projects": 1, "runs": 0, "storage_bytes": 10_000,
        }}, headers=headers)
        assert limited.status_code == 200, limited.text
        assert limited.json()["usage"]["active_projects"] == 1
        assert limited.json()["limits"]["runs"] == 0

        health = client.get("/health")
        assert health.status_code == 200
        assert {"last_heartbeat_at", "claimed_run_id", "queue_depth", "retried_run_count", "failed_run_count", "overdue_schedule_count"} <= set(health.json()["worker"])

        project_limit = client.post("/projects", json={"name": "Second", "client_name": "Client"}, headers=headers)
        assert project_limit.status_code == 429
        assert "active projects" in project_limit.json()["detail"]

        run_limit = client.post("/runs", json={
            "dataset_id": dataset.json()["id"], "dataset_version_id": dataset.json()["active_version"]["id"],
            "candidate_provider": "mock", "candidate_model": "mock",
            "evaluator_provider": "mock", "evaluator_model": "mock",
        }, headers=headers)
        assert run_limit.status_code == 429
        assert "runs" in run_limit.json()["detail"]

        snapshot = client.post("/workspace/usage/snapshot", headers=headers)
        assert snapshot.status_code == 200
        assert snapshot.json()["snapshot_created_at"] is not None
        assert {"runs", "evaluated_cases", "provider_calls", "storage_bytes", "report_shares", "active_projects"} <= set(snapshot.json()["usage"])
