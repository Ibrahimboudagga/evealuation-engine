"""Single-process persistent evaluation queue with safe retry behavior."""

from __future__ import annotations

import asyncio
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional, Type

from app.database.connection import get_db
from app.database.models import (
    EvaluationResultDB,
    EvaluationRunDB,
    PairwiseComparisonDB,
    PairwiseRunDB,
    ProjectDB,
    WorkerStateDB,
)
from app.errors import RunCancellationRequested, sanitize_error
from app.evaluators.pairwise_judge import PairwiseJudgeEvaluator
from app.evaluators.registry import EvaluatorRegistry
from app.providers.factory import ProviderFactory
from app.runners.eval_runner import EvaluationRunner
from app.runners.pairwise_runner import PairwiseEvaluationRunner
from app.schemas.outcomes import EvaluationOutcome, RunStatus
from app.services.operations_service import OperationsService
from app.services.provider_connection_service import ProviderConnectionService
from app.services.schedule_service import ScheduleService


RunKind = Literal["evaluation_run", "pairwise_run"]
_TRANSIENT_MARKERS = (
    "timed out",
    "timeout",
    "rate limit",
    "too many requests",
    "temporarily unavailable",
    "service unavailable",
    "connection reset",
    "connection aborted",
    "connection refused",
    "gateway timeout",
    " 429",
    " 503",
)


def utcnow() -> datetime:
    """Use a naive UTC time because the current database schema stores DateTime."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def is_transient_provider_failure(error: BaseException | str) -> bool:
    """Classify retryable provider/transport failures without retrying judge mistakes."""
    message = str(error).lower()
    return any(marker in message for marker in _TRANSIENT_MARKERS)


@dataclass(frozen=True)
class ClaimedRun:
    kind: RunKind
    run_id: str
    project_id: Optional[str]
    workspace_id: Optional[str]
    configuration: dict[str, Any]


class QueueWorker:
    """Claims and executes at most one persisted run at a time.

    This service intentionally targets one in-process worker. Its claim data is
    persisted, while configuration is rehydrated from safe run snapshots and
    workspace provider connections so queued work can be resumed by the same
    application process without retaining raw credentials in a run record.
    """

    def __init__(
        self,
        worker_id: Optional[str] = None,
        poll_interval_seconds: float = 0.25,
        retry_base_seconds: float = 1.0,
        retry_cap_seconds: float = 60.0,
    ) -> None:
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:12]}"
        self.poll_interval_seconds = poll_interval_seconds
        self.retry_base_seconds = retry_base_seconds
        self.retry_cap_seconds = retry_cap_seconds
        self._task: Optional[asyncio.Task] = None
        self._operations = OperationsService()
        self._connections = ProviderConnectionService()
        self._schedules = ScheduleService()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        await asyncio.to_thread(self._heartbeat)
        self._task = asyncio.create_task(self._loop(), name=f"evaluation-queue-{self.worker_id}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            processed = await self.run_once()
            if not processed:
                await asyncio.sleep(self.poll_interval_seconds)

    def _heartbeat(self, claimed: Optional[ClaimedRun] = None) -> None:
        now = utcnow()
        with get_db() as db:
            state = db.query(WorkerStateDB).filter(WorkerStateDB.worker_id == self.worker_id).first()
            if not state:
                state = WorkerStateDB(worker_id=self.worker_id, last_heartbeat_at=now, updated_at=now)
                db.add(state)
            state.last_heartbeat_at = now
            state.claimed_run_id = claimed.run_id if claimed else None
            state.claimed_run_type = claimed.kind if claimed else None
            state.updated_at = now
            db.commit()

    def health(self) -> dict[str, Any]:
        """Return persisted worker and queue signals without exposing run data."""
        with get_db() as db:
            state = db.query(WorkerStateDB).filter(WorkerStateDB.worker_id == self.worker_id).first()
            queue_depth = (
                db.query(EvaluationRunDB).filter(EvaluationRunDB.status == RunStatus.QUEUED.value).count()
                + db.query(PairwiseRunDB).filter(PairwiseRunDB.status == RunStatus.QUEUED.value).count()
            )
            failed_run_count = (
                db.query(EvaluationRunDB).filter(EvaluationRunDB.status == RunStatus.FAILED.value).count()
                + db.query(PairwiseRunDB).filter(PairwiseRunDB.status == RunStatus.FAILED.value).count()
            )
            retried_run_count = (
                db.query(EvaluationRunDB).filter(EvaluationRunDB.attempt_count > 1).count()
                + db.query(PairwiseRunDB).filter(PairwiseRunDB.attempt_count > 1).count()
            )
        heartbeat_age_seconds = None
        if state:
            heartbeat_age_seconds = max(0, int((utcnow() - state.last_heartbeat_at).total_seconds()))
        worker_status = "unstarted" if not state else "working" if state.claimed_run_id else "healthy"
        if heartbeat_age_seconds is not None and heartbeat_age_seconds > 120:
            worker_status = "stale"
        return {
            "worker_id": self.worker_id,
            "status": worker_status,
            "last_heartbeat_at": state.last_heartbeat_at if state else None,
            "heartbeat_age_seconds": heartbeat_age_seconds,
            "claimed_run_id": state.claimed_run_id if state else None,
            "claimed_run_type": state.claimed_run_type if state else None,
            "queue_depth": queue_depth,
            "retried_run_count": retried_run_count,
            "retry_count": retried_run_count,
            "failed_run_count": failed_run_count,
            "failed_count": failed_run_count,
            "overdue_schedule_count": self._schedules.overdue_count(),
        }

    def _record(
        self,
        claimed: ClaimedRun,
        action: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        if claimed.workspace_id:
            self._operations.record(
                claimed.workspace_id,
                action,
                claimed.kind,
                claimed.run_id,
                claimed.project_id,
                metadata=metadata,
            )

    @staticmethod
    def _due_query(model: Type[EvaluationRunDB] | Type[PairwiseRunDB], now: datetime):
        return (
            model.status == RunStatus.QUEUED.value,
            model.cancellation_requested_at.is_(None),
            (model.next_attempt_at.is_(None) | (model.next_attempt_at <= now)),
        )

    def claim_next(self) -> Optional[ClaimedRun]:
        """Atomically claim the oldest due single-model or pairwise run."""
        now = utcnow()
        with get_db() as db:
            candidates: list[tuple[RunKind, Any]] = []
            for kind, model in (
                ("evaluation_run", EvaluationRunDB),
                ("pairwise_run", PairwiseRunDB),
            ):
                record = (
                    db.query(model)
                    .filter(*self._due_query(model, now))
                    .order_by(model.created_at.asc())
                    .first()
                )
                if record:
                    candidates.append((kind, record))

            if not candidates:
                return None

            kind, candidate = min(candidates, key=lambda value: value[1].created_at)
            model = EvaluationRunDB if kind == "evaluation_run" else PairwiseRunDB
            updated = (
                db.query(model)
                .filter(model.id == candidate.id, *self._due_query(model, now))
                .update(
                    {
                        model.status: RunStatus.RUNNING.value,
                        model.worker_id: self.worker_id,
                        model.worker_claimed_at: now,
                        model.started_at: candidate.started_at or now,
                        model.attempt_count: model.attempt_count + 1,
                    },
                    synchronize_session=False,
                )
            )
            if updated != 1:
                db.rollback()
                return None
            db.commit()

            project = (
                db.query(ProjectDB).filter(ProjectDB.id == candidate.project_id).first()
                if candidate.project_id
                else None
            )
            claimed = ClaimedRun(
                kind=kind,
                run_id=candidate.id,
                project_id=candidate.project_id,
                workspace_id=project.workspace_id if project else None,
                configuration=candidate.run_configuration or {},
            )

        self._record(claimed, "run.claimed", {"worker_id": self.worker_id})
        self._heartbeat(claimed)
        return claimed

    def _resolve_provider(self, workspace_id: Optional[str], settings: dict[str, Any]):
        connection_id = settings.get("connection_id")
        provider = settings.get("provider")
        model = settings.get("model")
        base_url = settings.get("base_url")
        allow_unauthenticated = bool(settings.get("allow_unauthenticated", False))
        api_key = None

        if connection_id:
            if not workspace_id:
                raise ValueError("Run provider connection is not associated with a workspace.")
            connection = self._connections.resolve(workspace_id, connection_id)
            provider = connection.provider
            model = model or connection.default_model
            base_url = connection.base_url
            allow_unauthenticated = connection.allow_unauthenticated
            api_key = connection.api_key

        if not provider or not model:
            raise ValueError("Run configuration is missing provider or model settings.")
        return ProviderFactory.create(
            provider=provider,
            model_id=model,
            api_key=api_key,
            base_url=base_url,
            allow_unauthenticated=allow_unauthenticated,
        )

    def _dataset_arguments(self, claimed: ClaimedRun) -> dict[str, Optional[str]]:
        dataset = claimed.configuration.get("dataset") or {}
        if dataset.get("source") == "dataset_registry":
            return {
                "dataset_id": dataset.get("dataset_id"),
                "dataset_version_id": dataset.get("version_id"),
                "dataset_path": None,
            }
        if dataset.get("source") == "path" and dataset.get("path"):
            return {
                "dataset_id": None,
                "dataset_version_id": None,
                "dataset_path": dataset["path"],
            }
        raise ValueError("Run configuration is missing a reproducible dataset reference.")

    def _build_single_runner(self, claimed: ClaimedRun) -> tuple[EvaluationRunner, dict[str, Optional[str]]]:
        request = claimed.configuration.get("request") or {}
        execution = claimed.configuration.get("execution") or {}
        candidate = self._resolve_provider(claimed.workspace_id, request.get("candidate") or {})
        judge = self._resolve_provider(claimed.workspace_id, request.get("judge") or {})
        runner = EvaluationRunner(
            candidate,
            EvaluatorRegistry(judge_provider=judge, judge_prompt_template=request.get("judge_prompt_template")),
            concurrency_limit=int(execution.get("concurrency_limit", 5)),
            execution_timeout_seconds=float(execution.get("timeout_seconds", 60.0)),
            result_batch_size=int(execution.get("result_batch_size", 10)),
            requested_configuration=request,
        )
        return runner, self._dataset_arguments(claimed)

    def _build_pairwise_runner(self, claimed: ClaimedRun) -> tuple[PairwiseEvaluationRunner, dict[str, Optional[str]]]:
        request = claimed.configuration.get("request") or {}
        execution = claimed.configuration.get("execution") or {}
        provider_a = self._resolve_provider(claimed.workspace_id, request.get("model_a") or {})
        provider_b = self._resolve_provider(claimed.workspace_id, request.get("model_b") or {})
        judge = self._resolve_provider(claimed.workspace_id, request.get("judge") or {})
        runner = PairwiseEvaluationRunner(
            provider_a,
            provider_b,
            PairwiseJudgeEvaluator(judge, prompt_template=request.get("judge_prompt_template")),
            concurrency_limit=int(execution.get("concurrency_limit", 5)),
            execution_timeout_seconds=float(execution.get("timeout_seconds", 60.0)),
            result_batch_size=int(execution.get("result_batch_size", 10)),
            requested_configuration=request,
        )
        return runner, self._dataset_arguments(claimed)

    def _cancellation_requested(self, claimed: ClaimedRun) -> bool:
        model = EvaluationRunDB if claimed.kind == "evaluation_run" else PairwiseRunDB
        with get_db() as db:
            record = db.query(model).filter(model.id == claimed.run_id).first()
            return bool(record and record.cancellation_requested_at is not None)

    def _mark_cancelled(self, claimed: ClaimedRun) -> None:
        model = EvaluationRunDB if claimed.kind == "evaluation_run" else PairwiseRunDB
        with get_db() as db:
            record = db.query(model).filter(model.id == claimed.run_id).first()
            if record:
                record.status = RunStatus.INTERRUPTED.value
                record.completed_at = utcnow()
                record.worker_id = None
                record.worker_claimed_at = None
                record.error_message = "Execution cancelled by user request."
                db.commit()
        self._record(claimed, "run.cancelled", {"worker_id": self.worker_id})

    def _retry_delay(self, attempt_count: int) -> float:
        capped = min(self.retry_cap_seconds, self.retry_base_seconds * (2 ** max(attempt_count - 1, 0)))
        return capped + random.uniform(0, capped * 0.2)

    def _retry_or_fail(self, claimed: ClaimedRun, error: BaseException | str) -> None:
        model = EvaluationRunDB if claimed.kind == "evaluation_run" else PairwiseRunDB
        error_message = sanitize_error(error)
        with get_db() as db:
            record = db.query(model).filter(model.id == claimed.run_id).first()
            if not record:
                return
            if record.cancellation_requested_at is not None:
                db.rollback()
                self._mark_cancelled(claimed)
                return
            if record.attempt_count >= record.max_attempts:
                record.status = RunStatus.FAILED.value
                record.completed_at = utcnow()
                record.error_message = error_message
                record.last_transient_error = error_message
                record.next_attempt_at = None
                record.worker_id = None
                record.worker_claimed_at = None
                attempts = record.attempt_count
                db.commit()
                self._record(
                    claimed,
                    "run.retry_exhausted",
                    {"worker_id": self.worker_id, "attempt_count": attempts, "error": error_message},
                )
                return

            delay = self._retry_delay(record.attempt_count)
            record.status = RunStatus.QUEUED.value
            record.next_attempt_at = utcnow() + timedelta(seconds=delay)
            record.last_transient_error = error_message
            record.error_message = None
            record.worker_id = None
            record.worker_claimed_at = None
            attempts = record.attempt_count
            db.commit()

        self._record(
            claimed,
            "run.retry_scheduled",
            {
                "worker_id": self.worker_id,
                "attempt_count": attempts,
                "retry_delay_seconds": round(delay, 3),
                "error": error_message,
            },
        )

    def _clear_completed_claim(self, claimed: ClaimedRun) -> None:
        model = EvaluationRunDB if claimed.kind == "evaluation_run" else PairwiseRunDB
        with get_db() as db:
            record = db.query(model).filter(model.id == claimed.run_id).first()
            if record:
                record.worker_id = None
                record.worker_claimed_at = None
                record.next_attempt_at = None
                db.commit()

    def _all_results_are_transient(self, claimed: ClaimedRun) -> bool:
        result_model = EvaluationResultDB if claimed.kind == "evaluation_run" else PairwiseComparisonDB
        with get_db() as db:
            results = db.query(result_model).filter(result_model.run_id == claimed.run_id).all()
        return bool(results) and all(
            result.outcome in {EvaluationOutcome.GENERATION_ERROR.value, EvaluationOutcome.EVALUATION_ERROR.value}
            and bool(result.error_message)
            and is_transient_provider_failure(result.error_message)
            for result in results
        )

    def _discard_attempt_results(self, claimed: ClaimedRun) -> None:
        result_model = EvaluationResultDB if claimed.kind == "evaluation_run" else PairwiseComparisonDB
        with get_db() as db:
            db.query(result_model).filter(result_model.run_id == claimed.run_id).delete(synchronize_session=False)
            db.commit()

    async def _execute_claim(self, claimed: ClaimedRun) -> None:
        if self._cancellation_requested(claimed):
            self._mark_cancelled(claimed)
            return

        try:
            if claimed.kind == "evaluation_run":
                runner, dataset = await asyncio.to_thread(self._build_single_runner, claimed)
                await runner.run_evaluation(run_id=claimed.run_id, **dataset)
            else:
                runner, dataset = await asyncio.to_thread(self._build_pairwise_runner, claimed)
                await runner.run_pairwise_evaluation(run_id=claimed.run_id, **dataset)
        except RunCancellationRequested:
            self._mark_cancelled(claimed)
            return
        except Exception as error:
            if is_transient_provider_failure(error):
                self._retry_or_fail(claimed, error)
            else:
                self._mark_nonretryable_failure(claimed, error)
            return

        if self._cancellation_requested(claimed):
            self._mark_cancelled(claimed)
        elif self._all_results_are_transient(claimed):
            self._discard_attempt_results(claimed)
            self._retry_or_fail(claimed, "Provider request timed out or was temporarily unavailable.")
        else:
            self._clear_completed_claim(claimed)

    def _mark_nonretryable_failure(self, claimed: ClaimedRun, error: BaseException | str) -> None:
        model = EvaluationRunDB if claimed.kind == "evaluation_run" else PairwiseRunDB
        with get_db() as db:
            record = db.query(model).filter(model.id == claimed.run_id).first()
            if record:
                record.status = RunStatus.FAILED.value
                record.completed_at = utcnow()
                record.error_message = sanitize_error(error)
                record.worker_id = None
                record.worker_claimed_at = None
                db.commit()
        self._record(
            claimed,
            "run.failed",
            {"worker_id": self.worker_id, "error": sanitize_error(error)},
        )

    async def run_once(self) -> bool:
        self._heartbeat()
        schedule_result = await asyncio.to_thread(self._schedules.trigger_due)
        claimed = await asyncio.to_thread(self.claim_next)
        if not claimed:
            return bool(schedule_result["triggered"] or schedule_result["failed"])
        try:
            await self._execute_claim(claimed)
        finally:
            self._heartbeat()
        return True

    def retry_now(self, kind: RunKind, run_id: str) -> bool:
        model = EvaluationRunDB if kind == "evaluation_run" else PairwiseRunDB
        with get_db() as db:
            record = db.query(model).filter(model.id == run_id).first()
            if not record or record.status not in {RunStatus.QUEUED.value, RunStatus.FAILED.value, RunStatus.INTERRUPTED.value}:
                return False
            previous_status = record.status
            record.status = RunStatus.QUEUED.value
            record.next_attempt_at = utcnow()
            record.cancellation_requested_at = None
            record.completed_at = None
            record.error_message = None
            record.worker_id = None
            record.worker_claimed_at = None
            if previous_status == RunStatus.FAILED.value:
                record.attempt_count = 0
            project = db.query(ProjectDB).filter(ProjectDB.id == record.project_id).first() if record.project_id else None
            workspace_id = project.workspace_id if project else None
            project_id = record.project_id
            db.commit()

        claimed = ClaimedRun(kind, run_id, project_id, workspace_id, {})
        self._record(claimed, "run.retry_requested", {"worker_id": self.worker_id})
        return True

