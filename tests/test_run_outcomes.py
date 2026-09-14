import json

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
from app.providers.openai import OpenAIProvider
from app.runners.eval_runner import EvaluationRunner
from app.runners.pairwise_runner import PairwiseEvaluationRunner
from app.schemas.outcomes import EvaluationOutcome, RunStatus
from tests.fakes import DeterministicFakeProvider


@pytest.fixture
def one_example_dataset(tmp_path):
    path = tmp_path / "one_example.jsonl"
    path.write_text(
        json.dumps({"id": "case-1", "input": "What is 2+2?", "expected_output": "4"}) + "\n",
        encoding="utf-8",
    )
    return str(path)


@pytest.mark.asyncio
async def test_generation_failure_is_not_judged_and_has_no_score(one_example_dataset):
    judge_provider = DeterministicFakeProvider.valid_judge()
    runner = EvaluationRunner(
        provider=DeterministicFakeProvider.failing(),
        registry=EvaluatorRegistry(judge_provider=judge_provider),
    )

    run_id = await runner.run_evaluation(dataset_path=one_example_dataset)

    with get_db() as db:
        run = db.query(EvaluationRunDB).one()
        results = db.query(EvaluationResultDB).all()

    assert run.id == run_id
    assert run.status == RunStatus.COMPLETED.value
    assert run.started_at is not None
    assert run.completed_at is not None
    assert run.is_simulated is False
    assert len(results) == 3
    assert all(result.outcome == EvaluationOutcome.GENERATION_ERROR.value for result in results)
    assert all(result.score is None for result in results)
    assert all(result.error_message == "deterministic fake provider failure" for result in results)
    assert judge_provider.prompts == []


@pytest.mark.asyncio
async def test_failed_judge_is_distinct_from_an_incorrect_answer(one_example_dataset):
    runner = EvaluationRunner(
        provider=DeterministicFakeProvider.successful(),
        registry=EvaluatorRegistry(judge_provider=DeterministicFakeProvider.malformed_json()),
    )

    await runner.run_evaluation(dataset_path=one_example_dataset)

    with get_db() as db:
        results = db.query(EvaluationResultDB).all()

    exact_match = next(result for result in results if result.evaluator_name == "exact_match")
    judge_result = next(result for result in results if result.evaluator_name == "llm_judge")
    assert exact_match.outcome == EvaluationOutcome.EVALUATED.value
    assert exact_match.score == 0.0
    assert judge_result.outcome == EvaluationOutcome.EVALUATION_ERROR.value
    assert judge_result.score is None
    assert judge_result.error_message


@pytest.mark.asyncio
async def test_pairwise_generation_failure_skips_judging(one_example_dataset):
    judge_provider = DeterministicFakeProvider.valid_pairwise_judge()
    runner = PairwiseEvaluationRunner(
        provider_a=DeterministicFakeProvider.failing(),
        provider_b=DeterministicFakeProvider.successful(),
        pairwise_evaluator=PairwiseJudgeEvaluator(judge_provider=judge_provider),
    )

    run_id = await runner.run_pairwise_evaluation(dataset_path=one_example_dataset)

    with get_db() as db:
        run = db.query(PairwiseRunDB).one()
        comparison = db.query(PairwiseComparisonDB).one()

    assert run.id == run_id
    assert run.status == RunStatus.COMPLETED.value
    assert comparison.outcome == EvaluationOutcome.GENERATION_ERROR.value
    assert comparison.winner is None
    assert comparison.score_a is None
    assert comparison.score_b is None
    assert "model_a: deterministic fake provider failure" in comparison.error_message
    assert judge_provider.prompts == []


@pytest.mark.asyncio
async def test_pairwise_judge_failure_has_null_winner(one_example_dataset):
    runner = PairwiseEvaluationRunner(
        provider_a=DeterministicFakeProvider.successful(),
        provider_b=DeterministicFakeProvider.successful(),
        pairwise_evaluator=PairwiseJudgeEvaluator(
            judge_provider=DeterministicFakeProvider.malformed_json()
        ),
    )

    await runner.run_pairwise_evaluation(dataset_path=one_example_dataset)

    with get_db() as db:
        comparison = db.query(PairwiseComparisonDB).one()

    assert comparison.outcome == EvaluationOutcome.EVALUATION_ERROR.value
    assert comparison.winner is None
    assert comparison.score_a is None
    assert comparison.score_b is None
    assert comparison.error_message


@pytest.mark.asyncio
async def test_mock_provider_marks_the_run_as_simulated(one_example_dataset):
    runner = EvaluationRunner(
        provider=OpenAIProvider(model_name="mock"),
        registry=EvaluatorRegistry(),
    )

    await runner.run_evaluation(dataset_path=one_example_dataset)

    with get_db() as db:
        run = db.query(EvaluationRunDB).one()

    assert run.is_simulated is True
