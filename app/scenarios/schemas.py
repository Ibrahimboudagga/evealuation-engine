"""Versioned application execution and typed observable-evidence contracts."""

from datetime import date, datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Assertion(StrictModel):
    name: str = Field(min_length=1)
    path: str = Field(pattern=r"^(|/.*)$", description="JSON Pointer into the observed value")
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


class ObservedValue(StrictModel):
    # A wrapper distinguishes a measured null result from unavailable evidence.
    value: Any


class ToolExpectation(StrictModel):
    tool_name: str = Field(min_length=1)
    occurrence: int = Field(default=0, ge=0)
    arguments: list[Assertion] = Field(default_factory=list, max_length=100)
    result: list[Assertion] = Field(default_factory=list, max_length=100)


class RetrievalExpectation(StrictModel):
    relevant_source_ids: list[str] = Field(min_length=1, max_length=1000)
    min_recall: float = Field(default=1, ge=0, le=1)
    max_irrelevant: int | None = Field(default=None, ge=0)


class GroundingExpectation(StrictModel):
    name: str = Field(min_length=1)
    claim_path: str = Field(pattern=r"^(|/.*)$")
    expected_value: Any
    source_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)
    reference_status: Literal["unreviewed", "synthetic", "human_reviewed"] = "unreviewed"
    reference_id: str | None = None

    @model_validator(mode="after")
    def validate_reference(self):
        if self.reference_status == "human_reviewed" and not self.reference_id:
            raise ValueError("Human-reviewed truth requires a reference ID")
        return self


class StateExpectation(StrictModel):
    name: str = Field(min_length=1)
    phase: Literal["before", "after"]
    assertions: list[Assertion] = Field(min_length=1, max_length=100)


class SideEffectPolicy(StrictModel):
    allowed: list[str] = Field(default_factory=list, max_length=100)
    required: list[str] = Field(default_factory=list, max_length=100)
    require_authorized: bool = True

    @model_validator(mode="after")
    def validate_policy(self):
        if not set(self.required) <= set(self.allowed):
            raise ValueError("Required side effects must also be allowed")
        return self


class TurnExpectation(StrictModel):
    id: str = Field(min_length=1)
    input: str = Field(min_length=1, max_length=100000)
    assertions: list[Assertion] = Field(default_factory=list, max_length=100)
    state_assertions: list[Assertion] = Field(default_factory=list, max_length=100)


class CellKey(StrictModel):
    day: date
    lt: int = Field(ge=0, description="Lead time")


class RevenueCell(CellKey):
    value: float


class AnomalyLabel(CellKey):
    anomalous: bool


class CalculationExpectation(StrictModel):
    name: str = Field(min_length=1)
    operation: Literal["sum", "mean", "count"]
    cells: list[CellKey] = Field(min_length=1, max_length=10000)
    absolute_tolerance: float = Field(default=1e-6, ge=0)

    @model_validator(mode="after")
    def unique_cells(self):
        if len({(c.day, c.lt) for c in self.cells}) != len(self.cells):
            raise ValueError("Calculation cell keys must be unique")
        return self


class RevenueOpsExpectation(StrictModel):
    hotel_id: str = Field(min_length=1)
    date_from: date
    date_to: date
    day_lt: list[RevenueCell] = Field(min_length=1, max_length=10000)
    chosen_cluster: str = Field(min_length=1)
    anomaly_labels: list[AnomalyLabel] = Field(default_factory=list, max_length=10000)
    reference_status: Literal["unreviewed", "synthetic", "human_reviewed"] = "unreviewed"
    reference_id: str | None = None
    calculations: list[CalculationExpectation] = Field(default_factory=list, max_length=100)
    narrative_calculations: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_reference(self):
        keys = {(c.day, c.lt) for c in self.day_lt}
        if self.date_to < self.date_from or any(not self.date_from <= c.day <= self.date_to for c in self.day_lt):
            raise ValueError("Revenue cells must fall within an ordered date range")
        if len(keys) != len(self.day_lt):
            raise ValueError("DAY x LT cells must be unique")
        label_keys = {(c.day, c.lt) for c in self.anomaly_labels}
        if len(label_keys) != len(self.anomaly_labels) or not label_keys <= keys:
            raise ValueError("Anomaly labels must reference unique input cells")
        if self.reference_status == "human_reviewed" and not self.reference_id:
            raise ValueError("Human-reviewed truth requires a reference ID")
        names = {c.name for c in self.calculations}
        if len(names) != len(self.calculations) or not set(self.narrative_calculations) <= names:
            raise ValueError("Narrative calculations must name unique declared calculations")
        if any(not {(c.day, c.lt) for c in calc.cells} <= keys for calc in self.calculations):
            raise ValueError("Calculations must reference observed input cells")
        return self


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
    max_cost: float | None = Field(default=None, ge=0)
    cost_currency: str | None = Field(default=None, min_length=3, max_length=3)
    schedule: ScheduleExpectation | None = None
    tool_expectations: list[ToolExpectation] = Field(default_factory=list, max_length=100)
    tool_order: list[str] = Field(default_factory=list, max_length=100)
    retrieval: RetrievalExpectation | None = None
    grounding: list[GroundingExpectation] = Field(default_factory=list, max_length=100)
    state_checks: list[StateExpectation] = Field(default_factory=list, max_length=100)
    side_effect_policy: SideEffectPolicy | None = None
    turns: list[TurnExpectation] = Field(default_factory=list, max_length=20)
    revenue_ops: RevenueOpsExpectation | None = None

    @model_validator(mode="after")
    def validate_checks(self):
        if (self.max_cost is None) != (self.cost_currency is None):
            raise ValueError("Cost budgets require both max_cost and cost_currency")
        if set(self.required_tools) & set(self.forbidden_tools) or {t.tool_name for t in self.tool_expectations} & set(self.forbidden_tools):
            raise ValueError("A tool cannot be both required and forbidden")
        if len({a.name for a in self.assertions}) != len(self.assertions):
            raise ValueError("Assertion names must be unique")
        if len({t.id for t in self.turns}) != len(self.turns):
            raise ValueError("Turn IDs must be unique")
        if not (self.assertions or self.schedule or self.required_tools or self.forbidden_tools
                or self.expected_citation_ids or self.tool_expectations or self.tool_order
                or self.retrieval or self.grounding or self.state_checks or self.side_effect_policy
                or self.turns or self.revenue_ops or any(x is not None for x in
                (self.max_tool_calls, self.max_latency_ms, self.max_total_tokens, self.max_cost))):
            raise ValueError("At least one explicit evaluation check is required")
        return self


class ToolCall(StrictModel):
    name: str = Field(min_length=1)
    status: Literal["succeeded", "failed"]
    call_id: str | None = None
    step_id: str | None = None
    parent_step_id: str | None = None
    sequence: int | None = Field(default=None, ge=0)
    arguments: dict[str, Any] | None = None
    result: ObservedValue | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    latency_ms: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_timing(self):
        if self.started_at and self.finished_at and self.finished_at < self.started_at:
            raise ValueError("Tool call finished_at cannot precede started_at")
        if self.parent_step_id and not self.step_id:
            raise ValueError("A parent step requires a step ID")
        return self


class TargetIdentity(StrictModel):
    provider: str | None = None
    model: str | None = None
    model_version: str | None = None
    application_version: str | None = None
    prompt_template_version: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def identifies_build(self):
        if not (self.application_version or (self.provider and self.model and self.model_version)):
            raise ValueError("Target identity requires an application version or provider/model/model version")
        return self


class UsageEvidence(StrictModel):
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    cost_basis: Literal["measured", "estimated"] | None = None

    @model_validator(mode="after")
    def validate_usage(self):
        if self.total_tokens is not None and self.prompt_tokens is not None and self.completion_tokens is not None:
            if self.total_tokens != self.prompt_tokens + self.completion_tokens:
                raise ValueError("total_tokens must equal prompt_tokens plus completion_tokens")
        if self.cost is not None and (self.currency is None or self.cost_basis is None):
            raise ValueError("Cost requires currency and measured/estimated cost basis")
        return self


class RetrievedDocument(StrictModel):
    source_id: str = Field(min_length=1)
    query: str
    rank: int = Field(ge=1)
    content: str | None = None


class SideEffect(StrictModel):
    name: str = Field(min_length=1)
    status: Literal["succeeded", "failed"]
    authorized: bool | None = None
    # These observations come from the application's authorization boundary.
    arguments: dict[str, Any] | None = None
    principal_id: str | None = None
    resource_id: str | None = None


class TurnEvidence(StrictModel):
    id: str = Field(min_length=1)
    sequence: int = Field(ge=0)
    output: Any
    state: dict[str, Any] | None = None


class RevenueOpsEvidence(StrictModel):
    hotel_id: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    day_lt: list[RevenueCell] | None = Field(default=None, max_length=10000)
    clustering_inputs: list[RevenueCell] | None = Field(default=None, max_length=10000)
    chosen_cluster: str | None = None
    anomaly_labels: list[AnomalyLabel] | None = Field(default=None, max_length=10000)
    report_calculations: dict[str, float] | None = None
    narrative_claims: dict[str, float] | None = None

    @model_validator(mode="after")
    def unique_cells(self):
        for collection in (self.day_lt, self.clustering_inputs, self.anomaly_labels):
            if collection is not None and len({(c.day, c.lt) for c in collection}) != len(collection):
                raise ValueError("Revenue evidence cell keys must be unique")
        return self


class Evidence(StrictModel):
    schema_version: Literal[1, 2] = 1
    output: Any
    source: Literal["fixture", "imported", "live_capture"] = "imported"
    case_id: str | None = None
    run_id: str | None = None
    session_id: str | None = None
    captured_at: datetime | None = None
    target: TargetIdentity | None = None
    evaluator_versions: dict[str, str] = Field(default_factory=dict)
    human_annotations: dict[str, Any] = Field(default_factory=dict)
    usage: UsageEvidence | None = None
    # Null means unavailable; [] means an observed empty collection.
    tool_calls: list[ToolCall] | None = Field(default=None, max_length=10000)
    trace_complete: bool = False
    citation_ids: list[str] | None = None
    latency_ms: float | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    simulated: bool
    external_run_id: str | None = None
    retrievals: list[RetrievedDocument] | None = Field(default=None, max_length=10000)
    retrieval_complete: bool = False
    state_before: dict[str, Any] | None = None
    state_after: dict[str, Any] | None = None
    side_effects: list[SideEffect] | None = Field(default=None, max_length=10000)
    side_effects_complete: bool = False
    turns: list[TurnEvidence] | None = Field(default=None, max_length=20)
    turns_complete: bool = False
    revenue_ops: RevenueOpsEvidence | None = None

    @model_validator(mode="after")
    def unique_trace_identity(self):
        for field in ("sequence", "call_id", "step_id"):
            values = [getattr(c, field) for c in self.tool_calls or [] if getattr(c, field) is not None]
            if len(values) != len(set(values)):
                raise ValueError("Tool call sequence and IDs must be unique")
        step_ids = {c.step_id for c in self.tool_calls or [] if c.step_id}
        if any(c.parent_step_id and c.parent_step_id not in step_ids for c in self.tool_calls or []):
            raise ValueError("Tool-call parent_step_id must reference a step in the same trace")
        calls_by_id = {c.step_id: c for c in self.tool_calls or [] if c.step_id}
        for call in self.tool_calls or []:
            if call.parent_step_id:
                parent = calls_by_id[call.parent_step_id]
                if call.sequence is not None and parent.sequence is not None and parent.sequence >= call.sequence:
                    raise ValueError("Tool-call parents must precede their children")
                seen = {call.step_id}
                cursor = parent
                while cursor.parent_step_id:
                    if cursor.parent_step_id in seen:
                        raise ValueError("Tool-call parent relationships cannot contain cycles")
                    seen.add(cursor.parent_step_id)
                    cursor = calls_by_id[cursor.parent_step_id]
        for field in ("sequence", "id"):
            values = [getattr(t, field) for t in self.turns or []]
            if len(values) != len(set(values)):
                raise ValueError("Turn sequence and IDs must be unique")
        if self.usage and self.total_tokens is not None and self.usage.total_tokens is not None:
            if self.total_tokens != self.usage.total_tokens:
                raise ValueError("Evidence total_tokens must match usage.total_tokens")
        if self.schema_version == 2 and self.source in ("fixture", "live_capture"):
            required = (self.case_id, self.run_id, self.session_id, self.captured_at, self.target)
            if any(value is None for value in required) or not self.evaluator_versions:
                raise ValueError("Authoritative schema-v2 evidence requires case, run, session, capture time, target, and evaluator versions")
        return self


class CheckResult(StrictModel):
    name: str
    status: Literal["passed", "failed", "unverified"]
    explanation: str


class DimensionResult(StrictModel):
    status: Literal["passed", "failed", "inconclusive"]
    score: float | None = Field(default=None, ge=0, le=1)
    evaluated_checks: int = Field(ge=0)
    total_checks: int = Field(ge=0)
    evidence_checks: list[str] = Field(default_factory=list)


class ScenarioResult(StrictModel):
    scenario_id: str
    outcome: Literal["evaluated", "generation_error", "evaluation_error"]
    decision: Literal["passed", "regressed", "inconclusive"]
    quality_decision: Literal["passed", "regressed", "inconclusive"] | None = None
    score: float | None = None
    simulated: bool
    checks: list[CheckResult] = Field(default_factory=list)
    dimensions: dict[Literal["task_outcome", "trajectory", "safety", "operational"], DimensionResult] = Field(default_factory=dict)
    error_message: str | None = None
    evidence: Evidence | None = None
