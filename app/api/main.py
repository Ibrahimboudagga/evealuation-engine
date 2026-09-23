import asyncio
import structlog
from typing import Literal, Optional

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
from app.config import deployment_health, require_production_configuration
from app.schemas.outcomes import EvaluationOutcome, RunStatus

log = structlog.get_logger()

app = FastAPI(title="LLM Evaluation Engine", version="1.0.0")
_single_execution_worker = asyncio.Lock()

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


async def get_auth_context(
    authorization: Optional[str] = Header(default=None),
    x_workspace_id: Optional[str] = Header(default=None),
) -> Optional[AuthContext]:
    """Allow unauthenticated setup only before the first owner is bootstrapped."""
    has_users = await asyncio.to_thread(_identity_service.has_users)
    if not authorization:
        if not has_users:
            return None
        raise HTTPException(status_code=401, detail="Authorization: Bearer <workspace API token> is required")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Use Authorization: Bearer <workspace API token>")
    try:
        return await asyncio.to_thread(_identity_service.authenticate, token, x_workspace_id)
    except ValueError as error:
        raise HTTPException(status_code=401, detail=str(error))


def _require_role(context: Optional[AuthContext], allowed_roles: set[str]) -> None:
    if context is not None and context.role not in allowed_roles:
        raise HTTPException(status_code=403, detail="Your workspace role does not allow this action")


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
def startup():
    require_production_configuration()
    init_db()
    recovered = reconcile_abandoned_runs()
    if recovered["evaluation_runs"] or recovered["pairwise_runs"]:
        log.info("abandoned_runs_reconciled", **recovered)


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Safe readiness data for orchestration and agency operations."""
    health = deployment_health()
    if not await asyncio.to_thread(database_is_reachable):
        health["issues"].append("Database is not reachable.")
        health["status"] = "degraded"
    return HealthResponse(**health)


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
async def bootstrap_workspace(req: WorkspaceBootstrapRequest):
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
    _require_role(context, WRITE_ROLES | {"viewer", "client_viewer"})
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
        share, token = await asyncio.to_thread(
            _agency_service.create_share, context.workspace_id, req.run_id, req.project_id,
            req.expires_in_hours, req.branding,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    await _audit(context, "report_share.created", "report_share", share.id, share.project_id, {"run_id": share.run_id, "expires_at": share.expires_at.isoformat()})
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
    return DemoSeedResponse(**(await asyncio.to_thread(_demo_seed_service.seed, context.workspace_id if context else None)))


# ── Existing Run Endpoints ───────────────────────────────────

@app.post("/runs", response_model=RunResponse)
async def create_run(req: RunRequest, context: Optional[AuthContext] = Depends(get_auth_context)):
    _require_role(context, WRITE_ROLES)
    if context is not None and req.dataset_path:
        raise HTTPException(status_code=400, detail="Workspace runs must use a registered dataset_id.")
    if req.dataset_id:
        _require_dataset_access(req.dataset_id, context, write=True)
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

    project_id = _require_dataset_access(req.dataset_id, context).project_id if req.dataset_id else None
    await _audit(context, "run.launched", "evaluation_run", run_id, project_id, {"simulated": runner._is_simulated()})
    return RunResponse(run_id=run_id, status=RunStatus.QUEUED, is_simulated=runner._is_simulated())


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
        is_baseline=is_baseline,
    )


@app.get("/runs", response_model=RunsListResponse)
async def list_runs(baseline_only: bool = Query(default=False, description="List only marked baselines"), context: Optional[AuthContext] = Depends(get_auth_context)):
    with get_db() as db:
        query = db.query(EvaluationRunDB)
        if context is not None:
            query = query.join(ProjectDB, EvaluationRunDB.project_id == ProjectDB.id).filter(ProjectDB.workspace_id == context.workspace_id)
        if baseline_only:
            query = query.filter(EvaluationRunDB.is_baseline == True)
        db_runs = query.order_by(EvaluationRunDB.created_at.desc()).all()
        runs = [
            RunListItem(
                run_id=db_run.id,
                status=RunStatus(db_run.status),
                created_at=db_run.created_at,
                is_simulated=db_run.is_simulated,
                project_id=db_run.project_id,
                is_baseline=db_run.is_baseline,
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
    coverage_minimum: float = Query(default=0.95, ge=0.0, le=1.0),
    exact_match_pass_rate_max_drop: float = Query(default=0.05, ge=0.0, le=1.0),
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
    return ProjectsListResponse(
        projects=[_project_to_response(project) for project in _project_service.list_projects(
            context.workspace_id if context else None
        )]
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
    project = await asyncio.to_thread(
        _project_service.create_project, req.name, req.client_name, req.description, req.tags,
        context.workspace_id if context else None,
    )
    await _audit(context, "project.created", "project", project.id, project.id, {"name": project.name})
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
        tag=tag, search=search, project_id=project_id, workspace_id=context.workspace_id if context else None
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
    return _dataset_to_response(dataset)


@app.post("/datasets/{dataset_id}/versions", response_model=DatasetVersionResponse, status_code=201)
async def add_dataset_version(dataset_id: str, req: DatasetAddVersionRequest, context: Optional[AuthContext] = Depends(get_auth_context)):
    if context is not None:
        _require_dataset_access(dataset_id, context, write=True)
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
    examples = None
    if req.dataset_path:
        try:
            examples = await asyncio.to_thread(load_dataset, req.dataset_path)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to load dataset: {e}")

    async def resolve_connection(connection_id, provider, model, api_key, base_url, allow_unauthenticated, label):
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

    project_id = _require_dataset_access(req.dataset_id, context).project_id if req.dataset_id else None
    await _audit(context, "pairwise_run.launched", "pairwise_run", run_id, project_id, {"simulated": runner._is_simulated()})
    return PairwiseRunResponse(
        run_id=run_id,
        status=RunStatus.QUEUED,
        is_simulated=runner._is_simulated(),
    )


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
async def list_pairwise_runs(context: Optional[AuthContext] = Depends(get_auth_context)):
    with get_db() as db:
        query = db.query(PairwiseRunDB)
        if context is not None:
            query = query.join(ProjectDB, PairwiseRunDB.project_id == ProjectDB.id).filter(ProjectDB.workspace_id == context.workspace_id)
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
            )
            for db_run in db_runs
        ]

    return PairwiseRunsListResponse(runs=runs)
