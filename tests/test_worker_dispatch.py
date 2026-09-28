"""Regression coverage for dispatch timing and schedule transaction boundaries."""

import asyncio
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.database.connection import get_db
from app.database.models import (
    AuditEventDB, EvaluationRunDB, EvaluationScheduleDB, ProjectDB,
    ScheduleExecutionDB, WorkspaceDB,
)
from app.services.agency_service import AgencyService
from app.services.dataset_service import DatasetService
from app.services.operations_service import OperationsService
from app.services.provider_connection_service import ProviderConnectionService
from app.services.queue_worker import ClaimedRun, QueueWorker
from app.services.schedule_service import ScheduleService


def _due_schedule():
    with get_db() as db:
        db.add(WorkspaceDB(id="dispatch-workspace", name="Agency", slug="dispatch"))
        db.add(ProjectDB(id="dispatch-project", workspace_id="dispatch-workspace",
                         name="Project", client_name="Client"))
        db.commit()
    dataset = DatasetService().create_dataset(
        "Cases", '{"id":"case-1","input":"hello","expected_output":"hello"}',
        project_id="dispatch-project",
    )
    connection = ProviderConnectionService().create(
        "dispatch-workspace", "Mock", "mock", "mock", allow_unauthenticated=True,
    )
    template = AgencyService().create_template("dispatch-workspace", {
        "name": "Daily check", "candidate_connection_id": connection.id,
        "evaluator_connection_id": connection.id, "concurrency": 1,
        "timeout_seconds": 10,
    })
    return ScheduleService().create(
        "dispatch-workspace", template.id, dataset.id, dataset.versions[0].id,
        "daily", datetime.now(timezone.utc) - timedelta(seconds=1),
    )


@pytest.mark.asyncio
async def test_due_schedule_dispatch_continues_during_long_running_evaluation(monkeypatch):
    worker = QueueWorker(poll_interval_seconds=0.01, heartbeat_interval_seconds=0.01)
    executing = asyncio.Event()
    dispatched = asyncio.Event()
    release = asyncio.Event()
    claim = ClaimedRun("evaluation_run", "long-running", None, None, {})
    claims = iter([claim])
    monkeypatch.setattr(worker, "claim_next", lambda: next(claims, None))

    async def execute(_claim):
        executing.set()
        await release.wait()

    loop = asyncio.get_running_loop()

    def dispatch():
        if executing.is_set():
            loop.call_soon_threadsafe(dispatched.set)
        return {"triggered": 0, "failed": 0}

    monkeypatch.setattr(worker, "_execute_claim", execute)
    monkeypatch.setattr(worker._schedules, "trigger_due", dispatch)
    await worker.start()
    try:
        await asyncio.wait_for(executing.wait(), 2)
        await asyncio.wait_for(dispatched.wait(), 0.5)
        assert not release.is_set(), "Dispatch must not wait for evaluation completion."
    finally:
        release.set()
        await worker.stop()


def test_schedule_run_occurrence_and_audit_commit_or_rollback_together(monkeypatch):
    schedule = _due_schedule()
    original_record = OperationsService.record

    def fail_audit(self, workspace_id, action, *args, **kwargs):
        if action == "schedule.triggered":
            raise RuntimeError("injected audit storage failure")
        return original_record(self, workspace_id, action, *args, **kwargs)

    monkeypatch.setattr(OperationsService, "record", fail_audit)
    with pytest.raises(RuntimeError, match="audit storage failure"):
        ScheduleService().trigger_due()
    with get_db() as db:
        assert db.query(EvaluationRunDB).count() == 0
        assert db.query(ScheduleExecutionDB).count() == 0
        assert db.get(EvaluationScheduleDB, schedule.id).next_execution_at == schedule.next_execution_at

    monkeypatch.setattr(OperationsService, "record", original_record)
    assert ScheduleService().trigger_due() == {"triggered": 1, "failed": 0}
    assert ScheduleService().trigger_due() == {"triggered": 0, "failed": 0}
    with get_db() as db:
        occurrence = db.query(ScheduleExecutionDB).one()
        assert db.get(EvaluationRunDB, occurrence.run_id).status == "queued"
        assert db.query(AuditEventDB).filter_by(action="schedule.triggered").count() == 1


@pytest.mark.asyncio
async def test_shutdown_completes_when_inflight_dispatch_fails(monkeypatch):
    worker = QueueWorker(poll_interval_seconds=0.01)
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def dispatch():
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=2)
        raise RuntimeError("injected schedule transaction failure")

    monkeypatch.setattr(worker, "claim_next", lambda: None)
    monkeypatch.setattr(worker._schedules, "trigger_due", dispatch)
    await worker.start()
    stop_task = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        stop_task = asyncio.create_task(worker.stop())
        await asyncio.sleep(0)
        assert worker._schedule_task.cancelling()
        release.set()
        await asyncio.wait_for(stop_task, 0.5)
        assert worker._schedule_task is None
    finally:
        release.set()
        if stop_task and not stop_task.done():
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
        await worker.stop()


@pytest.mark.asyncio
async def test_worker_health_reports_dead_heartbeat_task():
    worker = QueueWorker()
    worker._heartbeat()
    worker._heartbeat_task = asyncio.create_task(asyncio.sleep(0))
    await worker._heartbeat_task
    try:
        assert worker.health()["status"] == "failed"
    finally:
        await worker.stop()
