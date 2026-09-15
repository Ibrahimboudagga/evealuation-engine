"""Shared lifecycle and result-outcome vocabulary."""

from enum import Enum


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class EvaluationOutcome(str, Enum):
    EVALUATED = "evaluated"
    GENERATION_ERROR = "generation_error"
    EVALUATION_ERROR = "evaluation_error"
    UNVERIFIED = "unverified"
