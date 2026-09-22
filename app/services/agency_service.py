"""Reusable evaluation templates, client-safe report links, and project health summaries."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.database.connection import get_db
from app.database.models import EvaluationResultDB, EvaluationRunDB, EvaluationTemplateDB, ProjectDB, ReportShareDB
from app.errors import sanitize_error
from app.runners.eval_runner import get_run_metrics
from app.schemas.outcomes import EvaluationOutcome, RunStatus
from app.services.baseline_service import BaselineService
from app.services.report_service import ReportService


def _now() -> datetime:
    # SQLite persists naive timestamps; use UTC consistently for comparisons.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AgencyService:
    def __init__(self) -> None:
        self.reports = ReportService()
        self.baselines = BaselineService()

    def create_template(self, workspace_id: str, payload: dict[str, Any]) -> EvaluationTemplateDB:
        now = _now()
        template = EvaluationTemplateDB(
            id=str(uuid.uuid4()), workspace_id=workspace_id, name=payload["name"],
            description=payload.get("description"), created_at=now, updated_at=now,
        )
        template.settings = {key: value for key, value in payload.items() if key not in {"name", "description"}}
        with get_db() as db:
            db.add(template)
            db.commit()
            db.refresh(template)
            return template

    def list_templates(self, workspace_id: str) -> list[EvaluationTemplateDB]:
        with get_db() as db:
            return db.query(EvaluationTemplateDB).filter(
                EvaluationTemplateDB.workspace_id == workspace_id
            ).order_by(EvaluationTemplateDB.name).all()

    def get_template(self, workspace_id: str, template_id: str) -> Optional[EvaluationTemplateDB]:
        with get_db() as db:
            return db.query(EvaluationTemplateDB).filter(
                EvaluationTemplateDB.id == template_id, EvaluationTemplateDB.workspace_id == workspace_id
            ).first()

    def delete_template(self, workspace_id: str, template_id: str) -> bool:
        with get_db() as db:
            template = db.query(EvaluationTemplateDB).filter(
                EvaluationTemplateDB.id == template_id, EvaluationTemplateDB.workspace_id == workspace_id
            ).first()
            if not template:
                return False
            db.delete(template)
            db.commit()
            return True

    def create_share(self, workspace_id: str, run_id: Optional[str], project_id: Optional[str], expires_in_hours: int, branding: dict[str, Any]) -> tuple[ReportShareDB, str]:
        with get_db() as db:
            if run_id:
                run = db.query(EvaluationRunDB).filter(EvaluationRunDB.id == run_id).first()
                if not run or not run.project_id:
                    raise ValueError("A shared run must belong to a project.")
                project = db.query(ProjectDB).filter(ProjectDB.id == run.project_id, ProjectDB.workspace_id == workspace_id).first()
                if not project:
                    raise ValueError("Run was not found in this workspace.")
                project_id = project.id
            else:
                project = db.query(ProjectDB).filter(ProjectDB.id == project_id, ProjectDB.workspace_id == workspace_id).first()
                if not project:
                    raise ValueError("Project was not found in this workspace.")
            token = secrets.token_urlsafe(32)
            share = ReportShareDB(
                id=str(uuid.uuid4()), workspace_id=workspace_id, project_id=project_id, run_id=run_id,
                token_hash=_token_hash(token), expires_at=_now() + timedelta(hours=expires_in_hours),
                created_at=_now(),
            )
            share.branding = branding
            db.add(share)
            db.commit()
            db.refresh(share)
            return share, token

    def revoke_share(self, workspace_id: str, share_id: str) -> bool:
        with get_db() as db:
            share = db.query(ReportShareDB).filter(
                ReportShareDB.id == share_id, ReportShareDB.workspace_id == workspace_id
            ).first()
            if not share:
                return False
            share.revoked_at = _now()
            db.commit()
            return True

    def public_report(self, token: str) -> tuple[dict[str, Any], dict[str, Any]]:
        with get_db() as db:
            share = db.query(ReportShareDB).filter(ReportShareDB.token_hash == _token_hash(token)).first()
            if not share or share.revoked_at is not None or share.expires_at <= _now():
                raise ValueError("This report link is invalid, expired, or revoked.")
            run_id = share.run_id
            if not run_id:
                run = db.query(EvaluationRunDB).filter(
                    EvaluationRunDB.project_id == share.project_id,
                    EvaluationRunDB.status == RunStatus.COMPLETED.value,
                ).order_by(EvaluationRunDB.completed_at.desc(), EvaluationRunDB.created_at.desc()).first()
                if not run:
                    raise ValueError("This project has no completed run to share.")
                run_id = run.id
            branding = share.branding
        report = self.reports.build_run_report(run_id)
        report["sharing"] = {"expires_at": share.expires_at.isoformat(), "branding": branding}
        report["baseline_outcome"] = self._baseline_outcome(run_id)
        return report, branding

    def _baseline_outcome(self, run_id: str) -> Optional[dict[str, Any]]:
        with get_db() as db:
            run = db.query(EvaluationRunDB).filter(EvaluationRunDB.id == run_id).first()
            if not run or not run.project_id:
                return None
            baseline = db.query(EvaluationRunDB).filter(
                EvaluationRunDB.project_id == run.project_id, EvaluationRunDB.is_baseline == True,
                EvaluationRunDB.id != run_id,
            ).order_by(EvaluationRunDB.created_at.desc()).first()
        if not baseline:
            return {"status": "inconclusive", "reasons": ["No baseline has been marked for this project."]}
        return self.baselines.compare(run_id, baseline.id, 0.95, 0.05)

    def project_dashboard(self, workspace_id: str, project_id: str) -> dict[str, Any]:
        with get_db() as db:
            project = db.query(ProjectDB).filter(ProjectDB.id == project_id, ProjectDB.workspace_id == workspace_id).first()
            if not project:
                raise ValueError("Project not found in this workspace.")
            runs = db.query(EvaluationRunDB).filter(EvaluationRunDB.project_id == project_id).order_by(
                EvaluationRunDB.created_at.desc()
            ).limit(12).all()
            baseline = db.query(EvaluationRunDB).filter(
                EvaluationRunDB.project_id == project_id, EvaluationRunDB.is_baseline == True
            ).order_by(EvaluationRunDB.created_at.desc()).first()
            failures = db.query(EvaluationResultDB).join(EvaluationRunDB).filter(
                EvaluationRunDB.project_id == project_id,
                EvaluationResultDB.outcome.in_([EvaluationOutcome.GENERATION_ERROR.value, EvaluationOutcome.EVALUATION_ERROR.value]),
            ).order_by(EvaluationResultDB.id.desc()).limit(10).all()
            project_data = {"project_id": project.id, "client_name": project.client_name, "project_name": project.name}
            run_ids = [run.id for run in runs if run.status == RunStatus.COMPLETED.value]
        coverage_trend, quality_trend = [], []
        for run_id in reversed(run_ids):
            metrics = get_run_metrics(run_id).get("evaluators", {})
            preferred = metrics.get("exact_match") or next(iter(metrics.values()), None)
            if preferred:
                coverage_trend.append({"run_id": run_id, "coverage": preferred["evaluation_coverage"]})
                quality_trend.append({"run_id": run_id, "average_score": preferred["avg_score"], "pass_rate": preferred["pass_rate"]})
        latest = next((run for run in runs if run.status == RunStatus.COMPLETED.value), None)
        release = self._baseline_outcome(latest.id) if latest else None
        project_data.update({
            "latest_run": {"run_id": latest.id, "status": latest.status, "created_at": latest.created_at, "is_simulated": latest.is_simulated} if latest else None,
            "baseline_run_id": baseline.id if baseline else None,
            "release_check": release,
            "coverage_trend": coverage_trend,
            "quality_trend": quality_trend,
            "recent_failures": [
                {"run_id": item.run_id, "example_id": item.example_id, "evaluator": item.evaluator_name,
                 "outcome": item.outcome, "error": sanitize_error(item.error_message) if item.error_message else None}
                for item in failures
            ],
        })
        return project_data
