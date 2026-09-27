import asyncio
import structlog
from typing import Literal, Optional
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, UploadFile, File, Form, Query, Request
from fastapi.responses import Response

from app.api.schemas import (
    RunRequest,
    RunResponse,
    WorkspaceBootstrapRequest,
    WorkspaceBootstrapResponse,
    WorkspaceMemberCreateRequest,
    WorkspaceMemberResponse,
    WorkspaceMemberUpdateRequest,
    ProjectAccessRequest,
    SignInRequest,
    SignInResponse,
    ChangePasswordRequest,
    ProviderConnectionCreateRequest,
    ProviderConnectionResponse,
    EvaluationTemplateCreateRequest,
    EvaluationTemplateResponse,
    TemplateLaunchRequest,
    ScheduleCreateRequest,
    ScheduleActiveRequest,
    ScheduleExecutionResponse,
    ScheduleResponse,
    WorkspaceLimitsRequest,
    WorkspaceUsageResponse,
    BillingAccountUpdateRequest,
    BillingAccountResponse,
    ActivationFunnelResponse,
    NotificationSettingsRequest,
    NotificationSettingsResponse,
    AdminConsoleResponse,
    ReportShareCreateRequest,
    ReportShareResponse,
    ProjectDashboardResponse,
    AuditEventResponse,
    AuditEventsResponse,
    RetentionSettingsRequest,
    RetentionSettingsResponse,
    RetentionApplyResponse,
    HealthResponse,
    RunStatusResponse,
    RunListItem,
    RunsListResponse,
    BaselineMarkResponse,
    RunComparisonResponse,
    DemoSeedResponse,
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
from app.database.connection import get_db, init_db, database_is_reachable
from app.database.models import DatasetDB, EvaluationRunDB, EvaluationResultDB, MembershipDB, PairwiseRunDB, ProjectAccessDB, ProjectDB
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
from app.services.baseline_service import BaselineService
from app.services.demo_seed import DemoSeedService
from app.services.project_service import ProjectService
from app.services.report_service import ReportService
from app.services.identity_service import AuthContext, IdentityService, OWNER_ROLES, WRITE_ROLES
from app.services.provider_connection_service import ProviderConnectionService
from app.services.agency_service import AgencyService
from app.services.operations_service import OperationsService
from app.services.run_recovery import reconcile_abandoned_runs
from app.services.queue_worker import QueueWorker
from app.services.schedule_service import ScheduleService
from app.services.usage_service import UsageService, WorkspaceLimitExceeded
from app.services.billing_service import BillingService
from app.services.activation_service import ActivationService
from app.services.notification_service import NotificationService
from app.config import deployment_health, require_production_configuration, get_settings
from app.schemas.outcomes import EvaluationOutcome, RunStatus

log = structlog.get_logger()

app = FastAPI(title="LLM Evaluation Engine", version="1.0.0")
_queue_worker = QueueWorker()

# Service singleton
_dataset_service = DatasetService()
_project_service = ProjectService()
_report_service = ReportService()
_baseline_service = BaselineService()
_demo_seed_service = DemoSeedService()
_identity_service = IdentityService()
_provider_connection_service = ProviderConnectionService()
_agency_service = AgencyService()
_operations_service = OperationsService()
_schedule_service = ScheduleService()
_usage_service = UsageService()
_billing_service = BillingService()
_activation_service = ActivationService()
_notification_service = NotificationService()


async def get_auth_context(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_workspace_id: Optional[str] = Header(default=None),
) -> Optional[AuthContext]:
    """Allow unauthenticated setup only before the first owner is bootstrapped."""
    has_users = await asyncio.to_thread(_identity_service.has_users)
    if not authorization:
        if not has_users and get_settings().app_environment.lower() != "production":
            return None
        raise HTTPException(status_code=401, detail="Authorization: Bearer <workspace API token> is required")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Use Authorization: Bearer <workspace API token>")
    try:
        return await asyncio.to_thread(_identity_service.authenticate, token, x_workspace_id, request.url.path == "/auth/password")
    except ValueError as error:
        raise HTTPException(status_code=401, detail=str(error))


def _require_role(context: Optional[AuthContext], allowed_roles: set[str]) -> None:
    if context is not None and context.role not in allowed_roles:
        raise HTTPException(status_code=403, detail="Your workspace role does not allow this action")


def _require_provider_connection(connection_id, provider, model, api_key=None, base_url=None):
    if connection_id:
        return
    demo = (provider or "").lower() in {"mock", "demo", "dummy"} or model == "mock"
    if not demo or (api_key and api_key != "mock") or base_url:
        raise HTTPException(status_code=400, detail="Live API runs require a workspace provider connection; raw keys and custom endpoints are not accepted on submission.")


async def _audit(
    context: Optional[AuthContext], action: str, entity_type: str, entity_id: Optional[str] = None,
    project_id: Optional[str] = None, metadata: Optional[dict] = None,
) -> None:
    """Persist a credential-safe record of a user-visible operational action."""
    if context is not None:
        await asyncio.to_thread(
            _operations_service.record, context.workspace_id, action, entity_type, entity_id,
            project_id, context, metadata,
        )


def _require_project_access(project_id: str, context: Optional[AuthContext], write: bool = False) -> ProjectDB:
    with get_db() as db:
        project = db.query(ProjectDB).filter(ProjectDB.id == project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
        if context is not None:
            if project.workspace_id != context.workspace_id:
                raise HTTPException(status_code=404, detail="Project not found in this workspace")
            if context.role == "client_viewer":
                membership = db.query(MembershipDB).filter(MembershipDB.workspace_id == context.workspace_id, MembershipDB.user_id == context.user_id).first()
                granted = membership and db.query(ProjectAccessDB).filter(ProjectAccessDB.membership_id == membership.id, ProjectAccessDB.project_id == project_id).first()
                if not granted:
                    raise HTTPException(status_code=404, detail="Project not found in this workspace")
            if write:
                _require_role(context, WRITE_ROLES)
        return project


def _require_dataset_access(dataset_id: str, context: Optional[AuthContext], write: bool = False) -> DatasetDB:
    with get_db() as db:
        dataset = db.query(DatasetDB).filter(DatasetDB.id == dataset_id).first()
        if not dataset:
            raise HTTPException(status_code=404, detail=f"Dataset '{dataset_id}' not found")
        if context is not None:
            if not dataset.project_id:
                raise HTTPException(status_code=404, detail="Dataset is not assigned to this workspace")
            _require_project_access(dataset.project_id, context, write=write)
        return dataset


def _require_run_access(run_id: str, context: Optional[AuthContext], write: bool = False) -> EvaluationRunDB:
    with get_db() as db:
        run = db.query(EvaluationRunDB).filter(EvaluationRunDB.id == run_id).first()
        if not run:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
        if context is not None:
            if not run.project_id:
                raise HTTPException(status_code=404, detail="Run is not assigned to this workspace")
            _require_project_access(run.project_id, context, write=write)
        return run


def _require_pairwise_run_access(run_id: str, context: Optional[AuthContext], write: bool = False) -> PairwiseRunDB:
    with get_db() as db:
        run = db.query(PairwiseRunDB).filter(PairwiseRunDB.id == run_id).first()
        if not run:
            raise HTTPException(status_code=404, detail=f"Pairwise run '{run_id}' not found")
        if context is not None:
            if not run.project_id:
                raise HTTPException(status_code=404, detail="Run is not assigned to this workspace")
            _require_project_access(run.project_id, context, write=write)
        return run


def _queue_position(model, run_id: str) -> Optional[int]:
    """Return a run's position among queued work of the same type."""
    with get_db() as db:
        run = db.query(model).filter(model.id == run_id).first()
        if not run or run.status != RunStatus.QUEUED.value or run.cancellation_requested_at is not None:
            return None
        ahead = db.query(model).filter(
            model.status == RunStatus.QUEUED.value,
            model.cancellation_requested_at.is_(None),
            model.created_at < run.created_at,
        ).count()
        return ahead + 1


def _connection_to_response(connection) -> ProviderConnectionResponse:
    return ProviderConnectionResponse(
        id=connection.id,
        name=connection.name,
        provider=connection.provider,
        default_model=connection.default_model,
        base_url=connection.base_url,
        allow_unauthenticated=connection.allow_unauthenticated,
        credential_configured=bool(connection.encrypted_api_key or connection.credential_reference),
        created_at=connection.created_at,
        updated_at=connection.updated_at,
    )


def _template_to_response(template) -> EvaluationTemplateResponse:
    return EvaluationTemplateResponse(
        id=template.id, name=template.name, description=template.description,
        created_at=template.created_at, updated_at=template.updated_at, **template.settings,
    )


def _schedule_to_response(schedule) -> ScheduleResponse:
    return ScheduleResponse(
        id=schedule.id,
        template_id=schedule.template_id,
        dataset_id=schedule.dataset_id,
        dataset_version_id=schedule.dataset_version_id,
        frequency=schedule.frequency,
        next_execution_at=schedule.next_execution_at,
        last_executed_at=schedule.last_executed_at,
        active=schedule.active,
        last_error=schedule.last_error,
        created_at=schedule.created_at,
        executions=[
            ScheduleExecutionResponse(
                id=execution.id,
                run_id=execution.run_id,
                scheduled_for=execution.scheduled_for,
                created_at=execution.created_at,
                status=execution.status,
                error_message=execution.error_message,
            )
            for execution in sorted(schedule.executions, key=lambda item: item.created_at, reverse=True)
        ],
    )


def _share_to_response(share, url: Optional[str] = None) -> ReportShareResponse:
    return ReportShareResponse(
        id=share.id, project_id=share.project_id, run_id=share.run_id,
        expires_at=share.expires_at, revoked_at=share.revoked_at, url=url,
    )


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
async def startup():
    require_production_configuration()
    init_db()
    recovered = reconcile_abandoned_runs()
    if recovered["evaluation_runs"] or recovered["pairwise_runs"]:
        log.info("abandoned_runs_reconciled", **recovered)
    await _queue_worker.start()


@app.on_event("shutdown")
async def shutdown():
    await _queue_worker.stop()


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Safe readiness data for orchestration and agency operations."""
    health = deployment_health()
    if not await asyncio.to_thread(database_is_reachable):
        health["issues"].append("Database is not reachable.")
        health["status"] = "degraded"
    health["worker"] = await asyncio.to_thread(_queue_worker.health)
    if health["worker"]["status"] in {"unstarted", "stale", "failed"}:
        health["issues"].append(f"Evaluation worker status is {health['worker']['status']}.")
        health["status"] = "degraded"
    if health["worker"]["overdue_schedule_count"]:
        health["issues"].append("One or more active schedules are overdue.")
        health["status"] = "degraded"
    return HealthResponse(**health)


@app.get("/health/live")
async def liveness():
    return {"status": "alive"}


@app.get("/health/ready")
async def readiness():
    try:
        health = await health_check()
    except Exception:
        raise HTTPException(status_code=503, detail="Readiness dependencies unavailable.")
    if health.status != "ok":
        raise HTTPException(status_code=503, detail="Service is not ready; inspect operator health.")
    return {"status": "ready"}


async def _owner_health() -> dict:
    """Build the same safe operational health payload for the owner console."""
    health = deployment_health()
    if not await asyncio.to_thread(database_is_reachable):
        health["issues"].append("Database is not reachable.")
        health["status"] = "degraded"
    health["worker"] = await asyncio.to_thread(_queue_worker.health)
    if health["worker"]["status"] in {"unstarted", "stale", "failed"}:
        health["issues"].append(f"Evaluation worker status is {health['worker']['status']}.")
        health["status"] = "degraded"
    if health["worker"]["overdue_schedule_count"]:
        health["issues"].append("One or more active schedules are overdue.")
        health["status"] = "degraded"
    return health


@app.get("/setup/status")
async def setup_status():
    """Public bootstrap status used only by the first-run setup wizard."""
    return {"workspace_bootstrapped": await asyncio.to_thread(_identity_service.has_users), "health": deployment_health()}


@app.get("/audit-events", response_model=AuditEventsResponse)
async def list_audit_events(
    project_id: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    context: Optional[AuthContext] = Depends(get_auth_context),
):
    if context is None:
        raise HTTPException(status_code=401, detail="Workspace authentication is required")
    _require_role(context, OWNER_ROLES | {"editor"})
    if project_id:
        _require_project_access(project_id, context)
    events = await asyncio.to_thread(_operations_service.list_events, context.workspace_id, project_id, limit)
    return AuditEventsResponse(events=[AuditEventResponse(
        id=event.id, action=event.action, entity_type=event.entity_type, entity_id=event.entity_id,
        project_id=event.project_id, actor_email=event.actor_email, metadata=event.metadata_dict,
        created_at=event.created_at,
    ) for event in events])


@app.get("/operations/retention", response_model=RetentionSettingsResponse)
async def get_retention_settings(context: Optional[AuthContext] = Depends(get_auth_context)):
    if context is None:
        raise HTTPException(status_code=401, detail="Workspace authentication is required")
    _require_role(context, OWNER_ROLES)
    return RetentionSettingsResponse(retention_days=await asyncio.to_thread(
        _operations_service.retention_days, context.workspace_id
    ))


@app.put("/operations/retention", response_model=RetentionSettingsResponse)
async def update_retention_settings(
    req: RetentionSettingsRequest, context: Optional[AuthContext] = Depends(get_auth_context)
):
    if context is None:
        raise HTTPException(status_code=401, detail="Workspace authentication is required")
    _require_role(context, OWNER_ROLES)
    days = await asyncio.to_thread(_operations_service.set_retention_days, context.workspace_id, req.retention_days)
    await _audit(context, "retention.updated", "workspace", context.workspace_id, metadata={"retention_days": days})
    return RetentionSettingsResponse(retention_days=days)


@app.post("/operations/retention/apply", response_model=RetentionApplyResponse)
async def apply_retention_settings(context: Optional[AuthContext] = Depends(get_auth_context)):
    if context is None:
        raise HTTPException(status_code=401, detail="Workspace authentication is required")
    _require_role(context, OWNER_ROLES)
    result = await asyncio.to_thread(_operations_service.apply_retention, context.workspace_id)
    await _audit(context, "retention.applied", "workspace", context.workspace_id, metadata=result)
    return RetentionApplyResponse(**result)


@app.post("/auth/bootstrap", response_model=WorkspaceBootstrapResponse, status_code=201)
async def bootstrap_workspace(req: WorkspaceBootstrapRequest, x_setup_token: Optional[str] = Header(default=None)):
    import secrets
    settings = get_settings()
    if settings.app_environment.lower() == "production" and (not settings.bootstrap_secret or
            not secrets.compare_digest(x_setup_token or "", settings.bootstrap_secret)):
        raise HTTPException(status_code=403, detail="The operator setup secret is required.")
    """Create the first local owner and claim pre-workspace project records."""
    try:
        context, token = await asyncio.to_thread(
            _identity_service.bootstrap, req.email, req.display_name, req.workspace_name, req.password
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    await _audit(context, "workspace.bootstrapped", "workspace", context.workspace_id)
    return WorkspaceBootstrapResponse(
        user_id=context.user_id, workspace_id=context.workspace_id, role="owner", api_token=token
    )


@app.post("/auth/sign-in", response_model=SignInResponse)
async def sign_in(req: SignInRequest):
    try:
        context, token, expires_at = await asyncio.to_thread(
            _identity_service.sign_in, req.email, req.password, req.workspace_id
        )
    except ValueError as error:
        raise HTTPException(status_code=401, detail=str(error))
    await _audit(context, "user.signed_in", "user", context.user_id)
    return SignInResponse(access_token=token, expires_at=expires_at, workspace_id=context.workspace_id, role=context.role)


@app.post("/auth/sign-out", status_code=204)
async def sign_out(
    authorization: Optional[str] = Header(default=None),
    context: Optional[AuthContext] = Depends(get_auth_context),
):
    if context is None or not authorization:
        raise HTTPException(status_code=401, detail="Sign in first")
    _, _, token = authorization.partition(" ")
    await asyncio.to_thread(_identity_service.sign_out, token)
    await _audit(context, "user.signed_out", "user", context.user_id)
    return Response(status_code=204)


@app.put("/auth/password", status_code=204)
async def change_password(
    req: ChangePasswordRequest, context: Optional[AuthContext] = Depends(get_auth_context)
):
    if context is None:
        raise HTTPException(status_code=401, detail="Sign in first")
    try:
        await asyncio.to_thread(_identity_service.change_password, context.user_id, req.current_password, req.new_password)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    await _audit(context, "user.password_changed", "user", context.user_id)
    return Response(status_code=204)


@app.post("/workspace/members", response_model=WorkspaceMemberResponse, status_code=201)
async def add_workspace_member(
    req: WorkspaceMemberCreateRequest,
    context: Optional[AuthContext] = Depends(get_auth_context),
):
    if context is None:
        raise HTTPException(status_code=401, detail="Bootstrap an owner before adding workspace members")
    try:
        member, token = await asyncio.to_thread(
            _identity_service.add_member, context, req.email, req.display_name, req.role, req.initial_password
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    await _audit(context, "member.added", "user", member.user_id, metadata={"role": member.role})
    return WorkspaceMemberResponse(
        user_id=member.user_id, email=member.email, workspace_id=member.workspace_id,
        role=member.role, api_token=token or None,
    )


@app.get("/workspace/members", response_model=list[WorkspaceMemberResponse])
async def list_workspace_members(context: Optional[AuthContext] = Depends(get_auth_context)):
    if context is None: raise HTTPException(status_code=401, detail="Workspace authentication is required")
    _require_role(context, OWNER_ROLES)
    members = await asyncio.to_thread(_identity_service.members, context.workspace_id)
    return [WorkspaceMemberResponse(user_id=m.user_id, email=m.user.email, workspace_id=m.workspace_id, role=m.role) for m in members]


@app.put("/workspace/members/{user_id}", status_code=204)
async def update_workspace_member(user_id: str, req: WorkspaceMemberUpdateRequest, context: Optional[AuthContext] = Depends(get_auth_context)):
    if context is None: raise HTTPException(status_code=401, detail="Workspace authentication is required")
    _require_role(context, OWNER_ROLES)
    if not await asyncio.to_thread(_identity_service.update_member_role, context.workspace_id, user_id, req.role): raise HTTPException(status_code=404, detail="Member not found")
    await _audit(context, "member.role_updated", "user", user_id, metadata={"role": req.role})
    return Response(status_code=204)


@app.delete("/workspace/members/{user_id}", status_code=204)
async def remove_workspace_member(user_id: str, context: Optional[AuthContext] = Depends(get_auth_context)):
    if context is None: raise HTTPException(status_code=401, detail="Workspace authentication is required")
    _require_role(context, OWNER_ROLES)
    if user_id == context.user_id: raise HTTPException(status_code=400, detail="Owners cannot remove themselves")
    if not await asyncio.to_thread(_identity_service.remove_member, context.workspace_id, user_id): raise HTTPException(status_code=404, detail="Member not found")
    await _audit(context, "member.removed", "user", user_id)
    return Response(status_code=204)


@app.post("/workspace/members/{user_id}/projects", status_code=204)
async def grant_member_project_access(user_id: str, req: ProjectAccessRequest, context: Optional[AuthContext] = Depends(get_auth_context)):
    if context is None: raise HTTPException(status_code=401, detail="Workspace authentication is required")
    _require_role(context, OWNER_ROLES)
    if not await asyncio.to_thread(_identity_service.grant_project_access, context.workspace_id, user_id, req.project_id): raise HTTPException(status_code=404, detail="Member or project not found")
    await _audit(context, "member.project_granted", "user", user_id, req.project_id)
    return Response(status_code=204)


@app.get("/provider-connections", response_model=list[ProviderConnectionResponse])
async def list_provider_connections(context: Optional[AuthContext] = Depends(get_auth_context)):
    if context is None:
        raise HTTPException(status_code=401, detail="Bootstrap an owner before configuring provider connections")
    _require_role(context, WRITE_ROLES | {"viewer"})
    connections = await asyncio.to_thread(_provider_connection_service.list, context.workspace_id)
    return [_connection_to_response(connection) for connection in connections]


@app.post("/provider-connections", response_model=ProviderConnectionResponse, status_code=201)
async def create_provider_connection(
    req: ProviderConnectionCreateRequest,
    context: Optional[AuthContext] = Depends(get_auth_context),
):
    if context is None:
        raise HTTPException(status_code=401, detail="Bootstrap an owner before configuring provider connections")
    _require_role(context, OWNER_ROLES)
    try:
        connection = await asyncio.to_thread(
            _provider_connection_service.create,
            context.workspace_id, req.name, req.provider, req.default_model, req.api_key,
            req.credential_reference, req.base_url, req.allow_unauthenticated,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    await _audit(context, "provider_connection.created", "provider_connection", connection.id, metadata={"provider": connection.provider, "name": connection.name})
    return _connection_to_response(connection)


@app.delete("/provider-connections/{connection_id}", status_code=204)
async def delete_provider_connection(
    connection_id: str,
    context: Optional[AuthContext] = Depends(get_auth_context),
):
    if context is None:
        raise HTTPException(status_code=401, detail="Bootstrap an owner before configuring provider connections")
    _require_role(context, OWNER_ROLES)
    deleted = await asyncio.to_thread(_provider_connection_service.delete, context.workspace_id, connection_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Provider connection not found")
    return Response(status_code=204)


@app.get("/evaluation-templates", response_model=list[EvaluationTemplateResponse])
async def list_evaluation_templates(context: Optional[AuthContext] = Depends(get_auth_context)):
    _require_role(context, WRITE_ROLES | {"viewer"})
    if context is None:
        raise HTTPException(status_code=401, detail="Workspace authentication is required")
    templates = await asyncio.to_thread(_agency_service.list_templates, context.workspace_id)
    return [_template_to_response(template) for template in templates]


@app.post("/evaluation-templates", response_model=EvaluationTemplateResponse, status_code=201)
async def create_evaluation_template(
    req: EvaluationTemplateCreateRequest,
    context: Optional[AuthContext] = Depends(get_auth_context),
):
    if context is None:
        raise HTTPException(status_code=401, detail="Workspace authentication is required")
    _require_role(context, WRITE_ROLES)
    for connection_id in (req.candidate_connection_id, req.evaluator_connection_id):
        try:
            await asyncio.to_thread(_provider_connection_service.resolve, context.workspace_id, connection_id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error))
    try:
        template = await asyncio.to_thread(_agency_service.create_template, context.workspace_id, req.model_dump())
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error))
    await _audit(context, "template.created", "evaluation_template", template.id, metadata={"name": template.name})
    await asyncio.to_thread(_activation_service.record_first, context.workspace_id, "first_template")
    return _template_to_response(template)


@app.delete("/evaluation-templates/{template_id}", status_code=204)
async def delete_evaluation_template(template_id: str, context: Optional[AuthContext] = Depends(get_auth_context)):
    if context is None:
        raise HTTPException(status_code=401, detail="Workspace authentication is required")
    _require_role(context, WRITE_ROLES)
    if not await asyncio.to_thread(_agency_service.delete_template, context.workspace_id, template_id):
        raise HTTPException(status_code=404, detail="Evaluation template not found")
    await _audit(context, "template.deleted", "evaluation_template", template_id)
    return Response(status_code=204)


@app.post("/evaluation-templates/{template_id}/launch", response_model=RunResponse)
async def launch_evaluation_template(
    template_id: str,
    req: TemplateLaunchRequest,
    context: Optional[AuthContext] = Depends(get_auth_context),
):
    if context is None:
        raise HTTPException(status_code=401, detail="Workspace authentication is required")
    _require_role(context, WRITE_ROLES)
    template = await asyncio.to_thread(_agency_service.get_template, context.workspace_id, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Evaluation template not found")
    payload = {
        **template.settings,
        "dataset_id": req.dataset_id,
        "dataset_version_id": req.dataset_version_id,
        "release_rules": {
            "coverage_minimum": template.settings.get("coverage_minimum"),
            "exact_match_pass_rate_max_drop": template.settings.get("exact_match_pass_rate_max_drop"),
        },
    }
    await _audit(context, "template.launched", "evaluation_template", template_id, metadata={"dataset_id": req.dataset_id})
    return await create_run(RunRequest(**payload), context)


@app.get("/schedules", response_model=list[ScheduleResponse])
async def list_schedules(context: Optional[AuthContext] = Depends(get_auth_context)):
    if context is None:
        raise HTTPException(status_code=401, detail="Workspace authentication is required")
    _require_role(context, OWNER_ROLES | {"editor"})
    schedules = await asyncio.to_thread(_schedule_service.list, context.workspace_id)
    return [_schedule_to_response(schedule) for schedule in schedules]


@app.post("/schedules", response_model=ScheduleResponse, status_code=201)
async def create_schedule(req: ScheduleCreateRequest, context: Optional[AuthContext] = Depends(get_auth_context)):
    if context is None:
        raise HTTPException(status_code=401, detail="Workspace authentication is required")
    _require_role(context, WRITE_ROLES)
    _require_dataset_access(req.dataset_id, context, write=True)
    try:
        schedule = await asyncio.to_thread(
            _schedule_service.create,
            context.workspace_id,
            req.template_id,
            req.dataset_id,
            req.dataset_version_id,
            req.frequency,
            req.next_execution_at,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    await _audit(
        context,
        "schedule.created",
        "evaluation_schedule",
        schedule.id,
        metadata={"template_id": schedule.template_id, "dataset_version_id": schedule.dataset_version_id, "frequency": schedule.frequency},
    )
    await asyncio.to_thread(_activation_service.record_first, context.workspace_id, "first_recurring_schedule")
    return _schedule_to_response(schedule)


@app.put("/schedules/{schedule_id}", response_model=ScheduleResponse)
async def set_schedule_active(
    schedule_id: str,
    req: ScheduleActiveRequest,
    context: Optional[AuthContext] = Depends(get_auth_context),
):
    if context is None:
        raise HTTPException(status_code=401, detail="Workspace authentication is required")
    _require_role(context, WRITE_ROLES)
    schedule = await asyncio.to_thread(_schedule_service.set_active, context.workspace_id, schedule_id, req.active)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    await _audit(context, "schedule.updated", "evaluation_schedule", schedule.id, metadata={"active": schedule.active})
    return _schedule_to_response(schedule)


@app.get("/workspace/usage", response_model=WorkspaceUsageResponse)
async def get_workspace_usage(context: Optional[AuthContext] = Depends(get_auth_context)):
    if context is None:
        raise HTTPException(status_code=401, detail="Workspace authentication is required")
    _require_role(context, OWNER_ROLES | {"editor"})
    # Reading the account page refreshes the aggregate record for the current
    # billing month, so pilots have a durable monthly snapshot without a job.
    return WorkspaceUsageResponse(**(await asyncio.to_thread(_usage_service.snapshot, context.workspace_id)))


@app.post("/workspace/usage/snapshot", response_model=WorkspaceUsageResponse)
async def snapshot_workspace_usage(context: Optional[AuthContext] = Depends(get_auth_context)):
    if context is None:
        raise HTTPException(status_code=401, detail="Workspace authentication is required")
    _require_role(context, OWNER_ROLES)
    snapshot = await asyncio.to_thread(_usage_service.snapshot, context.workspace_id)
    await _audit(context, "usage.snapshot_created", "workspace", context.workspace_id)
    return WorkspaceUsageResponse(**snapshot)


@app.put("/workspace/limits", response_model=WorkspaceUsageResponse)
async def update_workspace_limits(
    req: WorkspaceLimitsRequest,
    context: Optional[AuthContext] = Depends(get_auth_context),
):
    if context is None:
        raise HTTPException(status_code=401, detail="Workspace authentication is required")
    _require_role(context, OWNER_ROLES)
    try:
        await asyncio.to_thread(_usage_service.set_limits, context.workspace_id, req.limits)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    await _audit(context, "workspace.limits_updated", "workspace", context.workspace_id, metadata={"limits": req.limits})
    return WorkspaceUsageResponse(**(await asyncio.to_thread(_usage_service.overview, context.workspace_id)))


@app.get("/workspace/billing", response_model=BillingAccountResponse)
async def get_billing_account(context: Optional[AuthContext] = Depends(get_auth_context)):
    if context is None:
        raise HTTPException(status_code=401, detail="Workspace authentication is required")
    _require_role(context, OWNER_ROLES)
    try:
        return BillingAccountResponse(**(await asyncio.to_thread(_billing_service.account, context.workspace_id)))
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))


@app.put("/workspace/billing", response_model=BillingAccountResponse)
async def update_billing_account(
    req: BillingAccountUpdateRequest,
    context: Optional[AuthContext] = Depends(get_auth_context),
):
    if context is None:
        raise HTTPException(status_code=401, detail="Workspace authentication is required")
    _require_role(context, OWNER_ROLES)
    try:
        account = await asyncio.to_thread(
            _billing_service.update_account,
            context.workspace_id,
            plan=req.plan,
            billing_status=req.billing_status,
            trial_ends_at=req.trial_ends_at,
            invoice_contact_email=req.invoice_contact_email,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    await _audit(
        context,
        "workspace.billing_updated",
        "workspace",
        context.workspace_id,
        metadata={"plan": account["plan"], "billing_status": account["billing_status"], "trial_ends_at": account["trial_ends_at"].isoformat() if account["trial_ends_at"] else None},
    )
    return BillingAccountResponse(**account)


@app.get("/workspace/activation", response_model=ActivationFunnelResponse)
async def get_activation_funnel(context: Optional[AuthContext] = Depends(get_auth_context)):
    if context is None:
        raise HTTPException(status_code=401, detail="Workspace authentication is required")
    _require_role(context, OWNER_ROLES)
    return ActivationFunnelResponse(**(await asyncio.to_thread(_activation_service.funnel, context.workspace_id)))


@app.get("/workspace/notifications", response_model=NotificationSettingsResponse)
async def get_notification_settings(context: Optional[AuthContext] = Depends(get_auth_context)):
    if context is None:
        raise HTTPException(status_code=401, detail="Workspace authentication is required")
    _require_role(context, OWNER_ROLES)
    return NotificationSettingsResponse(**(await asyncio.to_thread(_notification_service.get, context.workspace_id)))


@app.put("/workspace/notifications", response_model=NotificationSettingsResponse)
async def update_notification_settings(
    req: NotificationSettingsRequest, context: Optional[AuthContext] = Depends(get_auth_context)
):
    if context is None:
        raise HTTPException(status_code=401, detail="Workspace authentication is required")
    _require_role(context, OWNER_ROLES)
    try:
        settings = await asyncio.to_thread(_notification_service.update, context.workspace_id, req.model_dump())
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    await _audit(
        context, "workspace.notifications_updated", "workspace", context.workspace_id,
        metadata={"enabled": settings["enabled"], "recipient_count": len(settings["recipients"]), "events": settings["events"]},
    )
    return NotificationSettingsResponse(**settings)


@app.get("/workspace/admin-console", response_model=AdminConsoleResponse)
async def get_admin_console(context: Optional[AuthContext] = Depends(get_auth_context)):
    """Return the owner account overview without exposing provider credentials."""
    if context is None:
        raise HTTPException(status_code=401, detail="Workspace authentication is required")
    _require_role(context, OWNER_ROLES)
    members, projects, templates, connections, usage, billing, notifications, events, health = await asyncio.gather(
        asyncio.to_thread(_identity_service.members, context.workspace_id),
        asyncio.to_thread(_project_service.list_projects, context.workspace_id),
        asyncio.to_thread(_agency_service.list_templates, context.workspace_id),
        asyncio.to_thread(_provider_connection_service.list, context.workspace_id),
        asyncio.to_thread(_usage_service.overview, context.workspace_id),
        asyncio.to_thread(_billing_service.account, context.workspace_id),
        asyncio.to_thread(_notification_service.get, context.workspace_id),
        asyncio.to_thread(_operations_service.list_events, context.workspace_id, None, 50),
        _owner_health(),
    )
    return AdminConsoleResponse(
        members=[WorkspaceMemberResponse(user_id=m.user_id, email=m.user.email, workspace_id=m.workspace_id, role=m.role) for m in members],
        projects=[_project_to_response(project).model_dump() for project in projects],
        templates=[_template_to_response(template).model_dump() for template in templates],
        provider_connections=[_connection_to_response(connection) for connection in connections],
        usage=WorkspaceUsageResponse(**usage),
        billing=BillingAccountResponse(**billing),
        notifications=NotificationSettingsResponse(**notifications),
        health=HealthResponse(**health),
        audit_events=[AuditEventResponse(
            id=event.id, action=event.action, entity_type=event.entity_type, entity_id=event.entity_id,
            project_id=event.project_id, actor_email=event.actor_email, metadata=event.metadata_dict,
            created_at=event.created_at,
        ) for event in events],
    )


@app.post("/report-shares", response_model=ReportShareResponse, status_code=201)
async def create_report_share(
    req: ReportShareCreateRequest,
    request: Request,
    context: Optional[AuthContext] = Depends(get_auth_context),
):
    if context is None:
        raise HTTPException(status_code=401, detail="Workspace authentication is required")
    _require_role(context, WRITE_ROLES)
    try:
        await asyncio.to_thread(_usage_service.assert_capacity, context.workspace_id, report_shares=1)
        share, token = await asyncio.to_thread(
            _agency_service.create_share, context.workspace_id, req.run_id, req.project_id,
            req.expires_in_hours, req.branding,
        )
    except WorkspaceLimitExceeded as error:
        raise HTTPException(status_code=429, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    await _audit(context, "report_share.created", "report_share", share.id, share.project_id, {"run_id": share.run_id, "expires_at": share.expires_at.isoformat()})
    await asyncio.to_thread(_activation_service.record_first, context.workspace_id, "first_shared_report")
    return _share_to_response(share, f"{str(request.base_url).rstrip('/')}/shared-reports/{token}")


@app.delete("/report-shares/{share_id}", status_code=204)
async def revoke_report_share(share_id: str, context: Optional[AuthContext] = Depends(get_auth_context)):
    if context is None:
        raise HTTPException(status_code=401, detail="Workspace authentication is required")
    _require_role(context, WRITE_ROLES)
    if not await asyncio.to_thread(_agency_service.revoke_share, context.workspace_id, share_id):
        raise HTTPException(status_code=404, detail="Report share not found")
    await _audit(context, "report_share.revoked", "report_share", share_id)
    return Response(status_code=204)


@app.get("/shared-reports/{token}")
async def get_shared_report(token: str):
    """Public, read-only HTML. The random token is the sole access capability."""
    try:
        report, _ = await asyncio.to_thread(_agency_service.public_report, token)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    return Response(content=_report_service.to_html(report), media_type="text/html")


@app.post("/demo/seed", response_model=DemoSeedResponse)
async def seed_agency_demo(context: Optional[AuthContext] = Depends(get_auth_context)):
    """Create an idempotent mock-only demo workspace with sample datasets."""
    _require_role(context, WRITE_ROLES)
    seeded = await asyncio.to_thread(_demo_seed_service.seed, context.workspace_id if context else None)
    if context is not None:
        await _audit(context, "demo.seeded", "workspace", context.workspace_id, seeded["project_id"])
        for milestone in ("demo_seeded", "first_project", "first_dataset"):
            await asyncio.to_thread(_activation_service.record_first, context.workspace_id, milestone)
    return DemoSeedResponse(**seeded)


# ── Existing Run Endpoints ───────────────────────────────────

@app.post("/runs", response_model=RunResponse)
async def create_run(req: RunRequest, context: Optional[AuthContext] = Depends(get_auth_context)):
    _require_provider_connection(req.candidate_connection_id, req.candidate_provider, req.candidate_model, req.candidate_api_key, req.candidate_base_url)
    _require_provider_connection(req.evaluator_connection_id, req.evaluator_provider, req.evaluator_model, req.evaluator_api_key)
    _require_role(context, WRITE_ROLES)
    if context is not None and req.dataset_path:
        raise HTTPException(status_code=400, detail="Workspace runs must use a registered dataset_id.")
    if req.dataset_id:
        _require_dataset_access(req.dataset_id, context, write=True)
        if context is not None:
            try:
                await asyncio.to_thread(
                    _usage_service.assert_run_capacity,
                    context.workspace_id,
                    req.dataset_id,
                    req.dataset_version_id,
                    2,
                )
            except WorkspaceLimitExceeded as error:
                raise HTTPException(status_code=429, detail=str(error))
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error))
    examples = None
    if req.dataset_path:
        try:
            examples = await asyncio.to_thread(load_dataset, req.dataset_path)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to load dataset: {e}")

    candidate_provider_name = req.candidate_provider
    candidate_model_name = req.candidate_model
    candidate_api_key = req.candidate_api_key
    candidate_base_url = req.candidate_base_url
    candidate_allow_unauthenticated = req.candidate_allow_unauthenticated
    if req.candidate_connection_id:
        if context is None:
            raise HTTPException(status_code=401, detail="Provider connections require workspace authentication")
        try:
            connection = await asyncio.to_thread(
                _provider_connection_service.resolve, context.workspace_id, req.candidate_connection_id
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error))
        candidate_provider_name = connection.provider
        candidate_model_name = req.candidate_model or connection.default_model
        candidate_api_key = connection.api_key
        candidate_base_url = connection.base_url
        candidate_allow_unauthenticated = connection.allow_unauthenticated
    elif context is not None and req.candidate_api_key:
        raise HTTPException(status_code=400, detail="Store candidate credentials in a workspace provider connection.")

    evaluator_provider_name = req.evaluator_provider
    evaluator_model_name = req.evaluator_model
    evaluator_api_key = req.evaluator_api_key
    evaluator_base_url = None
    evaluator_allow_unauthenticated = False
    if req.evaluator_connection_id:
        if context is None:
            raise HTTPException(status_code=401, detail="Provider connections require workspace authentication")
        try:
            connection = await asyncio.to_thread(
                _provider_connection_service.resolve, context.workspace_id, req.evaluator_connection_id
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error))
        evaluator_provider_name = connection.provider
        evaluator_model_name = req.evaluator_model or connection.default_model
        evaluator_api_key = connection.api_key
        evaluator_base_url = connection.base_url
        evaluator_allow_unauthenticated = connection.allow_unauthenticated
    elif context is not None and req.evaluator_api_key:
        raise HTTPException(status_code=400, detail="Store judge credentials in a workspace provider connection.")

    try:
        candidate_provider = await asyncio.to_thread(
            ProviderFactory.create,
            use_default_api_key=False,
            provider=candidate_provider_name,
            model_id=candidate_model_name,
            api_key=candidate_api_key,
            base_url=candidate_base_url,
            allow_unauthenticated=candidate_allow_unauthenticated,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to create candidate provider: {e}")

    try:
        evaluator_provider = await asyncio.to_thread(
            ProviderFactory.create,
            use_default_api_key=False,
            provider=evaluator_provider_name,
            model_id=evaluator_model_name,
            api_key=evaluator_api_key,
            base_url=evaluator_base_url,
            allow_unauthenticated=evaluator_allow_unauthenticated,
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
        execution_timeout_seconds=req.timeout_seconds,
        requested_configuration={
            "candidate": {
                "provider": candidate_provider_name,
                "model": candidate_model_name,
                "connection_id": req.candidate_connection_id,
                "base_url": candidate_base_url,
                "allow_unauthenticated": candidate_allow_unauthenticated,
            },
            "judge": {
                "provider": evaluator_provider_name,
                "model": evaluator_model_name,
                "connection_id": req.evaluator_connection_id,
                "base_url": evaluator_base_url,
                "allow_unauthenticated": evaluator_allow_unauthenticated,
            },
            "judge_prompt_template": req.judge_prompt_template,
            "template_execution": {"timeout_seconds": req.timeout_seconds},
            "release_rules": req.release_rules,
            "report_preferences": req.report_preferences,
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

    project_id = _require_dataset_access(req.dataset_id, context).project_id if req.dataset_id else None
    await _audit(context, "run.launched", "evaluation_run", run_id, project_id, {"simulated": runner._is_simulated()})
    return RunResponse(run_id=run_id, status=RunStatus.QUEUED, is_simulated=runner._is_simulated())


@app.post("/runs/{run_id}/cancel", response_model=RunStatusResponse)
async def cancel_run(run_id: str, context: Optional[AuthContext] = Depends(get_auth_context)):
    run = _require_run_access(run_id, context, write=True)
    with get_db() as db:
        record = db.query(EvaluationRunDB).filter(EvaluationRunDB.id == run_id).first()
        if record.status not in {RunStatus.QUEUED.value, RunStatus.RUNNING.value}:
            raise HTTPException(status_code=409, detail="Only queued or running runs can be cancelled")
        record.cancellation_requested_at = datetime.now(timezone.utc)
        if record.status == RunStatus.QUEUED.value:
            record.status = RunStatus.INTERRUPTED.value
            record.completed_at = datetime.now(timezone.utc)
        db.commit()
    await _audit(context, "run.cancellation_requested", "evaluation_run", run_id, run.project_id)
    return await get_run(run_id, context)


@app.post("/runs/{run_id}/retry", response_model=RunStatusResponse)
@app.post("/runs/{run_id}/retry-now", response_model=RunStatusResponse, deprecated=True)
async def retry_run_now(run_id: str, context: Optional[AuthContext] = Depends(get_auth_context)):
    run = _require_run_access(run_id, context, write=True)
    retry_id = await asyncio.to_thread(_queue_worker.retry_now, "evaluation_run", run_id)
    if not retry_id:
        raise HTTPException(status_code=409, detail="Only queued, failed, or interrupted runs can be retried")
    await _audit(context, "run.retry_requested", "evaluation_run", run_id, run.project_id)
    return await get_run(retry_id, context)


@app.get("/runs/{run_id}", response_model=RunStatusResponse)
async def get_run(run_id: str, context: Optional[AuthContext] = Depends(get_auth_context)):
    _require_run_access(run_id, context)
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
        is_baseline = run.is_baseline
        attempt_count = run.attempt_count
        max_attempts = run.max_attempts
        next_attempt_at = run.next_attempt_at
        cancellation_requested_at = run.cancellation_requested_at
        worker_id = run.worker_id
        last_transient_error = run.last_transient_error

    evaluator_metrics = None
    if status in {RunStatus.RUNNING, RunStatus.COMPLETED, RunStatus.INTERRUPTED}:
        metrics = get_run_metrics(run_id)
        evaluator_metrics = [
            EvaluatorMetric(
                evaluator=name,
                unverified_cases=data.get("unverified_cases", 0),
                denominator_verified=data.get("denominator_verified", False),
                backends=data.get("backends", []),
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
        parent_run_id=run.parent_run_id,
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
        is_baseline=is_baseline,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        next_attempt_at=next_attempt_at,
        cancellation_requested_at=cancellation_requested_at,
        worker_id=worker_id,
        last_transient_error=last_transient_error,
        queue_position=_queue_position(EvaluationRunDB, run_id),
    )


@app.get("/runs", response_model=RunsListResponse)
async def list_runs(baseline_only: bool = Query(default=False, description="List only marked baselines"), status: Optional[RunStatus] = Query(default=None), context: Optional[AuthContext] = Depends(get_auth_context)):
    with get_db() as db:
        query = db.query(EvaluationRunDB)
        if context is not None:
            query = query.filter(EvaluationRunDB.project_id.in_(_identity_service.accessible_project_ids(context)))
        if baseline_only:
            query = query.filter(EvaluationRunDB.is_baseline == True)
        if status:
            query = query.filter(EvaluationRunDB.status == status.value)
        db_runs = query.order_by(EvaluationRunDB.created_at.desc()).all()
        runs = [
            RunListItem(
                run_id=db_run.id,
                status=RunStatus(db_run.status),
                created_at=db_run.created_at,
                is_simulated=db_run.is_simulated,
                project_id=db_run.project_id,
                is_baseline=db_run.is_baseline,
                attempt_count=db_run.attempt_count,
                max_attempts=db_run.max_attempts,
                next_attempt_at=db_run.next_attempt_at,
                cancellation_requested_at=db_run.cancellation_requested_at,
                queue_position=_queue_position(EvaluationRunDB, db_run.id),
            )
            for db_run in db_runs
        ]

    return RunsListResponse(runs=runs)


@app.put("/runs/{run_id}/baseline", response_model=BaselineMarkResponse)
async def mark_run_as_baseline(run_id: str, context: Optional[AuthContext] = Depends(get_auth_context)):
    _require_run_access(run_id, context, write=True)
    try:
        run = await asyncio.to_thread(_baseline_service.mark_baseline, run_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    await _audit(context, "baseline.updated", "evaluation_run", run.id, run.project_id, {"is_baseline": run.is_baseline})
    return BaselineMarkResponse(run_id=run.id, is_baseline=run.is_baseline, status=RunStatus(run.status))


@app.get("/runs/{run_id}/comparison", response_model=RunComparisonResponse)
async def compare_run_to_baseline(
    run_id: str,
    baseline_run_id: str = Query(..., description="Completed run previously marked as a baseline"),
    coverage_minimum: Optional[float] = Query(default=None, ge=0.0, le=1.0),
    exact_match_pass_rate_max_drop: Optional[float] = Query(default=None, ge=0.0, le=1.0),
    context: Optional[AuthContext] = Depends(get_auth_context),
):
    _require_run_access(run_id, context)
    _require_run_access(baseline_run_id, context)
    try:
        comparison = await asyncio.to_thread(
            _baseline_service.compare,
            run_id,
            baseline_run_id,
            coverage_minimum,
            exact_match_pass_rate_max_drop,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return RunComparisonResponse(**comparison)


@app.get("/runs/{run_id}/results", response_model=RunResultsResponse)
async def list_run_results(
    run_id: str,
    evaluator: Optional[str] = Query(default=None, description="Filter by evaluator name"),
    outcome: Optional[list[EvaluationOutcome]] = Query(default=None, description="Filter by one or more outcomes"),
    score_min: Optional[float] = Query(default=None, ge=0.0, le=1.0, description="Minimum completed score"),
    score_max: Optional[float] = Query(default=None, ge=0.0, le=1.0, description="Maximum completed score"),
    context: Optional[AuthContext] = Depends(get_auth_context),
):
    """Return reviewable persisted results without exposing raw provider errors."""
    if score_min is not None and score_max is not None and score_min > score_max:
        raise HTTPException(status_code=422, detail="score_min must be less than or equal to score_max")

    _require_run_access(run_id, context)
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
    context: Optional[AuthContext] = Depends(get_auth_context),
):
    """Download a client-ready JSON, CSV, or HTML evaluation report."""
    _require_run_access(run_id, context)
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

    run = _require_run_access(run_id, context)
    await _audit(context, "report.exported", "evaluation_run", run_id, run.project_id, {"format": report_format})
    filename = _report_service.filename(run_id, report_format)
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Project Endpoints ────────────────────────────────────────

@app.get("/projects", response_model=ProjectsListResponse)
async def list_projects(context: Optional[AuthContext] = Depends(get_auth_context)):
    allowed = set(_identity_service.accessible_project_ids(context)) if context else None
    return ProjectsListResponse(
        projects=[_project_to_response(project) for project in _project_service.list_projects(
            context.workspace_id if context else None
        ) if allowed is None or project.id in allowed]
    )


@app.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: str, context: Optional[AuthContext] = Depends(get_auth_context)):
    project = _require_project_access(project_id, context)
    return _project_to_response(project)


@app.get("/projects/{project_id}/dashboard", response_model=ProjectDashboardResponse)
async def get_project_dashboard(project_id: str, context: Optional[AuthContext] = Depends(get_auth_context)):
    if context is None:
        raise HTTPException(status_code=401, detail="Workspace authentication is required")
    _require_project_access(project_id, context)
    try:
        return ProjectDashboardResponse(**(await asyncio.to_thread(
            _agency_service.project_dashboard, context.workspace_id, project_id
        )))
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))


@app.post("/projects", response_model=ProjectResponse, status_code=201)
async def create_project(req: ProjectCreateRequest, context: Optional[AuthContext] = Depends(get_auth_context)):
    _require_role(context, WRITE_ROLES)
    if context is not None:
        try:
            await asyncio.to_thread(_usage_service.assert_capacity, context.workspace_id, active_projects=1)
        except WorkspaceLimitExceeded as error:
            raise HTTPException(status_code=429, detail=str(error))
    project = await asyncio.to_thread(
        _project_service.create_project, req.name, req.client_name, req.description, req.tags,
        context.workspace_id if context else None,
    )
    await _audit(context, "project.created", "project", project.id, project.id, {"name": project.name})
    if context is not None:
        await asyncio.to_thread(_activation_service.record_first, context.workspace_id, "first_project")
    return _project_to_response(project)


@app.put("/projects/{project_id}", response_model=ProjectResponse)
async def update_project(project_id: str, req: ProjectUpdateRequest, context: Optional[AuthContext] = Depends(get_auth_context)):
    _require_project_access(project_id, context, write=True)
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
    await _audit(context, "project.updated", "project", project.id, project.id)
    return _project_to_response(project)


@app.delete("/projects/{project_id}", response_model=ProjectDeleteResponse)
async def delete_project(project_id: str, context: Optional[AuthContext] = Depends(get_auth_context)):
    _require_project_access(project_id, context, write=True)
    deleted = await asyncio.to_thread(_project_service.delete_project, project_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    await _audit(context, "project.deleted", "project", project_id, project_id)
    return ProjectDeleteResponse(message="Project deleted; datasets and runs were unassigned.", id=project_id)


# ── Dataset Endpoints ────────────────────────────────────────

@app.get("/datasets", response_model=DatasetsListResponse)
async def list_datasets(
    tag: Optional[str] = Query(default=None, description="Filter by tag"),
    search: Optional[str] = Query(default=None, description="Search by name"),
    project_id: Optional[str] = Query(default=None, description="Filter by project"),
    context: Optional[AuthContext] = Depends(get_auth_context),
):
    if project_id:
        _require_project_access(project_id, context)
    datasets = _dataset_service.list_datasets(
        tag=tag, search=search, project_id=project_id, workspace_id=context.workspace_id if context else None,
        allowed_project_ids=_identity_service.accessible_project_ids(context) if context else None,
    )
    return DatasetsListResponse(
        datasets=[_dataset_to_response(d) for d in datasets]
    )


@app.get("/datasets/{dataset_id}", response_model=DatasetDetailResponse)
async def get_dataset(dataset_id: str, context: Optional[AuthContext] = Depends(get_auth_context)):
    _require_dataset_access(dataset_id, context)
    dataset = _dataset_service.get_dataset(dataset_id)
    return _dataset_to_detail(dataset)


@app.post("/datasets", response_model=DatasetResponse, status_code=201)
async def create_dataset(req: DatasetCreateRequest, context: Optional[AuthContext] = Depends(get_auth_context)):
    _require_role(context, WRITE_ROLES)
    if context is not None:
        if not req.project_id:
            raise HTTPException(status_code=400, detail="Workspace datasets must be assigned to a project.")
        _require_project_access(req.project_id, context, write=True)
        try:
            await asyncio.to_thread(_usage_service.assert_capacity, context.workspace_id, storage_bytes=len(req.content.encode("utf-8")))
        except WorkspaceLimitExceeded as error:
            raise HTTPException(status_code=429, detail=str(error))
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
    await _audit(context, "dataset.created", "dataset", dataset.id, dataset.project_id, {"name": dataset.name})
    if context is not None:
        await asyncio.to_thread(_activation_service.record_first, context.workspace_id, "first_dataset")
    return _dataset_to_response(dataset)


@app.post("/datasets/upload", response_model=DatasetResponse, status_code=201)
async def upload_dataset(
    file: UploadFile = File(..., description="JSONL dataset file"),
    name: str = Form(..., description="Dataset name"),
    description: Optional[str] = Form(default=None, description="Dataset description"),
    tags: Optional[str] = Form(default=None, description="Comma-separated tags"),
    project_id: Optional[str] = Form(default=None, description="Project that owns this dataset"),
    context: Optional[AuthContext] = Depends(get_auth_context),
):
    _require_role(context, WRITE_ROLES)
    if context is not None:
        if not project_id:
            raise HTTPException(status_code=400, detail="Workspace datasets must be assigned to a project.")
        _require_project_access(project_id, context, write=True)
    file_content = await file.read()
    if context is not None:
        try:
            await asyncio.to_thread(_usage_service.assert_capacity, context.workspace_id, storage_bytes=len(file_content))
        except WorkspaceLimitExceeded as error:
            raise HTTPException(status_code=429, detail=str(error))
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
    await _audit(context, "dataset.uploaded", "dataset", dataset.id, dataset.project_id, {"name": dataset.name})
    if context is not None:
        await asyncio.to_thread(_activation_service.record_first, context.workspace_id, "first_dataset")
    return _dataset_to_response(dataset)


@app.post("/datasets/{dataset_id}/versions", response_model=DatasetVersionResponse, status_code=201)
async def add_dataset_version(dataset_id: str, req: DatasetAddVersionRequest, context: Optional[AuthContext] = Depends(get_auth_context)):
    if context is not None:
        _require_dataset_access(dataset_id, context, write=True)
        try:
            await asyncio.to_thread(_usage_service.assert_capacity, context.workspace_id, storage_bytes=len(req.content.encode("utf-8")))
        except WorkspaceLimitExceeded as error:
            raise HTTPException(status_code=429, detail=str(error))
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
async def set_active_version(dataset_id: str, req: DatasetSetActiveVersionRequest, context: Optional[AuthContext] = Depends(get_auth_context)):
    if context is not None:
        _require_dataset_access(dataset_id, context, write=True)
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
async def delete_dataset(dataset_id: str, context: Optional[AuthContext] = Depends(get_auth_context)):
    if context is not None:
        _require_dataset_access(dataset_id, context, write=True)
    deleted = await asyncio.to_thread(_dataset_service.delete_dataset, dataset_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Dataset '{dataset_id}' not found")
    return DatasetDeleteResponse(message="Dataset deleted successfully", id=dataset_id)


# ── Pairwise Run Endpoints ──────────────────────────────────

@app.post("/pairwise-runs", response_model=PairwiseRunResponse)
async def create_pairwise_run(req: PairwiseRunRequest, context: Optional[AuthContext] = Depends(get_auth_context)):
    _require_role(context, WRITE_ROLES)
    if context is not None and req.dataset_path:
        raise HTTPException(status_code=400, detail="Workspace runs must use a registered dataset_id.")
    if req.dataset_id:
        _require_dataset_access(req.dataset_id, context, write=True)
        if context is not None:
            try:
                await asyncio.to_thread(
                    _usage_service.assert_run_capacity,
                    context.workspace_id,
                    req.dataset_id,
                    req.dataset_version_id,
                    3,
                )
            except WorkspaceLimitExceeded as error:
                raise HTTPException(status_code=429, detail=str(error))
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error))
    examples = None
    if req.dataset_path:
        try:
            examples = await asyncio.to_thread(load_dataset, req.dataset_path)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to load dataset: {e}")

    async def resolve_connection(connection_id, provider, model, api_key, base_url, allow_unauthenticated, label):
        _require_provider_connection(connection_id, provider, model, api_key, base_url)
        if connection_id:
            if context is None:
                raise HTTPException(status_code=401, detail="Provider connections require workspace authentication")
            try:
                connection = await asyncio.to_thread(
                    _provider_connection_service.resolve, context.workspace_id, connection_id
                )
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error))
            return connection.provider, model or connection.default_model, connection.api_key, connection.base_url, connection.allow_unauthenticated
        if context is not None and api_key:
            raise HTTPException(status_code=400, detail=f"Store {label} credentials in a workspace provider connection.")
        return provider, model, api_key, base_url, allow_unauthenticated

    model_a_provider, model_a_model, model_a_api_key, model_a_base_url, model_a_allow_unauthenticated = await resolve_connection(
        req.model_a_connection_id, req.model_a_provider, req.model_a_model, req.model_a_api_key,
        req.model_a_base_url, req.model_a_allow_unauthenticated, "model A"
    )
    model_b_provider, model_b_model, model_b_api_key, model_b_base_url, model_b_allow_unauthenticated = await resolve_connection(
        req.model_b_connection_id, req.model_b_provider, req.model_b_model, req.model_b_api_key,
        req.model_b_base_url, req.model_b_allow_unauthenticated, "model B"
    )
    judge_provider_name, judge_model_name, judge_api_key, judge_base_url, judge_allow_unauthenticated = await resolve_connection(
        req.judge_connection_id, req.judge_provider, req.judge_model, req.judge_api_key,
        None, False, "judge"
    )

    try:
        provider_a = await asyncio.to_thread(
            ProviderFactory.create,
            use_default_api_key=False,
            provider=model_a_provider,
            model_id=model_a_model,
            api_key=model_a_api_key,
            base_url=model_a_base_url,
            allow_unauthenticated=model_a_allow_unauthenticated,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to create model A provider: {e}")

    try:
        provider_b = await asyncio.to_thread(
            ProviderFactory.create,
            use_default_api_key=False,
            provider=model_b_provider,
            model_id=model_b_model,
            api_key=model_b_api_key,
            base_url=model_b_base_url,
            allow_unauthenticated=model_b_allow_unauthenticated,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to create model B provider: {e}")

    try:
        judge_provider = await asyncio.to_thread(
            ProviderFactory.create,
            use_default_api_key=False,
            provider=judge_provider_name,
            model_id=judge_model_name,
            api_key=judge_api_key,
            base_url=judge_base_url,
            allow_unauthenticated=judge_allow_unauthenticated,
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
                "provider": model_a_provider,
                "model": model_a_model,
                "connection_id": req.model_a_connection_id,
                "base_url": model_a_base_url,
                "allow_unauthenticated": model_a_allow_unauthenticated,
            },
            "model_b": {
                "provider": model_b_provider,
                "model": model_b_model,
                "connection_id": req.model_b_connection_id,
                "base_url": model_b_base_url,
                "allow_unauthenticated": model_b_allow_unauthenticated,
            },
            "judge": {
                "provider": judge_provider_name,
                "model": judge_model_name,
                "connection_id": req.judge_connection_id,
                "base_url": judge_base_url,
                "allow_unauthenticated": judge_allow_unauthenticated,
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

    project_id = _require_dataset_access(req.dataset_id, context).project_id if req.dataset_id else None
    await _audit(context, "pairwise_run.launched", "pairwise_run", run_id, project_id, {"simulated": runner._is_simulated()})
    return PairwiseRunResponse(
        run_id=run_id,
        status=RunStatus.QUEUED,
        is_simulated=runner._is_simulated(),
    )


@app.post("/pairwise-runs/{run_id}/cancel", response_model=PairwiseRunStatusResponse)
async def cancel_pairwise_run(run_id: str, context: Optional[AuthContext] = Depends(get_auth_context)):
    run = _require_pairwise_run_access(run_id, context, write=True)
    with get_db() as db:
        record = db.query(PairwiseRunDB).filter(PairwiseRunDB.id == run_id).first()
        if record.status not in {RunStatus.QUEUED.value, RunStatus.RUNNING.value}:
            raise HTTPException(status_code=409, detail="Only queued or running runs can be cancelled")
        record.cancellation_requested_at = datetime.now(timezone.utc)
        if record.status == RunStatus.QUEUED.value:
            record.status = RunStatus.INTERRUPTED.value
            record.completed_at = datetime.now(timezone.utc)
        db.commit()
    await _audit(context, "pairwise_run.cancellation_requested", "pairwise_run", run_id, run.project_id)
    return await get_pairwise_run(run_id, context=context)


@app.post("/pairwise-runs/{run_id}/retry", response_model=PairwiseRunStatusResponse)
@app.post("/pairwise-runs/{run_id}/retry-now", response_model=PairwiseRunStatusResponse, deprecated=True)
async def retry_pairwise_run_now(run_id: str, context: Optional[AuthContext] = Depends(get_auth_context)):
    run = _require_pairwise_run_access(run_id, context, write=True)
    retry_id = await asyncio.to_thread(_queue_worker.retry_now, "pairwise_run", run_id)
    if not retry_id:
        raise HTTPException(status_code=409, detail="Only queued, failed, or interrupted runs can be retried")
    await _audit(context, "pairwise_run.retry_requested", "pairwise_run", run_id, run.project_id)
    return await get_pairwise_run(retry_id, context=context)


@app.get("/pairwise-runs/{run_id}", response_model=PairwiseRunStatusResponse)
async def get_pairwise_run(run_id: str, include_comparisons: bool = Query(default=False), context: Optional[AuthContext] = Depends(get_auth_context)):
    _require_pairwise_run_access(run_id, context)
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
        attempt_count = run.attempt_count
        max_attempts = run.max_attempts
        next_attempt_at = run.next_attempt_at
        cancellation_requested_at = run.cancellation_requested_at
        worker_id = run.worker_id
        last_transient_error = run.last_transient_error

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
        parent_run_id=run.parent_run_id,
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
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        next_attempt_at=next_attempt_at,
        cancellation_requested_at=cancellation_requested_at,
        worker_id=worker_id,
        last_transient_error=last_transient_error,
        queue_position=_queue_position(PairwiseRunDB, run_id),
    )


@app.get("/pairwise-runs", response_model=PairwiseRunsListResponse)
async def list_pairwise_runs(status: Optional[RunStatus] = Query(default=None), context: Optional[AuthContext] = Depends(get_auth_context)):
    with get_db() as db:
        query = db.query(PairwiseRunDB)
        if context is not None:
            query = query.filter(PairwiseRunDB.project_id.in_(_identity_service.accessible_project_ids(context)))
        if status:
            query = query.filter(PairwiseRunDB.status == status.value)
        db_runs = query.order_by(PairwiseRunDB.created_at.desc()).all()
        runs = [
            PairwiseRunListItem(
                run_id=db_run.id,
                model_a_name=db_run.model_a_name,
                model_b_name=db_run.model_b_name,
                status=RunStatus(db_run.status),
                created_at=db_run.created_at,
                is_simulated=db_run.is_simulated,
                project_id=db_run.project_id,
                attempt_count=db_run.attempt_count,
                max_attempts=db_run.max_attempts,
                next_attempt_at=db_run.next_attempt_at,
                cancellation_requested_at=db_run.cancellation_requested_at,
                queue_position=_queue_position(PairwiseRunDB, db_run.id),
            )
            for db_run in db_runs
        ]

    return PairwiseRunsListResponse(runs=runs)
