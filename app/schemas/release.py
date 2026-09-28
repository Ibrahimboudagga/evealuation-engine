"""Validated release policy and per-evaluator measurement settings."""

from typing import Any
from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvaluatorSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pass_threshold: float = Field(default=.5, ge=0, le=1)


class EvaluatorReleaseRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    average_score_minimum: float | None = Field(default=None, ge=0, le=1)
    average_score_max_drop: float | None = Field(default=None, ge=0, le=1)
    pass_rate_minimum: float | None = Field(default=None, ge=0, le=1)
    pass_rate_max_drop: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def nonempty(self):
        if all(value is None for value in self.model_dump().values()):
            raise ValueError("An evaluator rule must specify at least one quality threshold.")
        return self


class SliceReleaseRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    metadata: dict[str, Any] = Field(min_length=1)
    coverage_minimum: float | None = Field(default=None, ge=0, le=1)
    minimum_valid_cases: int | None = Field(default=None, ge=1)
    evaluators: dict[str, EvaluatorReleaseRule] = Field(min_length=1)


class ReleaseRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    coverage_minimum: float = Field(default=.95, ge=0, le=1)
    minimum_valid_cases: int = Field(default=1, ge=1)
    exact_match_pass_rate_max_drop: float | None = Field(default=None, ge=0, le=1)
    evaluators: dict[str, EvaluatorReleaseRule] = Field(default_factory=dict)
    slices: list[SliceReleaseRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_slice_names(self):
        names = [item.name for item in self.slices]
        if len(set(names)) != len(names):
            raise ValueError("Release slice names must be unique.")
        return self
