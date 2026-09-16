"""Recovery helpers for the single-process execution worker."""

from datetime import datetime, timezone

from app.database.connection import get_db, init_db
from app.database.models import EvaluationRunDB, PairwiseRunDB
from app.schemas.outcomes import RunStatus


RESTART_INTERRUPTION_MESSAGE = "Execution interrupted by application restart."
_UNFINISHED_STATUSES = (RunStatus.QUEUED.value, RunStatus.RUNNING.value)


def reconcile_abandoned_runs() -> dict[str, int]:
    """Mark work left by a stopped single worker as interrupted.

    This is safe only while the application has one process-local execution
    worker. Multi-worker deployments need worker ownership/heartbeats before
    using this recovery step.
    """
    init_db()
    now = datetime.now(timezone.utc)
    with get_db() as db:
        evaluation_runs = (
            db.query(EvaluationRunDB)
            .filter(EvaluationRunDB.status.in_(_UNFINISHED_STATUSES))
            .update(
                {
                    EvaluationRunDB.status: RunStatus.INTERRUPTED.value,
                    EvaluationRunDB.completed_at: now,
                    EvaluationRunDB.error_message: RESTART_INTERRUPTION_MESSAGE,
                },
                synchronize_session=False,
            )
        )
        pairwise_runs = (
            db.query(PairwiseRunDB)
            .filter(PairwiseRunDB.status.in_(_UNFINISHED_STATUSES))
            .update(
                {
                    PairwiseRunDB.status: RunStatus.INTERRUPTED.value,
                    PairwiseRunDB.completed_at: now,
                    PairwiseRunDB.error_message: RESTART_INTERRUPTION_MESSAGE,
                },
                synchronize_session=False,
            )
        )
        db.commit()

    return {"evaluation_runs": evaluation_runs, "pairwise_runs": pairwise_runs}
