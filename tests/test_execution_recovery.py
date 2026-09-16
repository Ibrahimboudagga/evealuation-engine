import asyncio

import pytest

from app.database.connection import get_db
from app.database.models import (
    EvaluationResultDB,
    EvaluationRunDB,
    PairwiseComparisonDB,
    PairwiseRunDB,
)
from app.evaluators.pairwise_judge import PairwiseJudgeEvaluator
from app.evaluators.registry import EvaluatorRegistry
from app.providers.base import BaseProvider
from app.runners.eval_runner import EvaluationRunner
from app.runners.pairwise_runner import PairwiseEvaluationRunner
from app.schemas.example import EvaluationExample
from app.schemas.outcomes import EvaluationOutcome, RunStatus
from app.services.run_recovery import RESTART_INTERRUPTION_MESSAGE, reconcile_abandoned_runs
from tests.fakes import DeterministicFakeProvider


class NeverReturningProvider(BaseProvider):
    model_name = "never-returning"

    async def generate(self, prompt):
        await asyncio.Event().wait()


def _result(run_id: str, example: EvaluationExample) -> EvaluationResultDB:
    return EvaluationResultDB(
        run_id=run_id,
        example_id=example.id,
        prompt=example.input,
        prediction="answer",
        expected_output=example.expected_output,
        score=1.0,
        evaluator_name="test",
        outcome=EvaluationOutcome.EVALUATED.value,
    )


@pytest.mark.asyncio
async def test_single_runner_commits_completed_batches_before_interruption(tmp_path):
    dataset_path = str(tmp_path / "batch.jsonl")
    examples = [
        EvaluationExample(id="first", input="first", expected_output="first"),
        EvaluationExample(id="second", input="second", expected_output="second"),
    ]
    runner = EvaluationRunner(
        DeterministicFakeProvider.successful(),
        EvaluatorRegistry(),
        result_batch_size=1,
    )
    run_id = runner.create_run(dataset_path=dataset_path)

    async def staged_example(example, active_run_id):
        if example.id == "second":
            await asyncio.Event().wait()
        return [_result(active_run_id, example)]

    runner._run_example = staged_example
    task = asyncio.create_task(
        runner.run_evaluation(dataset_path=dataset_path, examples=examples, run_id=run_id)
    )

    for _ in range(50):
        with get_db() as db:
            completed_results = db.query(EvaluationResultDB).filter_by(run_id=run_id).count()
        if completed_results == 1:
            break
        await asyncio.sleep(0.01)
    assert completed_results == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with get_db() as db:
        assert db.query(EvaluationResultDB).filter_by(run_id=run_id).count() == 1
        run = db.query(EvaluationRunDB).filter_by(id=run_id).one()
        assert run.status == RunStatus.INTERRUPTED.value


@pytest.mark.asyncio
async def test_generation_timeout_is_saved_as_a_generation_error(tmp_path):
    dataset_path = str(tmp_path / "timeout.jsonl")
    example = EvaluationExample(id="timeout", input="wait", expected_output="answer")
    runner = EvaluationRunner(
        NeverReturningProvider(),
        EvaluatorRegistry(),
        execution_timeout_seconds=0.01,
    )

    run_id = await runner.run_evaluation(dataset_path=dataset_path, examples=[example])

    with get_db() as db:
        results = db.query(EvaluationResultDB).filter_by(run_id=run_id).all()
    assert results
    assert {result.outcome for result in results} == {EvaluationOutcome.GENERATION_ERROR.value}
    assert all("timed out after 0.01 seconds" in result.error_message for result in results)


@pytest.mark.asyncio
async def test_pairwise_runner_commits_completed_batches_before_interruption(tmp_path):
    dataset_path = str(tmp_path / "pairwise-batch.jsonl")
    examples = [
        EvaluationExample(id="first", input="first", expected_output="first"),
        EvaluationExample(id="second", input="second", expected_output="second"),
    ]
    runner = PairwiseEvaluationRunner(
        DeterministicFakeProvider.successful(),
        DeterministicFakeProvider.successful(),
        PairwiseJudgeEvaluator(DeterministicFakeProvider.valid_pairwise_judge()),
        result_batch_size=1,
    )
    run_id = runner.create_run(dataset_path=dataset_path)

    async def staged_comparison(example, active_run_id):
        if example.id == "second":
            await asyncio.Event().wait()
        return PairwiseComparisonDB(
            run_id=active_run_id,
            example_id=example.id,
            prompt=example.input,
            response_a="A",
            response_b="B",
            expected_output=example.expected_output,
            winner="A",
            score_a=1.0,
            score_b=0.0,
            judge_reason="test",
            original_order="AB",
            outcome=EvaluationOutcome.EVALUATED.value,
        )

    runner._run_example = staged_comparison
    task = asyncio.create_task(
        runner.run_pairwise_evaluation(dataset_path=dataset_path, examples=examples, run_id=run_id)
    )

    for _ in range(50):
        with get_db() as db:
            completed_comparisons = db.query(PairwiseComparisonDB).filter_by(run_id=run_id).count()
        if completed_comparisons == 1:
            break
        await asyncio.sleep(0.01)
    assert completed_comparisons == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with get_db() as db:
        assert db.query(PairwiseComparisonDB).filter_by(run_id=run_id).count() == 1
        run = db.query(PairwiseRunDB).filter_by(id=run_id).one()
        assert run.status == RunStatus.INTERRUPTED.value


@pytest.mark.asyncio
async def test_pairwise_generation_timeout_is_saved_as_an_invalid_comparison(tmp_path):
    dataset_path = str(tmp_path / "pairwise-timeout.jsonl")
    example = EvaluationExample(id="timeout", input="wait", expected_output="answer")
    runner = PairwiseEvaluationRunner(
        NeverReturningProvider(),
        DeterministicFakeProvider.successful(),
        PairwiseJudgeEvaluator(DeterministicFakeProvider.valid_pairwise_judge()),
        execution_timeout_seconds=0.01,
    )

    run_id = await runner.run_pairwise_evaluation(dataset_path=dataset_path, examples=[example])

    with get_db() as db:
        comparison = db.query(PairwiseComparisonDB).filter_by(run_id=run_id).one()
    assert comparison.outcome == EvaluationOutcome.GENERATION_ERROR.value
    assert comparison.winner is None
    assert "timed out after 0.01 seconds" in comparison.error_message


def test_restart_recovery_marks_only_unfinished_runs_as_interrupted(tmp_path):
    dataset_path = str(tmp_path / "recovery.jsonl")
    single_runner = EvaluationRunner(DeterministicFakeProvider.successful(), EvaluatorRegistry())
    pairwise_runner = PairwiseEvaluationRunner(
        DeterministicFakeProvider.successful(),
        DeterministicFakeProvider.successful(),
        PairwiseJudgeEvaluator(DeterministicFakeProvider.valid_pairwise_judge()),
    )
    queued_single = single_runner.create_run(dataset_path=dataset_path)
    running_single = single_runner.create_run(dataset_path=dataset_path)
    completed_single = single_runner.create_run(dataset_path=dataset_path)
    queued_pairwise = pairwise_runner.create_run(dataset_path=dataset_path)
    completed_pairwise = pairwise_runner.create_run(dataset_path=dataset_path)

    with get_db() as db:
        db.query(EvaluationRunDB).filter_by(id=running_single).update({"status": RunStatus.RUNNING.value})
        db.query(EvaluationRunDB).filter_by(id=completed_single).update({"status": RunStatus.COMPLETED.value})
        db.query(PairwiseRunDB).filter_by(id=completed_pairwise).update({"status": RunStatus.COMPLETED.value})
        db.commit()

    recovered = reconcile_abandoned_runs()
    assert recovered == {"evaluation_runs": 2, "pairwise_runs": 1}

    with get_db() as db:
        for run_id in (queued_single, running_single):
            run = db.query(EvaluationRunDB).filter_by(id=run_id).one()
            assert run.status == RunStatus.INTERRUPTED.value
            assert run.error_message == RESTART_INTERRUPTION_MESSAGE
            assert run.completed_at is not None
        assert db.query(EvaluationRunDB).filter_by(id=completed_single).one().status == RunStatus.COMPLETED.value
        pairwise_run = db.query(PairwiseRunDB).filter_by(id=queued_pairwise).one()
        assert pairwise_run.status == RunStatus.INTERRUPTED.value
        assert pairwise_run.error_message == RESTART_INTERRUPTION_MESSAGE
        assert db.query(PairwiseRunDB).filter_by(id=completed_pairwise).one().status == RunStatus.COMPLETED.value
