import time

from fastapi.testclient import TestClient

from app.api.main import app
from app.database.connection import get_db
from app.database.models import DatasetDB, EvaluationRunDB, PairwiseRunDB


def _create_project(client: TestClient, name: str, client_name: str) -> dict:
    response = client.post(
        "/projects",
        json={"name": name, "client_name": client_name, "description": "Client evaluation workspace", "tags": ["pilot"]},
    )
    assert response.status_code == 201
    return response.json()


def _create_dataset(client: TestClient, project_id: str, name: str) -> dict:
    response = client.post(
        "/datasets",
        json={
            "name": name,
            "project_id": project_id,
            "content": '{"id":"case-1","input":"What is 2+2?","expected_output":"4"}\n',
        },
    )
    assert response.status_code == 201
    return response.json()


def test_projects_can_be_created_listed_and_updated():
    with TestClient(app) as client:
        created = _create_project(client, "Support Copilot", "Acme")
        listed = client.get("/projects")
        updated = client.put(
            f"/projects/{created['id']}",
            json={"name": "Support Copilot v2", "tags": ["pilot", "priority"]},
        )

    assert listed.status_code == 200
    assert listed.json()["projects"][0]["client_name"] == "Acme"
    assert updated.status_code == 200
    assert updated.json()["name"] == "Support Copilot v2"
    assert updated.json()["tags"] == ["pilot", "priority"]


def test_projects_keep_client_datasets_and_runs_separate():
    with TestClient(app) as client:
        acme = _create_project(client, "Support Copilot", "Acme")
        beta = _create_project(client, "Sales Assistant", "Beta Corp")
        acme_dataset = _create_dataset(client, acme["id"], "Acme regression set")
        beta_dataset = _create_dataset(client, beta["id"], "Beta regression set")

        filtered = client.get("/datasets", params={"project_id": acme["id"]})
        assert filtered.status_code == 200
        assert [item["id"] for item in filtered.json()["datasets"]] == [acme_dataset["id"]]
        assert filtered.json()["datasets"][0]["client_name"] == "Acme"

        response = client.post(
            "/runs",
            json={
                "dataset_id": acme_dataset["id"],
                "dataset_version_id": acme_dataset["active_version"]["id"],
                "candidate_provider": "mock",
                "candidate_model": "mock",
                "evaluator_provider": "mock",
                "evaluator_model": "mock",
            },
        )
        assert response.status_code == 200
        run_id = response.json()["run_id"]

        status = None
        for _ in range(40):
            status = client.get(f"/runs/{run_id}")
            if status.json()["status"] == "completed":
                break
            time.sleep(0.05)

        pairwise_response = client.post(
            "/pairwise-runs",
            json={
                "dataset_id": acme_dataset["id"],
                "dataset_version_id": acme_dataset["active_version"]["id"],
                "model_a_provider": "mock",
                "model_a_model": "mock-a",
                "model_b_provider": "mock",
                "model_b_model": "mock-b",
                "judge_provider": "mock",
                "judge_model": "mock-judge",
            },
        )
        assert pairwise_response.status_code == 200
        pairwise_run_id = pairwise_response.json()["run_id"]
        pairwise_status = None
        for _ in range(40):
            pairwise_status = client.get(f"/pairwise-runs/{pairwise_run_id}")
            if pairwise_status.json()["status"] == "completed":
                break
            time.sleep(0.05)

    assert status is not None
    assert status.json()["project_id"] == acme["id"]
    assert status.json()["run_configuration"]["dataset"]["project_id"] == acme["id"]
    assert pairwise_status is not None
    assert pairwise_status.json()["project_id"] == acme["id"]
    assert pairwise_status.json()["run_configuration"]["dataset"]["project_id"] == acme["id"]
    with get_db() as db:
        assert db.query(DatasetDB).filter_by(id=beta_dataset["id"]).one().project_id == beta["id"]
        assert db.query(EvaluationRunDB).filter_by(id=run_id).one().project_id == acme["id"]
        assert db.query(PairwiseRunDB).filter_by(id=pairwise_run_id).one().project_id == acme["id"]


def test_deleting_a_project_preserves_and_unassigns_its_records():
    with TestClient(app) as client:
        project = _create_project(client, "Knowledge Base", "Acme")
        dataset = _create_dataset(client, project["id"], "Knowledge base checks")
        deleted = client.delete(f"/projects/{project['id']}")
        detail = client.get(f"/datasets/{dataset['id']}")

    assert deleted.status_code == 200
    assert detail.status_code == 200
    assert detail.json()["project_id"] is None
