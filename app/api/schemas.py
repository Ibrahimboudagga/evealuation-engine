from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, model_validator

from app.schemas.outcomes import EvaluationOutcome, RunStatus


# ── Run Schemas ──────────────────────────────────────────────

class RunRequest(BaseModel):
    """Request body for triggering a new evaluation run."""
    dataset_id: Optional[str] = Field(default=None, description="Registered dataset ID")
    dataset_version_id: Optional[str] = Field(default=None, description="Immutable dataset version ID; defaults to the active version")
    dataset_path: Optional[str] = Field(default=None, description="Legacy server-side JSONL path")
    candidate_connection_id: Optional[str] = Field(default=None, description="Workspace provider connection for candidate generation")
    candidate_provider: Optional[str] = Field(default=None, description="Candidate provider name (e.g. openai, anthropic, cohere)")
    candidate_model: Optional[str] = Field(default=None, description="Candidate model ID / identifier")
    candidate_api_key: Optional[str] = Field(default=None, description="Authentication key for the candidate provider")
    candidate_base_url: Optional[str] = Field(default=None, description="Custom API base URL for the candidate provider")
    candidate_allow_unauthenticated: bool = Field(
        default=False,
        description="Allow a keyless request only for an explicit compatible endpoint with a base URL",
    )
    evaluator_connection_id: Optional[str] = Field(default=None, description="Workspace provider connection for judging")
    evaluator_provider: Optional[str] = Field(default=None, description="Evaluator/judge provider name")
    evaluator_model: Optional[str] = Field(default=None, description="Evaluator/judge model ID / identifier")
    evaluator_api_key: Optional[str] = Field(default=None, description="Authentication key for the evaluator model")
    concurrency: int = Field(default=5, ge=1, le=50, description="Maximum parallel evaluations")
    timeout_seconds: float = Field(default=60.0, gt=0, le=600, description="Per-provider call timeout")
    judge_prompt_template: Optional[str] = Field(default=None, description="Optional custom judge prompt template text")
    release_rules: Optional[Dict[str, Any]] = Field(default=None, description="Saved template release thresholds")
    report_preferences: Optional[Dict[str, Any]] = Field(default=None, description="Saved template report preferences")

    @model_validator(mode="after")
    def validate_dataset_source(self) -> "RunRequest":
        if bool(self.dataset_id) == bool(self.dataset_path):
            raise ValueError("Provide exactly one of dataset_id or dataset_path.")
        if self.dataset_version_id and not self.dataset_id:
            raise ValueError("dataset_version_id requires dataset_id.")
        if not self.candidate_connection_id and (not self.candidate_provider or not self.candidate_model):
            raise ValueError("Provide candidate_connection_id or both candidate_provider and candidate_model.")
        if not self.evaluator_connection_id and (not self.evaluator_provider or not self.evaluator_model):
            raise ValueError("Provide evaluator_connection_id or both evaluator_provider and evaluator_model.")
        return self


class RunResponse(BaseModel):
    """Response returned immediately after starting a run."""
    run_id: str = Field(..., description="Unique ID of the evaluation run")
    status: RunStatus = Field(default=RunStatus.QUEUED, description="Current lifecycle status")
    is_simulated: bool = Field(default=False, description="Whether any provider returned simulated output")


# ── Workspace access and provider connection schemas ────────

class WorkspaceBootstrapRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=255)
    display_name: str = Field(..., min_length=1, max_length=255)
    workspace_name: str = Field(..., min_length=1, max_length=255)
    password: Optional[str] = Field(default=None, min_length=12, max_length=255)


class WorkspaceBootstrapResponse(BaseModel):
    user_id: str
    workspace_id: str
    role: Literal["owner"]
    api_token: str = Field(..., description="Shown once; store it in a secret manager")


class WorkspaceMemberCreateRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=255)
    display_name: str = Field(..., min_length=1, max_length=255)
    role: Literal["owner", "editor", "viewer", "client_viewer"]
    initial_password: Optional[str] = Field(default=None, min_length=12, max_length=255)


class SignInRequest(BaseModel):
    email: str
    password: str
    workspace_id: Optional[str] = None


class SignInResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_at: datetime
    workspace_id: str
    role: Literal["owner", "editor", "viewer", "client_viewer"]


class ChangePasswordRequest(BaseModel):
    current_password: Optional[str] = None
    new_password: str = Field(..., min_length=12, max_length=255)


class WorkspaceMemberResponse(BaseModel):
    user_id: str
    email: str
    workspace_id: str
    role: Literal["owner", "editor", "viewer", "client_viewer"]
    api_token: Optional[str] = Field(default=None, description="Only returned for a newly created local user")


class WorkspaceMemberUpdateRequest(BaseModel):
    role: Literal["owner", "editor", "viewer", "client_viewer"]


class ProjectAccessRequest(BaseModel):
    project_id: str


class ProviderConnectionCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    provider: str = Field(..., min_length=1, max_length=100)
    default_model: str = Field(..., min_length=1, max_length=255)
    api_key: Optional[str] = Field(default=None, description="Encrypted before it is persisted")
    credential_reference: Optional[str] = Field(default=None, max_length=500)
    base_url: Optional[str] = None
    allow_unauthenticated: bool = False


class ProviderConnectionResponse(BaseModel):
    id: str
    name: str
    provider: str
    default_model: str
    base_url: Optional[str] = None
    allow_unauthenticated: bool
    credential_configured: bool
    created_at: datetime
    updated_at: datetime


class EvaluationTemplateCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    candidate_connection_id: str
    candidate_model: Optional[str] = None
    evaluator_connection_id: str
    evaluator_model: Optional[str] = None
    judge_prompt_template: Optional[str] = None
    concurrency: int = Field(default=5, ge=1, le=50)
    timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    coverage_minimum: float = Field(default=0.95, ge=0, le=1)
    exact_match_pass_rate_max_drop: float = Field(default=0.05, ge=0, le=1)
    report_preferences: Dict[str, Any] = Field(default_factory=dict)


class EvaluationTemplateResponse(EvaluationTemplateCreateRequest):
    id: str
    created_at: datetime
    updated_at: datetime


class TemplateLaunchRequest(BaseModel):
    dataset_id: str
    dataset_version_id: Optional[str] = None


class ReportShareCreateRequest(BaseModel):
    run_id: Optional[str] = None
    project_id: Optional[str] = None
    expires_in_hours: int = Field(default=168, ge=1, le=8760)
    branding: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def one_scope(self):
        if bool(self.run_id) == bool(self.project_id):
            raise ValueError("Provide exactly one of run_id or project_id.")
        return self


class ReportShareResponse(BaseModel):
    id: str
    project_id: Optional[str]
    run_id: Optional[str]
    expires_at: datetime
    revoked_at: Optional[datetime]
    url: Optional[str] = Field(default=None, description="Shown only when the share is created")


class ProjectDashboardResponse(BaseModel):
    project_id: str
    client_name: str
    project_name: str
    latest_run: Optional[Dict[str, Any]] = None
    baseline_run_id: Optional[str] = None
    release_check: Optional[Dict[str, Any]] = None
    coverage_trend: List[Dict[str, Any]] = Field(default_factory=list)
    quality_trend: List[Dict[str, Any]] = Field(default_factory=list)
    recent_failures: List[Dict[str, Any]] = Field(default_factory=list)


class AuditEventResponse(BaseModel):
    id: str
    action: str
    entity_type: str
    entity_id: Optional[str] = None
    project_id: Optional[str] = None
    actor_email: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class AuditEventsResponse(BaseModel):
    events: List[AuditEventResponse] = Field(default_factory=list)


class RetentionSettingsRequest(BaseModel):
    retention_days: int = Field(..., ge=30, le=3650)


class RetentionSettingsResponse(BaseModel):
    retention_days: int


class RetentionApplyResponse(BaseModel):
    expired_share_links_deleted: int
    audit_events_deleted: int


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    environment: str
    database_backend: str
    issues: List[str] = Field(default_factory=list)
    backup_guidance_url: str


class EvaluatorMetric(BaseModel):
    """Aggregated metric for a single evaluator within a run."""
    evaluator: str = Field(..., description="Name of the evaluator")
    total_cases: int = Field(..., description="All expected cases for this evaluator")
    valid_evaluations: int = Field(..., description="Cases with a completed numeric evaluation")
    generation_errors: int = Field(..., description="Cases where candidate generation failed")
    evaluation_errors: int = Field(..., description="Cases where evaluation or judging failed")
    error_count: int = Field(..., description="Generation and evaluation errors combined")
    passing_evaluations: int = Field(..., description="Valid evaluations with score >= 0.5")
    evaluation_coverage: float = Field(..., description="Valid evaluations divided by total cases")
    mean_score: Optional[float] = Field(default=None, description="Average score over valid evaluations only")
    pass_rate: Optional[float] = Field(default=None, description="Passing valid evaluations divided by valid evaluations")


class RunStatusResponse(BaseModel):
    """Full status and metrics for a completed (or running) run."""
    run_id: str
    status: RunStatus
    metrics: Optional[List[EvaluatorMetric]] = None
    error: Optional[str] = None
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    is_simulated: bool = False
    run_configuration: Optional[Dict[str, Any]] = None
    configuration_verified: bool = False
    project_id: Optional[str] = None
    is_baseline: bool = False
    attempt_count: int = 0
    max_attempts: int = 3
    next_attempt_at: Optional[datetime] = None
    cancellation_requested_at: Optional[datetime] = None
    worker_id: Optional[str] = None
    last_transient_error: Optional[str] = None
    queue_position: Optional[int] = None


class RunListItem(BaseModel):
    """Summary entry for listing all runs."""
    run_id: str
    status: RunStatus
    created_at: Optional[datetime] = None
    is_simulated: bool = False
    project_id: Optional[str] = None
    is_baseline: bool = False
    attempt_count: int = 0
    max_attempts: int = 3
    next_attempt_at: Optional[datetime] = None
    cancellation_requested_at: Optional[datetime] = None
    queue_position: Optional[int] = None


class RunsListResponse(BaseModel):
    """Response for listing all runs."""
    runs: List[RunListItem]


class BaselineMarkResponse(BaseModel):
    run_id: str
    is_baseline: bool
    status: RunStatus


class EvaluatorBaselineComparison(BaseModel):
    evaluator: str
    baseline_average_score: Optional[float] = None
    current_average_score: Optional[float] = None
    average_score_delta: Optional[float] = None
    baseline_pass_rate: Optional[float] = None
    current_pass_rate: Optional[float] = None
    pass_rate_delta: Optional[float] = None
    baseline_coverage: Optional[float] = None
    current_coverage: Optional[float] = None
    coverage_delta: Optional[float] = None
    baseline_error_count: Optional[int] = None
    current_error_count: Optional[int] = None
    error_count_delta: Optional[int] = None


class RunComparisonResponse(BaseModel):
    run_id: str
    baseline_run_id: str
    status: Literal["passed", "regressed", "inconclusive"]
    rules: Dict[str, Optional[float]]
    reasons: List[str] = Field(default_factory=list)
    comparisons: List[EvaluatorBaselineComparison] = Field(default_factory=list)


class DemoSeedResponse(BaseModel):
    project_id: str
    project_name: str
    client_name: str
    dataset_ids: List[str]
    message: str


class EvaluationResultReviewItem(BaseModel):
    """A persisted evaluator result with the evidence needed for review."""
    id: int
    example_id: str
    evaluator_name: str
    outcome: EvaluationOutcome
    score: Optional[float] = None
    prompt: str
    prediction: str
    expected_output: str
    judge_explanation: Optional[str] = None
    error_message: Optional[str] = None


class RunResultsResponse(BaseModel):
    """Filtered, per-example results for a single-model evaluation run."""
    run_id: str
    total_count: int
    filtered_count: int
    available_evaluators: List[str] = Field(default_factory=list)
    results: List[EvaluationResultReviewItem]


# ── Dataset Schemas ──────────────────────────────────────────

class DatasetVersionResponse(BaseModel):
    """A single dataset version."""
    id: str = Field(..., description="Version ID")
    version_number: int = Field(..., description="Sequential version number")
    example_count: int = Field(..., description="Number of examples in this version")
    is_active: bool = Field(..., description="Whether this is the current active version")
    created_at: datetime = Field(..., description="Version creation timestamp")


class ProjectCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="Product or evaluation project name")
    client_name: str = Field(..., min_length=1, max_length=255, description="Agency client name")
    description: Optional[str] = Field(default=None, description="Project scope or notes")
    tags: Optional[List[str]] = Field(default=None, description="Project tags")


class ProjectUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    client_name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    tags: Optional[List[str]] = None


class ProjectResponse(BaseModel):
    id: str
    name: str
    client_name: str
    description: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ProjectsListResponse(BaseModel):
    projects: List[ProjectResponse]


class ProjectDeleteResponse(BaseModel):
    message: str
    id: str


class DatasetResponse(BaseModel):
    """Full dataset record."""
    id: str = Field(..., description="Dataset UUID")
    name: str = Field(..., description="Dataset name")
    description: Optional[str] = Field(default=None, description="Dataset description")
    tags: List[str] = Field(default_factory=list, description="Dataset tags")
    latest_version_number: int = Field(..., description="Latest version number")
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")
    project_id: Optional[str] = None
    project_name: Optional[str] = None
    client_name: Optional[str] = None
    active_version: Optional[DatasetVersionResponse] = Field(default=None, description="Currently active version")


class DatasetDetailResponse(BaseModel):
    """Full dataset details including version history."""
    id: str
    name: str
    description: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    project_id: Optional[str] = None
    project_name: Optional[str] = None
    client_name: Optional[str] = None
    latest_version_number: int
    created_at: datetime
    updated_at: datetime
    active_version: Optional[DatasetVersionResponse] = None
    versions: List[DatasetVersionResponse] = Field(default_factory=list)


class DatasetCreateRequest(BaseModel):
    """Request body for creating a dataset from JSONL content."""
    name: str = Field(..., min_length=1, max_length=255, description="Dataset name")
    description: Optional[str] = Field(default=None, description="Dataset description")
    tags: Optional[List[str]] = Field(default=None, description="List of tags")
    project_id: Optional[str] = Field(default=None, description="Agency project that owns this dataset")
    content: str = Field(..., min_length=1, description="JSONL content string (one JSON object per line)")


class DatasetDeleteResponse(BaseModel):
    """Response after deleting a dataset."""
    message: str
    id: str


class DatasetsListResponse(BaseModel):
    """Response for listing all datasets."""
    datasets: List[DatasetResponse]


class DatasetAddVersionRequest(BaseModel):
    """Request body for adding a new version to an existing dataset."""
    content: str = Field(..., min_length=1, description="JSONL content string for the new version")


class DatasetSetActiveVersionRequest(BaseModel):
    """Request body for setting the active version."""
    version_id: str = Field(..., description="ID of the version to activate")


# ── Pairwise Schemas ────────────────────────────────────────

class PairwiseRunRequest(BaseModel):
    """Request body for triggering a pairwise evaluation run."""
    dataset_id: Optional[str] = Field(default=None, description="Registered dataset ID")
    dataset_version_id: Optional[str] = Field(default=None, description="Immutable dataset version ID; defaults to the active version")
    dataset_path: Optional[str] = Field(default=None, description="Legacy server-side JSONL path")
    model_a_connection_id: Optional[str] = Field(default=None, description="Workspace provider connection for model A")
    model_a_provider: Optional[str] = Field(default=None, description="Model A provider name")
    model_a_model: Optional[str] = Field(default=None, description="Model A model ID")
    model_a_api_key: Optional[str] = Field(default=None, description="Model A API key")
    model_a_base_url: Optional[str] = Field(default=None, description="Model A custom base URL")
    model_a_allow_unauthenticated: bool = Field(default=False, description="Allow a keyless compatible endpoint for model A")
    model_b_connection_id: Optional[str] = Field(default=None, description="Workspace provider connection for model B")
    model_b_provider: Optional[str] = Field(default=None, description="Model B provider name")
    model_b_model: Optional[str] = Field(default=None, description="Model B model ID")
    model_b_api_key: Optional[str] = Field(default=None, description="Model B API key")
    model_b_base_url: Optional[str] = Field(default=None, description="Model B custom base URL")
    model_b_allow_unauthenticated: bool = Field(default=False, description="Allow a keyless compatible endpoint for model B")
    judge_connection_id: Optional[str] = Field(default=None, description="Workspace provider connection for judge")
    judge_provider: Optional[str] = Field(default=None, description="Judge provider name")
    judge_model: Optional[str] = Field(default=None, description="Judge model ID")
    judge_api_key: Optional[str] = Field(default=None, description="Judge API key")
    judge_prompt_template: Optional[str] = Field(default=None, description="Custom pairwise judge prompt template")
    concurrency: int = Field(default=5, ge=1, le=50, description="Maximum parallel comparisons")

    @model_validator(mode="after")
    def validate_dataset_source(self) -> "PairwiseRunRequest":
        if bool(self.dataset_id) == bool(self.dataset_path):
            raise ValueError("Provide exactly one of dataset_id or dataset_path.")
        if self.dataset_version_id and not self.dataset_id:
            raise ValueError("dataset_version_id requires dataset_id.")
        for label, connection_id, provider, model in (
            ("model_a", self.model_a_connection_id, self.model_a_provider, self.model_a_model),
            ("model_b", self.model_b_connection_id, self.model_b_provider, self.model_b_model),
            ("judge", self.judge_connection_id, self.judge_provider, self.judge_model),
        ):
            if not connection_id and (not provider or not model):
                raise ValueError(f"Provide {label}_connection_id or both {label}_provider and {label}_model.")
        return self


class PairwiseRunResponse(BaseModel):
    """Response returned immediately after starting a pairwise run."""
    run_id: str = Field(..., description="Unique ID of the pairwise run")
    status: RunStatus = Field(default=RunStatus.QUEUED, description="Current lifecycle status")
    is_simulated: bool = Field(default=False, description="Whether any provider returned simulated output")


class PairwiseMetrics(BaseModel):
    """Aggregated pairwise comparison metrics."""
    total_comparisons: int = Field(..., description="All expected pairwise comparisons")
    valid_comparisons: int = Field(..., description="Comparisons with a completed winner and scores")
    generation_errors: int = Field(..., description="Comparisons skipped because candidate generation failed")
    evaluation_errors: int = Field(..., description="Comparisons where judging or parsing failed")
    error_count: int = Field(..., description="Generation and evaluation errors combined")
    evaluation_coverage: float = Field(..., description="Valid comparisons divided by total comparisons")
    wins_a: int = Field(..., description="Number of wins for model A")
    wins_b: int = Field(..., description="Number of wins for model B")
    ties: int = Field(..., description="Number of ties")
    win_rate_a: Optional[float] = Field(default=None, description="Wins for A divided by valid comparisons")
    win_rate_b: Optional[float] = Field(default=None, description="Wins for B divided by valid comparisons")
    tie_rate: Optional[float] = Field(default=None, description="Ties divided by valid comparisons")
    elo_a: Optional[float] = Field(default=None, description="Final Elo from valid comparisons only")
    elo_b: Optional[float] = Field(default=None, description="Final Elo from valid comparisons only")
    avg_score_a: Optional[float] = Field(default=None, description="Average judge score for A over valid comparisons")
    avg_score_b: Optional[float] = Field(default=None, description="Average judge score for B over valid comparisons")


class PairwiseComparisonItem(BaseModel):
    """A single pairwise comparison result."""
    example_id: str
    prompt: str
    response_a: str
    response_b: str
    expected_output: str
    winner: Optional[Literal["A", "B", "tie"]]
    score_a: Optional[float]
    score_b: Optional[float]
    judge_reason: str
    original_order: str
    outcome: EvaluationOutcome
    error_message: Optional[str] = None


class PairwiseRunStatusResponse(BaseModel):
    """Full status and metrics for a pairwise run."""
    run_id: str
    model_a_name: str
    model_b_name: str
    status: RunStatus
    metrics: Optional[PairwiseMetrics] = None
    comparisons: Optional[List[PairwiseComparisonItem]] = None
    error: Optional[str] = None
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    is_simulated: bool = False
    run_configuration: Optional[Dict[str, Any]] = None
    configuration_verified: bool = False
    project_id: Optional[str] = None
    attempt_count: int = 0
    max_attempts: int = 3
    next_attempt_at: Optional[datetime] = None
    cancellation_requested_at: Optional[datetime] = None
    worker_id: Optional[str] = None
    last_transient_error: Optional[str] = None
    queue_position: Optional[int] = None


class PairwiseRunListItem(BaseModel):
    """Summary entry for listing pairwise runs."""
    run_id: str
    model_a_name: str
    model_b_name: str
    status: RunStatus
    created_at: Optional[datetime] = None
    is_simulated: bool = False
    project_id: Optional[str] = None
    attempt_count: int = 0
    max_attempts: int = 3
    next_attempt_at: Optional[datetime] = None
    cancellation_requested_at: Optional[datetime] = None
    queue_position: Optional[int] = None


class PairwiseRunsListResponse(BaseModel):
    """Response for listing all pairwise runs."""
    runs: List[PairwiseRunListItem]
