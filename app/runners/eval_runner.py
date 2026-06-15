import uuid
import asyncio
import time
import json
import structlog
from pathlib import Path
from typing import List, Dict, Any, Optional

from app.database.connection import get_db, init_db
from app.database.models import DatasetDB, DatasetVersionDB, EvaluationRunDB, EvaluationResultDB
from app.evaluators.registry import EvaluatorRegistry
from app.providers.base import BaseProvider
from app.schemas.example import EvaluationExample

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
                data = json.loads(line_str)
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
        concurrency_limit: int = 5
    ):
        self.provider = provider
        self.registry = registry
        self.semaphore = asyncio.Semaphore(concurrency_limit)

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
            try:
                prediction, provider_usage = await self.provider.generate(example.input)
            except Exception as e:
                log.error("failed_to_generate_prediction", example_id=example.id, error=str(e))
                prediction = f"[GENERATION FAILURE] Error: {str(e)}"
                
            latency_sec = time.perf_counter() - start_time
            
            # Run all evaluators concurrently
            eval_tasks = []
            evaluators = self.registry.get_all()
            
            for evaluator in evaluators:
                eval_tasks.append(
                    evaluator.evaluate(
                        input_text=example.input,
                        expected_output=example.expected_output,
                        prediction=prediction
                    )
                )
                
            # Gather evaluation results (safeguard against individual evaluator exceptions)
            eval_results = await asyncio.gather(*eval_tasks, return_exceptions=True)
            
            db_results = []
            for evaluator, res in zip(evaluators, eval_results):
                if isinstance(res, Exception):
                    log.error("evaluator_failed", evaluator=evaluator.name, example_id=example.id, error=str(res))
                    # Create a failure record
                    db_res = EvaluationResultDB(
                        run_id=run_id,
                        example_id=example.id,
                        prompt=example.input,
                        prediction=prediction,
                        expected_output=example.expected_output,
                        score=0.0,
                        evaluator_name=evaluator.name,
                    )
                    db_res.metadata_dict = {"error": str(res), "latency_sec": latency_sec}
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
                    score=res.score,
                    evaluator_name=res.evaluator_name,
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
        # Ensure database tables exist
        init_db()
        
        run_id = str(uuid.uuid4())
        
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
                        data = json.loads(line_str)
                        if "id" not in data:
                            data["id"] = f"example_{line_num}"
                        if "input" not in data or "expected_output" not in data:
                            raise KeyError("Line must contain 'input' and 'expected_output' fields.")
                        examples.append(EvaluationExample(**data))
                except Exception as e:
                    raise ValueError(f"Error parsing stored dataset content: {e}")

                model_name = getattr(self.provider, "model_name", "unknown-model")
                db_run = EvaluationRunDB(
                    id=run_id,
                    dataset_id=db_dataset.id,
                    dataset_version_id=db_version.id,
                    model_name=model_name,
                )
                db.add(db_run)
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
                    
                # Create Evaluation Run
                model_name = getattr(self.provider, "model_name", "unknown-model")
                db_run = EvaluationRunDB(
                    id=run_id,
                    dataset_id=db_dataset.id,
                    model_name=model_name
                )
                db.add(db_run)
                db.commit()
            
        # Run examples concurrently
        tasks = [self._run_example(example, run_id) for example in examples]
        results_nested = await asyncio.gather(*tasks)
        
        # Flatten and insert to DB
        all_results = [res for sublist in results_nested for res in sublist]
        
        with get_db() as db:
            db.add_all(all_results)
            db.commit()
            
        return run_id

def get_run_metrics(run_id: str) -> Dict[str, Any]:
    """
    Computes aggregated performance metrics for a specific evaluation run.
    """
    with get_db() as db:
        results = db.query(EvaluationResultDB).filter(EvaluationResultDB.run_id == run_id).all()
        
    if not results:
        return {}
        
    # Group scores by evaluator
    scores_by_evaluator: Dict[str, List[float]] = {}
    for r in results:
        scores_by_evaluator.setdefault(r.evaluator_name, []).append(r.score)
        
    metrics = {
        "run_id": run_id,
        "total_examples": len(results) // len(scores_by_evaluator) if scores_by_evaluator else 0,
        "evaluators": {}
    }
    
    for eval_name, scores in scores_by_evaluator.items():
        total = len(scores)
        pass_count = sum(1 for s in scores if s >= 0.5)
        metrics["evaluators"][eval_name] = {
            "avg_score": sum(scores) / total if total else 0.0,
            "pass_rate": pass_count / total if total else 0.0,
            "count": total
        }
        
    return metrics
