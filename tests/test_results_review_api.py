from fastapi.testclient import TestClient

from app.api.main import app
from app.database.connection import get_db
from app.database.models import DatasetDB, EvaluationResultDB, EvaluationRunDB
from app.schemas.outcomes import EvaluationOutcome, RunStatus


def _seed_reviewable_run() -> str:
    run_id = "review-run"
    with get_db() as db:
        db.add(DatasetDB(id="review-dataset", name="Review dataset", latest_version_number=1))
        db.add(
            EvaluationRunDB(
                id=run_id,
                dataset_id="review-dataset",
                model_name="candidate-model",
                status=RunStatus.COMPLETED.value,
            )
        )
        evaluated = EvaluationResultDB(
            run_id=run_id,
            example_id="case-success",
            prompt="What is 2+2?",
            prediction="4",
            expected_output="4",
            score=1.0,
            evaluator_name="llm_judge",
            outcome=EvaluationOutcome.EVALUATED.value,
        )
        evaluated.metadata_dict = {"reason": "The output matches the expected answer."}
        db.add(evaluated)
        db.add(
            EvaluationResultDB(
                run_id=run_id,
                example_id="case-generation-failure",
                prompt="Generate an answer",
                prediction="",
                expected_output="Expected answer",
                score=None,
                evaluator_name="exact_match",
                outcome=EvaluationOutcome.GENERATION_ERROR.value,
                error_message="api_key=sk-thismustberedacted provider unavailable",
            )
        )
        judge_error = EvaluationResultDB(
            run_id=run_id,
            example_id="case-judge-failure",
            prompt="Judge this answer",
            prediction="Candidate answer",
            expected_output="Expected answer",
            score=None,
            evaluator_name="llm_judge",
            outcome=EvaluationOutcome.EVALUATION_ERROR.value,
            error_message="Judge response could not be parsed",
        )
        judge_error.metadata_dict = {"reason": "Failed to parse judge output or generate response"}
        db.add(judge_error)
        db.commit()
    return run_id


def test_results_review_returns_full_evidence_and_sanitized_errors():
    run_id = _seed_reviewable_run()

    with TestClient(app) as client:
        response = client.get(f"/runs/{run_id}/results")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_count"] == 3
    assert payload["filtered_count"] == 3
    assert payload["available_evaluators"] == ["exact_match", "llm_judge"]

    successful = next(item for item in payload["results"] if item["example_id"] == "case-success")
    assert successful["prompt"] == "What is 2+2?"
    assert successful["prediction"] == "4"
    assert successful["expected_output"] == "4"
    assert successful["judge_explanation"] == "The output matches the expected answer."

    failed = next(item for item in payload["results"] if item["example_id"] == "case-generation-failure")
    assert failed["outcome"] == "generation_error"
    assert failed["score"] is None
    assert "sk-thismustberedacted" not in failed["error_message"]
    assert "[REDACTED]" in failed["error_message"]


def test_results_review_filters_by_evaluator_score_and_errors():
    run_id = _seed_reviewable_run()

    with TestClient(app) as client:
        judged = client.get(f"/runs/{run_id}/results", params={"evaluator": "llm_judge"})
        high_scores = client.get(f"/runs/{run_id}/results", params={"score_min": 0.8})
        generation_errors = client.get(
            f"/runs/{run_id}/results", params=[("outcome", "generation_error")]
        )
        evaluation_errors = client.get(
            f"/runs/{run_id}/results", params=[("outcome", "evaluation_error")]
        )

    assert judged.json()["filtered_count"] == 2
    assert all(item["evaluator_name"] == "llm_judge" for item in judged.json()["results"])
    assert high_scores.json()["filtered_count"] == 1
    assert high_scores.json()["results"][0]["score"] == 1.0
    assert generation_errors.json()["results"][0]["example_id"] == "case-generation-failure"
    assert evaluation_errors.json()["results"][0]["example_id"] == "case-judge-failure"
