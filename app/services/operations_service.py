"""Audit logging and safe operational maintenance for agency workspaces."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.database.connection import get_db
from app.database.models import (
    AuditEventDB,
    DatasetDB,
    DatasetVersionDB,
    EvaluationResultDB,
    EvaluationRunDB,
    EvaluationScheduleDB,
    PairwiseComparisonDB,
    PairwiseRunDB,
    ProjectAccessDB,
    ProjectDB,
    ProjectReleasePolicyRevisionDB,
    ReportShareDB,
    RunAttemptDB,
    ScenarioRunDB,
    ScenarioShareDB,
    ScenarioSuiteDB,
    ScheduleExecutionDB,
    WorkspaceDB,
)
from app.services.identity_service import AuthContext


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class OperationsService:
    def record(
        self, workspace_id: str, action: str, entity_type: str, entity_id: Optional[str] = None,
        project_id: Optional[str] = None, context: Optional[AuthContext] = None,
        metadata: Optional[dict[str, Any]] = None,
        db_session: Optional[Session] = None,
    ) -> None:
        event = AuditEventDB(
            id=str(uuid.uuid4()), workspace_id=workspace_id, action=action, entity_type=entity_type,
            entity_id=entity_id, project_id=project_id,
            actor_user_id=context.user_id if context else None,
            actor_email=context.email if context else None, created_at=_now(),
        )
        event.metadata_dict = metadata
        if db_session is not None:
            db_session.add(event)
            return
        with get_db() as db:
            db.add(event)
            db.commit()

    def list_events(self, workspace_id: str, project_id: Optional[str] = None, limit: int = 100) -> list[AuditEventDB]:
        with get_db() as db:
            query = db.query(AuditEventDB).filter(AuditEventDB.workspace_id == workspace_id)
            if project_id:
                query = query.filter(AuditEventDB.project_id == project_id)
            return query.order_by(AuditEventDB.created_at.desc()).limit(limit).all()

    @staticmethod
    def _retention_payload(workspace: WorkspaceDB) -> dict[str, Any]:
        return {
            "retention_days": workspace.retention_days,
            "content_retention_enabled": workspace.content_retention_enabled,
            "legal_hold": workspace.legal_hold_at is not None,
            "legal_hold_reason": workspace.legal_hold_reason,
            "last_retention_applied_at": workspace.last_retention_applied_at,
        }

    def retention_settings(self, workspace_id: str) -> dict[str, Any]:
        with get_db() as db:
            workspace = db.query(WorkspaceDB).filter(WorkspaceDB.id == workspace_id).first()
            if not workspace:
                raise ValueError("Workspace not found")
            return self._retention_payload(workspace)

    def set_retention_settings(
        self,
        workspace_id: str,
        days: int,
        content_retention_enabled: Optional[bool],
        legal_hold: Optional[bool],
        legal_hold_reason: Optional[str],
    ) -> dict[str, Any]:
        with get_db() as db:
            workspace = db.query(WorkspaceDB).filter(
                WorkspaceDB.id == workspace_id
            ).with_for_update().first()
            if not workspace:
                raise ValueError("Workspace not found")
            workspace.retention_days = days
            if content_retention_enabled is not None:
                workspace.content_retention_enabled = content_retention_enabled
            if legal_hold is True:
                reason = (legal_hold_reason or workspace.legal_hold_reason or "").strip()
                if not reason:
                    raise ValueError("A legal-hold reason is required when legal hold is enabled.")
                workspace.legal_hold_at = workspace.legal_hold_at or _now()
                workspace.legal_hold_reason = reason
            elif legal_hold is False:
                workspace.legal_hold_at = None
                workspace.legal_hold_reason = None
            elif legal_hold_reason is not None:
                if workspace.legal_hold_at is None:
                    raise ValueError("Enable legal hold before setting its reason.")
                reason = legal_hold_reason.strip()
                if not reason:
                    raise ValueError("A legal-hold reason cannot be blank.")
                workspace.legal_hold_reason = reason
            db.commit()
            return self._retention_payload(workspace)

    def apply_retention(
        self, workspace_id: str, context: Optional[AuthContext] = None
    ) -> dict[str, Any]:
        with get_db() as db:
            workspace = db.query(WorkspaceDB).filter(
                WorkspaceDB.id == workspace_id
            ).with_for_update().first()
            if not workspace:
                raise ValueError("Workspace not found")
            cutoff = _now() - timedelta(days=workspace.retention_days)
            result: dict[str, Any] = {
                "expired_share_links_deleted": 0,
                "audit_events_deleted": 0,
                "evaluation_results_deleted": 0,
                "evaluation_runs_deleted": 0,
                "pairwise_comparisons_deleted": 0,
                "pairwise_runs_deleted": 0,
                "scenario_shares_deleted": 0,
                "scenario_runs_deleted": 0,
                "scenario_suites_deleted": 0,
                "run_attempts_deleted": 0,
                "dataset_versions_deleted": 0,
                "content_retention_enabled": workspace.content_retention_enabled,
                "legal_hold": workspace.legal_hold_at is not None,
                # The same receipt is stored in the audit log, so keep it JSON
                # serializable. The API response schema parses the ISO value.
                "cutoff": cutoff.isoformat(),
            }

            # A legal hold freezes all governed deletion, including expired
            # share snapshots and audit evidence. Recording the attempted
            # application timestamp is operational metadata, not a deletion.
            if workspace.legal_hold_at is not None:
                workspace.last_retention_applied_at = _now()
                self.record(
                    workspace_id, "retention.applied", "workspace", workspace_id,
                    context=context, metadata=result, db_session=db,
                )
                db.commit()
                return result

            expired_shares = db.query(ReportShareDB).filter(
                ReportShareDB.workspace_id == workspace_id,
                ReportShareDB.expires_at < _now(),
            ).delete(synchronize_session=False)
            expired_shares += db.query(ScenarioShareDB).filter(
                ScenarioShareDB.workspace_id == workspace_id,
                ScenarioShareDB.expires_at < _now(),
            ).delete(synchronize_session=False)
            result["expired_share_links_deleted"] = expired_shares
            old_events = db.query(AuditEventDB).filter(
                AuditEventDB.workspace_id == workspace_id,
                AuditEventDB.created_at < cutoff,
            ).delete(synchronize_session=False)
            result["audit_events_deleted"] = old_events

            if workspace.content_retention_enabled and workspace.legal_hold_at is None:
                project_ids = [project_id for (project_id,) in db.query(ProjectDB.id).filter(
                    ProjectDB.workspace_id == workspace_id
                ).all()]
                terminal = ("completed", "failed", "interrupted")
                evaluation_ids = [run_id for (run_id,) in db.query(EvaluationRunDB.id).filter(
                    EvaluationRunDB.project_id.in_(project_ids),
                    EvaluationRunDB.status.in_(terminal),
                    or_(
                        and_(
                            EvaluationRunDB.completed_at.is_not(None),
                            EvaluationRunDB.completed_at < cutoff,
                        ),
                        and_(
                            EvaluationRunDB.completed_at.is_(None),
                            EvaluationRunDB.created_at < cutoff,
                        ),
                    ),
                ).all()] if project_ids else []
                pairwise_ids = [run_id for (run_id,) in db.query(PairwiseRunDB.id).filter(
                    PairwiseRunDB.project_id.in_(project_ids),
                    PairwiseRunDB.status.in_(terminal),
                    or_(
                        and_(
                            PairwiseRunDB.completed_at.is_not(None),
                            PairwiseRunDB.completed_at < cutoff,
                        ),
                        and_(
                            PairwiseRunDB.completed_at.is_(None),
                            PairwiseRunDB.created_at < cutoff,
                        ),
                    ),
                ).all()] if project_ids else []
                scenario_ids = [run_id for (run_id,) in db.query(ScenarioRunDB.id).filter(
                    ScenarioRunDB.workspace_id == workspace_id,
                    ScenarioRunDB.finished_at < cutoff,
                ).all()]

                if evaluation_ids:
                    db.query(ScheduleExecutionDB).filter(
                        ScheduleExecutionDB.run_id.in_(evaluation_ids)
                    ).update({"run_id": None, "status": "retained_data_deleted"}, synchronize_session=False)
                    result["expired_share_links_deleted"] += db.query(ReportShareDB).filter(
                        ReportShareDB.run_id.in_(evaluation_ids)
                    ).delete(synchronize_session=False)
                    result["evaluation_results_deleted"] = db.query(EvaluationResultDB).filter(
                        EvaluationResultDB.run_id.in_(evaluation_ids)
                    ).delete(synchronize_session=False)
                if pairwise_ids:
                    result["pairwise_comparisons_deleted"] = db.query(PairwiseComparisonDB).filter(
                        PairwiseComparisonDB.run_id.in_(pairwise_ids)
                    ).delete(synchronize_session=False)
                all_run_ids = evaluation_ids + pairwise_ids
                if all_run_ids:
                    result["run_attempts_deleted"] = db.query(RunAttemptDB).filter(
                        RunAttemptDB.run_id.in_(all_run_ids)
                    ).delete(synchronize_session=False)
                if evaluation_ids:
                    result["evaluation_runs_deleted"] = db.query(EvaluationRunDB).filter(
                        EvaluationRunDB.id.in_(evaluation_ids)
                    ).delete(synchronize_session=False)
                if pairwise_ids:
                    result["pairwise_runs_deleted"] = db.query(PairwiseRunDB).filter(
                        PairwiseRunDB.id.in_(pairwise_ids)
                    ).delete(synchronize_session=False)
                if scenario_ids:
                    result["scenario_shares_deleted"] = db.query(ScenarioShareDB).filter(
                        ScenarioShareDB.run_id.in_(scenario_ids)
                    ).delete(synchronize_session=False)
                    result["scenario_runs_deleted"] = db.query(ScenarioRunDB).filter(
                        ScenarioRunDB.id.in_(scenario_ids)
                    ).delete(synchronize_session=False)

                old_suite_ids = [suite_id for (suite_id,) in db.query(ScenarioSuiteDB.id).filter(
                    ScenarioSuiteDB.workspace_id == workspace_id,
                    ScenarioSuiteDB.created_at < cutoff,
                ).all()]
                deletable_suites = [suite_id for suite_id in old_suite_ids if not db.query(
                    ScenarioRunDB.id
                ).filter(ScenarioRunDB.suite_id == suite_id).first()]
                if deletable_suites:
                    result["scenario_suites_deleted"] = db.query(ScenarioSuiteDB).filter(
                        ScenarioSuiteDB.id.in_(deletable_suites)
                    ).delete(synchronize_session=False)

                old_version_ids = [version_id for (version_id,) in db.query(DatasetVersionDB.id).join(
                    DatasetDB, DatasetVersionDB.dataset_id == DatasetDB.id
                ).join(ProjectDB, DatasetDB.project_id == ProjectDB.id).filter(
                    ProjectDB.workspace_id == workspace_id,
                    DatasetVersionDB.is_active == False,
                    DatasetVersionDB.created_at < cutoff,
                ).all()]
                deletable_versions = []
                for version_id in old_version_ids:
                    referenced = (
                        db.query(EvaluationRunDB.id).filter(EvaluationRunDB.dataset_version_id == version_id).first()
                        or db.query(PairwiseRunDB.id).filter(PairwiseRunDB.dataset_version_id == version_id).first()
                        or db.query(EvaluationScheduleDB.id).filter(EvaluationScheduleDB.dataset_version_id == version_id).first()
                    )
                    if not referenced:
                        deletable_versions.append(version_id)
                if deletable_versions:
                    result["dataset_versions_deleted"] = db.query(DatasetVersionDB).filter(
                        DatasetVersionDB.id.in_(deletable_versions)
                    ).delete(synchronize_session=False)

            workspace.last_retention_applied_at = _now()
            self.record(
                workspace_id, "retention.applied", "workspace", workspace_id,
                context=context, metadata=result, db_session=db,
            )
            db.commit()
            return result

    def project_deletion_preview(self, workspace_id: str, project_id: str) -> dict[str, Any]:
        with get_db() as db:
            workspace = db.query(WorkspaceDB).filter(WorkspaceDB.id == workspace_id).first()
            project = db.query(ProjectDB).filter(
                ProjectDB.id == project_id, ProjectDB.workspace_id == workspace_id
            ).first()
            if not workspace or not project:
                raise ValueError("Project not found in this workspace")
            dataset_ids = [row[0] for row in db.query(DatasetDB.id).filter(DatasetDB.project_id == project_id)]
            evaluation_ids = [row[0] for row in db.query(EvaluationRunDB.id).filter(EvaluationRunDB.project_id == project_id)]
            pairwise_ids = [row[0] for row in db.query(PairwiseRunDB.id).filter(PairwiseRunDB.project_id == project_id)]
            scenario_ids = [row[0] for row in db.query(ScenarioRunDB.id).filter(ScenarioRunDB.project_id == project_id)]
            counts = {
                "datasets": len(dataset_ids),
                "dataset_versions": db.query(DatasetVersionDB).filter(DatasetVersionDB.dataset_id.in_(dataset_ids)).count() if dataset_ids else 0,
                "evaluation_runs": len(evaluation_ids),
                "evaluation_results": db.query(EvaluationResultDB).filter(EvaluationResultDB.run_id.in_(evaluation_ids)).count() if evaluation_ids else 0,
                "pairwise_runs": len(pairwise_ids),
                "pairwise_comparisons": db.query(PairwiseComparisonDB).filter(PairwiseComparisonDB.run_id.in_(pairwise_ids)).count() if pairwise_ids else 0,
                "scenario_suites": db.query(ScenarioSuiteDB).filter(ScenarioSuiteDB.project_id == project_id).count(),
                "scenario_runs": len(scenario_ids),
                "scenario_shares": db.query(ScenarioShareDB).filter(ScenarioShareDB.run_id.in_(scenario_ids)).count() if scenario_ids else 0,
                "report_shares": db.query(ReportShareDB).filter(ReportShareDB.project_id == project_id).count(),
                "schedules": db.query(EvaluationScheduleDB).filter(EvaluationScheduleDB.dataset_id.in_(dataset_ids)).count() if dataset_ids else 0,
                "project_access_grants": db.query(ProjectAccessDB).filter(ProjectAccessDB.project_id == project_id).count(),
                "release_policy_revisions": db.query(ProjectReleasePolicyRevisionDB).filter(ProjectReleasePolicyRevisionDB.project_id == project_id).count(),
                "audit_events": db.query(AuditEventDB).filter(
                    AuditEventDB.workspace_id == workspace_id,
                    AuditEventDB.project_id == project_id,
                ).count(),
                "run_attempts": db.query(RunAttemptDB).filter(
                    RunAttemptDB.run_id.in_(evaluation_ids + pairwise_ids)
                ).count() if evaluation_ids or pairwise_ids else 0,
                "schedule_executions": db.query(ScheduleExecutionDB).filter(
                    ScheduleExecutionDB.schedule_id.in_(
                        db.query(EvaluationScheduleDB.id).filter(
                            EvaluationScheduleDB.dataset_id.in_(dataset_ids)
                        )
                    )
                ).count() if dataset_ids else 0,
                "projects": 1,
            }
            return {"project_id": project.id, "project_name": project.name, "counts": counts,
                    "preview": True, "legal_hold": workspace.legal_hold_at is not None}

    def purge_project_data(
        self, workspace_id: str, project_id: str, confirm_project_name: str,
        context: Optional[AuthContext] = None,
    ) -> dict[str, Any]:
        with get_db() as db:
            workspace = db.query(WorkspaceDB).filter(
                WorkspaceDB.id == workspace_id
            ).with_for_update().first()
            project = db.query(ProjectDB).filter(
                ProjectDB.id == project_id, ProjectDB.workspace_id == workspace_id
            ).with_for_update().first()
            if not workspace or not project:
                raise ValueError("Project not found in this workspace")
            if workspace.legal_hold_at is not None:
                raise PermissionError("Customer data deletion is blocked while the workspace legal hold is active.")
            if confirm_project_name != project.name:
                raise ValueError("confirm_project_name must exactly match the project name")

            project_name = project.name
            dataset_ids = [row[0] for row in db.query(DatasetDB.id).filter(DatasetDB.project_id == project_id)]
            evaluation_ids = [row[0] for row in db.query(EvaluationRunDB.id).filter(EvaluationRunDB.project_id == project_id)]
            pairwise_ids = [row[0] for row in db.query(PairwiseRunDB.id).filter(PairwiseRunDB.project_id == project_id)]
            scenario_ids = [row[0] for row in db.query(ScenarioRunDB.id).filter(ScenarioRunDB.project_id == project_id)]
            schedule_ids = [row[0] for row in db.query(EvaluationScheduleDB.id).filter(
                EvaluationScheduleDB.dataset_id.in_(dataset_ids)
            )] if dataset_ids else []

            counts: dict[str, int] = {}
            share_query = db.query(ReportShareDB).filter(ReportShareDB.project_id == project_id)
            if evaluation_ids:
                share_query = db.query(ReportShareDB).filter(
                    (ReportShareDB.project_id == project_id) |
                    ReportShareDB.run_id.in_(evaluation_ids)
                )
            counts["report_shares"] = share_query.delete(synchronize_session=False)
            counts["scenario_shares"] = db.query(ScenarioShareDB).filter(
                ScenarioShareDB.run_id.in_(scenario_ids)
            ).delete(synchronize_session=False) if scenario_ids else 0
            counts["schedule_executions"] = db.query(ScheduleExecutionDB).filter(
                ScheduleExecutionDB.schedule_id.in_(schedule_ids)
            ).delete(synchronize_session=False) if schedule_ids else 0
            counts["schedules"] = db.query(EvaluationScheduleDB).filter(
                EvaluationScheduleDB.id.in_(schedule_ids)
            ).delete(synchronize_session=False) if schedule_ids else 0
            all_run_ids = evaluation_ids + pairwise_ids
            counts["run_attempts"] = db.query(RunAttemptDB).filter(
                RunAttemptDB.run_id.in_(all_run_ids)
            ).delete(synchronize_session=False) if all_run_ids else 0
            counts["evaluation_results"] = db.query(EvaluationResultDB).filter(
                EvaluationResultDB.run_id.in_(evaluation_ids)
            ).delete(synchronize_session=False) if evaluation_ids else 0
            counts["pairwise_comparisons"] = db.query(PairwiseComparisonDB).filter(
                PairwiseComparisonDB.run_id.in_(pairwise_ids)
            ).delete(synchronize_session=False) if pairwise_ids else 0
            counts["evaluation_runs"] = db.query(EvaluationRunDB).filter(
                EvaluationRunDB.id.in_(evaluation_ids)
            ).delete(synchronize_session=False) if evaluation_ids else 0
            counts["pairwise_runs"] = db.query(PairwiseRunDB).filter(
                PairwiseRunDB.id.in_(pairwise_ids)
            ).delete(synchronize_session=False) if pairwise_ids else 0
            counts["scenario_runs"] = db.query(ScenarioRunDB).filter(
                ScenarioRunDB.id.in_(scenario_ids)
            ).delete(synchronize_session=False) if scenario_ids else 0
            counts["scenario_suites"] = db.query(ScenarioSuiteDB).filter(
                ScenarioSuiteDB.project_id == project_id
            ).delete(synchronize_session=False)
            counts["dataset_versions"] = db.query(DatasetVersionDB).filter(
                DatasetVersionDB.dataset_id.in_(dataset_ids)
            ).delete(synchronize_session=False) if dataset_ids else 0
            counts["datasets"] = db.query(DatasetDB).filter(
                DatasetDB.id.in_(dataset_ids)
            ).delete(synchronize_session=False) if dataset_ids else 0
            counts["project_access_grants"] = db.query(ProjectAccessDB).filter(
                ProjectAccessDB.project_id == project_id
            ).delete(synchronize_session=False)
            counts["release_policy_revisions"] = db.query(ProjectReleasePolicyRevisionDB).filter(
                ProjectReleasePolicyRevisionDB.project_id == project_id
            ).delete(synchronize_session=False)
            # Remove historical project-scoped audit metadata before writing a
            # new, content-free deletion receipt in this same transaction.
            counts["audit_events"] = db.query(AuditEventDB).filter(
                AuditEventDB.workspace_id == workspace_id,
                AuditEventDB.project_id == project_id,
            ).delete(synchronize_session=False)
            db.delete(project)
            counts["projects"] = 1
            self.record(
                workspace_id,
                "project.customer_data_deleted",
                "project",
                project_id,
                project_id,
                context,
                {"counts": counts},
                db,
            )
            db.commit()
            return {"project_id": project_id, "project_name": project_name,
                    "counts": counts, "preview": False, "legal_hold": False}
