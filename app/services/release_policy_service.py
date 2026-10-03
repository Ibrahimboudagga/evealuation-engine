"""Immutable, owner-approved project release policy revisions."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import func

from app.database.connection import get_db
from app.database.models import ProjectDB, ProjectReleasePolicyRevisionDB
from app.schemas.release import ReleaseRules, ScenarioReleaseRules
from app.services.identity_service import AuthContext, ROLE_OWNER


PolicyType = Literal["model", "scenario"]


class ReleasePolicyIntegrityError(ValueError):
    """Raised when stored policy evidence no longer matches its signed digest."""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _canonical_rules(policy_type: PolicyType, rules: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    model = ReleaseRules.model_validate(rules) if policy_type == "model" else ScenarioReleaseRules.model_validate(rules)
    normalized = model.model_dump(mode="json", exclude_none=True)
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return normalized, encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def policy_snapshot(revision: ProjectReleasePolicyRevisionDB | None) -> dict[str, Any]:
    if revision is None:
        return {"governance_verified": False}
    return {
        "id": revision.id,
        "type": revision.policy_type,
        "version": revision.version_number,
        "sha256": revision.rules_sha256,
        "approved_by_user_id": revision.approved_by_user_id,
        "approved_at": revision.approved_at.isoformat() if revision.approved_at else None,
        "governance_verified": bool(revision.approved_at and revision.approved_by_user_id),
    }


class ReleasePolicyService:
    def create_revision(
        self,
        context: AuthContext,
        project_id: str,
        policy_type: PolicyType,
        rules: dict[str, Any],
        change_note: str | None = None,
    ) -> ProjectReleasePolicyRevisionDB:
        normalized, encoded, rules_hash = _canonical_rules(policy_type, rules)
        with get_db() as db:
            project = db.query(ProjectDB).filter(
                ProjectDB.id == project_id,
                ProjectDB.workspace_id == context.workspace_id,
            ).with_for_update().first()
            if not project:
                raise ValueError("Project not found in this workspace")
            version = (
                db.query(func.max(ProjectReleasePolicyRevisionDB.version_number))
                .filter(
                    ProjectReleasePolicyRevisionDB.project_id == project_id,
                    ProjectReleasePolicyRevisionDB.policy_type == policy_type,
                )
                .scalar()
                or 0
            ) + 1
            revision = ProjectReleasePolicyRevisionDB(
                id=str(uuid.uuid4()),
                workspace_id=context.workspace_id,
                project_id=project_id,
                policy_type=policy_type,
                version_number=version,
                rules_json=encoded,
                rules_sha256=rules_hash,
                change_note=change_note.strip() if change_note else None,
                created_by_user_id=context.user_id,
                created_at=_now(),
            )
            # Accessing this property verifies that the canonical payload is
            # valid JSON before persistence; callers receive normalized rules.
            assert revision.rules == normalized
            db.add(revision)
            db.commit()
            db.refresh(revision)
            db.expunge(revision)
            return revision

    def list_revisions(
        self, workspace_id: str, project_id: str, policy_type: PolicyType | None = None
    ) -> list[ProjectReleasePolicyRevisionDB]:
        with get_db() as db:
            query = db.query(ProjectReleasePolicyRevisionDB).filter(
                ProjectReleasePolicyRevisionDB.workspace_id == workspace_id,
                ProjectReleasePolicyRevisionDB.project_id == project_id,
            )
            if policy_type:
                query = query.filter(ProjectReleasePolicyRevisionDB.policy_type == policy_type)
            rows = query.order_by(
                ProjectReleasePolicyRevisionDB.policy_type,
                ProjectReleasePolicyRevisionDB.version_number.desc(),
            ).all()
            for row in rows:
                db.expunge(row)
            return rows

    def approve(
        self, context: AuthContext, project_id: str, revision_id: str
    ) -> ProjectReleasePolicyRevisionDB:
        if context.role != ROLE_OWNER:
            raise PermissionError("Only workspace owners can approve release policy revisions")
        with get_db() as db:
            revision = db.query(ProjectReleasePolicyRevisionDB).filter(
                ProjectReleasePolicyRevisionDB.id == revision_id,
                ProjectReleasePolicyRevisionDB.project_id == project_id,
                ProjectReleasePolicyRevisionDB.workspace_id == context.workspace_id,
            ).with_for_update().first()
            if not revision:
                raise ValueError("Release policy revision not found")
            if not self.verify_revision(revision):
                raise ReleasePolicyIntegrityError(
                    "Release policy revision failed its integrity check and cannot be approved."
                )
            if revision.approved_at is None:
                revision.approved_by_user_id = context.user_id
                revision.approved_at = _now()
                db.commit()
                db.refresh(revision)
            db.expunge(revision)
            return revision

    def latest_approved(
        self, project_id: str | None, policy_type: PolicyType
    ) -> ProjectReleasePolicyRevisionDB | None:
        if not project_id:
            return None
        with get_db() as db:
            revision = db.query(ProjectReleasePolicyRevisionDB).filter(
                ProjectReleasePolicyRevisionDB.project_id == project_id,
                ProjectReleasePolicyRevisionDB.policy_type == policy_type,
                ProjectReleasePolicyRevisionDB.approved_at.is_not(None),
            ).order_by(ProjectReleasePolicyRevisionDB.version_number.desc()).first()
            if revision:
                if not self.verify_revision(revision):
                    raise ReleasePolicyIntegrityError(
                        "The approved project release policy failed its integrity check."
                    )
                db.expunge(revision)
            return revision

    def approved_by_id(self, revision_id: str | None) -> ProjectReleasePolicyRevisionDB | None:
        if not revision_id:
            return None
        with get_db() as db:
            revision = db.query(ProjectReleasePolicyRevisionDB).filter(
                ProjectReleasePolicyRevisionDB.id == revision_id,
                ProjectReleasePolicyRevisionDB.approved_at.is_not(None),
            ).first()
            if revision:
                if not self.verify_revision(revision):
                    raise ReleasePolicyIntegrityError(
                        "The approved project release policy failed its integrity check."
                    )
                db.expunge(revision)
            return revision

    @staticmethod
    def verify_revision(revision: ProjectReleasePolicyRevisionDB) -> bool:
        try:
            _, encoded, expected_hash = _canonical_rules(revision.policy_type, revision.rules)
        except (ValueError, TypeError):
            return False
        return encoded == revision.rules_json and expected_hash == revision.rules_sha256
