import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from app.scenarios.adapters import FixtureAdapter, HttpAdapter, LegalRagAdapter
from app.scenarios.evaluation import evaluate
from app.scenarios.runner import load_scenarios, run_scenarios
from app.scenarios.schemas import Evidence, Scenario


def scenario(**kwargs):
    return Scenario(id="case", task="Evaluate task", assertions=[{"name": "answer", "path": "/answer", "value": "yes"}], **kwargs)


def evidence(**kwargs):
    return Evidence(output={"answer": "yes"}, simulated=True, **kwargs)


def test_complete_empty_trace_differs_from_missing_trace():
    case = scenario(forbidden_tools=["send_email"])
    missing = evaluate(case, evidence())
    assert missing.outcome == "evaluation_error" and missing.score is None
    assert evaluate(case, evidence(tool_calls=[], trace_complete=True)).score == 1
    assert evaluate(case, evidence(tool_calls=[], trace_complete=False)).score is None


def test_tool_attempts_and_citation_budgets():
    case = scenario(required_tools=["retrieve"], forbidden_tools=["send_email"], max_tool_calls=1,
                    expected_citation_ids=["document-1"], max_total_tokens=100)
    result = evaluate(case, evidence(tool_calls=[{"name": "retrieve", "status": "succeeded"},
                                               {"name": "send_email", "status": "failed"}],
                                     trace_complete=True, citation_ids=["document-1"], total_tokens=101))
    checks = {c.name: c.status for c in result.checks}
    assert result.outcome == "evaluated" and result.decision == "regressed"
    assert checks["forbidden_tool:send_email"] == "failed"
    assert checks["tool_call_budget"] == "failed"
    assert checks["total_tokens_budget"] == "failed"


@pytest.mark.parametrize("output", [{}, {"answer": False}, {"answer": ["yes"]}])
def test_wrong_answer_is_valid_failure(output):
    result = evaluate(scenario(), Evidence(output=output, simulated=False))
    assert result.outcome == "evaluated" and result.score == 0


def test_json_pointer_escaping():
    case = Scenario(id="escaped", task="test", assertions=[{"name": "nested", "path": "/a~1b/~0/0", "value": 3}])
    assert evaluate(case, Evidence(output={"a/b": {"~": [3]}}, simulated=True)).score == 1


def test_schedule_checks_actual_open_saas_shape():
    case = Scenario(id="schedule", task="Plan", schedule={"task_names": ["Review"], "available_hours": 1})
    output = {"tasks": [{"name": "Review", "priority": "high"}],
              "taskItems": [{"taskName": "Review", "description": "Step", "time": 0.25} for _ in range(3)]}
    assert evaluate(case, Evidence(output=output, simulated=True)).score == 1
    output["taskItems"][0]["time"] = 2
    result = evaluate(case, Evidence(output=output, simulated=True))
    assert next(c for c in result.checks if c.name == "schedule_hours").status == "failed"
    output["tasks"][0]["name"] = "Another tenant's task"
    assert next(c for c in evaluate(case, Evidence(output=output, simulated=True)).checks
                if c.name == "schedule_tasks").status == "failed"


@pytest.mark.asyncio
async def test_fixture_run_persists_configuration_results_and_html(tmp_path):
    output = tmp_path / "run"
    fixtures = {"case": {"output": {"answer": "yes", "unsafe": "<script>alert(1)</script>"}, "simulated": False}}
    manifest = await run_scenarios([scenario()], FixtureAdapter(fixtures), output, dataset_sha256="digest")
    assert manifest["status"] == "completed" and manifest["simulated"] is True
    assert manifest["metrics"]["pass_rate"] == 1
    assert json.loads((output / "results.jsonl").read_text())["simulated"] is True
    report = (output / "report.html").read_text(encoding="utf-8")
    assert "<script>" not in report and "&lt;script&gt;" in report
    assert json.loads((output / "manifest.json").read_text())["dataset_sha256"] == "digest"
    with pytest.raises(FileExistsError):
        await run_scenarios([scenario()], FixtureAdapter(fixtures), output)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected", [("503", "generation_error"), ("json", "generation_error"), ("schema", "evaluation_error"), ("timeout", "generation_error")])
async def test_http_failures_never_score_or_retry_submission(tmp_path, mode, expected):
    calls = []

    def handle(request):
        calls.append(request)
        if mode == "503":
            return httpx.Response(503, text="private credential leaked by target")
        if mode == "json":
            return httpx.Response(200, text="not json")
        if mode == "timeout":
            raise httpx.ReadTimeout("private secret", request=request)
        return httpx.Response(200, json={"output": "private secret"})

    result = await run_scenarios([scenario()], HttpAdapter("https://target.test/evaluate", token="hidden",
                                 transport=httpx.MockTransport(handle)), tmp_path / mode)
    row = json.loads((tmp_path / mode / "results.jsonl").read_text())
    assert len(calls) == 1
    assert row["outcome"] == expected and row["score"] is None
    assert "secret" not in row["error_message"] and "credential" not in row["error_message"]
    assert result["metrics"]["coverage"] == 0 and result["metrics"]["pass_rate"] is None
    assert result["metrics"]["decision"] == "inconclusive"


@pytest.mark.asyncio
async def test_legal_adapter_waits_for_human_review_not_partial_report(tmp_path):
    calls = []
    polls = 0

    def handle(request):
        nonlocal polls
        calls.append(request.method)
        if request.url.path.endswith("/start"):
            assert json.loads(request.content)["query"] == "Evaluate task"
            return httpx.Response(200, json={"workflow_id": "agent-123"})
        if request.url.path.endswith("/status"):
            polls += 1
            return httpx.Response(200, json={"desc_status": "RUNNING", "workflow_status": {
                "status": "running_agent_graph" if polls == 1 else "human_in_loop"}})
        return httpx.Response(200, json={"workflow_report": {"answer": "yes"}})

    adapter = LegalRagAdapter("https://legal.test", poll_seconds=.001, transport=httpx.MockTransport(handle))
    result = await run_scenarios([scenario(required_tools=["retrieve"])], adapter, tmp_path / "legal")
    assert polls == 2 and calls.count("POST") == 1
    assert result["metrics"]["evaluation_errors"] == 1  # native API has no complete tool trace
    assert result["simulated"] is False


@pytest.mark.asyncio
async def test_deadline_and_cancellation_preserve_finished_examples(tmp_path):
    class SlowAdapter:
        simulated = True

        async def execute(self, case, request_id):
            if case.id == "slow":
                await asyncio.sleep(10)
            return evidence()

    slow = scenario().model_copy(update={"id": "slow"})
    manifest = await run_scenarios([scenario(), slow], SlowAdapter(), tmp_path / "deadline", timeout_seconds=.01)
    assert manifest["metrics"]["valid_evaluations"] == 1
    assert manifest["metrics"]["generation_errors"] == 1
    task = asyncio.create_task(run_scenarios([scenario(), slow], SlowAdapter(), tmp_path / "cancel"))
    for _ in range(100):
        path = tmp_path / "cancel" / "results.jsonl"
        if path.exists() and path.stat().st_size:
            break
        await asyncio.sleep(.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stored = json.loads((tmp_path / "cancel" / "manifest.json").read_text())
    assert stored["status"] == "interrupted" and stored["metrics"]["valid_evaluations"] == 1


def test_dataset_validation_and_url_safety(tmp_path):
    with pytest.raises(ValidationError):
        Scenario(id="empty", task="test")
    path = tmp_path / "dataset.jsonl"
    path.write_text(scenario().model_dump_json() + "\n" + scenario().model_dump_json())
    with pytest.raises(ValueError, match="unique"):
        load_scenarios(path)
    for url in ("file:///tmp/file", "https://key@target.test", "https://target.test?token=secret"):
        with pytest.raises(ValueError):
            HttpAdapter(url)
    with pytest.raises(ValueError):
        HttpAdapter("http://remote.test", token="secret")


@pytest.mark.asyncio
async def test_http_success_preserves_target_simulation_label(tmp_path):
    def handle(request):
        assert request.headers["Authorization"] == "Bearer hidden"
        assert request.headers["Idempotency-Key"] == json.loads(request.content)["request_id"]
        return httpx.Response(200, json={"output": {"answer": "yes"}, "simulated": True})

    manifest = await run_scenarios([scenario()], HttpAdapter("https://target.test/evaluate", "hidden",
                                  transport=httpx.MockTransport(handle)), tmp_path / "success")
    assert manifest["simulated"] and manifest["metrics"]["pass_rate"] == 1
    assert "hidden" not in (tmp_path / "success" / "manifest.json").read_text()


@pytest.mark.asyncio
async def test_redirect_is_not_followed(tmp_path):
    calls = []

    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://other.test/steal"})

    manifest = await run_scenarios([scenario()], HttpAdapter("https://target.test/evaluate", "hidden",
                                  transport=httpx.MockTransport(handle)), tmp_path / "redirect")
    assert len(calls) == 1 and manifest["metrics"]["generation_errors"] == 1


@pytest.mark.asyncio
async def test_completed_legal_workflow_without_report_fails(tmp_path):
    def handle(request):
        if request.method == "POST":
            return httpx.Response(200, json={"workflow_id": "finished"})
        return httpx.Response(200, json={"desc_status": "COMPLETED", "workflow_report": None})

    adapter = LegalRagAdapter("https://legal.test", transport=httpx.MockTransport(handle))
    manifest = await run_scenarios([scenario()], adapter, tmp_path / "finished")
    assert manifest["metrics"]["generation_errors"] == 1
    assert manifest["metrics"]["average_score"] is None
