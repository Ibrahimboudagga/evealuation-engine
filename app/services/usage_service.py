"""Workspace usage accounting, monthly snapshots, and plan-limit enforcement."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Mapping, Optional

from app.database.connection import get_db
from app.database.models import (
    DatasetVersionDB,
    EvaluationResultDB,
    EvaluationRunDB,
    PairwiseComparisonDB,
    PairwiseRunDB,
    ProjectDB,
    ReportShareDB,
    WorkspaceDB,
    WorkspaceUsageSnapshotDB,
)
from app.schemas.outcomes import EvaluationOutcome


USAGE_KEYS = {
    "runs", "evaluated_cases", "provider_calls", "storage_bytes", "report_shares", "active_projects",
}


class WorkspaceLimitExceeded(ValueError):
    """Raised before a write would exceed an agency plan limit."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _period_bounds(now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    now = (now or _utcnow()).replace(tzinfo=None)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end


def _bytes(value: Optional[str]) -> int:
    return len((value or "").encode("utf-8"))


class UsageService:
    """Produces privacy-safe aggregate workspace usage only."""

    def _limits(self, workspace: WorkspaceDB) -> dict[str, int]:
        try:
            raw = json.loads(workspace.limits_json or "{}")
        except json.JSONDecodeError:
            raw = {}
        return {key: int(value) for key, value in raw.items() if key in USAGE_KEYS and isinstance(value, int) and value >= 0}

    def set_limits(self, workspace_id: str, limits: Mapping[str, Optional[int]]) -> dict[str, int]:
        unknown = set(limits) - USAGE_KEYS
        if unknown:
            raise ValueError(f"Unsupported limits: {', '.join(sorted(unknown))}.")
        cleaned: dict[str, int] = {}
        for key, value in limits.items():
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"Limit '{key}' must be a non-negative whole number or null.")
            cleaned[key] = value
        with get_db() as db:
            workspace = db.query(WorkspaceDB).filter(WorkspaceDB.id == workspace_id).first()
            if not workspace:
                raise ValueError("Workspace not found.")
            workspace.limits_json = json.dumps(cleaned, sort_keys=True)
            db.commit()
        return cleaned

    def metrics(self, workspace_id: str, now: Optional[datetime] = None) -> dict[str, int]:
        start, end = _period_bounds(now)
        with get_db() as db:
            project_ids = [project_id for (project_id,) in db.query(ProjectDB.id).filter(ProjectDB.workspace_id == workspace_id).all()]
            active_projects = len(project_ids)
            if not project_ids:
                return {"runs": 0, "evaluated_cases": 0, "provider_calls": 0, "storage_bytes": 0, "report_shares": 0, "active_projects": 0}

            eval_runs = db.query(EvaluationRunDB).filter(EvaluationRunDB.project_id.in_(project_ids), EvaluationRunDB.created_at >= start, EvaluationRunDB.created_at < end).all()
            pairwise_runs = db.query(PairwiseRunDB).filter(PairwiseRunDB.project_id.in_(project_ids), PairwiseRunDB.created_at >= start, PairwiseRunDB.created_at < end).all()
            eval_ids, pairwise_ids = [run.id for run in eval_runs], [run.id for run in pairwise_runs]
            results = db.query(EvaluationResultDB).filter(EvaluationResultDB.run_id.in_(eval_ids)).all() if eval_ids else []
            comparisons = db.query(PairwiseComparisonDB).filter(PairwiseComparisonDB.run_id.in_(pairwise_ids)).all() if pairwise_ids else []
            evaluated_cases = len({(item.run_id, item.example_id) for item in results if item.outcome == EvaluationOutcome.EVALUATED.value})
            evaluated_cases += len({(item.run_id, item.example_id) for item in comparisons if item.outcome == EvaluationOutcome.EVALUATED.value})
            single_requests = len({(item.run_id, item.example_id) for item in results})
            single_judges = len({(item.run_id, item.example_id) for item in results if item.evaluator_name == "llm_judge"})
            pairwise_requests = 2 * len({(item.run_id, item.example_id) for item in comparisons})
            pairwise_judges = len({(item.run_id, item.example_id) for item in comparisons if item.outcome != EvaluationOutcome.GENERATION_ERROR.value})
            storage = sum(_bytes(version.content) for version in db.query(DatasetVersionDB).join(DatasetVersionDB.dataset).join(ProjectDB).filter(ProjectDB.workspace_id == workspace_id).all())
            storage += sum(_bytes(item.prompt) + _bytes(item.prediction) + _bytes(item.expected_output) + _bytes(item.error_message) + _bytes(item.metadata_json) for item in results)
            storage += sum(_bytes(item.prompt) + _bytes(item.response_a) + _bytes(item.response_b) + _bytes(item.expected_output) + _bytes(item.judge_reason) + _bytes(item.error_message) + _bytes(item.metadata_json) for item in comparisons)
            shares = db.query(ReportShareDB).filter(ReportShareDB.workspace_id == workspace_id, ReportShareDB.created_at >= start, ReportShareDB.created_at < end).count()
            return {"runs": len(eval_runs) + len(pairwise_runs), "evaluated_cases": evaluated_cases, "provider_calls": single_requests + single_judges + pairwise_requests + pairwise_judges, "storage_bytes": storage, "report_shares": shares, "active_projects": active_projects}

    def overview(self, workspace_id: str, now: Optional[datetime] = None) -> dict:
        start, end = _period_bounds(now)
        usage = self.metrics(workspace_id, now)
        with get_db() as db:
            workspace = db.query(WorkspaceDB).filter(WorkspaceDB.id == workspace_id).first()
            if not workspace:
                raise ValueError("Workspace not found.")
            limits = self._limits(workspace)
            snapshot = db.query(WorkspaceUsageSnapshotDB).filter(WorkspaceUsageSnapshotDB.workspace_id == workspace_id, WorkspaceUsageSnapshotDB.period_start == start, WorkspaceUsageSnapshotDB.period_end == end).first()
            snapshot_at = snapshot.created_at if snapshot else None
        return {"period_start": start, "period_end": end, "usage": usage, "limits": limits, "remaining": {key: max(limits[key] - usage[key], 0) if key in limits else None for key in USAGE_KEYS}, "snapshot_created_at": snapshot_at}

    def snapshot(self, workspace_id: str, now: Optional[datetime] = None) -> dict:
        start, end = _period_bounds(now)
        usage = self.metrics(workspace_id, now)
        with get_db() as db:
            snapshot = db.query(WorkspaceUsageSnapshotDB).filter(WorkspaceUsageSnapshotDB.workspace_id == workspace_id, WorkspaceUsageSnapshotDB.period_start == start, WorkspaceUsageSnapshotDB.period_end == end).first()
            if not snapshot:
                snapshot = WorkspaceUsageSnapshotDB(id=str(uuid.uuid4()), workspace_id=workspace_id, period_start=start, period_end=end)
                db.add(snapshot)
            snapshot.run_count = usage["runs"]
            snapshot.evaluated_case_count = usage["evaluated_cases"]
            snapshot.provider_call_count = usage["provider_calls"]
            snapshot.storage_bytes = usage["storage_bytes"]
            snapshot.report_share_count = usage["report_shares"]
            snapshot.active_project_count = usage["active_projects"]
            snapshot.created_at = _utcnow()
            db.commit()
        return self.overview(workspace_id, now)

    def assert_capacity(self, workspace_id: str, **increments: int) -> None:
        unknown = set(increments) - USAGE_KEYS
        if unknown:
            raise ValueError(f"Unsupported usage counters: {', '.join(sorted(unknown))}.")
        overview = self.overview(workspace_id)
        for key, increment in increments.items():
            if increment <= 0 or key not in overview["limits"]:
                continue
            proposed = overview["usage"][key] + increment
            if proposed > overview["limits"][key]:
                raise WorkspaceLimitExceeded(f"Workspace limit reached for {key.replace('_', ' ')}: {overview['usage'][key]} of {overview['limits'][key]} used this month.")

    def assert_run_capacity(self, workspace_id: str, dataset_id: str, dataset_version_id: Optional[str], provider_calls_per_case: int) -> None:
        with get_db() as db:
            query = db.query(DatasetVersionDB).filter(DatasetVersionDB.dataset_id == dataset_id)
            query = query.filter(DatasetVersionDB.id == dataset_version_id) if dataset_version_id else query.filter(DatasetVersionDB.is_active == True)
            version = query.first()
            if not version:
                raise ValueError("Dataset version was not found.")
            cases = version.example_count
        self.assert_capacity(workspace_id, runs=1, evaluated_cases=cases, provider_calls=cases * provider_calls_per_case)
