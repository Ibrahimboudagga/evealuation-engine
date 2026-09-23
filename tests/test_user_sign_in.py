from fastapi.testclient import TestClient
from app.api.main import app


def test_password_sign_in_creates_individual_expiring_session_and_logout_revokes_it():
    with TestClient(app) as client:
        boot = client.post('/auth/bootstrap', json={
            'email': 'owner@example.com', 'display_name': 'Owner', 'workspace_name': 'Agency',
            'password': 'a-long-local-password',
        })
        assert boot.status_code == 201
        sign_in = client.post('/auth/sign-in', json={'email': 'owner@example.com', 'password': 'a-long-local-password'})
        assert sign_in.status_code == 200, sign_in.text
        token = sign_in.json()['access_token']
        headers = {'Authorization': f'Bearer {token}'}
        assert client.get('/projects', headers=headers).status_code == 200
        assert client.post('/auth/sign-out', headers=headers).status_code == 204
        assert client.get('/projects', headers=headers).status_code == 401


def test_password_can_be_set_from_a_legacy_bootstrap_token():
    with TestClient(app) as client:
        boot = client.post('/auth/bootstrap', json={
            'email': 'owner@example.com', 'display_name': 'Owner', 'workspace_name': 'Agency',
        }).json()
        headers = {'Authorization': f"Bearer {boot['api_token']}"}
        assert client.put('/auth/password', json={'new_password': 'new-strong-password'}, headers=headers).status_code == 204
        assert client.post('/auth/sign-in', json={'email': 'owner@example.com', 'password': 'new-strong-password'}).status_code == 200
