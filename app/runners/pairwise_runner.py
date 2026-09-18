import uuid
import asyncio
import math
import random
import time
import structlog
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

from json_repair import repair_json

from app.database.connection import get_db, init_db
from app.database.models import (
    DatasetDB, DatasetVersionDB,
    PairwiseRunDB, PairwiseComparisonDB,
)
from app.errors import sanitize_error
from app.evaluators.base_pairwise import BasePairwiseEvaluator
from app.providers.base import BaseProvider
from app.schemas.example import EvaluationExample
from app.schemas.outcomes import EvaluationOutcome, RunStatus
from app.services.run_configuration import (
    build_pairwise_run_configuration,
    dataset_path_snapshot,
    dataset_version_snapshot,
)

log = structlog.get_logger()

# Elo rating constants
ELO_INITIAL = 1500.0
ELO_K = 32.0


def _expected_score(rating_a: float, rating_b: float) -> float:
    """Compute expected score for player A given ratings."""
    return 1.0 / (1.0 + math.pow(10, (rating_b - rating_a) / 400.0))


def _elo_update(rating_a: float, rating_b: float, score_a: float, k: float = ELO_K) -> tuple[float, float]:
    """
    Update Elo ratings after a comparison.

    Args:
        rating_a: Current Elo of model A.
        rating_b: Current Elo of model B.
        score_a: Actual score for A (1.0 = win, 0.5 = tie, 0.0 = loss).
        k: K-factor.

    Returns:
        Updated (rating_a, rating_b).
    """
    exp_a = _expected_score(rating_a, rating_b)
    exp_b = 1.0 - exp_a
    new_a = rating_a + k * (score_a - exp_a)
    new_b = rating_b + k * ((1.0 - score_a) - exp_b)
    return new_a, new_b


class PairwiseEvaluationRunner:
    """
    Runs pairwise evaluation: two models on the same dataset, compared by a judge.
    Computes win/loss/tie rates, Elo ratings, and average scores.
    """

    def __init__(
        self,
        provider_a: BaseProvider,
        provider_b: BaseProvider,
        pairwise_evaluator: BasePairwiseEvaluator,
        concurrency_limit: int = 5,
        execution_timeout_seconds: float = 60.0,
        result_batch_size: int = 10,
        requested_configuration: Optional[Dict[str, Any]] = None,
    ):
        self.provider_a = provider_a
        self.provider_b = provider_b
        self.evaluator = pairwise_evaluator
        self.semaphore = asyncio.Semaphore(concurrency_limit)
        self.concurrency_limit = concurrency_limit
        self.execution_timeout_seconds = execution_timeout_seconds
        self.result_batch_size = result_batch_size
        self.requested_configuration = requested_configuration

        if self.execution_timeout_seconds <= 0:
            raise ValueError("execution_timeout_seconds must be greater than zero.")
        if self.result_batch_size <= 0:
            raise ValueError("result_batch_size must be greater than zero.")

    def _is_simulated(self) -> bool:
        providers = [self.provider_a, self.provider_b]
        if hasattr(self.evaluator, "provider"):
            providers.append(self.evaluator.provider)
        return any(getattr(provider, "is_mock", False) for provider in providers)

    def _mark_run_failed(self, run_id: str, error: Exception) -> None:
        with get_db() as db:
            run = db.query(PairwiseRunDB).filter(PairwiseRunDB.id == run_id).first()
            if run:
                run.status = RunStatus.FAILED.value
                run.completed_at = datetime.now(timezone.utc)
                run.error_message = sanitize_error(error)
                db.commit()

    def _mark_run_interrupted(self, run_id: str) -> None:
        with get_db() as db:
            run = db.query(PairwiseRunDB).filter(PairwiseRunDB.id == run_id).first()
            if run:
                run.status = RunStatus.INTERRUPTED.value
                run.completed_at = datetime.now(timezone.utc)
                run.error_message = "Execution interrupted."
                db.commit()

    def _persist_comparison_batch(self, run_id: str, comparisons: List[PairwiseComparisonDB]) -> None:
        """Commit a small completed batch so a restart retains prior work."""
        if not comparisons:
            return
        with get_db() as db:
            db.add_all(comparisons)
            db.commit()

    def create_run(
        self,
        dataset_path: Optional[str] = None,
        dataset_id: Optional[str] = None,
        dataset_version_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> str:
        """Persist a queued pairwise run before comparison work begins."""
        init_db()
        run_id = run_id or str(uuid.uuid4())

        with get_db() as db:
            existing_run = db.query(PairwiseRunDB).filter(PairwiseRunDB.id == run_id).first()
            if existing_run:
                return run_id

            if dataset_id is not None:
                db_dataset = db.query(DatasetDB).filter(DatasetDB.id == dataset_id).first()
                if not db_dataset:
                    raise ValueError(f"Dataset '{dataset_id}' not found.")
                if dataset_version_id:
                    db_version = db.query(DatasetVersionDB).filter(
                        DatasetVersionDB.id == dataset_version_id,
                        DatasetVersionDB.dataset_id == dataset_id,
                    ).first()
                else:
                    db_version = db.query(DatasetVersionDB).filter(
                        DatasetVersionDB.dataset_id == dataset_id,
                        DatasetVersionDB.is_active == True,
                    ).first()
                if not db_version:
                    raise ValueError(f"No active version found for dataset '{dataset_id}'.")
                db_run = PairwiseRunDB(
                    id=run_id,
                    dataset_id=db_dataset.id,
                    dataset_version_id=db_version.id,
                    project_id=db_dataset.project_id,
                    model_a_name=getattr(self.provider_a, "model_name", "unknown-model-a"),
                    model_b_name=getattr(self.provider_b, "model_name", "unknown-model-b"),
                    status=RunStatus.QUEUED.value,
                    is_simulated=self._is_simulated(),
                    configuration_verified=True,
                )
                db_run.run_configuration = build_pairwise_run_configuration(
                    dataset=dataset_version_snapshot(db_dataset, db_version),
                    provider_a=self.provider_a,
                    provider_b=self.provider_b,
                    evaluator=self.evaluator,
                    concurrency_limit=self.concurrency_limit,
                    execution_timeout_seconds=self.execution_timeout_seconds,
                    result_batch_size=self.result_batch_size,
                    is_simulated=self._is_simulated(),
                    requested_configuration=self.requested_configuration,
                )
            else:
                if dataset_path is None:
                    raise ValueError("Either dataset_path or dataset_id must be provided.")
                resolved_path = str(Path(dataset_path).resolve())
                db_dataset = db.query(DatasetDB).filter(DatasetDB.id == resolved_path).first()
                if not db_dataset:
                    db_dataset = DatasetDB(
                        id=resolved_path,
                        name=Path(dataset_path).name,
                        latest_version_number=0,
                    )
                    db.add(db_dataset)
                    db.flush()
                db_run = PairwiseRunDB(
                    id=run_id,
                    dataset_id=db_dataset.id,
                    project_id=db_dataset.project_id,
                    model_a_name=getattr(self.provider_a, "model_name", "unknown-model-a"),
                    model_b_name=getattr(self.provider_b, "model_name", "unknown-model-b"),
                    status=RunStatus.QUEUED.value,
                    is_simulated=self._is_simulated(),
                    configuration_verified=True,
                )
                db_run.run_configuration = build_pairwise_run_configuration(
                    dataset=dataset_path_snapshot(dataset_path),
                    provider_a=self.provider_a,
                    provider_b=self.provider_b,
                    evaluator=self.evaluator,
                    concurrency_limit=self.concurrency_limit,
                    execution_timeout_seconds=self.execution_timeout_seconds,
                    result_batch_size=self.result_batch_size,
                    is_simulated=self._is_simulated(),
                    requested_configuration=self.requested_configuration,
                )

            db.add(db_run)
            db.commit()
        return run_id

    async def _run_example(
        self,
        example: EvaluationExample,
        run_id: str,
    ) -> Optional[PairwiseComparisonDB]:
        """Run a single pairwise comparison for one example."""
        async with self.semaphore:
            log.info("running_pairwise_comparison", example_id=example.id)
            start_time = time.perf_counter()

            # Generate predictions from both models concurrently
            async def _gen(provider: BaseProvider, label: str) -> tuple[str, Optional[Dict], Optional[str]]:
                try:
                    prediction, usage = await asyncio.wait_for(
                        provider.generate(example.input),
                        timeout=self.execution_timeout_seconds,
                    )
                    return prediction, usage, None
                except asyncio.TimeoutError:
                    error_message = (
                        f"Generation timed out after {self.execution_timeout_seconds:g} seconds."
                    )
                    log.error(f"generation_timed_out_{label}", example_id=example.id)
                    return "", None, error_message
                except Exception as e:
                    error_message = sanitize_error(e)
                    log.error(f"failed_to_generate_{label}", example_id=example.id, error=error_message)
                    return "", None, error_message

            (pred_a, usage_a, error_a), (pred_b, usage_b, error_b) = await asyncio.gather(
                _gen(self.provider_a, "model_a"),
                _gen(self.provider_b, "model_b"),
            )

            latency_sec = time.perf_counter() - start_time

            if error_a or error_b:
                errors = []
                if error_a:
                    errors.append(f"model_a: {error_a}")
                if error_b:
                    errors.append(f"model_b: {error_b}")
                error_message = "; ".join(errors)
                db_comp = PairwiseComparisonDB(
                    run_id=run_id,
                    example_id=example.id,
                    prompt=example.input,
                    response_a=pred_a,
                    response_b=pred_b,
                    expected_output=example.expected_output,
                    winner=None,
                    score_a=None,
                    score_b=None,
                    judge_reason="Generation failed; pairwise judging was skipped.",
                    original_order="AB",
                    outcome=EvaluationOutcome.GENERATION_ERROR.value,
                    error_message=error_message,
                )
                db_comp.metadata_dict = {"latency_sec": latency_sec}
                return db_comp

            # Randomize presentation order before judging.
            if random.random() < 0.5:
                # Normal order: A is position A, B is position B
                judge_a, judge_b = pred_a, pred_b
                original_order = "AB"
            else:
                # Swapped: B is position A, A is position B
                judge_a, judge_b = pred_b, pred_a
                original_order = "BA"

            # Run pairwise judge
            judge_error = None
            try:
                result = await asyncio.wait_for(
                    self.evaluator.evaluate(
                        input_text=example.input,
                        expected_output=example.expected_output,
                        response_a=judge_a,
                        response_b=judge_b,
                    ),
                    timeout=self.execution_timeout_seconds,
                )
            except asyncio.TimeoutError:
                log.error("pairwise_judge_timed_out", example_id=example.id)
                result = None
                judge_error = (
                    f"Evaluation timed out after {self.execution_timeout_seconds:g} seconds."
                )
            except Exception as e:
                log.error("pairwise_judge_failed", example_id=example.id, error=str(e))
                result = None
                judge_error = sanitize_error(e)

            # Un-swap the winner if needed
            if result is not None:
                if result.outcome == EvaluationOutcome.EVALUATED:
                    winner = result.winner
                    if original_order == "BA":
                        # Positions were swapped, so un-swap the winner.
                        if winner == "A":
                            winner = "B"
                        elif winner == "B":
                            winner = "A"
                    score_a = result.score_a if original_order == "AB" else result.score_b
                    score_b = result.score_b if original_order == "AB" else result.score_a
                else:
                    winner = None
                    score_a = None
                    score_b = None
                judge_reason = result.reason
                meta = result.metadata or {}
            else:
                winner = None
                score_a = None
                score_b = None
                judge_reason = "Judge evaluation failed"
                meta = {"error": judge_error or "Judge evaluation raised an exception"}
                outcome = EvaluationOutcome.EVALUATION_ERROR
                error_message = judge_error or "Judge evaluation raised an exception"

            if result is not None:
                outcome = result.outcome
                error_message = sanitize_error(result.error_message) if result.error_message else None

            meta["latency_sec"] = latency_sec
            meta["original_order"] = original_order

            db_comp = PairwiseComparisonDB(
                run_id=run_id,
                example_id=example.id,
                prompt=example.input,
                response_a=pred_a,
                response_b=pred_b,
                expected_output=example.expected_output,
                winner=winner,
                score_a=score_a,
                score_b=score_b,
                judge_reason=judge_reason,
                original_order=original_order,
                outcome=outcome.value,
                error_message=error_message,
            )
            db_comp.metadata_dict = meta
            return db_comp

    async def run_pairwise_evaluation(
        self,
        dataset_path: Optional[str] = None,
        examples: Optional[List[EvaluationExample]] = None,
        dataset_id: Optional[str] = None,
        dataset_version_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> str:
        """Create or reuse a queued pairwise run, then execute its lifecycle."""
        run_id = self.create_run(
            dataset_path=dataset_path,
            dataset_id=dataset_id,
            dataset_version_id=dataset_version_id,
            run_id=run_id,
        )
        try:
            return await self._execute_pairwise_evaluation(
                dataset_path=dataset_path,
                examples=examples,
                dataset_id=dataset_id,
                dataset_version_id=dataset_version_id,
                run_id=run_id,
            )
        except asyncio.CancelledError:
            self._mark_run_interrupted(run_id)
            raise
        except Exception as e:
            self._mark_run_failed(run_id, e)
            raise

    async def _execute_pairwise_evaluation(
        self,
        dataset_path: Optional[str] = None,
        examples: Optional[List[EvaluationExample]] = None,
        dataset_id: Optional[str] = None,
        dataset_version_id: Optional[str] = None,
        run_id: str = "",
    ) -> str:
        """
        Runs the pairwise evaluation pipeline.

        Supports two modes:
        - Legacy: provide dataset_path and examples.
        - New: provide dataset_id + dataset_version_id.

        Returns:
            The run ID string.
        """
        if dataset_id is not None:
            # New mode: load from DB
            with get_db() as db:
                db_dataset = db.query(DatasetDB).filter(DatasetDB.id == dataset_id).first()
                if not db_dataset:
                    raise ValueError(f"Dataset '{dataset_id}' not found.")

                if dataset_version_id:
                    db_version = db.query(DatasetVersionDB).filter(
                        DatasetVersionDB.id == dataset_version_id,
                        DatasetVersionDB.dataset_id == dataset_id,
                    ).first()
                else:
                    db_version = db.query(DatasetVersionDB).filter(
                        DatasetVersionDB.dataset_id == dataset_id,
                        DatasetVersionDB.is_active == True,
                    ).first()

                if not db_version:
                    raise ValueError(f"No active version found for dataset '{dataset_id}'.")

                try:
                    examples = []
                    for line_num, line in enumerate(db_version.content.strip().splitlines(), 1):
                        line_str = line.strip()
                        if not line_str:
                            continue
                        data = repair_json(line_str, return_objects=True)
                        if not isinstance(data, dict):
                            raise ValueError(f"Expected a JSON object, got {type(data).__name__}")
                        if "id" not in data:
                            data["id"] = f"example_{line_num}"
                        if "input" not in data or "expected_output" not in data:
                            raise KeyError("Line must contain 'input' and 'expected_output' fields.")
                        examples.append(EvaluationExample(**data))
                except Exception as e:
                    raise ValueError(f"Error parsing stored dataset content: {e}")

                db_run = db.query(PairwiseRunDB).filter(PairwiseRunDB.id == run_id).one()
                db_run.status = RunStatus.RUNNING.value
                db_run.started_at = datetime.now(timezone.utc)
                db.commit()
        else:
            # Legacy mode: load from filesystem
            if examples is None:
                if dataset_path is None:
                    raise ValueError("Either dataset_path or dataset_id must be provided.")
                from app.runners.eval_runner import load_dataset
                examples = await asyncio.to_thread(load_dataset, dataset_path)

            dataset_name = Path(dataset_path).name if dataset_path else "unknown"
            resolved_path = str(Path(dataset_path).resolve()) if dataset_path else "unknown"

            with get_db() as db:
                db_dataset = db.query(DatasetDB).filter(DatasetDB.id == resolved_path).first()
                if not db_dataset:
                    db_dataset = DatasetDB(
                        id=resolved_path,
                        name=dataset_name,
                        latest_version_number=0,
                    )
                    db.add(db_dataset)
                    db.commit()
                    db.refresh(db_dataset)

                db_run = db.query(PairwiseRunDB).filter(PairwiseRunDB.id == run_id).one()
                db_run.status = RunStatus.RUNNING.value
                db_run.started_at = datetime.now(timezone.utc)
                db.commit()

        try:
            for start in range(0, len(examples), self.result_batch_size):
                batch = examples[start:start + self.result_batch_size]
                results = await asyncio.gather(
                    *(self._run_example(example, run_id) for example in batch)
                )
                db_comparisons = [result for result in results if result is not None]
                await asyncio.to_thread(self._persist_comparison_batch, run_id, db_comparisons)

            with get_db() as db:
                run = db.query(PairwiseRunDB).filter(PairwiseRunDB.id == run_id).first()
                if run:
                    run.status = RunStatus.COMPLETED.value
                    run.completed_at = datetime.now(timezone.utc)
                db.commit()
        except asyncio.CancelledError:
            self._mark_run_interrupted(run_id)
            raise
        except Exception as e:
            self._mark_run_failed(run_id, e)
            raise

        return run_id


def get_pairwise_run_metrics(run_id: str) -> Dict[str, Any]:
    """
    Computes pairwise comparison metrics for a specific run.
    Returns win rate, loss rate, tie rate, Elo ratings, and average scores.
    """
    with get_db() as db:
        comparisons = db.query(PairwiseComparisonDB).filter(
            PairwiseComparisonDB.run_id == run_id
        ).all()

    if not comparisons:
        return {}

    total_comparisons = len(comparisons)
    evaluated_comparisons = [
        comparison
        for comparison in comparisons
        if (
            comparison.outcome == EvaluationOutcome.EVALUATED.value
            and comparison.winner is not None
            and comparison.score_a is not None
            and comparison.score_b is not None
        )
    ]
    valid_comparisons = len(evaluated_comparisons)
    generation_errors = sum(
        comparison.outcome == EvaluationOutcome.GENERATION_ERROR.value
        for comparison in comparisons
    )
    evaluation_errors = sum(
        comparison.outcome == EvaluationOutcome.EVALUATION_ERROR.value
        for comparison in comparisons
    )
    wins_a = sum(1 for c in evaluated_comparisons if c.winner == "A")
    wins_b = sum(1 for c in evaluated_comparisons if c.winner == "B")
    ties = sum(1 for c in evaluated_comparisons if c.winner == "tie")

    # Compute Elo ratings
    elo_a = ELO_INITIAL
    elo_b = ELO_INITIAL
    if valid_comparisons:
        for c in evaluated_comparisons:
            if c.winner == "A":
                score_a = 1.0
            elif c.winner == "B":
                score_a = 0.0
            else:
                score_a = 0.5
            elo_a, elo_b = _elo_update(elo_a, elo_b, score_a)

    avg_score_a = (
        sum(c.score_a for c in evaluated_comparisons) / valid_comparisons
        if valid_comparisons else None
    )
    avg_score_b = (
        sum(c.score_b for c in evaluated_comparisons) / valid_comparisons
        if valid_comparisons else None
    )

    # Get model names from the run
    with get_db() as db:
        run = db.query(PairwiseRunDB).filter(PairwiseRunDB.id == run_id).first()
        model_a_name = run.model_a_name if run else "unknown"
        model_b_name = run.model_b_name if run else "unknown"

    return {
        "run_id": run_id,
        "model_a_name": model_a_name,
        "model_b_name": model_b_name,
        "total_comparisons": total_comparisons,
        "valid_comparisons": valid_comparisons,
        "generation_errors": generation_errors,
        "evaluation_errors": evaluation_errors,
        "error_count": generation_errors + evaluation_errors,
        "evaluation_coverage": valid_comparisons / total_comparisons if total_comparisons else 0.0,
        "wins_a": wins_a,
        "wins_b": wins_b,
        "ties": ties,
        "win_rate_a": wins_a / valid_comparisons if valid_comparisons else None,
        "win_rate_b": wins_b / valid_comparisons if valid_comparisons else None,
        "tie_rate": ties / valid_comparisons if valid_comparisons else None,
        "elo_a": round(elo_a, 1) if valid_comparisons else None,
        "elo_b": round(elo_b, 1) if valid_comparisons else None,
        "avg_score_a": round(avg_score_a, 4) if avg_score_a is not None else None,
        "avg_score_b": round(avg_score_b, 4) if avg_score_b is not None else None,
    }


def get_pairwise_comparisons(run_id: str) -> List[Dict[str, Any]]:
    """Retrieve all individual comparisons for a pairwise run."""
    with get_db() as db:
        comparisons = db.query(PairwiseComparisonDB).filter(
            PairwiseComparisonDB.run_id == run_id
        ).all()

    return [
        {
            "example_id": c.example_id,
            "prompt": c.prompt,
            "response_a": c.response_a,
            "response_b": c.response_b,
            "expected_output": c.expected_output,
            "winner": c.winner,
            "score_a": c.score_a,
            "score_b": c.score_b,
            "judge_reason": c.judge_reason,
            "original_order": c.original_order,
            "outcome": c.outcome,
            "error_message": c.error_message,
        }
        for c in comparisons
    ]
