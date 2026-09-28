"""Saved policies and evaluator thresholds survive the public launch workflow."""
import time
from fastapi.testclient import TestClient
from app.api.main import app
from app.services.report_service import ReportService
from tests.test_workspace_scenarios import setup


def test_template_policy_is_persisted_and_threshold_controls_report_failures():
    with TestClient(app) as client:
        headers, project = setup(client)
        connection = client.post("/provider-connections", headers=headers, json={
            "name": "Demo judge", "provider": "mock", "default_model": "mock", "allow_unauthenticated": True})
        assert connection.status_code == 201, connection.text
        connection_id = connection.json()["id"]
        dataset = client.post("/datasets", headers=headers, json={"name": "Cases", "project_id": project,
            "content": '{"id":"one","input":"Say hello","expected_output":"hello"}'}).json()
        rules = {"minimum_valid_cases": 3,
                 "evaluators": {"llm_judge": {"average_score_minimum": 0.8}}}
        template = client.post("/evaluation-templates", headers=headers, json={
            "name": "Client release", "candidate_connection_id": connection_id,
            "evaluator_connection_id": connection_id, "release_rules": rules,
            "evaluator_settings": {"llm_judge": {"pass_threshold": 1}}})
        assert template.status_code == 201, template.text
        launched = client.post(f"/evaluation-templates/{template.json()['id']}/launch", headers=headers,
            json={"dataset_id": dataset["id"], "dataset_version_id": dataset["active_version"]["id"]})
        assert launched.status_code == 200, launched.text
        run_id = launched.json()["run_id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = client.get(f"/runs/{run_id}", headers=headers).json()
            if result["status"] in {"completed", "failed"}:
                break
            time.sleep(.05)
        assert result["status"] == "completed", result
        configuration = result["run_configuration"]
        assert configuration["request"]["release_rules"]["minimum_valid_cases"] == 3
        assert configuration["request"]["release_rules"]["evaluators"]["llm_judge"]["average_score_minimum"] == .8
        judge = next(m for m in result["metrics"] if m["evaluator"] == "llm_judge")
        assert judge["pass_threshold"] == 1 and judge["pass_rate"] == 0
        report = ReportService().build_run_report(run_id)
        assert any(r["evaluator"] == "llm_judge" for r in report["quality_failures"])


def test_invalid_legacy_score_is_an_error_in_review_and_exports():
    from app.database.connection import get_db
    from app.database.models import EvaluationResultDB
    from tests.test_baseline_comparisons import _add_run
    _add_run("invalid-legacy")
    with get_db() as db:
        result = db.query(EvaluationResultDB).filter_by(run_id="invalid-legacy", evaluator_name="llm_judge").one()
        result.score = float("inf")
        db.commit()
    with TestClient(app) as client:
        response = client.get("/runs/invalid-legacy/results", params={"outcome": "evaluation_error"})
        assert response.status_code == 200, response.text
        assert len(response.json()["results"]) == 1
        assert response.json()["results"][0]["score"] is None
        report = ReportService().build_run_report("invalid-legacy")
        assert report["failure_counts"]["evaluation_errors"] == 1
        assert "Infinity" not in ReportService.to_json(report)
