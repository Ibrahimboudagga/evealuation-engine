import asyncio
import structlog
from typing import Dict, Any, Optional

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Query

from app.api.schemas import (
    RunRequest,
    RunResponse,
    RunStatusResponse,
    RunListItem,
    RunsListResponse,
    EvaluatorMetric,
    DatasetResponse,
    DatasetDetailResponse,
    DatasetCreateRequest,
    DatasetDeleteResponse,
    DatasetsListResponse,
    DatasetVersionResponse,
    DatasetAddVersionRequest,
    DatasetSetActiveVersionRequest,
    PairwiseRunRequest,
    PairwiseRunResponse,
    PairwiseRunStatusResponse,
    PairwiseRunListItem,
    PairwiseRunsListResponse,
    PairwiseMetrics,
    PairwiseComparisonItem,
)
from app.database.connection import get_db, init_db
from app.database.models import DatasetDB
from app.evaluators.registry import EvaluatorRegistry
from app.evaluators.pairwise_judge import PairwiseJudgeEvaluator
from app.providers.factory import ProviderFactory
from app.runners.eval_runner import EvaluationRunner, load_dataset, get_run_metrics
from app.runners.pairwise_runner import (
    PairwiseEvaluationRunner,
    get_pairwise_run_metrics,
    get_pairwise_comparisons,
)
from app.services.dataset_service import DatasetService

log = structlog.get_logger()

app = FastAPI(title="LLM Evaluation Engine", version="1.0.0")

# In-memory run tracking: run_id -> {"status": ..., "error": ...}
_run_store: Dict[str, Dict[str, Any]] = {}

# In-memory pairwise run tracking
_pairwise_run_store: Dict[str, Dict[str, Any]] = {}

# Service singleton
_dataset_service = DatasetService()


def _dataset_to_response(dataset: DatasetDB) -> DatasetResponse:
    """Convert a DatasetDB model to a DatasetResponse schema."""
    active_version = None
    for v in dataset.versions:
        if v.is_active:
            active_version = DatasetVersionResponse(
                id=v.id,
                version_number=v.version_number,
                example_count=v.example_count,
                is_active=v.is_active,
                created_at=v.created_at,
            )
            break
    return DatasetResponse(
        id=dataset.id,
        name=dataset.name,
        description=dataset.description,
        tags=dataset.tags,
        latest_version_number=dataset.latest_version_number,
        created_at=dataset.created_at,
        updated_at=dataset.updated_at,
        active_version=active_version,
    )


def _dataset_to_detail(dataset: DatasetDB) -> DatasetDetailResponse:
    """Convert a DatasetDB model to a DatasetDetailResponse schema."""
    base = _dataset_to_response(dataset)
    versions = [
        DatasetVersionResponse(
            id=v.id,
            version_number=v.version_number,
            example_count=v.example_count,
            is_active=v.is_active,
            created_at=v.created_at,
        )
        for v in dataset.versions
    ]
    return DatasetDetailResponse(
        id=base.id,
        name=base.name,
        description=base.description,
        tags=base.tags,
        latest_version_number=base.latest_version_number,
        created_at=base.created_at,
        updated_at=base.updated_at,
        active_version=base.active_version,
        versions=versions,
    )


@app.on_event("startup")
def startup():
    init_db()


# ── Existing Run Endpoints ───────────────────────────────────

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


# ── Dataset Endpoints ────────────────────────────────────────

@app.get("/datasets", response_model=DatasetsListResponse)
async def list_datasets(
    tag: Optional[str] = Query(default=None, description="Filter by tag"),
    search: Optional[str] = Query(default=None, description="Search by name"),
):
    datasets = _dataset_service.list_datasets(tag=tag, search=search)
    return DatasetsListResponse(
        datasets=[_dataset_to_response(d) for d in datasets]
    )


@app.get("/datasets/{dataset_id}", response_model=DatasetDetailResponse)
async def get_dataset(dataset_id: str):
    dataset = _dataset_service.get_dataset(dataset_id)
    if not dataset:
        raise HTTPException(status_code=404, detail=f"Dataset '{dataset_id}' not found")
    return _dataset_to_detail(dataset)


@app.post("/datasets", response_model=DatasetResponse, status_code=201)
async def create_dataset(req: DatasetCreateRequest):
    try:
        dataset = await asyncio.to_thread(
            _dataset_service.create_dataset,
            name=req.name,
            content_jsonl=req.content,
            description=req.description,
            tags=req.tags,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _dataset_to_response(dataset)


@app.post("/datasets/upload", response_model=DatasetResponse, status_code=201)
async def upload_dataset(
    file: UploadFile = File(..., description="JSONL dataset file"),
    name: str = Form(..., description="Dataset name"),
    description: Optional[str] = Form(default=None, description="Dataset description"),
    tags: Optional[str] = Form(default=None, description="Comma-separated tags"),
):
    file_content = await file.read()
    tag_list = [t.strip() for t in tags.split(",")] if tags else None
    try:
        dataset = await asyncio.to_thread(
            _dataset_service.upload_dataset,
            name=name,
            file_content=file_content,
            filename=file.filename or "upload.jsonl",
            description=description,
            tags=tag_list,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _dataset_to_response(dataset)


@app.post("/datasets/{dataset_id}/versions", response_model=DatasetVersionResponse, status_code=201)
async def add_dataset_version(dataset_id: str, req: DatasetAddVersionRequest):
    try:
        version = await asyncio.to_thread(
            _dataset_service.add_version,
            dataset_id=dataset_id,
            content_jsonl=req.content,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return DatasetVersionResponse(
        id=version.id,
        version_number=version.version_number,
        example_count=version.example_count,
        is_active=version.is_active,
        created_at=version.created_at,
    )


@app.put("/datasets/{dataset_id}/active-version", response_model=DatasetVersionResponse)
async def set_active_version(dataset_id: str, req: DatasetSetActiveVersionRequest):
    try:
        version = await asyncio.to_thread(
            _dataset_service.set_active_version,
            dataset_id=dataset_id,
            version_id=req.version_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return DatasetVersionResponse(
        id=version.id,
        version_number=version.version_number,
        example_count=version.example_count,
        is_active=version.is_active,
        created_at=version.created_at,
    )


@app.delete("/datasets/{dataset_id}", response_model=DatasetDeleteResponse)
async def delete_dataset(dataset_id: str):
    deleted = await asyncio.to_thread(_dataset_service.delete_dataset, dataset_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Dataset '{dataset_id}' not found")
    return DatasetDeleteResponse(message="Dataset deleted successfully", id=dataset_id)


# ── Pairwise Run Endpoints ──────────────────────────────────

@app.post("/pairwise-runs", response_model=PairwiseRunResponse)
async def create_pairwise_run(req: PairwiseRunRequest):
    try:
        examples = await asyncio.to_thread(load_dataset, req.dataset_path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to load dataset: {e}")

    try:
        provider_a = await asyncio.to_thread(
            ProviderFactory.create,
            provider=req.model_a_provider,
            model_id=req.model_a_model,
            api_key=req.model_a_api_key,
            base_url=req.model_a_base_url,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to create model A provider: {e}")

    try:
        provider_b = await asyncio.to_thread(
            ProviderFactory.create,
            provider=req.model_b_provider,
            model_id=req.model_b_model,
            api_key=req.model_b_api_key,
            base_url=req.model_b_base_url,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to create model B provider: {e}")

    try:
        judge_provider = await asyncio.to_thread(
            ProviderFactory.create,
            provider=req.judge_provider,
            model_id=req.judge_model,
            api_key=req.judge_api_key,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to create judge provider: {e}")

    evaluator = PairwiseJudgeEvaluator(
        judge_provider=judge_provider,
        prompt_template=req.judge_prompt_template,
    )
    runner = PairwiseEvaluationRunner(
        provider_a=provider_a,
        provider_b=provider_b,
        pairwise_evaluator=evaluator,
        concurrency_limit=req.concurrency,
    )

    import uuid
    pre_run_id = str(uuid.uuid4())
    _pairwise_run_store[pre_run_id] = {"status": "started", "error": None}

    model_a_name = getattr(provider_a, "model_name", "unknown")
    model_b_name = getattr(provider_b, "model_name", "unknown")

    async def _background_pairwise_run():
        try:
            actual_run_id = await asyncio.to_thread(
                lambda: asyncio.run(runner.run_pairwise_evaluation(req.dataset_path, examples))
            )
            _pairwise_run_store.pop(pre_run_id, None)
            _pairwise_run_store[actual_run_id] = {"status": "completed", "error": None}
        except Exception as e:
            log.error("background_pairwise_run_failed", error=str(e))
            _pairwise_run_store[pre_run_id] = {"status": "failed", "error": str(e)}

    asyncio.create_task(_background_pairwise_run())

    return PairwiseRunResponse(run_id=pre_run_id, status="started")


@app.get("/pairwise-runs/{run_id}", response_model=PairwiseRunStatusResponse)
async def get_pairwise_run(run_id: str, include_comparisons: bool = Query(default=False)):
    info = _pairwise_run_store.get(run_id)

    if info is None:
        # Check database for completed runs
        metrics = get_pairwise_run_metrics(run_id)
        if not metrics:
            raise HTTPException(status_code=404, detail=f"Pairwise run '{run_id}' not found")

        comparisons = None
        if include_comparisons:
            raw = get_pairwise_comparisons(run_id)
            comparisons = [PairwiseComparisonItem(**c) for c in raw]

        return PairwiseRunStatusResponse(
            run_id=run_id,
            model_a_name=metrics["model_a_name"],
            model_b_name=metrics["model_b_name"],
            status="completed",
            metrics=PairwiseMetrics(
                total_comparisons=metrics["total_comparisons"],
                wins_a=metrics["wins_a"],
                wins_b=metrics["wins_b"],
                ties=metrics["ties"],
                win_rate_a=metrics["win_rate_a"],
                win_rate_b=metrics["win_rate_b"],
                tie_rate=metrics["tie_rate"],
                elo_a=metrics["elo_a"],
                elo_b=metrics["elo_b"],
                avg_score_a=metrics["avg_score_a"],
                avg_score_b=metrics["avg_score_b"],
            ),
            comparisons=comparisons,
        )

    status = info["status"]

    if status == "completed":
        metrics = get_pairwise_run_metrics(run_id)
        comparisons = None
        if include_comparisons:
            raw = get_pairwise_comparisons(run_id)
            comparisons = [PairwiseComparisonItem(**c) for c in raw]

        pw_metrics = None
        if metrics:
            pw_metrics = PairwiseMetrics(
                total_comparisons=metrics["total_comparisons"],
                wins_a=metrics["wins_a"],
                wins_b=metrics["wins_b"],
                ties=metrics["ties"],
                win_rate_a=metrics["win_rate_a"],
                win_rate_b=metrics["win_rate_b"],
                tie_rate=metrics["tie_rate"],
                elo_a=metrics["elo_a"],
                elo_b=metrics["elo_b"],
                avg_score_a=metrics["avg_score_a"],
                avg_score_b=metrics["avg_score_b"],
            )

        return PairwiseRunStatusResponse(
            run_id=run_id,
            model_a_name=metrics.get("model_a_name", "unknown") if metrics else "unknown",
            model_b_name=metrics.get("model_b_name", "unknown") if metrics else "unknown",
            status=status,
            metrics=pw_metrics,
            comparisons=comparisons,
        )

    return PairwiseRunStatusResponse(
        run_id=run_id,
        model_a_name="unknown",
        model_b_name="unknown",
        status=status,
        error=info.get("error"),
    )


@app.get("/pairwise-runs", response_model=PairwiseRunsListResponse)
async def list_pairwise_runs():
    runs = [
        PairwiseRunListItem(
            run_id=rid,
            model_a_name="unknown",
            model_b_name="unknown",
            status=info["status"],
        )
        for rid, info in _pairwise_run_store.items()
    ]

    # Also include runs from the database
    from app.database.models import PairwiseRunDB
    with get_db() as db:
        db_runs = db.query(PairwiseRunDB).all()
        known_ids = {r.run_id for r in runs}
        for db_run in db_runs:
            if db_run.id not in known_ids:
                runs.append(
                    PairwiseRunListItem(
                        run_id=db_run.id,
                        model_a_name=db_run.model_a_name,
                        model_b_name=db_run.model_b_name,
                        status="completed",
                    )
                )

    return PairwiseRunsListResponse(runs=runs)
