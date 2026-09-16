import uuid
import asyncio
import time
import structlog
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

from json_repair import repair_json

from app.database.connection import get_db, init_db
from app.database.models import DatasetDB, DatasetVersionDB, EvaluationRunDB, EvaluationResultDB
from app.errors import sanitize_error
from app.evaluators.registry import EvaluatorRegistry
from app.providers.base import BaseProvider
from app.schemas.example import EvaluationExample
from app.schemas.outcomes import EvaluationOutcome, RunStatus
from app.services.run_configuration import (
    build_single_run_configuration,
    dataset_path_snapshot,
    dataset_version_snapshot,
)

log = structlog.get_logger()

def load_dataset(dataset_path: str) -> List[EvaluationExample]:
    """
    Loads evaluation examples from a JSONL file.
    
    Args:
        dataset_path: Path to the JSONL dataset file.
        
    Returns:
        List of EvaluationExample objects.
    """
    path = Path(dataset_path)
    if not path.exists():
        raise FileNotFoundError(f"Dataset file not found: {dataset_path}")
        
    examples = []
    with open(path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line_str = line.strip()
            if not line_str:
                continue
            try:
                data = repair_json(line_str, return_objects=True)
                if not isinstance(data, dict):
                    raise ValueError(f"Expected a JSON object, got {type(data).__name__}")
                # Assign ID if missing
                if "id" not in data:
                    data["id"] = f"example_{line_num}"
                # Ensure input/expected_output exists
                if "input" not in data or "expected_output" not in data:
                    raise KeyError("Line must contain 'input' and 'expected_output' fields.")
                examples.append(EvaluationExample(**data))
            except Exception as e:
                raise ValueError(f"Error parsing line {line_num} in {dataset_path}: {e}")
    return examples

class EvaluationRunner:
    """
    Core evaluation engine that orchestrates generating completions from a provider,
    running evaluators, and storing the results to the database.
    """
    
    def __init__(
        self, 
        provider: BaseProvider, 
        registry: EvaluatorRegistry, 
        concurrency_limit: int = 5,
        execution_timeout_seconds: float = 60.0,
        result_batch_size: int = 10,
        requested_configuration: Optional[Dict[str, Any]] = None,
    ):
        self.provider = provider
        self.registry = registry
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
        providers = [self.provider]
        providers.extend(
            evaluator.provider
            for evaluator in self.registry.get_all()
            if hasattr(evaluator, "provider")
        )
        return any(getattr(provider, "is_mock", False) for provider in providers)

    def _mark_run_failed(self, run_id: str, error: Exception) -> None:
        with get_db() as db:
            run = db.query(EvaluationRunDB).filter(EvaluationRunDB.id == run_id).first()
            if run:
                run.status = RunStatus.FAILED.value
                run.completed_at = datetime.now(timezone.utc)
                run.error_message = sanitize_error(error)
                db.commit()

    def _mark_run_interrupted(self, run_id: str) -> None:
        with get_db() as db:
            run = db.query(EvaluationRunDB).filter(EvaluationRunDB.id == run_id).first()
            if run:
                run.status = RunStatus.INTERRUPTED.value
                run.completed_at = datetime.now(timezone.utc)
                run.error_message = "Execution interrupted."
                db.commit()

    def _persist_result_batch(self, run_id: str, results: List[EvaluationResultDB]) -> None:
        """Commit a small completed batch so a restart retains prior work."""
        if not results:
            return
        with get_db() as db:
            db.add_all(results)
            db.commit()

    def create_run(
        self,
        dataset_path: Optional[str] = None,
        dataset_id: Optional[str] = None,
        dataset_version_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> str:
        """Persist a queued run before evaluation begins and return its stable ID."""
        init_db()
        run_id = run_id or str(uuid.uuid4())

        with get_db() as db:
            existing_run = db.query(EvaluationRunDB).filter(EvaluationRunDB.id == run_id).first()
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
                db_run = EvaluationRunDB(
                    id=run_id,
                    dataset_id=db_dataset.id,
                    dataset_version_id=db_version.id,
                    model_name=getattr(self.provider, "model_name", "unknown-model"),
                    status=RunStatus.QUEUED.value,
                    is_simulated=self._is_simulated(),
                    configuration_verified=True,
                )
                db_run.run_configuration = build_single_run_configuration(
                    dataset=dataset_version_snapshot(db_dataset, db_version),
                    provider=self.provider,
                    evaluators=self.registry.get_all(),
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
                db_run = EvaluationRunDB(
                    id=run_id,
                    dataset_id=db_dataset.id,
                    model_name=getattr(self.provider, "model_name", "unknown-model"),
                    status=RunStatus.QUEUED.value,
                    is_simulated=self._is_simulated(),
                    configuration_verified=True,
                )
                db_run.run_configuration = build_single_run_configuration(
                    dataset=dataset_path_snapshot(dataset_path),
                    provider=self.provider,
                    evaluators=self.registry.get_all(),
                    concurrency_limit=self.concurrency_limit,
                    execution_timeout_seconds=self.execution_timeout_seconds,
                    result_batch_size=self.result_batch_size,
                    is_simulated=self._is_simulated(),
                    requested_configuration=self.requested_configuration,
                )

            db.add(db_run)
            db.commit()
        return run_id

    async def _run_example(self, example: EvaluationExample, run_id: str) -> List[EvaluationResultDB]:
        """
        Runs evaluation for a single example:
        1. Generate prediction from provider.
        2. Concurrently evaluate using all registered evaluators.
        3. Convert results to DB model representations.
        """
        async with self.semaphore:
            log.info("running_evaluation", example_id=example.id)
            start_time = time.perf_counter()
            prediction = ""
            provider_usage = None
            generation_error = None
            try:
                prediction, provider_usage = await asyncio.wait_for(
                    self.provider.generate(example.input),
                    timeout=self.execution_timeout_seconds,
                )
            except asyncio.TimeoutError:
                generation_error = (
                    f"Generation timed out after {self.execution_timeout_seconds:g} seconds."
                )
                log.error("prediction_generation_timed_out", example_id=example.id)
            except Exception as e:
                generation_error = sanitize_error(e)
                log.error("failed_to_generate_prediction", example_id=example.id, error=generation_error)
                
            latency_sec = time.perf_counter() - start_time
            
            # Run all evaluators concurrently
            eval_tasks = []
            evaluators = self.registry.get_all()

            if generation_error:
                return [
                    EvaluationResultDB(
                        run_id=run_id,
                        example_id=example.id,
                        prompt=example.input,
                        prediction=prediction,
                        expected_output=example.expected_output,
                        score=None,
                        evaluator_name=evaluator.name,
                        outcome=EvaluationOutcome.GENERATION_ERROR.value,
                        error_message=generation_error,
                        metadata_json=None,
                    )
                    for evaluator in evaluators
                ]
            
            for evaluator in evaluators:
                eval_tasks.append(
                    asyncio.wait_for(
                        evaluator.evaluate(
                            input_text=example.input,
                            expected_output=example.expected_output,
                            prediction=prediction,
                        ),
                        timeout=self.execution_timeout_seconds,
                    )
                )
                
            # Gather evaluation results (safeguard against individual evaluator exceptions)
            eval_results = await asyncio.gather(*eval_tasks, return_exceptions=True)
            
            db_results = []
            for evaluator, res in zip(evaluators, eval_results):
                if isinstance(res, Exception):
                    log.error("evaluator_failed", evaluator=evaluator.name, example_id=example.id, error=str(res))
                    error_message = (
                        f"Evaluation timed out after {self.execution_timeout_seconds:g} seconds."
                        if isinstance(res, asyncio.TimeoutError)
                        else sanitize_error(res)
                    )
                    db_res = EvaluationResultDB(
                        run_id=run_id,
                        example_id=example.id,
                        prompt=example.input,
                        prediction=prediction,
                        expected_output=example.expected_output,
                        score=None,
                        evaluator_name=evaluator.name,
                        outcome=EvaluationOutcome.EVALUATION_ERROR.value,
                        error_message=error_message,
                    )
                    db_res.metadata_dict = {"error": error_message, "latency_sec": latency_sec}
                    db_results.append(db_res)
                    continue
                
                # Fill in example_id and extend metadata
                res.example_id = example.id
                meta = res.metadata or {}
                meta["latency_sec"] = latency_sec
                res.metadata = meta
                
                # Build DB model
                db_res = EvaluationResultDB(
                    run_id=run_id,
                    example_id=res.example_id,
                    prompt=res.prompt,
                    prediction=res.prediction,
                    expected_output=res.expected_output,
                    score=res.score if res.outcome == EvaluationOutcome.EVALUATED else None,
                    evaluator_name=res.evaluator_name,
                    outcome=res.outcome.value,
                    error_message=sanitize_error(res.error_message) if res.error_message else None,
                    prompt_tokens=res.prompt_tokens,
                    completion_tokens=res.completion_tokens,
                )
                db_res.metadata_dict = res.metadata
                db_results.append(db_res)
                
            return db_results

    async def run_evaluation(
        self,
        dataset_path: Optional[str] = None,
        examples: Optional[List[EvaluationExample]] = None,
        dataset_id: Optional[str] = None,
        dataset_version_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> str:
        """Create or reuse a queued run, then execute its full lifecycle."""
        run_id = self.create_run(
            dataset_path=dataset_path,
            dataset_id=dataset_id,
            dataset_version_id=dataset_version_id,
            run_id=run_id,
        )
        try:
            return await self._execute_evaluation(
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

    async def _execute_evaluation(
        self,
        dataset_path: Optional[str] = None,
        examples: Optional[List[EvaluationExample]] = None,
        dataset_id: Optional[str] = None,
        dataset_version_id: Optional[str] = None,
        run_id: str = "",
    ) -> str:
        """
        Runs the evaluation pipeline for the complete list of examples.

        Supports two modes:
        - Legacy: provide dataset_path and examples (loaded from filesystem).
        - New: provide dataset_id + dataset_version_id (loaded from DB).
        
        Args:
            dataset_path: Path to JSONL file (legacy mode).
            examples: Pre-loaded examples (legacy mode, required if dataset_path used).
            dataset_id: UUID of a dataset in the registry (new mode).
            dataset_version_id: UUID of a specific version (optional, uses active version if not set).

        Returns:
            The run ID string.
        """
        if dataset_id is not None:
            # New mode: load from DB
            with get_db() as db:
                db_dataset = db.query(DatasetDB).filter(DatasetDB.id == dataset_id).first()
                if not db_dataset:
                    raise ValueError(f"Dataset '{dataset_id}' not found.")

                # Resolve version
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

                # Parse examples from stored content
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

                db_run = db.query(EvaluationRunDB).filter(EvaluationRunDB.id == run_id).one()
                db_run.status = RunStatus.RUNNING.value
                db_run.started_at = datetime.now(timezone.utc)
                db.commit()
        else:
            # Legacy mode: load from filesystem
            if examples is None:
                if dataset_path is None:
                    raise ValueError("Either dataset_path or dataset_id must be provided.")
                examples = await asyncio.to_thread(load_dataset, dataset_path)

            dataset_name = Path(dataset_path).name if dataset_path else "unknown"
            resolved_path = str(Path(dataset_path).resolve()) if dataset_path else "unknown"

            with get_db() as db:
                # Upsert dataset using resolved path as legacy identifier
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
                    
                db_run = db.query(EvaluationRunDB).filter(EvaluationRunDB.id == run_id).one()
                db_run.status = RunStatus.RUNNING.value
                db_run.started_at = datetime.now(timezone.utc)
                db.commit()
            
        try:
            for start in range(0, len(examples), self.result_batch_size):
                batch = examples[start:start + self.result_batch_size]
                results_nested = await asyncio.gather(
                    *(self._run_example(example, run_id) for example in batch)
                )
                all_results = [result for results in results_nested for result in results]
                await asyncio.to_thread(self._persist_result_batch, run_id, all_results)

            with get_db() as db:
                run = db.query(EvaluationRunDB).filter(EvaluationRunDB.id == run_id).first()
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

def get_run_metrics(run_id: str) -> Dict[str, Any]:
    """
    Computes aggregated performance metrics for a specific evaluation run.
    """
    with get_db() as db:
        results = db.query(EvaluationResultDB).filter(EvaluationResultDB.run_id == run_id).all()
        
    if not results:
        return {}
        
    total_cases = len({result.example_id for result in results})
    metrics = {
        "run_id": run_id,
        "total_examples": total_cases,
        "evaluators": {}
    }

    results_by_evaluator: Dict[str, List[EvaluationResultDB]] = {}
    for result in results:
        results_by_evaluator.setdefault(result.evaluator_name, []).append(result)

    for eval_name, evaluator_results in results_by_evaluator.items():
        valid_scores = [
            result.score
            for result in evaluator_results
            if result.outcome == EvaluationOutcome.EVALUATED.value and result.score is not None
        ]
        valid_count = len(valid_scores)
        generation_errors = sum(
            result.outcome == EvaluationOutcome.GENERATION_ERROR.value
            for result in evaluator_results
        )
        evaluation_errors = sum(
            result.outcome == EvaluationOutcome.EVALUATION_ERROR.value
            for result in evaluator_results
        )
        pass_count = sum(1 for score in valid_scores if score >= 0.5)
        metrics["evaluators"][eval_name] = {
            "total_cases": total_cases,
            "valid_evaluations": valid_count,
            "generation_errors": generation_errors,
            "evaluation_errors": evaluation_errors,
            "error_count": generation_errors + evaluation_errors,
            "passing_evaluations": pass_count,
            "evaluation_coverage": valid_count / total_cases if total_cases else 0.0,
            "avg_score": sum(valid_scores) / valid_count if valid_count else None,
            "pass_rate": pass_count / valid_count if valid_count else None,
        }
        
    return metrics
