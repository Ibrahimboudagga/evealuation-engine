import asyncio
import structlog
from typing import Dict, Any, Optional

from fastapi import FastAPI, HTTPException

from app.api.schemas import (
    RunRequest,
    RunResponse,
    RunStatusResponse,
    RunListItem,
    RunsListResponse,
    DatasetItem,
    DatasetsResponse,
    EvaluatorMetric,
)
from app.database.connection import get_db, init_db
from app.database.models import DatasetDB
from app.evaluators.registry import EvaluatorRegistry
from app.providers.factory import ProviderFactory
from app.runners.eval_runner import EvaluationRunner, load_dataset, get_run_metrics

log = structlog.get_logger()

app = FastAPI(title="LLM Evaluation Engine", version="1.0.0")

# In-memory run tracking: run_id -> {"status": ..., "error": ...}
_run_store: Dict[str, Dict[str, Any]] = {}


@app.on_event("startup")
def startup():
    init_db()


@app.post("/runs", response_model=RunResponse)
async def create_run(req: RunRequest):
    try:
        examples = await asyncio.to_thread(load_dataset, req.dataset_path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to load dataset: {e}")

    try:
        candidate_provider = await asyncio.to_thread(
            ProviderFactory.create,
            provider=req.candidate_provider,
            model_id=req.candidate_model,
            api_key=req.candidate_api_key,
            base_url=req.candidate_base_url,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to create candidate provider: {e}")

    try:
        evaluator_provider = await asyncio.to_thread(
            ProviderFactory.create,
            provider=req.evaluator_provider,
            model_id=req.evaluator_model,
            api_key=req.evaluator_api_key,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to create evaluator provider: {e}")

    registry = EvaluatorRegistry(
        judge_provider=evaluator_provider,
        judge_prompt_template=req.judge_prompt_template,
    )
    runner = EvaluationRunner(
        provider=candidate_provider,
        registry=registry,
        concurrency_limit=req.concurrency,
    )

    # We need to generate a run_id before the task starts so we can return it.
    # The runner creates its own run_id internally, so we create a placeholder
    # and let the background task update the store.
    import uuid
    pre_run_id = str(uuid.uuid4())
    _run_store[pre_run_id] = {"status": "started", "error": None}

    async def _background_run():
        try:
            actual_run_id = await asyncio.to_thread(
                lambda: asyncio.run(runner.run_evaluation(req.dataset_path, examples))
            )
            _run_store.pop(pre_run_id, None)
            _run_store[actual_run_id] = {"status": "completed", "error": None}
        except Exception as e:
            log.error("background_run_failed", error=str(e))
            _run_store[pre_run_id] = {"status": "failed", "error": str(e)}

    asyncio.create_task(_background_run())

    return RunResponse(run_id=pre_run_id, status="started")


@app.get("/runs/{run_id}", response_model=RunStatusResponse)
async def get_run(run_id: str):
    info = _run_store.get(run_id)

    if info is None:
        # Check if it is a completed run in the database (started via CLI or prior to restart)
        metrics = get_run_metrics(run_id)
        if not metrics:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
        evaluator_metrics = []
        for name, data in metrics.get("evaluators", {}).items():
            evaluator_metrics.append(
                EvaluatorMetric(
                    evaluator=name,
                    mean_score=data["avg_score"],
                    pass_rate=data["pass_rate"],
                    n=data["count"],
                )
            )
        return RunStatusResponse(run_id=run_id, status="completed", metrics=evaluator_metrics)

    status = info["status"]

    if status == "completed":
        metrics = get_run_metrics(run_id)
        evaluator_metrics = []
        if metrics:
            for name, data in metrics.get("evaluators", {}).items():
                evaluator_metrics.append(
                    EvaluatorMetric(
                        evaluator=name,
                        mean_score=data["avg_score"],
                        pass_rate=data["pass_rate"],
                        n=data["count"],
                    )
                )
        return RunStatusResponse(run_id=run_id, status=status, metrics=evaluator_metrics)

    return RunStatusResponse(run_id=run_id, status=status, error=info.get("error"))


@app.get("/runs", response_model=RunsListResponse)
async def list_runs():
    runs = [RunListItem(run_id=rid, status=info["status"]) for rid, info in _run_store.items()]

    # Also include runs from the database that were started via CLI
    with get_db() as db:
        from app.database.models import EvaluationRunDB
        db_runs = db.query(EvaluationRunDB).all()
        known_ids = {r.run_id for r in runs}
        for db_run in db_runs:
            if db_run.id not in known_ids:
                runs.append(RunListItem(run_id=db_run.id, status="completed"))

    return RunsListResponse(runs=runs)


@app.get("/datasets", response_model=DatasetsResponse)
async def list_datasets():
    with get_db() as db:
        datasets = db.query(DatasetDB).all()
    return DatasetsResponse(
        datasets=[DatasetItem(id=d.id, name=d.name) for d in datasets]
    )
