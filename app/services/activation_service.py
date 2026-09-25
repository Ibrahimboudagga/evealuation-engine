"""Privacy-safe workspace activation milestones and funnel reporting."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from app.database.connection import get_db
from app.database.models import ActivationEventDB, EvaluationRunDB, PairwiseRunDB, ProjectDB


MILESTONES = (
    "workspace_created",
    "demo_seeded",
    "first_project",
    "first_dataset",
    "first_template",
    "first_completed_run",
    "first_shared_report",
    "first_recurring_schedule",
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ActivationService:
    """Records only first-occurrence timestamps and an aggregate count of one."""

    def record_first(self, workspace_id: str, event_name: str) -> bool:
        if event_name not in MILESTONES:
            raise ValueError("Unsupported activation milestone.")
        with get_db() as db:
            existing = db.query(ActivationEventDB).filter(
                ActivationEventDB.workspace_id == workspace_id,
                ActivationEventDB.event_name == event_name,
            ).first()
            if existing:
                return False
            db.add(ActivationEventDB(
                id=str(uuid.uuid4()), workspace_id=workspace_id, event_name=event_name,
                occurred_at=_now(), occurrence_count=1,
            ))
            db.commit()
            return True

    def record_completed_run(self, run_id: str, pairwise: bool = False) -> bool:
        model = PairwiseRunDB if pairwise else EvaluationRunDB
        with get_db() as db:
            run = db.query(model).filter(model.id == run_id).first()
            project = db.query(ProjectDB).filter(ProjectDB.id == run.project_id).first() if run and run.project_id else None
            workspace_id = project.workspace_id if project else None
        return self.record_first(workspace_id, "first_completed_run") if workspace_id else False

    def funnel(self, workspace_id: str) -> dict:
        with get_db() as db:
            events = db.query(ActivationEventDB).filter(
                ActivationEventDB.workspace_id == workspace_id
            ).all()
        by_name = {event.event_name: event for event in events}
        milestones = [
            {
                "name": name,
                "completed": name in by_name,
                "occurred_at": by_name[name].occurred_at if name in by_name else None,
                "count": by_name[name].occurrence_count if name in by_name else 0,
            }
            for name in MILESTONES
        ]
        completed_count = sum(item["completed"] for item in milestones)
        next_milestone = next((item["name"] for item in milestones if not item["completed"]), None)
        return {
            "workspace_id": workspace_id,
            "completed_milestones": completed_count,
            "total_milestones": len(MILESTONES),
            "activation_rate": completed_count / len(MILESTONES),
            "next_milestone": next_milestone,
            "milestones": milestones,
        }
