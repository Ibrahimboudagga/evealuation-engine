import asyncio
import json
from datetime import datetime, timezone

import pytest

from app.database.connection import get_db
from app.database.models import AuditEventDB, EvaluationRunDB, PairwiseRunDB, ProjectDB, WorkspaceDB
from app.evaluators.pairwise_judge import PairwiseJudgeEvaluator
from app.evaluators.registry import EvaluatorRegistry
from app.providers.base import BaseProvider
from app.runners.eval_runner import EvaluationRunner
from app.runners.pairwise_runner import PairwiseEvaluationRunner
from app.schemas.example import EvaluationExample
from app.schemas.outcomes import RunStatus
from app.services.queue_worker import QueueWorker
from tests.fakes import DeterministicFakeProvider


class NeverReturningProvider(BaseProvider):
    model_name = "timeout-model"

    async def generate(self, prompt):
        await asyncio.Event().wait()


def _single_worker_for(runner, dataset_path):
    worker = QueueWorker(retry_base_seconds=0, retry_cap_seconds=0)
    worker._build_single_runner = lambda _claimed: (runner, {
        "dataset_path": dataset_path,
        "dataset_id": None,
        "dataset_version_id": None,
    })
    return worker


def _pairwise_worker_for(runner, dataset_path):
    worker = QueueWorker(retry_base_seconds=0, retry_cap_seconds=0)
    worker._build_pairwise_runner = lambda _claimed: (runner, {
        "dataset_path": dataset_path,
        "dataset_id": None,
        "dataset_version_id": None,
    })
    return worker


@pytest.mark.asyncio
async def test_transient_generation_timeout_is_retried_then_exhausted(tmp_path):
    dataset_path = str(tmp_path / "timeout.jsonl")
    example = EvaluationExample(id="timeout", input="wait", expected_output="answer")
    (tmp_path / "timeout.jsonl").write_text(
        json.dumps(example.model_dump()) + "\n", encoding="utf-8"
    )
    runner = EvaluationRunner(
        NeverReturningProvider(), EvaluatorRegistry(), execution_timeout_seconds=0.001
    )
    run_id = runner.create_run(dataset_path=dataset_path)
    with get_db() as db:
        workspace = WorkspaceDB(id="queue-workspace", name="Queue Agency", slug="queue-agency")
        project = ProjectDB(id="queue-project", name="Queue project", client_name="Queue client", workspace_id=workspace.id)
        run = db.query(EvaluationRunDB).filter_by(id=run_id).one()
        run.project_id = project.id
        db.add_all([workspace, project])
        db.commit()
    worker = _single_worker_for(runner, dataset_path)

    for expected_attempt in (1, 2):
        assert await worker.run_once()
        with get_db() as db:
            run = db.query(EvaluationRunDB).filter_by(id=run_id).one()
            assert run.status == RunStatus.QUEUED.value
            assert run.attempt_count == expected_attempt
            assert run.next_attempt_at is not None
            assert "temporarily unavailable" in run.last_transient_error

    assert await worker.run_once()
    with get_db() as db:
        run = db.query(EvaluationRunDB).filter_by(id=run_id).one()
        assert run.status == RunStatus.FAILED.value
        assert run.attempt_count == run.max_attempts == 3
        assert run.next_attempt_at is None
        actions = [event.action for event in db.query(AuditEventDB).filter_by(workspace_id="queue-workspace").all()]
        assert actions.count("run.claimed") == 3
        assert actions.count("run.retry_scheduled") == 2
        assert actions.count("run.retry_exhausted") == 1


@pytest.mark.asyncio
async def test_pairwise_timeout_uses_the_same_retry_queue(tmp_path):
    dataset_path = str(tmp_path / "pairwise-timeout.jsonl")
    (tmp_path / "pairwise-timeout.jsonl").write_text(
        json.dumps({"id": "timeout", "input": "wait", "expected_output": "answer"}) + "\n",
        encoding="utf-8",
    )
    runner = PairwiseEvaluationRunner(
        NeverReturningProvider(),
        DeterministicFakeProvider.successful(),
        PairwiseJudgeEvaluator(DeterministicFakeProvider.valid_pairwise_judge()),
        execution_timeout_seconds=0.001,
    )
    run_id = runner.create_run(dataset_path=dataset_path)
    worker = _pairwise_worker_for(runner, dataset_path)

    assert await worker.run_once()
    with get_db() as db:
        run = db.query(PairwiseRunDB).filter_by(id=run_id).one()
        assert run.status == RunStatus.QUEUED.value
        assert run.attempt_count == 1
        assert run.last_transient_error is not None


@pytest.mark.asyncio
async def test_cancellation_is_honored_before_execution(tmp_path):
    dataset_path = str(tmp_path / "cancel.jsonl")
    runner = EvaluationRunner(DeterministicFakeProvider.successful(), EvaluatorRegistry())
    run_id = runner.create_run(dataset_path=dataset_path)
    with get_db() as db:
        run = db.query(EvaluationRunDB).filter_by(id=run_id).one()
        run.cancellation_requested_at = datetime.now(timezone.utc)
        db.commit()

    completed_id = await runner.run_evaluation(
        dataset_path=dataset_path,
        examples=[EvaluationExample(id="one", input="one", expected_output="one")],
        run_id=run_id,
    )
    with get_db() as db:
        run = db.query(EvaluationRunDB).filter_by(id=run_id).one()
        assert completed_id == run_id
        assert run.status == RunStatus.INTERRUPTED.value
        assert run.error_message == "Execution cancelled by user request."


def test_manual_retry_resets_an_exhausted_run(tmp_path):
    dataset_path = str(tmp_path / "manual-retry.jsonl")
    runner = EvaluationRunner(DeterministicFakeProvider.successful(), EvaluatorRegistry())
    run_id = runner.create_run(dataset_path=dataset_path)
    with get_db() as db:
        run = db.query(EvaluationRunDB).filter_by(id=run_id).one()
        run.status = RunStatus.FAILED.value
        run.attempt_count = run.max_attempts
        db.commit()

    retry_id = QueueWorker().retry_now("evaluation_run", run_id)
    assert retry_id != run_id
    with get_db() as db:
        assert db.get(EvaluationRunDB, run_id).status == RunStatus.FAILED.value
        run = db.query(EvaluationRunDB).filter_by(id=retry_id).one()
        assert run.status == RunStatus.QUEUED.value
        assert run.attempt_count == 0
        assert run.next_attempt_at is not None
