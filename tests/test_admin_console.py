from fastapi.testclient import TestClient

from app.api.main import app


def _owner(client: TestClient):
    response = client.post("/auth/bootstrap", json={
        "email": "owner@example.com",
        "display_name": "Owner",
        "workspace_name": "Pilot Agency",
        "password": "correct-horse-battery-staple",
    })
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['api_token']}"}


def test_owner_admin_console_combines_account_operations_without_credentials():
    with TestClient(app) as client:
        headers = _owner(client)
        project = client.post("/projects", json={
            "name": "Support assistant", "client_name": "Client Co",
        }, headers=headers)
        assert project.status_code == 201, project.text

        member = client.post("/workspace/members", json={
            "email": "editor@example.com", "display_name": "Editor", "role": "editor",
            "initial_password": "correct-horse-battery-staple",
        }, headers=headers)
        assert member.status_code == 201, member.text

        preferences = client.put("/workspace/notifications", json={
            "enabled": True,
            "recipients": ["ops@example.com"],
            "events": ["run_failed", "run_regressed"],
        }, headers=headers)
        assert preferences.status_code == 200, preferences.text
        assert preferences.json()["delivery_status"] == "configured"

        console = client.get("/workspace/admin-console", headers=headers)
        assert console.status_code == 200, console.text
        data = console.json()
        assert {"members", "projects", "templates", "provider_connections", "usage", "billing", "notifications", "health", "audit_events"} <= set(data)
        assert {member["email"] for member in data["members"]} == {"owner@example.com", "editor@example.com"}
        assert data["projects"][0]["name"] == "Support assistant"
        assert data["notifications"] == {
            "enabled": True,
            "recipients": ["ops@example.com"],
            "events": ["run_failed", "run_regressed"],
            "delivery_status": "configured",
        }
        assert all("api_key" not in str(connection) for connection in data["provider_connections"])
        actions = {event["action"] for event in data["audit_events"]}
        assert {"member.added", "workspace.notifications_updated"} <= actions


def test_notification_preferences_and_admin_console_are_owner_only():
    with TestClient(app) as client:
        headers = _owner(client)
        member = client.post("/workspace/members", json={
            "email": "editor@example.com", "display_name": "Editor", "role": "editor",
            "initial_password": "correct-horse-battery-staple",
        }, headers=headers).json()
        editor_headers = {"Authorization": f"Bearer {member['api_token']}"}

        assert client.get("/workspace/admin-console", headers=editor_headers).status_code == 403
        assert client.put("/workspace/notifications", json={
            "enabled": False, "recipients": [], "events": [],
        }, headers=editor_headers).status_code == 403
