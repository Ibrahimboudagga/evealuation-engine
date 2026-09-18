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
    candidate_provider: str = Field(..., description="Candidate provider name (e.g. openai, anthropic, cohere)")
    candidate_model: str = Field(..., description="Candidate model ID / identifier")
    candidate_api_key: Optional[str] = Field(default=None, description="Authentication key for the candidate provider")
    candidate_base_url: Optional[str] = Field(default=None, description="Custom API base URL for the candidate provider")
    candidate_allow_unauthenticated: bool = Field(
        default=False,
        description="Allow a keyless request only for an explicit compatible endpoint with a base URL",
    )
    evaluator_provider: str = Field(..., description="Evaluator/judge provider name")
    evaluator_model: str = Field(..., description="Evaluator/judge model ID / identifier")
    evaluator_api_key: Optional[str] = Field(default=None, description="Authentication key for the evaluator model")
    concurrency: int = Field(default=5, ge=1, le=50, description="Maximum parallel evaluations")
    judge_prompt_template: Optional[str] = Field(default=None, description="Optional custom judge prompt template text")

    @model_validator(mode="after")
    def validate_dataset_source(self) -> "RunRequest":
        if bool(self.dataset_id) == bool(self.dataset_path):
            raise ValueError("Provide exactly one of dataset_id or dataset_path.")
        if self.dataset_version_id and not self.dataset_id:
            raise ValueError("dataset_version_id requires dataset_id.")
        return self


class RunResponse(BaseModel):
    """Response returned immediately after starting a run."""
    run_id: str = Field(..., description="Unique ID of the evaluation run")
    status: RunStatus = Field(default=RunStatus.QUEUED, description="Current lifecycle status")
    is_simulated: bool = Field(default=False, description="Whether any provider returned simulated output")


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


class RunListItem(BaseModel):
    """Summary entry for listing all runs."""
    run_id: str
    status: RunStatus
    created_at: Optional[datetime] = None
    is_simulated: bool = False
    project_id: Optional[str] = None


class RunsListResponse(BaseModel):
    """Response for listing all runs."""
    runs: List[RunListItem]


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
    model_a_provider: str = Field(..., description="Model A provider name")
    model_a_model: str = Field(..., description="Model A model ID")
    model_a_api_key: Optional[str] = Field(default=None, description="Model A API key")
    model_a_base_url: Optional[str] = Field(default=None, description="Model A custom base URL")
    model_a_allow_unauthenticated: bool = Field(default=False, description="Allow a keyless compatible endpoint for model A")
    model_b_provider: str = Field(..., description="Model B provider name")
    model_b_model: str = Field(..., description="Model B model ID")
    model_b_api_key: Optional[str] = Field(default=None, description="Model B API key")
    model_b_base_url: Optional[str] = Field(default=None, description="Model B custom base URL")
    model_b_allow_unauthenticated: bool = Field(default=False, description="Allow a keyless compatible endpoint for model B")
    judge_provider: str = Field(..., description="Judge provider name")
    judge_model: str = Field(..., description="Judge model ID")
    judge_api_key: Optional[str] = Field(default=None, description="Judge API key")
    judge_prompt_template: Optional[str] = Field(default=None, description="Custom pairwise judge prompt template")
    concurrency: int = Field(default=5, ge=1, le=50, description="Maximum parallel comparisons")

    @model_validator(mode="after")
    def validate_dataset_source(self) -> "PairwiseRunRequest":
        if bool(self.dataset_id) == bool(self.dataset_path):
            raise ValueError("Provide exactly one of dataset_id or dataset_path.")
        if self.dataset_version_id and not self.dataset_id:
            raise ValueError("dataset_version_id requires dataset_id.")
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


class PairwiseRunListItem(BaseModel):
    """Summary entry for listing pairwise runs."""
    run_id: str
    model_a_name: str
    model_b_name: str
    status: RunStatus
    created_at: Optional[datetime] = None
    is_simulated: bool = False
    project_id: Optional[str] = None


class PairwiseRunsListResponse(BaseModel):
    """Response for listing all pairwise runs."""
    runs: List[PairwiseRunListItem]
