import time

from fastapi.testclient import TestClient

from app.api.main import app
from app.database.connection import get_db
from app.database.models import EvaluationRunDB, PairwiseRunDB


def _create_dataset(client: TestClient) -> dict:
    response = client.post(
        "/datasets",
        json={
            "name": "Agency regression set",
            "content": '{"id":"case-1","input":"What is 2+2?","expected_output":"4"}\n',
        },
    )
    assert response.status_code == 201
    return response.json()


def _wait_for_terminal_status(client: TestClient, path: str) -> dict:
    response = None
    for _ in range(40):
        response = client.get(path)
        if response.json()["status"] in {"completed", "failed", "interrupted"}:
            return response.json()
        time.sleep(0.05)
    raise AssertionError("Run did not reach a terminal status.")


def test_single_run_can_be_submitted_from_an_immutable_dataset_version():
    with TestClient(app) as client:
        dataset = _create_dataset(client)
        version_id = dataset["active_version"]["id"]
        response = client.post(
            "/runs",
            json={
                "dataset_id": dataset["id"],
                "dataset_version_id": version_id,
                "candidate_provider": "mock",
                "candidate_model": "mock",
                "evaluator_provider": "mock",
                "evaluator_model": "mock",
            },
        )
        assert response.status_code == 200
        run_id = response.json()["run_id"]
        status = _wait_for_terminal_status(client, f"/runs/{run_id}")

    assert status["status"] == "completed"
    assert status["run_configuration"]["dataset"]["version_id"] == version_id
    with get_db() as db:
        run = db.query(EvaluationRunDB).filter_by(id=run_id).one()
        assert run.dataset_id == dataset["id"]
        assert run.dataset_version_id == version_id


def test_pairwise_run_can_be_submitted_from_an_immutable_dataset_version():
    with TestClient(app) as client:
        dataset = _create_dataset(client)
        version_id = dataset["active_version"]["id"]
        response = client.post(
            "/pairwise-runs",
            json={
                "dataset_id": dataset["id"],
                "dataset_version_id": version_id,
                "model_a_provider": "mock",
                "model_a_model": "mock",
                "model_b_provider": "mock",
                "model_b_model": "mock",
                "judge_provider": "mock",
                "judge_model": "mock",
            },
        )
        assert response.status_code == 200
        run_id = response.json()["run_id"]
        status = _wait_for_terminal_status(client, f"/pairwise-runs/{run_id}")

    assert status["status"] == "completed"
    assert status["run_configuration"]["dataset"]["version_id"] == version_id
    with get_db() as db:
        run = db.query(PairwiseRunDB).filter_by(id=run_id).one()
        assert run.dataset_id == dataset["id"]
        assert run.dataset_version_id == version_id


def test_run_request_requires_one_dataset_source():
    with TestClient(app) as client:
        response = client.post(
            "/runs",
            json={
                "candidate_provider": "mock",
                "candidate_model": "mock",
                "evaluator_provider": "mock",
                "evaluator_model": "mock",
            },
        )
    assert response.status_code == 422
    assert "dataset_id or dataset_path" in response.text
