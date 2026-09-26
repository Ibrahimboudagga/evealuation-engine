"""Run only against an isolated disposable database configured by DATABASE_URL."""

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from app.api.main import app
from app.database.connection import init_db, get_db
from app.database.models import EvaluationRunDB
from app.evaluators.exact_match import ExactMatchEvaluator
from app.providers.factory import ProviderFactory
from app.runners.eval_runner import EvaluationRunner, get_run_metrics


async def main():
    init_db()
    app.openapi()  # Exercise forward references on the actual runtime.
    folder = Path(tempfile.mkdtemp(prefix="evaluation-deployment-smoke-"))
    path = folder / "a-deliberately-long-legacy-dataset-file-name-for-postgresql.jsonl"
    path.write_text(json.dumps({"id": "one", "input": "hello", "expected_output": "hello"}), encoding="utf-8")
    runner = EvaluationRunner(ProviderFactory.create("mock", "mock"),
                              SimpleNamespace(get_all=lambda: [ExactMatchEvaluator()]))
    run_id = await runner.run_evaluation(dataset_path=str(path))
    with get_db() as db:
        run = db.get(EvaluationRunDB, run_id)
        assert run.status == "completed" and len(run.dataset_id) == 36
    assert get_run_metrics(run_id)["evaluators"]["exact_match"]["evaluation_coverage"] == 1
    print("API import, migrations, UUID dataset identity, execution and metrics passed.")


if __name__ == "__main__":
    asyncio.run(main())
