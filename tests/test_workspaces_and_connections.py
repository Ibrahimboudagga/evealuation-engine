from fastapi.testclient import TestClient
from datetime import datetime, timezone, timedelta

from app.api.main import app
from app.database.connection import get_db
from app.database.models import MembershipDB, ProviderConnectionDB, UserDB, WorkspaceDB, UserSessionDB
from app.services.identity_service import _token_hash


def _bootstrap(client: TestClient):
    response = client.post(
        "/auth/bootstrap",
        json={"email": "owner@example.com", "display_name": "Owner", "workspace_name": "Acme Agency"},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    return payload, {"Authorization": f"Bearer {payload['api_token']}"}


def test_workspace_scopes_projects_datasets_and_runs_and_enforces_roles():
    with TestClient(app) as client:
        owner, owner_headers = _bootstrap(client)
        project = client.post(
            "/projects", json={"name": "Support bot", "client_name": "Northstar"}, headers=owner_headers
        )
        assert project.status_code == 201
        project_id = project.json()["id"]
        dataset = client.post(
            "/datasets",
            json={
                "name": "Support cases", "project_id": project_id,
                "content": '{"input":"hello","expected_output":"hello"}',
            },
            headers=owner_headers,
        )
        assert dataset.status_code == 201
        assert client.get("/projects").status_code == 401

        with get_db() as db:
            now = datetime.now(timezone.utc)
            other_workspace = WorkspaceDB(id="other-workspace", name="Other", slug="other", created_at=now)
            other_user = UserDB(
                id="other-user", email="other@example.com", display_name="Other",
                api_token_hash=_token_hash("other-token"), created_at=now,
            )
            membership = MembershipDB(
                id="other-membership", workspace_id=other_workspace.id, user_id=other_user.id,
                role="viewer", created_at=now,
            )
            db.add_all([other_workspace, other_user, membership])
            db.add(UserSessionDB(id="other-session", user_id=other_user.id, token_hash=_token_hash("other-token"),
                                 expires_at=now + timedelta(hours=12)))
            db.commit()

        other_headers = {"Authorization": "Bearer other-token"}
        assert client.get("/projects", headers=other_headers).json()["projects"] == []
        assert client.get(f"/projects/{project_id}", headers=other_headers).status_code == 404
        assert client.post(
            "/projects", json={"name": "No write", "client_name": "Other"}, headers=other_headers
        ).status_code == 403


def test_provider_connection_encrypts_key_and_hides_it_from_api():
    with TestClient(app) as client:
        owner, headers = _bootstrap(client)
        response = client.post(
            "/provider-connections",
            json={"name": "Production OpenAI", "provider": "openai", "default_model": "gpt-4o", "api_key": "secret-value"},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        connection = response.json()
        assert connection["credential_configured"] is True
        assert "api_key" not in connection
        assert "secret-value" not in response.text

        listed = client.get("/provider-connections", headers=headers)
        assert listed.status_code == 200
        assert listed.json()[0]["id"] == connection["id"]
        assert "encrypted_api_key" not in listed.text

        with get_db() as db:
            stored = db.query(ProviderConnectionDB).filter(ProviderConnectionDB.id == connection["id"]).one()
            assert stored.encrypted_api_key != "secret-value"
            assert "secret-value" not in stored.encrypted_api_key


def test_workspace_run_accepts_connection_ids_without_raw_keys():
    with TestClient(app) as client:
        _, headers = _bootstrap(client)
        project = client.post("/projects", json={"name": "Demo", "client_name": "Client"}, headers=headers).json()
        dataset = client.post(
            "/datasets",
            json={"name": "Cases", "project_id": project["id"], "content": '{"input":"a","expected_output":"a"}'},
            headers=headers,
        ).json()
        connection = client.post(
            "/provider-connections",
            json={"name": "Mock", "provider": "mock", "default_model": "mock", "allow_unauthenticated": True},
            headers=headers,
        ).json()
        run = client.post(
            "/runs",
            json={
                "dataset_id": dataset["id"], "dataset_version_id": dataset["active_version"]["id"],
                "candidate_connection_id": connection["id"], "evaluator_connection_id": connection["id"],
            },
            headers=headers,
        )
        assert run.status_code == 200, run.text
        assert run.json()["is_simulated"] is True
