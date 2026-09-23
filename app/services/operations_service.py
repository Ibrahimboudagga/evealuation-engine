"""Audit logging and safe operational maintenance for agency workspaces."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.database.connection import get_db
from app.database.models import AuditEventDB, ReportShareDB, WorkspaceDB
from app.services.identity_service import AuthContext


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class OperationsService:
    def record(
        self, workspace_id: str, action: str, entity_type: str, entity_id: Optional[str] = None,
        project_id: Optional[str] = None, context: Optional[AuthContext] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        event = AuditEventDB(
            id=str(uuid.uuid4()), workspace_id=workspace_id, action=action, entity_type=entity_type,
            entity_id=entity_id, project_id=project_id,
            actor_user_id=context.user_id if context else None,
            actor_email=context.email if context else None, created_at=_now(),
        )
        event.metadata_dict = metadata
        with get_db() as db:
            db.add(event)
            db.commit()

    def list_events(self, workspace_id: str, project_id: Optional[str] = None, limit: int = 100) -> list[AuditEventDB]:
        with get_db() as db:
            query = db.query(AuditEventDB).filter(AuditEventDB.workspace_id == workspace_id)
            if project_id:
                query = query.filter(AuditEventDB.project_id == project_id)
            return query.order_by(AuditEventDB.created_at.desc()).limit(limit).all()

    def retention_days(self, workspace_id: str) -> int:
        with get_db() as db:
            workspace = db.query(WorkspaceDB).filter(WorkspaceDB.id == workspace_id).first()
            if not workspace:
                raise ValueError("Workspace not found")
            return workspace.retention_days

    def set_retention_days(self, workspace_id: str, days: int) -> int:
        with get_db() as db:
            workspace = db.query(WorkspaceDB).filter(WorkspaceDB.id == workspace_id).first()
            if not workspace:
                raise ValueError("Workspace not found")
            workspace.retention_days = days
            db.commit()
            return days

    def apply_retention(self, workspace_id: str) -> dict[str, int]:
        with get_db() as db:
            workspace = db.query(WorkspaceDB).filter(WorkspaceDB.id == workspace_id).first()
            if not workspace:
                raise ValueError("Workspace not found")
            cutoff = _now() - timedelta(days=workspace.retention_days)
            expired_shares = db.query(ReportShareDB).filter(
                ReportShareDB.workspace_id == workspace_id,
                ReportShareDB.expires_at < _now(),
            ).delete(synchronize_session=False)
            old_events = db.query(AuditEventDB).filter(
                AuditEventDB.workspace_id == workspace_id,
                AuditEventDB.created_at < cutoff,
            ).delete(synchronize_session=False)
            db.commit()
            return {"expired_share_links_deleted": expired_shares, "audit_events_deleted": old_events}
