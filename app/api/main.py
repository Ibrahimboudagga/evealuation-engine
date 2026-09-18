import asyncio
import structlog
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import Response

from app.api.schemas import (
    RunRequest,
    RunResponse,
    RunStatusResponse,
    RunListItem,
    RunsListResponse,
    EvaluationResultReviewItem,
    RunResultsResponse,
    EvaluatorMetric,
    DatasetResponse,
    DatasetDetailResponse,
    DatasetCreateRequest,
    DatasetDeleteResponse,
    DatasetsListResponse,
    DatasetVersionResponse,
    DatasetAddVersionRequest,
    DatasetSetActiveVersionRequest,
    ProjectCreateRequest,
    ProjectUpdateRequest,
    ProjectResponse,
    ProjectsListResponse,
    ProjectDeleteResponse,
    PairwiseRunRequest,
    PairwiseRunResponse,
    PairwiseRunStatusResponse,
    PairwiseRunListItem,
    PairwiseRunsListResponse,
    PairwiseMetrics,
    PairwiseComparisonItem,
)
from app.database.connection import get_db, init_db
from app.database.models import DatasetDB, EvaluationRunDB, EvaluationResultDB, PairwiseRunDB
from app.errors import sanitize_error
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
from app.services.project_service import ProjectService
from app.services.report_service import ReportService
from app.services.run_recovery import reconcile_abandoned_runs
from app.schemas.outcomes import EvaluationOutcome, RunStatus

log = structlog.get_logger()

app = FastAPI(title="LLM Evaluation Engine", version="1.0.0")
_single_execution_worker = asyncio.Lock()

# Service singleton
_dataset_service = DatasetService()
_project_service = ProjectService()
_report_service = ReportService()


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
        project_id=dataset.project_id,
        project_name=dataset.project.name if dataset.project else None,
        client_name=dataset.project.client_name if dataset.project else None,
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
        project_id=base.project_id,
        project_name=base.project_name,
        client_name=base.client_name,
        latest_version_number=base.latest_version_number,
        created_at=base.created_at,
        updated_at=base.updated_at,
        active_version=base.active_version,
        versions=versions,
    )


def _project_to_response(project) -> ProjectResponse:
    return ProjectResponse(
        id=project.id,
        name=project.name,
        client_name=project.client_name,
        description=project.description,
        tags=project.tags,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


@app.on_event("startup")
def startup():
    init_db()
    recovered = reconcile_abandoned_runs()
    if recovered["evaluation_runs"] or recovered["pairwise_runs"]:
        log.info("abandoned_runs_reconciled", **recovered)


# ── Existing Run Endpoints ───────────────────────────────────

@app.post("/runs", response_model=RunResponse)
async def create_run(req: RunRequest):
    examples = None
    if req.dataset_path:
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
            allow_unauthenticated=req.candidate_allow_unauthenticated,
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
        requested_configuration={
            "candidate": {
                "provider": req.candidate_provider,
                "model": req.candidate_model,
                "base_url": req.candidate_base_url,
                "allow_unauthenticated": req.candidate_allow_unauthenticated,
            },
            "judge": {
                "provider": req.evaluator_provider,
                "model": req.evaluator_model,
            },
            "judge_prompt_template": req.judge_prompt_template,
        },
    )

    try:
        run_id = await asyncio.to_thread(
            runner.create_run,
            req.dataset_path,
            req.dataset_id,
            req.dataset_version_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    async def _background_run():
        try:
            async with _single_execution_worker:
                await asyncio.to_thread(
                    lambda: asyncio.run(
                        runner.run_evaluation(
                            dataset_path=req.dataset_path,
                            examples=examples,
                            dataset_id=req.dataset_id,
                            dataset_version_id=req.dataset_version_id,
                            run_id=run_id,
                        )
                    )
                )
        except Exception as e:
            log.error("background_run_failed", error=str(e))
            await asyncio.to_thread(runner._mark_run_failed, run_id, e)

    asyncio.create_task(_background_run())

    return RunResponse(run_id=run_id, status=RunStatus.QUEUED, is_simulated=runner._is_simulated())


@app.get("/runs/{run_id}", response_model=RunStatusResponse)
async def get_run(run_id: str):
    with get_db() as db:
        run = db.query(EvaluationRunDB).filter(EvaluationRunDB.id == run_id).first()
        if not run:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
        status = RunStatus(run.status)
        created_at = run.created_at
        started_at = run.started_at
        completed_at = run.completed_at
        error_message = run.error_message
        is_simulated = run.is_simulated
        run_configuration = run.run_configuration
        configuration_verified = run.configuration_verified
        project_id = run.project_id

    evaluator_metrics = None
    if status in {RunStatus.RUNNING, RunStatus.COMPLETED, RunStatus.INTERRUPTED}:
        metrics = get_run_metrics(run_id)
        evaluator_metrics = [
            EvaluatorMetric(
                evaluator=name,
                total_cases=data["total_cases"],
                valid_evaluations=data["valid_evaluations"],
                generation_errors=data["generation_errors"],
                evaluation_errors=data["evaluation_errors"],
                error_count=data["error_count"],
                passing_evaluations=data["passing_evaluations"],
                evaluation_coverage=data["evaluation_coverage"],
                mean_score=data["avg_score"],
                pass_rate=data["pass_rate"],
            )
            for name, data in metrics.get("evaluators", {}).items()
        ]

    return RunStatusResponse(
        run_id=run_id,
        status=status,
        metrics=evaluator_metrics,
        error=error_message,
        created_at=created_at,
        started_at=started_at,
        completed_at=completed_at,
        is_simulated=is_simulated,
        run_configuration=run_configuration,
        configuration_verified=configuration_verified,
        project_id=project_id,
    )


@app.get("/runs", response_model=RunsListResponse)
async def list_runs():
    with get_db() as db:
        db_runs = db.query(EvaluationRunDB).order_by(EvaluationRunDB.created_at.desc()).all()
        runs = [
            RunListItem(
                run_id=db_run.id,
                status=RunStatus(db_run.status),
                created_at=db_run.created_at,
                is_simulated=db_run.is_simulated,
                project_id=db_run.project_id,
            )
            for db_run in db_runs
        ]

    return RunsListResponse(runs=runs)


@app.get("/runs/{run_id}/results", response_model=RunResultsResponse)
async def list_run_results(
    run_id: str,
    evaluator: Optional[str] = Query(default=None, description="Filter by evaluator name"),
    outcome: Optional[list[EvaluationOutcome]] = Query(default=None, description="Filter by one or more outcomes"),
    score_min: Optional[float] = Query(default=None, ge=0.0, le=1.0, description="Minimum completed score"),
    score_max: Optional[float] = Query(default=None, ge=0.0, le=1.0, description="Maximum completed score"),
):
    """Return reviewable persisted results without exposing raw provider errors."""
    if score_min is not None and score_max is not None and score_min > score_max:
        raise HTTPException(status_code=422, detail="score_min must be less than or equal to score_max")

    with get_db() as db:
        if not db.query(EvaluationRunDB.id).filter(EvaluationRunDB.id == run_id).first():
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

        base_query = db.query(EvaluationResultDB).filter(EvaluationResultDB.run_id == run_id)
        total_count = base_query.count()
        available_evaluators = [
            name
            for (name,) in base_query.with_entities(EvaluationResultDB.evaluator_name)
            .distinct()
            .order_by(EvaluationResultDB.evaluator_name)
            .all()
        ]
        query = base_query
        if evaluator:
            query = query.filter(EvaluationResultDB.evaluator_name == evaluator)
        if outcome:
            query = query.filter(EvaluationResultDB.outcome.in_([item.value for item in outcome]))
        if score_min is not None:
            query = query.filter(EvaluationResultDB.score >= score_min)
        if score_max is not None:
            query = query.filter(EvaluationResultDB.score <= score_max)
        db_results = query.order_by(EvaluationResultDB.id).all()

        results = []
        for result in db_results:
            metadata = result.metadata_dict
            reason = metadata.get("reason")
            results.append(
                EvaluationResultReviewItem(
                    id=result.id,
                    example_id=result.example_id,
                    evaluator_name=result.evaluator_name,
                    outcome=EvaluationOutcome(result.outcome),
                    score=result.score,
                    prompt=result.prompt,
                    prediction=result.prediction,
                    expected_output=result.expected_output,
                    judge_explanation=str(reason) if reason is not None else None,
                    error_message=sanitize_error(result.error_message) if result.error_message else None,
                )
            )

    return RunResultsResponse(
        run_id=run_id,
        total_count=total_count,
        filtered_count=len(results),
        available_evaluators=available_evaluators,
        results=results,
    )


@app.get("/runs/{run_id}/export")
async def export_run_report(
    run_id: str,
    report_format: Literal["json", "csv", "html"] = Query(default="json", alias="format"),
):
    """Download a client-ready JSON, CSV, or HTML evaluation report."""
    try:
        report = await asyncio.to_thread(_report_service.build_run_report, run_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))

    if report_format == "csv":
        content = _report_service.to_csv(report)
        media_type = "text/csv"
    elif report_format == "html":
        content = _report_service.to_html(report)
        media_type = "text/html"
    else:
        content = _report_service.to_json(report)
        media_type = "application/json"

    filename = _report_service.filename(run_id, report_format)
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Project Endpoints ────────────────────────────────────────

@app.get("/projects", response_model=ProjectsListResponse)
async def list_projects():
    return ProjectsListResponse(
        projects=[_project_to_response(project) for project in _project_service.list_projects()]
    )


@app.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: str):
    project = _project_service.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return _project_to_response(project)


@app.post("/projects", response_model=ProjectResponse, status_code=201)
async def create_project(req: ProjectCreateRequest):
    project = await asyncio.to_thread(
        _project_service.create_project, req.name, req.client_name, req.description, req.tags
    )
    return _project_to_response(project)


@app.put("/projects/{project_id}", response_model=ProjectResponse)
async def update_project(project_id: str, req: ProjectUpdateRequest):
    project = await asyncio.to_thread(
        _project_service.update_project,
        project_id,
        name=req.name,
        client_name=req.client_name,
        description=req.description,
        tags=req.tags,
    )
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return _project_to_response(project)


@app.delete("/projects/{project_id}", response_model=ProjectDeleteResponse)
async def delete_project(project_id: str):
    deleted = await asyncio.to_thread(_project_service.delete_project, project_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return ProjectDeleteResponse(message="Project deleted; datasets and runs were unassigned.", id=project_id)


# ── Dataset Endpoints ────────────────────────────────────────

@app.get("/datasets", response_model=DatasetsListResponse)
async def list_datasets(
    tag: Optional[str] = Query(default=None, description="Filter by tag"),
    search: Optional[str] = Query(default=None, description="Search by name"),
    project_id: Optional[str] = Query(default=None, description="Filter by project"),
):
    datasets = _dataset_service.list_datasets(tag=tag, search=search, project_id=project_id)
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
            project_id=req.project_id,
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
    project_id: Optional[str] = Form(default=None, description="Project that owns this dataset"),
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
            project_id=project_id,
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
    examples = None
    if req.dataset_path:
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
            allow_unauthenticated=req.model_a_allow_unauthenticated,
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
            allow_unauthenticated=req.model_b_allow_unauthenticated,
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
        requested_configuration={
            "model_a": {
                "provider": req.model_a_provider,
                "model": req.model_a_model,
                "base_url": req.model_a_base_url,
                "allow_unauthenticated": req.model_a_allow_unauthenticated,
            },
            "model_b": {
                "provider": req.model_b_provider,
                "model": req.model_b_model,
                "base_url": req.model_b_base_url,
                "allow_unauthenticated": req.model_b_allow_unauthenticated,
            },
            "judge": {
                "provider": req.judge_provider,
                "model": req.judge_model,
            },
            "judge_prompt_template": req.judge_prompt_template,
        },
    )

    try:
        run_id = await asyncio.to_thread(
            runner.create_run,
            req.dataset_path,
            req.dataset_id,
            req.dataset_version_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    async def _background_pairwise_run():
        try:
            async with _single_execution_worker:
                await asyncio.to_thread(
                    lambda: asyncio.run(
                        runner.run_pairwise_evaluation(
                            dataset_path=req.dataset_path,
                            examples=examples,
                            dataset_id=req.dataset_id,
                            dataset_version_id=req.dataset_version_id,
                            run_id=run_id,
                        )
                    )
                )
        except Exception as e:
            log.error("background_pairwise_run_failed", error=str(e))
            await asyncio.to_thread(runner._mark_run_failed, run_id, e)

    asyncio.create_task(_background_pairwise_run())

    return PairwiseRunResponse(
        run_id=run_id,
        status=RunStatus.QUEUED,
        is_simulated=runner._is_simulated(),
    )


@app.get("/pairwise-runs/{run_id}", response_model=PairwiseRunStatusResponse)
async def get_pairwise_run(run_id: str, include_comparisons: bool = Query(default=False)):
    with get_db() as db:
        run = db.query(PairwiseRunDB).filter(PairwiseRunDB.id == run_id).first()
        if not run:
            raise HTTPException(status_code=404, detail=f"Pairwise run '{run_id}' not found")
        status = RunStatus(run.status)
        model_a_name = run.model_a_name
        model_b_name = run.model_b_name
        created_at = run.created_at
        started_at = run.started_at
        completed_at = run.completed_at
        error_message = run.error_message
        is_simulated = run.is_simulated
        run_configuration = run.run_configuration
        configuration_verified = run.configuration_verified
        project_id = run.project_id

    pw_metrics = None
    comparisons = None
    if status in {RunStatus.RUNNING, RunStatus.COMPLETED, RunStatus.INTERRUPTED}:
        metrics = get_pairwise_run_metrics(run_id)
        if include_comparisons:
            raw = get_pairwise_comparisons(run_id)
            comparisons = [PairwiseComparisonItem(**c) for c in raw]
        if metrics:
            pw_metrics = PairwiseMetrics(
                total_comparisons=metrics["total_comparisons"],
                valid_comparisons=metrics["valid_comparisons"],
                generation_errors=metrics["generation_errors"],
                evaluation_errors=metrics["evaluation_errors"],
                error_count=metrics["error_count"],
                evaluation_coverage=metrics["evaluation_coverage"],
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
        model_a_name=model_a_name,
        model_b_name=model_b_name,
        status=status,
        metrics=pw_metrics,
        comparisons=comparisons,
        error=error_message,
        created_at=created_at,
        started_at=started_at,
        completed_at=completed_at,
        is_simulated=is_simulated,
        run_configuration=run_configuration,
        configuration_verified=configuration_verified,
        project_id=project_id,
    )


@app.get("/pairwise-runs", response_model=PairwiseRunsListResponse)
async def list_pairwise_runs():
    with get_db() as db:
        db_runs = db.query(PairwiseRunDB).order_by(PairwiseRunDB.created_at.desc()).all()
        runs = [
            PairwiseRunListItem(
                run_id=db_run.id,
                model_a_name=db_run.model_a_name,
                model_b_name=db_run.model_b_name,
                status=RunStatus(db_run.status),
                created_at=db_run.created_at,
                is_simulated=db_run.is_simulated,
                project_id=db_run.project_id,
            )
            for db_run in db_runs
        ]

    return PairwiseRunsListResponse(runs=runs)
