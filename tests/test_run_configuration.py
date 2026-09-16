import json
import time

from fastapi.testclient import TestClient
import pytest

from app.api.main import app
from app.database.connection import get_db
from app.database.models import EvaluationRunDB, PairwiseRunDB
from app.evaluators.pairwise_judge import PairwiseJudgeEvaluator
from app.evaluators.registry import EvaluatorRegistry
from app.runners.eval_runner import EvaluationRunner
from app.runners.pairwise_runner import PairwiseEvaluationRunner
from tests.fakes import DeterministicFakeProvider


@pytest.fixture
def configuration_dataset(tmp_path):
    path = tmp_path / "configuration.jsonl"
    path.write_text(
        json.dumps({"id": "case-1", "input": "What is 2+2?", "expected_output": "4"}) + "\n",
        encoding="utf-8",
    )
    return str(path)


def test_single_run_stores_a_safe_reproducible_configuration(configuration_dataset):
    runner = EvaluationRunner(
        DeterministicFakeProvider.successful(),
        EvaluatorRegistry(
            judge_provider=DeterministicFakeProvider.valid_judge(),
            judge_prompt_template="Judge {input} against {expected_output}: {prediction}",
        ),
        concurrency_limit=3,
        execution_timeout_seconds=17.5,
        result_batch_size=2,
        requested_configuration={"candidate": {"api_key": "do-not-store-me"}},
    )

    run_id = runner.create_run(dataset_path=configuration_dataset)
    with get_db() as db:
        run = db.query(EvaluationRunDB).filter_by(id=run_id).one()
        configuration = run.run_configuration

    assert run.configuration_verified is True
    assert configuration["run_type"] == "single_model"
    assert configuration["dataset"]["content_sha256"]
    assert configuration["candidate"]["model"] == "deterministic-fake-success"
    assert configuration["execution"] == {
        "concurrency_limit": 3,
        "timeout_seconds": 17.5,
        "result_batch_size": 2,
    }
    judge = next(item for item in configuration["evaluators"] if item["name"] == "llm_judge")
    assert judge["prompt_template"] == "Judge {input} against {expected_output}: {prediction}"
    assert configuration["request"]["candidate"]["api_key"] == "[REDACTED]"
    assert "do-not-store-me" not in run.run_configuration_json


def test_pairwise_run_stores_models_judge_and_execution_configuration(configuration_dataset):
    runner = PairwiseEvaluationRunner(
        DeterministicFakeProvider.successful(),
        DeterministicFakeProvider.successful(),
        PairwiseJudgeEvaluator(
            DeterministicFakeProvider.valid_pairwise_judge(),
            prompt_template="Compare {response_a} with {response_b}",
        ),
        concurrency_limit=4,
        execution_timeout_seconds=22.0,
        result_batch_size=3,
    )

    run_id = runner.create_run(dataset_path=configuration_dataset)
    with get_db() as db:
        run = db.query(PairwiseRunDB).filter_by(id=run_id).one()
        configuration = run.run_configuration

    assert run.configuration_verified is True
    assert configuration["run_type"] == "pairwise"
    assert configuration["model_a"]["model"] == "deterministic-fake-success"
    assert configuration["model_b"]["model"] == "deterministic-fake-success"
    assert configuration["judge"]["prompt_template"] == "Compare {response_a} with {response_b}"
    assert configuration["execution"]["timeout_seconds"] == 22.0


def test_run_status_api_exposes_configuration_without_credentials(configuration_dataset):
    with TestClient(app) as client:
        response = client.post(
            "/runs",
            json={
                "dataset_path": configuration_dataset,
                "candidate_provider": "openai",
                "candidate_model": "mock",
                "candidate_api_key": "sk-should-not-appear",
                "evaluator_provider": "openai",
                "evaluator_model": "mock",
                "evaluator_api_key": "sk-judge-should-not-appear",
            },
        )
        assert response.status_code == 200
        run_id = response.json()["run_id"]

        status_response = None
        for _ in range(40):
            status_response = client.get(f"/runs/{run_id}")
            if status_response.json()["status"] == "completed":
                break
            time.sleep(0.05)

    payload = status_response.json()
    assert payload["configuration_verified"] is True
    assert payload["run_configuration"]["request"]["candidate"]["provider"] == "openai"
    assert payload["run_configuration"]["request"]["judge"]["model"] == "mock"
    assert "sk-should-not-appear" not in json.dumps(payload["run_configuration"])
    assert "sk-judge-should-not-appear" not in json.dumps(payload["run_configuration"])
