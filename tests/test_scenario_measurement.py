import hashlib

from app.scenarios.calibration import CalibrationCase, CalibrationIdentity, calibrate
from app.scenarios.runner import summarize
from app.scenarios.schemas import Evidence, ScenarioResult


def result(case, latency, cost, basis="measured"):
    return ScenarioResult(scenario_id=case, outcome="evaluated", decision="passed", score=1,
        simulated=False, evidence=Evidence(output={}, simulated=False, latency_ms=latency,
            usage={"total_tokens": 10, "cost": cost, "currency": "USD", "cost_basis": basis}))


def test_summary_exposes_latency_tokens_cost_basis_and_dimensions():
    metrics = summarize([result("a", 100, .1), result("b", 200, .2, "estimated")], 2)
    assert metrics["usage"]["latency_ms"]["p50"] == 150
    assert metrics["usage"]["total_tokens"] == 20
    assert metrics["usage"]["cost_totals"] == [
        {"currency": "USD", "basis": "estimated", "amount": .2},
        {"currency": "USD", "basis": "measured", "amount": .1}]


def test_calibration_is_bound_to_exact_identity_and_reports_uncertainty():
    digest = hashlib.sha256(b"content").hexdigest()
    identity = CalibrationIdentity(provider="provider", model="judge", model_version="2026-10",
        prompt_version="v3", prompt_sha256=digest, rubric_version="v2", rubric_sha256=digest,
        label_set_version="v1", adjudication_version="v1", parameters={"temperature": 0})
    cases = [CalibrationCase(id=str(index), human_label="pass", human_labels=["pass", "pass"],
             judge_labels=["pass", "pass"], group="core") for index in range(3)]
    report = calibrate(cases, identity, minimum_cases=3, minimum_group_cases=3)
    assert report["release_eligible"] is True
    assert report["judge_identity_sha256"] == identity.artifact_sha256()
    assert report["human_inter_rater"]["agreement"] == 1
    assert report["exact_agreement_interval"]["lower"] < 1
