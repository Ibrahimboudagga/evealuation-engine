from fastapi.testclient import TestClient

from app.api.main import app
from app.database.connection import get_db
from app.database.models import DatasetDB, EvaluationResultDB, EvaluationRunDB
from app.schemas.outcomes import EvaluationOutcome, RunStatus


def _add_run(run_id: str, *, status: str = RunStatus.COMPLETED.value, exact_score: float = 1.0) -> None:
    with get_db() as db:
        if not db.query(DatasetDB).filter_by(id="baseline-dataset").first():
            db.add(DatasetDB(id="baseline-dataset", name="Baseline dataset", latest_version_number=1))
        db.add(
            EvaluationRunDB(
                id=run_id,
                dataset_id="baseline-dataset",
                model_name="candidate",
                status=status,
                project_id=None,
                configuration_verified=True,
                run_configuration_json='{"run_type":"single_model","dataset":{"content_sha256":"fixture-content","expected_case_ids":["case-1"]},"evaluators":[{"name":"exact_match","version":"1"},{"name":"llm_judge","version":"1"}],"is_simulated":false}',
            )
        )
        if status == RunStatus.COMPLETED.value:
            db.add_all(
                [
                    EvaluationResultDB(
                        run_id=run_id,
                        example_id="case-1",
                        prompt="Prompt",
                        prediction="Output",
                        expected_output="Expected",
                        score=exact_score,
                        evaluator_name="exact_match",
                        outcome=EvaluationOutcome.EVALUATED.value,
                    ),
                    EvaluationResultDB(
                        run_id=run_id,
                        example_id="case-1",
                        prompt="Prompt",
                        prediction="Output",
                        expected_output="Expected",
                        score=0.8,
                        evaluator_name="llm_judge",
                        outcome=EvaluationOutcome.EVALUATED.value,
                    ),
                ]
            )
        db.commit()


def test_completed_baseline_can_pass_or_regress_a_release_check():
    _add_run("baseline-run")
    _add_run("passing-run")
    _add_run("regressed-run", exact_score=0.0)

    with TestClient(app) as client:
        marked = client.put("/runs/baseline-run/baseline")
        baselines = client.get("/runs", params={"baseline_only": True})
        passed = client.get("/runs/passing-run/comparison", params={"baseline_run_id": "baseline-run"})
        regressed = client.get("/runs/regressed-run/comparison", params={"baseline_run_id": "baseline-run"})

    assert marked.status_code == 200
    assert marked.json()["is_baseline"] is True
    assert [item["run_id"] for item in baselines.json()["runs"]] == ["baseline-run"]
    assert passed.json()["status"] == "passed"
    assert passed.json()["comparisons"][0]["coverage_delta"] == 0.0
    assert regressed.json()["status"] == "regressed"
    assert "exact_match pass rate fell" in " ".join(regressed.json()["reasons"])


def test_release_check_is_inconclusive_when_current_run_is_unfinished():
    _add_run("baseline-run")
    _add_run("queued-run", status=RunStatus.QUEUED.value)

    with TestClient(app) as client:
        assert client.put("/runs/baseline-run/baseline").status_code == 200
        response = client.get("/runs/queued-run/comparison", params={"baseline_run_id": "baseline-run"})

    assert response.status_code == 200
    assert response.json()["status"] == "inconclusive"
    assert "must be completed" in response.json()["reasons"][0]
