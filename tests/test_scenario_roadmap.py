import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.main import app
from app.scenarios.calibration import CalibrationCase, calibrate
from app.scenarios.evaluation import evaluate
from app.scenarios.schemas import Evidence, Scenario


def test_canonical_agent_record_preserves_hierarchy_provenance_and_usage():
    evidence = Evidence.model_validate({
        "schema_version": 2, "source": "live_capture", "case_id": "case", "run_id": "run",
        "session_id": "session", "captured_at": "2026-10-03T10:00:00Z", "simulated": False,
        "output": {"created": True},
        "target": {"provider": "customer-app", "application_version": "git:abc",
                   "prompt_template_version": "support-v3", "parameters": {"temperature": 0}},
        "evaluator_versions": {"scenario-engine": "sha256:abc"},
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                  "cost": 0.02, "currency": "USD", "cost_basis": "measured"},
        "tool_calls": [
            {"name": "search", "status": "succeeded", "step_id": "one", "sequence": 0},
            {"name": "create", "status": "succeeded", "step_id": "two",
             "parent_step_id": "one", "sequence": 1}], "trace_complete": True,
    })
    assert Evidence.model_validate_json(evidence.model_dump_json()) == evidence
    with pytest.raises(ValidationError, match="parent_step_id"):
        Evidence(output={}, simulated=False, tool_calls=[
            {"name": "x", "status": "succeeded", "step_id": "two", "parent_step_id": "missing"}])


def test_agent_dimensions_are_separate_and_evidence_linked():
    scenario = Scenario(id="case", task="Create safely",
        assertions=[{"name": "created", "path": "/created", "value": True}],
        required_tools=["create"], forbidden_tools=["delete"], max_tool_calls=1)
    result = evaluate(scenario, Evidence(output={"created": True}, simulated=False,
        tool_calls=[{"name": "create", "status": "succeeded"}], trace_complete=True))
    assert result.dimensions["task_outcome"].status == "passed"
    assert result.dimensions["trajectory"].status == "passed"
    assert result.dimensions["safety"].status == "passed"
    assert result.dimensions["operational"].status == "passed"
    assert "created" in result.dimensions["task_outcome"].evidence_checks


def test_calibration_reports_agreement_repeatability_groups_and_disagreements():
    cases = [
        CalibrationCase(id="1", human_label="pass", judge_labels=["pass", "pass"], group="a"),
        CalibrationCase(id="2", human_label="fail", judge_labels=["pass", "fail"], group="b"),
    ]
    report = calibrate(cases)
    assert report["exact_agreement"] == .5 and report["repeatability"] == .5
    assert report["disagreement_count"] == 1 and report["groups"]["a"]["exact_agreement"] == 1
    assert report["uncertainty"]["agreement_denominator"] == 2
    assert report["release_eligible"] is False and report["exact_agreement_interval"]["lower"] < .5


def test_authoritative_v2_rejects_missing_identity_cycles_and_usage_mismatch():
    base = {"schema_version": 2, "source": "live_capture", "output": {}, "simulated": False}
    with pytest.raises(ValidationError, match="Authoritative schema-v2"):
        Evidence.model_validate(base)
    with pytest.raises(ValidationError, match="precede|cycles"):
        Evidence(output={}, simulated=False, tool_calls=[
            {"name": "parent", "status": "succeeded", "step_id": "p", "parent_step_id": "c", "sequence": 1},
            {"name": "child", "status": "succeeded", "step_id": "c", "parent_step_id": "p", "sequence": 0}])
    with pytest.raises(ValidationError, match="usage.total_tokens"):
        Evidence(output={}, simulated=False, total_tokens=4, usage={"total_tokens": 5})


def test_cost_budget_is_a_first_class_operational_check():
    scenario = Scenario(id="cost", task="Stay affordable", max_cost=.01, cost_currency="USD")
    result = evaluate(scenario, Evidence(output={}, simulated=False,
        usage={"cost": .02, "currency": "USD", "cost_basis": "measured"}))
    assert result.decision == "regressed"
    assert result.dimensions["operational"].status == "failed"


def test_suite_preview_returns_actionable_row_and_field_errors():
    with TestClient(app) as client:
        auth = client.post("/auth/bootstrap", json={"email": "roadmap@example.com",
            "display_name": "Owner", "workspace_name": "Roadmap"}).json()
        headers = {"Authorization": "Bearer " + auth["api_token"]}
        content = json.dumps({"schema_version": 1, "id": "bad", "task": "x",
                              "assertions": [{"name": "a", "path": "not-a-pointer"}]})
        response = client.post("/scenario-suites/preview", headers=headers, json={"content": content})
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is False and data["errors"][0]["row"] == 1
        assert "assertions.0.path" in data["errors"][0]["field"]
