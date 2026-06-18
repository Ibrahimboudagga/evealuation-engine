from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# ── Run Schemas ──────────────────────────────────────────────

class RunRequest(BaseModel):
    """Request body for triggering a new evaluation run."""
    dataset_path: str = Field(..., description="Path to the JSONL dataset file (legacy)")
    candidate_provider: str = Field(..., description="Candidate provider name (e.g. openai, anthropic, cohere)")
    candidate_model: str = Field(..., description="Candidate model ID / identifier")
    candidate_api_key: Optional[str] = Field(default=None, description="Authentication key for the candidate provider")
    candidate_base_url: Optional[str] = Field(default=None, description="Custom API base URL for the candidate provider")
    evaluator_provider: str = Field(..., description="Evaluator/judge provider name")
    evaluator_model: str = Field(..., description="Evaluator/judge model ID / identifier")
    evaluator_api_key: Optional[str] = Field(default=None, description="Authentication key for the evaluator model")
    concurrency: int = Field(default=5, ge=1, le=50, description="Maximum parallel evaluations")
    judge_prompt_template: Optional[str] = Field(default=None, description="Optional custom judge prompt template text")


class RunResponse(BaseModel):
    """Response returned immediately after starting a run."""
    run_id: str = Field(..., description="Unique ID of the evaluation run")
    status: str = Field(default="started", description="Current status of the run")


class EvaluatorMetric(BaseModel):
    """Aggregated metric for a single evaluator within a run."""
    evaluator: str = Field(..., description="Name of the evaluator")
    mean_score: float = Field(..., description="Average score across all examples")
    pass_rate: float = Field(..., description="Fraction of examples scoring >= 0.5")
    n: int = Field(..., description="Number of examples evaluated")


class RunStatusResponse(BaseModel):
    """Full status and metrics for a completed (or running) run."""
    run_id: str
    status: str
    metrics: Optional[List[EvaluatorMetric]] = None
    error: Optional[str] = None


class RunListItem(BaseModel):
    """Summary entry for listing all runs."""
    run_id: str
    status: str


class RunsListResponse(BaseModel):
    """Response for listing all runs."""
    runs: List[RunListItem]


# ── Dataset Schemas ──────────────────────────────────────────

class DatasetVersionResponse(BaseModel):
    """A single dataset version."""
    id: str = Field(..., description="Version ID")
    version_number: int = Field(..., description="Sequential version number")
    example_count: int = Field(..., description="Number of examples in this version")
    is_active: bool = Field(..., description="Whether this is the current active version")
    created_at: datetime = Field(..., description="Version creation timestamp")


class DatasetResponse(BaseModel):
    """Full dataset record."""
    id: str = Field(..., description="Dataset UUID")
    name: str = Field(..., description="Dataset name")
    description: Optional[str] = Field(default=None, description="Dataset description")
    tags: List[str] = Field(default_factory=list, description="Dataset tags")
    latest_version_number: int = Field(..., description="Latest version number")
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")
    active_version: Optional[DatasetVersionResponse] = Field(default=None, description="Currently active version")


class DatasetDetailResponse(BaseModel):
    """Full dataset details including version history."""
    id: str
    name: str
    description: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
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
    dataset_path: str = Field(..., description="Path to the JSONL dataset file")
    model_a_provider: str = Field(..., description="Model A provider name")
    model_a_model: str = Field(..., description="Model A model ID")
    model_a_api_key: Optional[str] = Field(default=None, description="Model A API key")
    model_a_base_url: Optional[str] = Field(default=None, description="Model A custom base URL")
    model_b_provider: str = Field(..., description="Model B provider name")
    model_b_model: str = Field(..., description="Model B model ID")
    model_b_api_key: Optional[str] = Field(default=None, description="Model B API key")
    model_b_base_url: Optional[str] = Field(default=None, description="Model B custom base URL")
    judge_provider: str = Field(..., description="Judge provider name")
    judge_model: str = Field(..., description="Judge model ID")
    judge_api_key: Optional[str] = Field(default=None, description="Judge API key")
    judge_prompt_template: Optional[str] = Field(default=None, description="Custom pairwise judge prompt template")
    concurrency: int = Field(default=5, ge=1, le=50, description="Maximum parallel comparisons")


class PairwiseRunResponse(BaseModel):
    """Response returned immediately after starting a pairwise run."""
    run_id: str = Field(..., description="Unique ID of the pairwise run")
    status: str = Field(default="started", description="Current status of the run")


class PairwiseMetrics(BaseModel):
    """Aggregated pairwise comparison metrics."""
    total_comparisons: int = Field(..., description="Total number of comparisons")
    wins_a: int = Field(..., description="Number of wins for model A")
    wins_b: int = Field(..., description="Number of wins for model B")
    ties: int = Field(..., description="Number of ties")
    win_rate_a: float = Field(..., description="Win rate for model A (0.0-1.0)")
    win_rate_b: float = Field(..., description="Win rate for model B (0.0-1.0)")
    tie_rate: float = Field(..., description="Tie rate (0.0-1.0)")
    elo_a: float = Field(..., description="Final Elo rating for model A")
    elo_b: float = Field(..., description="Final Elo rating for model B")
    avg_score_a: float = Field(..., description="Average judge score for model A (0.0-1.0)")
    avg_score_b: float = Field(..., description="Average judge score for model B (0.0-1.0)")


class PairwiseComparisonItem(BaseModel):
    """A single pairwise comparison result."""
    example_id: str
    prompt: str
    response_a: str
    response_b: str
    expected_output: str
    winner: str
    score_a: float
    score_b: float
    judge_reason: str
    original_order: str


class PairwiseRunStatusResponse(BaseModel):
    """Full status and metrics for a pairwise run."""
    run_id: str
    model_a_name: str
    model_b_name: str
    status: str
    metrics: Optional[PairwiseMetrics] = None
    comparisons: Optional[List[PairwiseComparisonItem]] = None
    error: Optional[str] = None


class PairwiseRunListItem(BaseModel):
    """Summary entry for listing pairwise runs."""
    run_id: str
    model_a_name: str
    model_b_name: str
    status: str


class PairwiseRunsListResponse(BaseModel):
    """Response for listing all pairwise runs."""
    runs: List[PairwiseRunListItem]
