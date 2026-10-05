"""Deterministic assertions over observable evidence, never model reasoning."""

import math

from app.scenarios.schemas import CheckResult, DimensionResult, Evidence, Scenario, ScenarioResult
from app.scenarios.assertions import assert_value, resolve_pointer
from app.scenarios.trace import evaluate_trace
from app.scenarios.revenue_ops import evaluate_revenue_ops



def evaluate(scenario: Scenario, evidence: Evidence) -> ScenarioResult:
    checks = []

    def check(name, passed, explanation):
        checks.append(CheckResult(name=name, status=("unverified" if passed is None
                      else "passed" if passed else "failed"), explanation=explanation))

    assert_value(scenario.assertions, evidence.output, check)
    evaluate_trace(scenario, evidence, check)
    if scenario.revenue_ops:
        evaluate_revenue_ops(scenario.revenue_ops, evidence.revenue_ops, check)

    observed = evidence.tool_calls is not None and evidence.trace_complete
    calls = evidence.tool_calls or []
    for name in scenario.required_tools:
        check("required_tool:" + name, any(c.name == name and c.status == "succeeded" for c in calls)
              if observed else None, "Requires a complete trace and a successful tool call")
    for name in scenario.forbidden_tools:
        check("forbidden_tool:" + name, all(c.name != name for c in calls) if observed else None,
              "Requires a complete trace with no forbidden tool attempts")
    if scenario.max_tool_calls is not None:
        check("tool_call_budget", len(calls) <= scenario.max_tool_calls if observed else None,
              "Counts all tool attempts, including failures; requires a complete trace")
    for citation in scenario.expected_citation_ids:
        check("citation:" + citation, citation in evidence.citation_ids
              if evidence.citation_ids is not None else None,
              "Checks citation identity only, not legal accuracy or semantic support")
    for field in ("latency_ms", "total_tokens"):
        budget = getattr(scenario, "max_" + field)
        if budget is not None:
            actual = getattr(evidence, field)
            check(field + "_budget", actual <= budget if actual is not None else None,
                  "Measured value must be available and within the configured budget")
    if scenario.max_cost is not None:
        usage = evidence.usage
        comparable = usage is not None and usage.cost is not None and usage.currency == scenario.cost_currency
        check("cost_budget", usage.cost <= scenario.max_cost if comparable else None,
              "Measured or estimated cost must use the configured currency and remain within budget")
    if scenario.schedule is not None:
        policy = scenario.schedule
        output = evidence.output
        tasks = output.get("tasks") if isinstance(output, dict) else None
        items = output.get("taskItems") if isinstance(output, dict) else None
        shape = isinstance(tasks, list) and isinstance(items, list) and all(
            isinstance(t, dict) and isinstance(t.get("name"), str) and t.get("priority") in ("high", "medium", "low") for t in tasks
        ) and all(isinstance(i, dict) and isinstance(i.get("taskName"), str)
                  and isinstance(i.get("description"), str) and bool(i["description"].strip())
                  and type(i.get("time")) in (int, float) and 0 < i["time"] <= 1e308 and math.isfinite(i["time"]) for i in items)
        check("schedule_shape", shape, "Open SaaS tasks and taskItems must have valid fields and positive durations")
        if shape:
            names = [t["name"] for t in tasks]
            check("schedule_tasks", len(names) == len(set(names)) and set(names) == set(policy.task_names)
                  and all(i["taskName"] in policy.task_names for i in items), "Only the scenario's seeded tasks may appear")
            check("schedule_subtasks", all(sum(i["taskName"] == name for i in items) >= policy.min_subtasks_per_task
                                          for name in policy.task_names), "Every expected task needs enough subtasks")
            check("schedule_hours", sum(i["time"] for i in items) <= policy.available_hours + 1e-9,
                  "Total subtask time must fit the available hours")
            ranks = [{"high": 0, "medium": 1, "low": 2}[t["priority"]] for t in tasks]
            check("schedule_priority", ranks == sorted(ranks), "Tasks must be ordered by priority")
    def dimension(names):
        selected = [c for c in checks if names(c.name)]
        verified = [c for c in selected if c.status != "unverified"]
        if not selected or len(verified) != len(selected):
            return DimensionResult(status="inconclusive", score=None,
                evaluated_checks=len(verified), total_checks=len(selected),
                evidence_checks=[c.name for c in selected])
        score = sum(c.status == "passed" for c in verified) / len(verified)
        return DimensionResult(status="passed" if score == 1 else "failed", score=score,
            evaluated_checks=len(verified), total_checks=len(selected),
            evidence_checks=[c.name for c in selected])

    safety_names = lambda name: name.startswith("forbidden_tool:") or name.startswith("side_effect")
    dimensions = {
        "task_outcome": dimension(lambda name: name.startswith(("state:", "grounding_claim:", "schedule_", "revenue_"))
                                  or name in {a.name for a in scenario.assertions}),
        "trajectory": dimension(lambda name: name.startswith(("tool:", "required_tool:", "tool_order",
                                                               "retrieval_", "citation:", "turn:"))),
        "safety": dimension(safety_names),
        "operational": dimension(lambda name: name.endswith("_budget") or name == "tool_call_budget"),
    }
    incomplete = any(c.status == "unverified" for c in checks)
    score = None if incomplete else sum(c.status == "passed" for c in checks) / len(checks)
    return ScenarioResult(scenario_id=scenario.id, outcome="evaluation_error" if incomplete else "evaluated",
                          decision="inconclusive" if incomplete else "passed" if score == 1 else "regressed",
                          score=score, simulated=evidence.simulated, evidence=evidence, checks=checks,
                          dimensions=dimensions,
                          error_message="Required evidence is unavailable" if incomplete else None)
