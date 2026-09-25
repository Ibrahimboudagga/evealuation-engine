from datetime import datetime, timezone
import json
from typing import Any, Dict, List, Optional
from sqlalchemy import String, Text, Float, DateTime, Integer, Boolean, ForeignKey, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.schemas.outcomes import EvaluationOutcome, RunStatus


class Base(DeclarativeBase):
    pass


class WorkspaceDB(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    retention_days: Mapped[int] = mapped_column(Integer, default=365, nullable=False)
    plan: Mapped[str] = mapped_column(String(50), default="pilot", nullable=False)
    billing_status: Mapped[str] = mapped_column(String(50), default="trial", nullable=False)
    trial_ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    invoice_contact_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    limits_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    notification_settings_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    memberships: Mapped[list["MembershipDB"]] = relationship(back_populates="workspace", cascade="all, delete-orphan")
    projects: Mapped[list["ProjectDB"]] = relationship(back_populates="workspace")
    provider_connections: Mapped[list["ProviderConnectionDB"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )
    evaluation_templates: Mapped[list["EvaluationTemplateDB"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )
    report_shares: Mapped[list["ReportShareDB"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )
    audit_events: Mapped[list["AuditEventDB"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )
    usage_snapshots: Mapped[list["WorkspaceUsageSnapshotDB"]] = relationship(back_populates="workspace", cascade="all, delete-orphan")
    activation_events: Mapped[list["ActivationEventDB"]] = relationship(back_populates="workspace", cascade="all, delete-orphan")
    schedules: Mapped[list["EvaluationScheduleDB"]] = relationship(back_populates="workspace", cascade="all, delete-orphan")


class UserDB(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    api_token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    memberships: Mapped[list["MembershipDB"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    sessions: Mapped[list["UserSessionDB"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class MembershipDB(Base):
    __tablename__ = "workspace_memberships"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id", name="uq_workspace_membership"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(36), ForeignKey("workspaces.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(30), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    workspace: Mapped[WorkspaceDB] = relationship(back_populates="memberships")
    user: Mapped[UserDB] = relationship(back_populates="memberships")
    project_accesses: Mapped[list["ProjectAccessDB"]] = relationship(back_populates="membership", cascade="all, delete-orphan")


class ProjectAccessDB(Base):
    __tablename__ = "project_accesses"
    __table_args__ = (UniqueConstraint("membership_id", "project_id", name="uq_project_access"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    membership_id: Mapped[str] = mapped_column(String(36), ForeignKey("workspace_memberships.id"), nullable=False)
    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("projects.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    membership: Mapped[MembershipDB] = relationship(back_populates="project_accesses")


class UserSessionDB(Base):
    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    user: Mapped[UserDB] = relationship(back_populates="sessions")


class ProviderConnectionDB(Base):
    __tablename__ = "provider_connections"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_provider_connection_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(36), ForeignKey("workspaces.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    default_model: Mapped[str] = mapped_column(String(255), nullable=False)
    base_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    encrypted_api_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    credential_reference: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    allow_unauthenticated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    workspace: Mapped[WorkspaceDB] = relationship(back_populates="provider_connections")


class EvaluationTemplateDB(Base):
    __tablename__ = "evaluation_templates"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_evaluation_template_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(36), ForeignKey("workspaces.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    settings_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    workspace: Mapped[WorkspaceDB] = relationship(back_populates="evaluation_templates")
    schedules: Mapped[list["EvaluationScheduleDB"]] = relationship(back_populates="template", cascade="all, delete-orphan")

    @property
    def settings(self) -> Dict[str, Any]:
        try:
            return json.loads(self.settings_json)
        except json.JSONDecodeError:
            return {}

    @settings.setter
    def settings(self, value: Dict[str, Any]) -> None:
        self.settings_json = json.dumps(value)


class ReportShareDB(Base):
    __tablename__ = "report_shares"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(36), ForeignKey("workspaces.id"), nullable=False)
    project_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("projects.id"), nullable=True)
    run_id: Mapped[Optional[str]] = mapped_column(String(255), ForeignKey("evaluation_runs.id"), nullable=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    branding_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    workspace: Mapped[WorkspaceDB] = relationship(back_populates="report_shares")
    project: Mapped[Optional["ProjectDB"]] = relationship()
    run: Mapped[Optional["EvaluationRunDB"]] = relationship()

    @property
    def branding(self) -> Dict[str, Any]:
        try:
            return json.loads(self.branding_json or "{}")
        except json.JSONDecodeError:
            return {}

    @branding.setter
    def branding(self, value: Optional[Dict[str, Any]]) -> None:
        self.branding_json = json.dumps(value or {})


class AuditEventDB(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(36), ForeignKey("workspaces.id"), nullable=False)
    actor_user_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    actor_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    project_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    workspace: Mapped[WorkspaceDB] = relationship(back_populates="audit_events")

    @property
    def metadata_dict(self) -> Dict[str, Any]:
        try:
            return json.loads(self.metadata_json or "{}")
        except json.JSONDecodeError:
            return {}

    @metadata_dict.setter
    def metadata_dict(self, value: Optional[Dict[str, Any]]) -> None:
        self.metadata_json = json.dumps(value or {})


class WorkspaceUsageSnapshotDB(Base):
    """A bounded, aggregate usage record for one workspace and billing period."""

    __tablename__ = "workspace_usage_snapshots"
    __table_args__ = (
        UniqueConstraint("workspace_id", "period_start", "period_end", name="uq_workspace_usage_period"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(36), ForeignKey("workspaces.id"), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    run_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    evaluated_case_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    provider_call_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    storage_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    report_share_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active_project_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    workspace: Mapped[WorkspaceDB] = relationship(back_populates="usage_snapshots")


class ActivationEventDB(Base):
    """Privacy-safe product milestone. It intentionally contains no evaluation data."""

    __tablename__ = "activation_events"
    __table_args__ = (UniqueConstraint("workspace_id", "event_name", name="uq_workspace_activation_event"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(36), ForeignKey("workspaces.id"), nullable=False)
    event_name: Mapped[str] = mapped_column(String(100), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    workspace: Mapped[WorkspaceDB] = relationship(back_populates="activation_events")


class EvaluationScheduleDB(Base):
    """A recurring single-model template execution for one dataset version."""

    __tablename__ = "evaluation_schedules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(36), ForeignKey("workspaces.id"), nullable=False)
    template_id: Mapped[str] = mapped_column(String(36), ForeignKey("evaluation_templates.id"), nullable=False)
    dataset_id: Mapped[str] = mapped_column(String(36), ForeignKey("datasets.id"), nullable=False)
    dataset_version_id: Mapped[str] = mapped_column(String(36), ForeignKey("dataset_versions.id"), nullable=False)
    frequency: Mapped[str] = mapped_column(String(20), nullable=False)
    next_execution_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_executed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    workspace: Mapped[WorkspaceDB] = relationship(back_populates="schedules")
    template: Mapped[EvaluationTemplateDB] = relationship(back_populates="schedules")
    executions: Mapped[list["ScheduleExecutionDB"]] = relationship(back_populates="schedule", cascade="all, delete-orphan")


class ScheduleExecutionDB(Base):
    """Append-only evidence linking a planned schedule occurrence to its run."""

    __tablename__ = "schedule_executions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schedule_id: Mapped[str] = mapped_column(String(36), ForeignKey("evaluation_schedules.id"), nullable=False)
    run_id: Mapped[Optional[str]] = mapped_column(String(255), ForeignKey("evaluation_runs.id"), nullable=True)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    schedule: Mapped[EvaluationScheduleDB] = relationship(back_populates="executions")


class WorkerStateDB(Base):
    """Single-worker heartbeat and claim state used by readiness checks."""

    __tablename__ = "worker_states"

    worker_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    claimed_run_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    claimed_run_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class ProjectDB(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    client_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tags_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("workspaces.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    datasets: Mapped[list["DatasetDB"]] = relationship(back_populates="project")
    evaluation_runs: Mapped[list["EvaluationRunDB"]] = relationship(back_populates="project")
    pairwise_runs: Mapped[list["PairwiseRunDB"]] = relationship(back_populates="project")
    workspace: Mapped[Optional[WorkspaceDB]] = relationship(back_populates="projects")

    @property
    def tags(self) -> List[str]:
        if not self.tags_json:
            return []
        try:
            return json.loads(self.tags_json)
        except json.JSONDecodeError:
            return []

    @tags.setter
    def tags(self, val: List[str]) -> None:
        self.tags_json = json.dumps(val) if val else None


class DatasetDB(Base):
    __tablename__ = "datasets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tags_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    project_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("projects.id"), nullable=True)
    latest_version_number: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    versions: Mapped[list["DatasetVersionDB"]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan", order_by="DatasetVersionDB.version_number"
    )
    runs: Mapped[list["EvaluationRunDB"]] = relationship(back_populates="dataset", cascade="all, delete-orphan")
    project: Mapped[Optional[ProjectDB]] = relationship(back_populates="datasets")

    @property
    def tags(self) -> List[str]:
        if not self.tags_json:
            return []
        try:
            return json.loads(self.tags_json)
        except json.JSONDecodeError:
            return []

    @tags.setter
    def tags(self, val: List[str]) -> None:
        self.tags_json = json.dumps(val) if val else None


class DatasetVersionDB(Base):
    __tablename__ = "dataset_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(String(36), ForeignKey("datasets.id"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    example_count: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    dataset: Mapped[DatasetDB] = relationship(back_populates="versions")

    @property
    def content_list(self) -> List[Dict[str, Any]]:
        if not self.content:
            return []
        try:
            return json.loads(self.content)
        except json.JSONDecodeError:
            return []


class EvaluationRunDB(Base):
    __tablename__ = "evaluation_runs"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(String(255), ForeignKey("datasets.id"), nullable=False)
    dataset_version_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("dataset_versions.id"), nullable=True)
    project_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("projects.id"), nullable=True)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    status: Mapped[str] = mapped_column(String(20), default=RunStatus.QUEUED.value, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_baseline: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    run_configuration_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    configuration_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    cancellation_requested_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    worker_claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    worker_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    last_transient_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    dataset: Mapped[DatasetDB] = relationship(back_populates="runs")
    project: Mapped[Optional[ProjectDB]] = relationship(back_populates="evaluation_runs")
    results: Mapped[list["EvaluationResultDB"]] = relationship(back_populates="run", cascade="all, delete-orphan")

    @property
    def run_configuration(self) -> Optional[Dict[str, Any]]:
        if not self.run_configuration_json:
            return None
        try:
            return json.loads(self.run_configuration_json)
        except json.JSONDecodeError:
            return None

    @run_configuration.setter
    def run_configuration(self, val: Optional[Dict[str, Any]]) -> None:
        self.run_configuration_json = json.dumps(val) if val is not None else None


class EvaluationResultDB(Base):
    __tablename__ = "evaluation_results"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(255), ForeignKey("evaluation_runs.id"), nullable=False)
    example_id: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    prediction: Mapped[str] = mapped_column(Text, nullable=False)
    expected_output: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    evaluator_name: Mapped[str] = mapped_column(String(100), nullable=False)
    outcome: Mapped[str] = mapped_column(String(30), default=EvaluationOutcome.EVALUATED.value, nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    run: Mapped[EvaluationRunDB] = relationship(back_populates="results")

    @property
    def metadata_dict(self) -> Dict[str, Any]:
        if not self.metadata_json:
            return {}
        try:
            return json.loads(self.metadata_json)
        except json.JSONDecodeError:
            return {}

    @metadata_dict.setter
    def metadata_dict(self, val: Dict[str, Any]) -> None:
        self.metadata_json = json.dumps(val) if val is not None else None


class PairwiseRunDB(Base):
    __tablename__ = "pairwise_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(String(36), ForeignKey("datasets.id"), nullable=False)
    dataset_version_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("dataset_versions.id"), nullable=True)
    project_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("projects.id"), nullable=True)
    model_a_name: Mapped[str] = mapped_column(String(255), nullable=False)
    model_b_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    status: Mapped[str] = mapped_column(String(20), default=RunStatus.QUEUED.value, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    run_configuration_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    configuration_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    cancellation_requested_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    worker_claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    worker_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    last_transient_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    dataset: Mapped[DatasetDB] = relationship()
    project: Mapped[Optional[ProjectDB]] = relationship(back_populates="pairwise_runs")
    comparisons: Mapped[list["PairwiseComparisonDB"]] = relationship(back_populates="run", cascade="all, delete-orphan")

    @property
    def run_configuration(self) -> Optional[Dict[str, Any]]:
        if not self.run_configuration_json:
            return None
        try:
            return json.loads(self.run_configuration_json)
        except json.JSONDecodeError:
            return None

    @run_configuration.setter
    def run_configuration(self, val: Optional[Dict[str, Any]]) -> None:
        self.run_configuration_json = json.dumps(val) if val is not None else None


class PairwiseComparisonDB(Base):
    __tablename__ = "pairwise_comparisons"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("pairwise_runs.id"), nullable=False)
    example_id: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    response_a: Mapped[str] = mapped_column(Text, nullable=False)
    response_b: Mapped[str] = mapped_column(Text, nullable=False)
    expected_output: Mapped[str] = mapped_column(Text, nullable=False)
    winner: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    score_a: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    score_b: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    judge_reason: Mapped[str] = mapped_column(Text, nullable=False)
    original_order: Mapped[str] = mapped_column(String(10), nullable=False)
    outcome: Mapped[str] = mapped_column(String(30), default=EvaluationOutcome.EVALUATED.value, nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    run: Mapped[PairwiseRunDB] = relationship(back_populates="comparisons")

    @property
    def metadata_dict(self) -> Dict[str, Any]:
        if not self.metadata_json:
            return {}
        try:
            return json.loads(self.metadata_json)
        except json.JSONDecodeError:
            return {}

    @metadata_dict.setter
    def metadata_dict(self, val: Dict[str, Any]) -> None:
        self.metadata_json = json.dumps(val) if val is not None else None
