import csv
import io

from fastapi.testclient import TestClient

from app.api.main import app
from app.database.connection import get_db
from app.database.models import DatasetDB, EvaluationResultDB, EvaluationRunDB
from app.schemas.outcomes import EvaluationOutcome, RunStatus


def _seed_report_run() -> str:
    run_id = "client-report-run"
    with get_db() as db:
        db.add(DatasetDB(id="report-dataset", name="Client regression set", latest_version_number=1))
        run = EvaluationRunDB(
            id=run_id,
            dataset_id="report-dataset",
            model_name="client-candidate-v1",
            status=RunStatus.COMPLETED.value,
            is_simulated=False,
            configuration_verified=True,
        )
        run.run_configuration = {
            "candidate": {"provider": "openai", "model": "gpt-4o", "api_key": "sk-do-not-export"},
            "dataset": {"name": "Client regression set", "version_number": 1},
        }
        db.add(run)

        success = EvaluationResultDB(
            run_id=run_id,
            example_id="success-case",
            prompt="What is 2+2?",
            prediction="4",
            expected_output="4",
            score=1.0,
            evaluator_name="llm_judge",
            outcome=EvaluationOutcome.EVALUATED.value,
        )
        success.metadata_dict = {"reason": "The answer is correct and complete."}
        db.add(success)
        db.add(
            EvaluationResultDB(
                run_id=run_id,
                example_id="generation-failure",
                prompt="<script>do not render</script>",
                prediction="",
                expected_output="Expected output",
                score=None,
                evaluator_name="exact_match",
                outcome=EvaluationOutcome.GENERATION_ERROR.value,
                error_message="authorization=Bearer very-private-token provider unavailable",
            )
        )
        judge_failure = EvaluationResultDB(
            run_id=run_id,
            example_id="judge-failure",
            prompt="Review this answer",
            prediction="Candidate output",
            expected_output="Expected output",
            score=None,
            evaluator_name="llm_judge",
            outcome=EvaluationOutcome.EVALUATION_ERROR.value,
            error_message="Judge returned malformed JSON",
        )
        judge_failure.metadata_dict = {"reason": "The judge response was not valid JSON."}
        db.add(judge_failure)
        db.commit()
    return run_id


def test_json_export_contains_configuration_metrics_and_failures():
    run_id = _seed_report_run()

    with TestClient(app) as client:
        response = client.get(f"/runs/{run_id}/export", params={"format": "json"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["content-disposition"].endswith('evaluation-report-client-report-run.json"')
    report = response.json()
    assert report["run"]["configuration"]["candidate"]["api_key"] == "[REDACTED]"
    assert report["metrics_by_evaluator"]["llm_judge"]["evaluation_coverage"] == 1 / 3
    assert report["failure_counts"] == {
        "total_result_records": 3,
        "total_examples": 3,
        "generation_errors": 1,
        "evaluation_errors": 1,
        "failed_result_records": 2,
        "failed_examples": 2,
    }
    assert len(report["example_level_failures"]) == 2
    assert "very-private-token" not in report["example_level_failures"][0]["sanitized_error"]


def test_csv_and_html_exports_are_client_ready():
    run_id = _seed_report_run()

    with TestClient(app) as client:
        csv_response = client.get(f"/runs/{run_id}/export", params={"format": "csv"})
        html_response = client.get(f"/runs/{run_id}/export", params={"format": "html"})

    assert csv_response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(csv_response.text)))
    assert len(rows) == 3
    assert rows[0]["judge_explanation"] == "The answer is correct and complete."
    assert "very-private-token" not in csv_response.text

    assert html_response.status_code == 200
    assert html_response.headers["content-type"].startswith("text/html")
    assert "Coverage and Quality" in html_response.text
    assert "Failure Counts" in html_response.text
    assert "Example-level Failures" in html_response.text
    assert "Run Configuration" in html_response.text
    assert "very-private-token" not in html_response.text
    assert "&lt;script&gt;do not render&lt;/script&gt;" in html_response.text
