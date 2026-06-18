import uuid
import asyncio
import math
import random
import time
import structlog
from pathlib import Path
from typing import List, Dict, Any, Optional

from json_repair import repair_json

from app.database.connection import get_db, init_db
from app.database.models import (
    DatasetDB, DatasetVersionDB,
    PairwiseRunDB, PairwiseComparisonDB,
)
from app.evaluators.base_pairwise import BasePairwiseEvaluator
from app.providers.base import BaseProvider
from app.schemas.example import EvaluationExample

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
    ):
        self.provider_a = provider_a
        self.provider_b = provider_b
        self.evaluator = pairwise_evaluator
        self.semaphore = asyncio.Semaphore(concurrency_limit)

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
            async def _gen(provider: BaseProvider, label: str) -> tuple[str, Optional[Dict]]:
                try:
                    return await provider.generate(example.input)
                except Exception as e:
                    log.error(f"failed_to_generate_{label}", example_id=example.id, error=str(e))
                    return f"[GENERATION FAILURE] Error: {str(e)}", None

            (pred_a, usage_a), (pred_b, usage_b) = await asyncio.gather(
                _gen(self.provider_a, "model_a"),
                _gen(self.provider_b, "model_b"),
            )

            latency_sec = time.perf_counter() - start_time

            # Randomize presentation order to eliminate bias
            if random.random() < 0.5:
                # Normal order: A is position A, B is position B
                judge_a, judge_b = pred_a, pred_b
                original_order = "AB"
            else:
                # Swapped: B is position A, A is position B
                judge_a, judge_b = pred_b, pred_a
                original_order = "BA"

            # Run pairwise judge
            try:
                result = await self.evaluator.evaluate(
                    input_text=example.input,
                    expected_output=example.expected_output,
                    response_a=judge_a,
                    response_b=judge_b,
                )
            except Exception as e:
                log.error("pairwise_judge_failed", example_id=example.id, error=str(e))
                result = None

            # Un-swap the winner if needed
            if result is not None:
                winner = result.winner
                if original_order == "BA":
                    # Positions were swapped, so un-swap the winner
                    if winner == "A":
                        winner = "B"
                    elif winner == "B":
                        winner = "A"
                score_a = result.score_a if original_order == "AB" else result.score_b
                score_b = result.score_b if original_order == "AB" else result.score_a
                judge_reason = result.reason
                meta = result.metadata or {}
            else:
                winner = "tie"
                score_a = 0.0
                score_b = 0.0
                judge_reason = "Judge evaluation failed"
                meta = {}

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
            )
            db_comp.metadata_dict = meta
            return db_comp

    async def run_pairwise_evaluation(
        self,
        dataset_path: Optional[str] = None,
        examples: Optional[List[EvaluationExample]] = None,
        dataset_id: Optional[str] = None,
        dataset_version_id: Optional[str] = None,
    ) -> str:
        """
        Runs the pairwise evaluation pipeline.

        Supports two modes:
        - Legacy: provide dataset_path and examples.
        - New: provide dataset_id + dataset_version_id.

        Returns:
            The run ID string.
        """
        init_db()
        run_id = str(uuid.uuid4())

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

                model_a_name = getattr(self.provider_a, "model_name", "unknown-model-a")
                model_b_name = getattr(self.provider_b, "model_name", "unknown-model-b")
                db_run = PairwiseRunDB(
                    id=run_id,
                    dataset_id=db_dataset.id,
                    dataset_version_id=db_version.id,
                    model_a_name=model_a_name,
                    model_b_name=model_b_name,
                )
                db.add(db_run)
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

                model_a_name = getattr(self.provider_a, "model_name", "unknown-model-a")
                model_b_name = getattr(self.provider_b, "model_name", "unknown-model-b")
                db_run = PairwiseRunDB(
                    id=run_id,
                    dataset_id=db_dataset.id,
                    model_a_name=model_a_name,
                    model_b_name=model_b_name,
                )
                db.add(db_run)
                db.commit()

        # Run all pairwise comparisons concurrently
        tasks = [self._run_example(example, run_id) for example in examples]
        results = await asyncio.gather(*tasks)

        # Filter out None results and insert to DB
        db_comparisons = [r for r in results if r is not None]
        with get_db() as db:
            db.add_all(db_comparisons)
            db.commit()

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

    total = len(comparisons)
    wins_a = sum(1 for c in comparisons if c.winner == "A")
    wins_b = sum(1 for c in comparisons if c.winner == "B")
    ties = sum(1 for c in comparisons if c.winner == "tie")

    # Compute Elo ratings
    elo_a = ELO_INITIAL
    elo_b = ELO_INITIAL
    for c in comparisons:
        if c.winner == "A":
            score_a = 1.0
        elif c.winner == "B":
            score_a = 0.0
        else:
            score_a = 0.5
        elo_a, elo_b = _elo_update(elo_a, elo_b, score_a)

    avg_score_a = sum(c.score_a for c in comparisons) / total
    avg_score_b = sum(c.score_b for c in comparisons) / total

    # Get model names from the run
    with get_db() as db:
        run = db.query(PairwiseRunDB).filter(PairwiseRunDB.id == run_id).first()
        model_a_name = run.model_a_name if run else "unknown"
        model_b_name = run.model_b_name if run else "unknown"

    return {
        "run_id": run_id,
        "model_a_name": model_a_name,
        "model_b_name": model_b_name,
        "total_comparisons": total,
        "wins_a": wins_a,
        "wins_b": wins_b,
        "ties": ties,
        "win_rate_a": wins_a / total,
        "win_rate_b": wins_b / total,
        "tie_rate": ties / total,
        "elo_a": round(elo_a, 1),
        "elo_b": round(elo_b, 1),
        "avg_score_a": round(avg_score_a, 4),
        "avg_score_b": round(avg_score_b, 4),
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
        }
        for c in comparisons
    ]
