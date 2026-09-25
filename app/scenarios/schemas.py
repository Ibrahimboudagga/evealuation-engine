"""Versioned application execution and evidence contract."""

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Assertion(StrictModel):
    name: str = Field(min_length=1)
    path: str = Field(pattern=r"^(|/.*)$", description="JSON Pointer into output")
    operator: Literal["equals", "contains", "exists", "max", "min"] = "equals"
    value: Any = None

    @model_validator(mode="after")
    def validate_operator(self):
        if self.operator in ("min", "max") and (isinstance(self.value, bool) or not isinstance(self.value, (int, float))):
            raise ValueError("Numeric assertions require a numeric value")
        return self


class ScheduleExpectation(StrictModel):
    task_names: list[str] = Field(min_length=1)
    available_hours: float = Field(gt=0)
    min_subtasks_per_task: int = Field(default=3, ge=1)


class Scenario(StrictModel):
    schema_version: Literal[1] = 1
    id: str = Field(min_length=1)
    task: str = Field(min_length=1)
    inputs: dict[str, Any] = Field(default_factory=dict)
    assertions: list[Assertion] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    expected_citation_ids: list[str] = Field(default_factory=list)
    max_tool_calls: int | None = Field(default=None, ge=0)
    max_latency_ms: float | None = Field(default=None, ge=0)
    max_total_tokens: int | None = Field(default=None, ge=0)
    schedule: ScheduleExpectation | None = None

    @model_validator(mode="after")
    def validate_checks(self):
        if set(self.required_tools) & set(self.forbidden_tools):
            raise ValueError("A tool cannot be both required and forbidden")
        if len({a.name for a in self.assertions}) != len(self.assertions):
            raise ValueError("Assertion names must be unique")
        if not (self.assertions or self.schedule or self.required_tools or self.forbidden_tools
                or self.expected_citation_ids or any(x is not None for x in
                (self.max_tool_calls, self.max_latency_ms, self.max_total_tokens))):
            raise ValueError("At least one explicit evaluation check is required")
        return self


class ToolCall(StrictModel):
    name: str = Field(min_length=1)
    status: Literal["succeeded", "failed"]


class Evidence(StrictModel):
    schema_version: Literal[1] = 1
    output: Any
    # Null means unavailable; [] means an observed empty collection.
    tool_calls: list[ToolCall] | None = None
    trace_complete: bool = False
    citation_ids: list[str] | None = None
    latency_ms: float | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    simulated: bool
    external_run_id: str | None = None


class CheckResult(StrictModel):
    name: str
    status: Literal["passed", "failed", "unverified"]
    explanation: str


class ScenarioResult(StrictModel):
    scenario_id: str
    outcome: Literal["evaluated", "generation_error", "evaluation_error"]
    decision: Literal["passed", "regressed", "inconclusive"]
    score: float | None = None
    simulated: bool
    checks: list[CheckResult] = Field(default_factory=list)
    error_message: str | None = None
    evidence: Evidence | None = None
