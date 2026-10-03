from fastapi.testclient import TestClient

from app.api.main import app


def test_audit_events_retention_controls_and_health_are_available():
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["database_backend"] == "sqlite"

        bootstrap = client.post("/auth/bootstrap", json={
            "email": "owner@example.com", "display_name": "Owner", "workspace_name": "Agency",
        })
        assert bootstrap.status_code == 201
        headers = {"Authorization": f"Bearer {bootstrap.json()['api_token']}"}
        project = client.post("/projects", json={"name": "Client App", "client_name": "Client"}, headers=headers)
        assert project.status_code == 201

        events = client.get("/audit-events", headers=headers)
        assert events.status_code == 200
        actions = {event["action"] for event in events.json()["events"]}
        assert {"workspace.bootstrapped", "project.created"} <= actions
        assert all(event["actor_email"] == "owner@example.com" for event in events.json()["events"])

        retention = client.put("/operations/retention", json={"retention_days": 90}, headers=headers)
        assert retention.status_code == 200
        assert retention.json()["retention_days"] == 90
        assert retention.json()["content_retention_enabled"] is False
        applied = client.post("/operations/retention/apply", headers=headers)
        assert applied.status_code == 200
        assert applied.json()["content_retention_enabled"] is False
        assert applied.json()["evaluation_runs_deleted"] == 0
        assert applied.json()["audit_events_deleted"] >= 0
