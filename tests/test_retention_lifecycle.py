"""Customer content retention and explicit project deletion receipts."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.api.main import app
from app.database.connection import get_db
from app.database.models import (
    AuditEventDB,
    DatasetDB,
    DatasetVersionDB,
    EvaluationResultDB,
    EvaluationRunDB,
    ProjectDB,
)


def _setup(client: TestClient):
    boot = client.post("/auth/bootstrap", json={
        "email": "owner@example.com", "display_name": "Owner", "workspace_name": "Agency",
    })
    assert boot.status_code == 201, boot.text
    headers = {"Authorization": "Bearer " + boot.json()["api_token"]}
    project = client.post("/projects", headers=headers, json={
        "name": "Client product", "client_name": "Client",
    })
    assert project.status_code == 201, project.text
    dataset = client.post("/datasets", headers=headers, json={
        "name": "Cases", "project_id": project.json()["id"],
        "content": '{"id":"one","input":"q","expected_output":"a"}',
    })
    assert dataset.status_code == 201, dataset.text
    return headers, project.json(), dataset.json()


def test_content_retention_deletes_old_terminal_evidence_but_not_active_dataset_version():
    with TestClient(app) as client:
        headers, project, dataset = _setup(client)
        old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=45)
        with get_db() as db:
            db.add(EvaluationRunDB(
                id="old-run", dataset_id=dataset["id"],
                dataset_version_id=dataset["active_version"]["id"],
                project_id=project["id"], model_name="mock", status="completed",
                created_at=old, completed_at=old, is_simulated=True,
            ))
            db.add(EvaluationResultDB(
                run_id="old-run", example_id="one", prompt="q", prediction="a",
                expected_output="a", score=1, evaluator_name="exact_match", outcome="evaluated",
            ))
            db.add(EvaluationRunDB(
                id="recently-completed-run", dataset_id=dataset["id"],
                dataset_version_id=dataset["active_version"]["id"],
                project_id=project["id"], model_name="mock", status="completed",
                created_at=old, completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                is_simulated=True,
            ))
            db.add(DatasetVersionDB(
                id="old-inactive-version", dataset_id=dataset["id"], version_number=0,
                content='[{"id":"old"}]', example_count=1, is_active=False, created_at=old,
            ))
            db.commit()

        settings = client.put("/operations/retention", headers=headers, json={
            "retention_days": 30, "content_retention_enabled": True,
        })
        assert settings.status_code == 200, settings.text
        applied = client.post("/operations/retention/apply", headers=headers)
        assert applied.status_code == 200, applied.text
        assert applied.json()["evaluation_runs_deleted"] == 1
        assert applied.json()["evaluation_results_deleted"] == 1
        assert applied.json()["dataset_versions_deleted"] == 1
        with get_db() as db:
            assert db.get(EvaluationRunDB, "old-run") is None
            assert db.get(EvaluationRunDB, "recently-completed-run") is not None
            assert db.get(DatasetVersionDB, "old-inactive-version") is None
            assert db.get(DatasetVersionDB, dataset["active_version"]["id"]) is not None


def test_legal_hold_blocks_explicit_project_purge_and_confirmed_purge_is_complete():
    with TestClient(app) as client:
        headers, project, dataset = _setup(client)
        preview = client.get(
            f"/projects/{project['id']}/customer-data/deletion-preview", headers=headers
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["counts"]["datasets"] == 1
        assert preview.json()["counts"]["dataset_versions"] == 1

        held = client.put("/operations/retention", headers=headers, json={
            "retention_days": 365,
            "legal_hold": True,
            "legal_hold_reason": "Client litigation hold",
        })
        assert held.status_code == 200, held.text
        # Updating the duration through an older client must not silently
        # release a hold or change content-retention mode.
        preserved = client.put("/operations/retention", headers=headers, json={
            "retention_days": 730,
        })
        assert preserved.status_code == 200, preserved.text
        assert preserved.json()["legal_hold"] is True
        assert preserved.json()["legal_hold_reason"] == "Client litigation hold"
        old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=400)
        with get_db() as db:
            workspace_id = db.get(ProjectDB, project["id"]).workspace_id
            db.add(AuditEventDB(
                id="held-audit-event", workspace_id=workspace_id,
                project_id=project["id"], action="held.evidence",
                entity_type="project", entity_id=project["id"], created_at=old,
            ))
            db.commit()
        retained = client.post("/operations/retention/apply", headers=headers)
        assert retained.status_code == 200, retained.text
        assert retained.json()["legal_hold"] is True
        assert all(
            value == 0
            for key, value in retained.json().items()
            if key.endswith("_deleted")
        )
        with get_db() as db:
            assert db.get(AuditEventDB, "held-audit-event") is not None

        blocked = client.request(
            "DELETE",
            f"/projects/{project['id']}/customer-data",
            headers=headers,
            json={"confirm_project_name": project["name"]},
        )
        assert blocked.status_code == 409

        released = client.put("/operations/retention", headers=headers, json={
            "retention_days": 365, "legal_hold": False,
        })
        assert released.status_code == 200, released.text
        wrong = client.request(
            "DELETE",
            f"/projects/{project['id']}/customer-data",
            headers=headers,
            json={"confirm_project_name": "wrong name"},
        )
        assert wrong.status_code == 422
        deleted = client.request(
            "DELETE",
            f"/projects/{project['id']}/customer-data",
            headers=headers,
            json={"confirm_project_name": project["name"]},
        )
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["legal_hold"] is False
        assert deleted.json()["counts"]["datasets"] == 1
        with get_db() as db:
            assert db.get(ProjectDB, project["id"]) is None
            assert db.get(DatasetDB, dataset["id"]) is None
            project_events = db.query(AuditEventDB).filter(
                AuditEventDB.project_id == project["id"]
            ).all()
            assert [event.action for event in project_events] == [
                "project.customer_data_deleted"
            ]
        actions = {event["action"] for event in client.get("/audit-events", headers=headers).json()["events"]}
        assert "project.customer_data_deleted" in actions
