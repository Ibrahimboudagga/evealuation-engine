from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class RunRequest(BaseModel):
    """Request body for triggering a new evaluation run."""
    dataset_path: str = Field(..., description="Path to the JSONL dataset file")
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


class DatasetItem(BaseModel):
    """A single dataset record from the database."""
    id: str
    name: str


class DatasetsResponse(BaseModel):
    """Response for listing all datasets."""
    datasets: List[DatasetItem]
