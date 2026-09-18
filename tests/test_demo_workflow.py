import time

from fastapi.testclient import TestClient

from app.api.main import app


def _wait_for_completion(client: TestClient, run_id: str) -> dict:
    for _ in range(40):
        response = client.get(f"/runs/{run_id}")
        if response.json()["status"] in {"completed", "failed", "interrupted"}:
            return response.json()
        time.sleep(0.05)
    raise AssertionError("Demo run did not finish")


def test_mock_only_demo_supports_upload_run_review_and_export():
    with TestClient(app) as client:
        seeded = client.post("/demo/seed")
        repeated = client.post("/demo/seed")
        assert seeded.status_code == repeated.status_code == 200
        assert seeded.json()["project_id"] == repeated.json()["project_id"]

        uploaded = client.post(
            "/datasets/upload",
            data={"name": "Demo upload", "project_id": seeded.json()["project_id"], "tags": "demo"},
            files={
                "file": (
                    "demo-upload.jsonl",
                    b'{"id":"demo-1","input":"What is 2+2?","expected_output":"4"}\n',
                    "application/jsonl",
                )
            },
        )
        assert uploaded.status_code == 201

        run = client.post(
            "/runs",
            json={
                "dataset_id": uploaded.json()["id"],
                "dataset_version_id": uploaded.json()["active_version"]["id"],
                "candidate_provider": "mock",
                "candidate_model": "mock",
                "evaluator_provider": "mock",
                "evaluator_model": "mock",
            },
        )
        assert run.status_code == 200
        status = _wait_for_completion(client, run.json()["run_id"])
        reviewed = client.get(f"/runs/{run.json()['run_id']}/results")
        exported = client.get(f"/runs/{run.json()['run_id']}/export", params={"format": "html"})

    assert status["status"] == "completed"
    assert status["is_simulated"] is True
    assert reviewed.status_code == 200
    assert reviewed.json()["filtered_count"] == 3
    assert exported.status_code == 200
    assert "Evaluation Summary" in exported.text
