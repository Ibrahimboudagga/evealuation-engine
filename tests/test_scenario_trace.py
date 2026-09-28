"""Typed evidence must fail honestly when instrumentation or truth is missing."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.scenarios.evaluation import evaluate
from app.scenarios.schemas import Evidence, Scenario


def assess(case, **evidence):
    return evaluate(Scenario(id="agent", task="Check the agent", **case),
                    Evidence(output={"answer": "30 days"}, simulated=True, **evidence))


def statuses(result):
    return {c.name: c.status for c in result.checks}


def test_tool_arguments_results_and_explicit_order():
    case = {"tool_expectations": [{"tool_name": "retrieve", "arguments": [
        {"name": "tenant", "path": "/tenant", "value": "client-a"}], "result": [
        {"name": "found", "path": "/count", "value": 1}]}],
        "tool_order": ["retrieve", "answer"]}
    calls = [{"name": "retrieve", "status": "succeeded", "sequence": 0,
              "arguments": {"tenant": "client-a"}, "result": {"value": {"count": 1}}},
             {"name": "answer", "status": "succeeded", "sequence": 1}]
    assert assess(case, tool_calls=calls, trace_complete=True).score == 1
    calls[0]["arguments"]["tenant"] = "client-b"
    assert assess(case, tool_calls=calls, trace_complete=True).decision == "regressed"
    calls[0].pop("arguments")
    assert assess(case, tool_calls=calls, trace_complete=True).decision == "inconclusive"
    calls[0]["arguments"] = {"tenant": "client-a"}
    calls.reverse()  # Array order cannot overwrite recorded sequence.
    assert assess(case, tool_calls=calls, trace_complete=True).score == 1
    calls[0].pop("sequence")
    assert statuses(assess(case, tool_calls=calls, trace_complete=True))["tool_order"] == "unverified"


def test_null_tool_result_is_observed_and_duplicate_order_rejected():
    case = {"tool_expectations": [{"tool_name": "write", "result": [
        {"name": "null-result", "path": "", "value": None}]}]}
    assert assess(case, trace_complete=True, tool_calls=[
        {"name": "write", "status": "succeeded", "result": {"value": None}}]).score == 1
    with pytest.raises(ValidationError):
        Evidence(output={}, simulated=True, tool_calls=[
            {"name": "read", "status": "succeeded", "sequence": 0},
            {"name": "write", "status": "succeeded", "sequence": 0}])


def test_retrieval_and_reviewed_grounding_require_observed_content():
    case = {"retrieval": {"relevant_source_ids": ["clause-4"], "min_recall": 1},
            "grounding": [{"name": "notice", "claim_path": "/answer", "expected_value": "30 days",
                           "source_id": "clause-4", "quote": "notice is 30 days",
                           "reference_status": "synthetic"}]}
    records = [{"source_id": "clause-4", "query": "notice period", "rank": 1,
                "content": "The required notice is 30 days."}]
    assert assess(case, retrievals=records, retrieval_complete=True).score == 1
    records[0].pop("content")
    assert assess(case, retrievals=records, retrieval_complete=True).score is None
    records[0]["content"] = "The required notice is 90 days."
    assert assess(case, retrievals=records, retrieval_complete=True).decision == "regressed"
    case["grounding"][0]["reference_status"] = "unreviewed"
    assert assess(case, retrievals=records, retrieval_complete=True).decision == "inconclusive"


def test_state_and_authorized_side_effect_checks():
    case = {"state_checks": [{"name": "credit", "phase": "after", "assertions": [
        {"name": "remaining", "path": "/credits", "value": 4}]}],
        "side_effect_policy": {"allowed": ["debit_credit"], "required": ["debit_credit"]}}
    assert assess(case).score is None
    row = {"name": "debit_credit", "status": "succeeded", "authorized": True}
    assert assess(case, state_after={"credits": 4}, side_effects=[row], side_effects_complete=True).score == 1
    row["authorized"] = False
    assert assess(case, state_after={"credits": 4}, side_effects=[row], side_effects_complete=True).decision == "regressed"
    row.pop("authorized")
    assert assess(case, state_after={"credits": 4}, side_effects=[row], side_effects_complete=True).decision == "inconclusive"


def test_bounded_multiturn_state_contract():
    case = {"turns": [{"id": "remember", "input": "Remember project alpha", "state_assertions": [
        {"name": "project", "path": "/project", "value": "alpha"}]},
        {"id": "recall", "input": "Which project?", "assertions": [
            {"name": "answer", "path": "/project", "value": "alpha"}]}]}
    turns = [{"id": "remember", "sequence": 0, "output": {}, "state": {"project": "alpha"}},
             {"id": "recall", "sequence": 1, "output": {"project": "alpha"}}]
    assert assess(case, turns=turns, turns_complete=True).score == 1
    turns[0].pop("state")
    assert assess(case, turns=turns, turns_complete=True).score is None
    turns[0]["state"] = {"project": "alpha"}
    assert assess(case, turns=turns[:1], turns_complete=True).decision == "regressed"
    with pytest.raises(ValidationError):
        Scenario(id="long", task="Too long", turns=[{**case["turns"][0], "id": str(i)} for i in range(21)])


def test_revenue_ops_fixture_and_corruptions():
    directory = Path(__file__).resolve().parents[1] / "datasets" / "scenarios"
    case = Scenario.model_validate_json((directory / "revenue_ops.jsonl").read_text())
    payload = json.loads((directory / "revenue_ops_fixtures.json").read_text())[case.id]
    assert evaluate(case, Evidence.model_validate(payload)).score == 1
    payload["revenue_ops"]["report_calculations"]["total"] = 999
    assert evaluate(case, Evidence.model_validate(payload)).decision == "regressed"
    payload["revenue_ops"]["report_calculations"]["total"] = 180
    payload["revenue_ops"]["anomaly_labels"] = None
    assert evaluate(case, Evidence.model_validate(payload)).score is None
    payload["revenue_ops"]["anomaly_labels"] = []
    assert evaluate(case, Evidence.model_validate(payload)).decision == "regressed"
    case.revenue_ops.reference_status = "unreviewed"
    assert evaluate(case, Evidence.model_validate(payload)).decision == "inconclusive"


def test_oversized_schedule_duration_is_a_failed_check_not_a_scoring_crash():
    case = Scenario(id="overflow", task="Plan", schedule={"task_names": ["Review"], "available_hours": 1})
    observed = Evidence(output={"tasks": [{"name": "Review", "priority": "high"}], "taskItems": [
        {"taskName": "Review", "description": "Step", "time": 10 ** 1000}]}, simulated=True)
    result = evaluate(case, observed)
    assert result.outcome == "evaluated"
    assert result.decision == "regressed"
    assert statuses(result)["schedule_shape"] == "failed"
