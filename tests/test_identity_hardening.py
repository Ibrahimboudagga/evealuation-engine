"""Regression tests for workspace owner safety and sign-in abuse controls."""

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.api.main import app
from app.database.connection import get_db
from app.database.models import AuthThrottleDB


def _bootstrap(client: TestClient, password: str = "owner-password-123"):
    response = client.post("/auth/bootstrap", json={
        "email": "owner@example.com",
        "display_name": "Owner",
        "workspace_name": "Agency",
        "password": password,
    })
    assert response.status_code == 201, response.text
    return response.json(), {"Authorization": "Bearer " + response.json()["api_token"]}


def test_last_owner_cannot_be_demoted_or_removed_through_role_mutations():
    with TestClient(app) as client:
        owner, headers = _bootstrap(client)

        demotion = client.put(
            f"/workspace/members/{owner['user_id']}",
            headers=headers,
            json={"role": "editor"},
        )
        assert demotion.status_code == 409
        assert "last owner" in demotion.json()["detail"]

        # The member-creation endpoint used to provide a second path for an
        # owner to overwrite their own role.
        overwrite = client.post("/workspace/members", headers=headers, json={
            "email": "owner@example.com",
            "display_name": "Owner",
            "role": "viewer",
        })
        assert overwrite.status_code in (400, 409)

        second = client.post("/workspace/members", headers=headers, json={
            "email": "second-owner@example.com",
            "display_name": "Second owner",
            "role": "owner",
        })
        assert second.status_code == 201, second.text
        removed = client.delete(
            f"/workspace/members/{second.json()['user_id']}", headers=headers
        )
        assert removed.status_code == 204

        # Removing the second owner restored the invariant: the remaining
        # owner cannot now demote themselves.
        assert client.put(
            f"/workspace/members/{owner['user_id']}",
            headers=headers,
            json={"role": "editor"},
        ).status_code == 409


def test_failed_sign_ins_are_persistently_throttled_without_storing_email():
    with TestClient(app) as client:
        _bootstrap(client)
        for _ in range(4):
            response = client.post("/auth/sign-in", json={
                "email": "owner@example.com", "password": "wrong-password-123",
            })
            assert response.status_code == 401
            assert response.json()["detail"] == "Invalid email or password"

        locked = client.post("/auth/sign-in", json={
            "email": "owner@example.com", "password": "wrong-password-123",
        })
        assert locked.status_code == 429
        assert locked.headers["retry-after"] == "900"

        # A correct password cannot bypass the lockout window.
        assert client.post("/auth/sign-in", json={
            "email": "owner@example.com", "password": "owner-password-123",
        }).status_code == 429

        with get_db() as db:
            rows = db.query(AuthThrottleDB).all()
            assert len(rows) == 1
            assert rows[0].failure_count == 5
            assert "owner@example.com" not in rows[0].subject_hash


def test_successful_sign_in_clears_prior_failures():
    with TestClient(app) as client:
        _bootstrap(client)
        for _ in range(2):
            assert client.post("/auth/sign-in", json={
                "email": "owner@example.com", "password": "wrong-password-123",
            }).status_code == 401
        success = client.post("/auth/sign-in", json={
            "email": "owner@example.com", "password": "owner-password-123",
        })
        assert success.status_code == 200
        with get_db() as db:
            assert db.query(AuthThrottleDB).count() == 0


def test_parallel_failed_sign_ins_increment_one_atomic_bucket():
    from app.services.identity_service import IdentityService

    with TestClient(app) as client:
        _bootstrap(client)

        def fail_sign_in(_):
            try:
                IdentityService().sign_in("owner@example.com", "wrong-password-123")
            except ValueError:
                return

        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(fail_sign_in, range(5)))

        with get_db() as db:
            throttle = db.query(AuthThrottleDB).one()
            assert throttle.failure_count == 5
            assert throttle.locked_until is not None


def test_production_health_rejects_documented_secret_placeholders(monkeypatch):
    from app.config import deployment_health, get_settings

    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@db/evals")
    monkeypatch.setenv("WORKSPACE_ENCRYPTION_KEY", "nE2GzCPhZYs7pF5uHnozL_Digkc8jvOnSeys1bpFHj8=")
    monkeypatch.setenv("BOOTSTRAP_SECRET", "replace-with-a-random-operator-setup-secret")
    monkeypatch.setenv("AUTH_THROTTLE_SECRET", "replace-with-an-independent-random-32-character-secret")
    get_settings.cache_clear()
    try:
        health = deployment_health()
        assert health["status"] == "degraded"
        assert any("BOOTSTRAP_SECRET" in issue for issue in health["issues"])
        assert any("AUTH_THROTTLE_SECRET" in issue for issue in health["issues"])
        assert "replace-with" not in str(health)
    finally:
        get_settings.cache_clear()
