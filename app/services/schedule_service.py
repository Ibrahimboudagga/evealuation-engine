"""Recurring template evaluation schedules and execution history."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional

from app.database.connection import get_db
from app.database.models import (
    DatasetDB,
    DatasetVersionDB,
    EvaluationScheduleDB,
    EvaluationTemplateDB,
    ProjectDB,
    ScheduleExecutionDB,
)
from app.errors import sanitize_error
from app.evaluators.registry import EvaluatorRegistry
from app.providers.factory import ProviderFactory
from app.runners.eval_runner import EvaluationRunner
from app.services.operations_service import OperationsService
from app.services.provider_connection_service import ProviderConnectionService
from app.services.usage_service import UsageService


FREQUENCIES = {"daily": timedelta(days=1), "weekly": timedelta(days=7)}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ScheduleService:
    def __init__(self) -> None:
        self._connections = ProviderConnectionService()
        self._operations = OperationsService()
        self._usage = UsageService()

    def create(
        self,
        workspace_id: str,
        template_id: str,
        dataset_id: str,
        dataset_version_id: str,
        frequency: str,
        next_execution_at: Optional[datetime] = None,
    ) -> EvaluationScheduleDB:
        if frequency not in FREQUENCIES:
            raise ValueError("Only daily and weekly schedules are supported.")
        with get_db() as db:
            template = db.query(EvaluationTemplateDB).filter(
                EvaluationTemplateDB.id == template_id,
                EvaluationTemplateDB.workspace_id == workspace_id,
            ).first()
            version = db.query(DatasetVersionDB).filter(
                DatasetVersionDB.id == dataset_version_id,
                DatasetVersionDB.dataset_id == dataset_id,
            ).first()
            dataset = db.query(DatasetDB).filter(DatasetDB.id == dataset_id).first()
            project = None
            if dataset and dataset.project_id:
                project = db.query(ProjectDB).filter(
                    ProjectDB.id == dataset.project_id,
                    ProjectDB.workspace_id == workspace_id,
                ).first()
            if not template or not dataset or not version or not project:
                raise ValueError("Template and dataset version must belong to this workspace.")
            now = _now()
            schedule = EvaluationScheduleDB(
                id=str(uuid.uuid4()),
                workspace_id=workspace_id,
                template_id=template_id,
                dataset_id=dataset_id,
                dataset_version_id=dataset_version_id,
                frequency=frequency,
                next_execution_at=(next_execution_at or now).replace(tzinfo=None),
                active=True,
                created_at=now,
                updated_at=now,
            )
            db.add(schedule)
            db.commit()
            db.refresh(schedule)
            return schedule

    def list(self, workspace_id: str) -> list[EvaluationScheduleDB]:
        with get_db() as db:
            schedules = db.query(EvaluationScheduleDB).filter(
                EvaluationScheduleDB.workspace_id == workspace_id
            ).order_by(EvaluationScheduleDB.next_execution_at).all()
            for schedule in schedules:
                _ = list(schedule.executions)
            return schedules

    def set_active(self, workspace_id: str, schedule_id: str, active: bool) -> Optional[EvaluationScheduleDB]:
        with get_db() as db:
            schedule = db.query(EvaluationScheduleDB).filter(
                EvaluationScheduleDB.id == schedule_id,
                EvaluationScheduleDB.workspace_id == workspace_id,
            ).first()
            if not schedule:
                return None
            schedule.active = active
            schedule.updated_at = _now()
            db.commit()
            db.refresh(schedule)
            return schedule

    def overdue_count(self) -> int:
        with get_db() as db:
            return db.query(EvaluationScheduleDB).filter(
                EvaluationScheduleDB.active == True,
                EvaluationScheduleDB.next_execution_at < _now(),
            ).count()

    def _provider(self, workspace_id: str, connection_id: str, model_override: Optional[str]):
        connection = self._connections.resolve(workspace_id, connection_id)
        return ProviderFactory.create(
            provider=connection.provider,
            model_id=model_override or connection.default_model,
            api_key=connection.api_key,
            base_url=connection.base_url,
            allow_unauthenticated=connection.allow_unauthenticated,
        ), connection

    def _build_runner(self, schedule: EvaluationScheduleDB, template: EvaluationTemplateDB) -> EvaluationRunner:
        settings = template.settings
        candidate, candidate_connection = self._provider(
            schedule.workspace_id, settings["candidate_connection_id"], settings.get("candidate_model")
        )
        judge, judge_connection = self._provider(
            schedule.workspace_id, settings["evaluator_connection_id"], settings.get("evaluator_model")
        )
        requested_configuration = {
            "candidate": {
                "provider": candidate_connection.provider,
                "model": settings.get("candidate_model") or candidate_connection.default_model,
                "connection_id": candidate_connection.id,
                "base_url": candidate_connection.base_url,
                "allow_unauthenticated": candidate_connection.allow_unauthenticated,
            },
            "judge": {
                "provider": judge_connection.provider,
                "model": settings.get("evaluator_model") or judge_connection.default_model,
                "connection_id": judge_connection.id,
                "base_url": judge_connection.base_url,
                "allow_unauthenticated": judge_connection.allow_unauthenticated,
            },
            "judge_prompt_template": settings.get("judge_prompt_template"),
            "template_execution": {"timeout_seconds": settings.get("timeout_seconds", 60.0)},
            "release_rules": {
                "coverage_minimum": settings.get("coverage_minimum"),
                "exact_match_pass_rate_max_drop": settings.get("exact_match_pass_rate_max_drop"),
            },
            "report_preferences": settings.get("report_preferences"),
            "schedule_id": schedule.id,
        }
        return EvaluationRunner(
            candidate,
            EvaluatorRegistry(judge_provider=judge, judge_prompt_template=settings.get("judge_prompt_template")),
            concurrency_limit=int(settings.get("concurrency", 5)),
            execution_timeout_seconds=float(settings.get("timeout_seconds", 60.0)),
            requested_configuration=requested_configuration,
        )

    @staticmethod
    def _next_after(current: datetime, frequency: str, now: datetime) -> datetime:
        next_execution = current
        step = FREQUENCIES[frequency]
        while next_execution <= now:
            next_execution += step
        return next_execution

    def trigger_due(self) -> dict[str, int]:
        """Create one queued run per due schedule and append execution evidence."""
        now = _now()
        with get_db() as db:
            due_ids = [
                schedule_id
                for (schedule_id,) in db.query(EvaluationScheduleDB.id).filter(
                    EvaluationScheduleDB.active == True,
                    EvaluationScheduleDB.next_execution_at <= now,
                ).order_by(EvaluationScheduleDB.next_execution_at).all()
            ]

        result = {"triggered": 0, "failed": 0}
        for schedule_id in due_ids:
            if self._trigger_one(schedule_id, now):
                result["triggered"] += 1
            else:
                result["failed"] += 1
        return result

    def _trigger_one(self, schedule_id: str, now: datetime) -> bool:
        with get_db() as db:
            schedule = db.query(EvaluationScheduleDB).filter(EvaluationScheduleDB.id == schedule_id).first()
            if not schedule or not schedule.active or schedule.next_execution_at > now:
                return True
            template = db.query(EvaluationTemplateDB).filter(
                EvaluationTemplateDB.id == schedule.template_id,
                EvaluationTemplateDB.workspace_id == schedule.workspace_id,
            ).first()
            scheduled_for = schedule.next_execution_at
            schedule_data = {
                "id": schedule.id,
                "workspace_id": schedule.workspace_id,
                "template_id": schedule.template_id,
                "dataset_id": schedule.dataset_id,
                "dataset_version_id": schedule.dataset_version_id,
                "frequency": schedule.frequency,
            }
            template_settings = template.settings if template else None

        run_id: Optional[str] = None
        error_message: Optional[str] = None
        try:
            # Re-load via IDs inside the runner builder so encrypted credentials
            # are resolved only for execution and never added to schedule records.
            if template_settings is None:
                raise ValueError("Scheduled evaluation template was not found.")
            self._usage.assert_run_capacity(
                schedule_data["workspace_id"],
                schedule_data["dataset_id"],
                schedule_data["dataset_version_id"],
                2,
            )
            runner = self._build_runner(
                SimpleNamespace(**schedule_data),
                SimpleNamespace(settings=template_settings),
            )
            run_id = runner.create_run(
                dataset_id=schedule_data["dataset_id"],
                dataset_version_id=schedule_data["dataset_version_id"],
            )
        except Exception as error:
            error_message = sanitize_error(error)

        with get_db() as db:
            schedule = db.query(EvaluationScheduleDB).filter(EvaluationScheduleDB.id == schedule_data["id"]).first()
            if not schedule:
                return False
            schedule.last_executed_at = now
            schedule.next_execution_at = self._next_after(scheduled_for, schedule.frequency, now)
            schedule.last_error = error_message
            schedule.updated_at = now
            execution = ScheduleExecutionDB(
                id=str(uuid.uuid4()),
                schedule_id=schedule.id,
                run_id=run_id,
                scheduled_for=scheduled_for,
                created_at=now,
                status="queued" if run_id else "failed",
                error_message=error_message,
            )
            db.add(execution)
            db.commit()

        action = "schedule.triggered" if run_id else "schedule.trigger_failed"
        self._operations.record(
            schedule_data["workspace_id"],
            action,
            "evaluation_schedule",
            schedule_data["id"],
            metadata={"run_id": run_id, "scheduled_for": scheduled_for.isoformat(), "error": error_message},
        )
        return run_id is not None

