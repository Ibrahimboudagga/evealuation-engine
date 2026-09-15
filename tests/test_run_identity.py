import json
import time
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.database.connection import get_db
from app.database.models import EvaluationRunDB, PairwiseRunDB
from app.evaluators.pairwise_judge import PairwiseJudgeEvaluator
from app.evaluators.registry import EvaluatorRegistry
from app.runners.eval_runner import EvaluationRunner
from app.runners.pairwise_runner import PairwiseEvaluationRunner
from app.schemas.outcomes import RunStatus
from tests.fakes import DeterministicFakeProvider


@pytest.fixture
def one_example_dataset(tmp_path):
    path = tmp_path / "identity-example.jsonl"
    path.write_text(
        json.dumps({"id": "case-1", "input": "What is 2+2?", "expected_output": "4"}) + "\n",
        encoding="utf-8",
    )
    return str(path)


@pytest.mark.asyncio
async def test_single_model_runner_reuses_the_submitted_database_id(one_example_dataset):
    provider = DeterministicFakeProvider.successful()
    runner = EvaluationRunner(provider, EvaluatorRegistry(judge_provider=DeterministicFakeProvider.valid_judge()))

    submitted_id = runner.create_run(dataset_path=one_example_dataset)
    with get_db() as db:
        assert db.query(EvaluationRunDB).filter_by(id=submitted_id).one().status == RunStatus.QUEUED.value

    completed_id = await runner.run_evaluation(dataset_path=one_example_dataset, run_id=submitted_id)

    with get_db() as db:
        run = db.query(EvaluationRunDB).filter_by(id=submitted_id).one()
    assert completed_id == submitted_id
    assert run.status == RunStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_failed_run_keeps_the_submitted_database_id(one_example_dataset):
    runner = EvaluationRunner(DeterministicFakeProvider.successful(), EvaluatorRegistry())
    submitted_id = runner.create_run(dataset_path=one_example_dataset)

    async def fail_example(*_args, **_kwargs):
        raise RuntimeError("forced execution failure")

    runner._run_example = fail_example
    with pytest.raises(RuntimeError, match="forced execution failure"):
        await runner.run_evaluation(dataset_path=one_example_dataset, run_id=submitted_id)

    with get_db() as db:
        run = db.query(EvaluationRunDB).filter_by(id=submitted_id).one()
    assert run.status == RunStatus.FAILED.value
    assert run.error_message == "forced execution failure"


@pytest.mark.asyncio
async def test_interrupted_run_keeps_the_submitted_database_id(one_example_dataset):
    runner = EvaluationRunner(DeterministicFakeProvider.successful(), EvaluatorRegistry())
    submitted_id = runner.create_run(dataset_path=one_example_dataset)

    async def wait_for_cancellation(*_args, **_kwargs):
        await asyncio.Event().wait()

    runner._run_example = wait_for_cancellation
    task = asyncio.create_task(
        runner.run_evaluation(dataset_path=one_example_dataset, run_id=submitted_id)
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with get_db() as db:
        run = db.query(EvaluationRunDB).filter_by(id=submitted_id).one()
    assert run.status == RunStatus.INTERRUPTED.value
    assert run.error_message == "Execution interrupted."


@pytest.mark.asyncio
async def test_pairwise_runner_reuses_the_submitted_database_id(one_example_dataset):
    runner = PairwiseEvaluationRunner(
        provider_a=DeterministicFakeProvider.successful(),
        provider_b=DeterministicFakeProvider.successful(),
        pairwise_evaluator=PairwiseJudgeEvaluator(DeterministicFakeProvider.valid_pairwise_judge()),
    )
    submitted_id = runner.create_run(dataset_path=one_example_dataset)

    completed_id = await runner.run_pairwise_evaluation(dataset_path=one_example_dataset, run_id=submitted_id)

    with get_db() as db:
        run = db.query(PairwiseRunDB).filter_by(id=submitted_id).one()
    assert completed_id == submitted_id
    assert run.status == RunStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_database_backed_run_status_survives_a_new_api_client(one_example_dataset):
    runner = EvaluationRunner(DeterministicFakeProvider.successful(), EvaluatorRegistry())
    submitted_id = runner.create_run(dataset_path=one_example_dataset)
    await runner.run_evaluation(dataset_path=one_example_dataset, run_id=submitted_id)

    # A new API client has no in-memory run state to restore; it must read the database.
    with TestClient(app) as client:
        response = client.get(f"/runs/{submitted_id}")

    assert response.status_code == 200
    assert response.json()["run_id"] == submitted_id
    assert response.json()["status"] == RunStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_failed_database_run_status_survives_a_new_api_client(one_example_dataset):
    runner = EvaluationRunner(DeterministicFakeProvider.successful(), EvaluatorRegistry())
    submitted_id = runner.create_run(dataset_path=one_example_dataset)

    async def fail_example(*_args, **_kwargs):
        raise RuntimeError("restart failure")

    runner._run_example = fail_example
    with pytest.raises(RuntimeError, match="restart failure"):
        await runner.run_evaluation(dataset_path=one_example_dataset, run_id=submitted_id)

    with TestClient(app) as client:
        response = client.get(f"/runs/{submitted_id}")

    assert response.status_code == 200
    assert response.json()["run_id"] == submitted_id
    assert response.json()["status"] == RunStatus.FAILED.value
    assert response.json()["error"] == "restart failure"


def test_api_submission_id_remains_valid_after_completion(one_example_dataset):
    with TestClient(app) as client:
        response = client.post(
            "/runs",
            json={
                "dataset_path": one_example_dataset,
                "candidate_provider": "openai",
                "candidate_model": "mock",
                "evaluator_provider": "openai",
                "evaluator_model": "mock",
            },
        )
        assert response.status_code == 200
        submitted_id = response.json()["run_id"]

        status_response = None
        for _ in range(40):
            status_response = client.get(f"/runs/{submitted_id}")
            if status_response.json()["status"] in {"completed", "failed"}:
                break
            time.sleep(0.05)

    assert status_response is not None
    assert status_response.status_code == 200
    assert status_response.json()["run_id"] == submitted_id
    assert status_response.json()["status"] == RunStatus.COMPLETED.value


def test_pairwise_api_submission_id_remains_valid_after_completion(one_example_dataset):
    with TestClient(app) as client:
        response = client.post(
            "/pairwise-runs",
            json={
                "dataset_path": one_example_dataset,
                "model_a_provider": "openai",
                "model_a_model": "mock",
                "model_b_provider": "openai",
                "model_b_model": "mock",
                "judge_provider": "openai",
                "judge_model": "mock",
            },
        )
        assert response.status_code == 200
        submitted_id = response.json()["run_id"]

        status_response = None
        for _ in range(40):
            status_response = client.get(f"/pairwise-runs/{submitted_id}")
            if status_response.json()["status"] in {"completed", "failed"}:
                break
            time.sleep(0.05)

    assert status_response is not None
    assert status_response.status_code == 200
    assert status_response.json()["run_id"] == submitted_id
    assert status_response.json()["status"] == RunStatus.COMPLETED.value
