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
from app.runners.eval_runner import EvaluationRunner, get_run_metrics
from app.runners.pairwise_runner import PairwiseEvaluationRunner, get_pairwise_run_metrics
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
    assert run.is_simulated is True
    assert len(results) == 3
    assert all(result.outcome == EvaluationOutcome.GENERATION_ERROR.value for result in results)
    assert all(result.score is None for result in results)
    assert all(result.error_message == "deterministic fake provider failure" for result in results)
    assert judge_provider.prompts == []

    metrics = get_run_metrics(run_id)
    for evaluator_metrics in metrics["evaluators"].values():
        assert evaluator_metrics["total_cases"] == 1
        assert evaluator_metrics["valid_evaluations"] == 0
        assert evaluator_metrics["generation_errors"] == 1
        assert evaluator_metrics["evaluation_errors"] == 0
        assert evaluator_metrics["evaluation_coverage"] == 0.0
        assert evaluator_metrics["avg_score"] is None
        assert evaluator_metrics["pass_rate"] is None


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

    metrics = get_pairwise_run_metrics(run_id)
    assert metrics["total_comparisons"] == 1
    assert metrics["valid_comparisons"] == 0
    assert metrics["generation_errors"] == 1
    assert metrics["evaluation_errors"] == 0
    assert metrics["evaluation_coverage"] == 0.0
    assert metrics["win_rate_a"] is None
    assert metrics["elo_a"] is None


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
async def test_invalid_pairwise_winner_is_recorded_as_a_judge_error(one_example_dataset):
    runner = PairwiseEvaluationRunner(
        provider_a=DeterministicFakeProvider.successful(),
        provider_b=DeterministicFakeProvider.successful(),
        pairwise_evaluator=PairwiseJudgeEvaluator(
            judge_provider=DeterministicFakeProvider.invalid_pairwise_winner()
        ),
    )

    await runner.run_pairwise_evaluation(dataset_path=one_example_dataset)

    with get_db() as db:
        comparison = db.query(PairwiseComparisonDB).one()

    assert comparison.outcome == EvaluationOutcome.EVALUATION_ERROR.value
    assert comparison.winner is None
    assert comparison.score_a is None
    assert comparison.score_b is None


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
