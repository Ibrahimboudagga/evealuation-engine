"""Sequential bounded execution with durable per-example output files."""

import asyncio
import hashlib
import json
import os
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from app.scenarios.adapters import Adapter, ExecutionFailure
from app.scenarios.evaluation import evaluate
from app.scenarios.schemas import Scenario, ScenarioResult
from app.scenarios.report import render_report


def load_scenarios(path):
    raw = Path(path).read_bytes()
    scenarios = [Scenario.model_validate_json(line) for line in raw.decode("utf-8-sig").splitlines() if line.strip()]
    if not scenarios or len({s.id for s in scenarios}) != len(scenarios):
        raise ValueError("Dataset must be nonempty and scenario IDs must be unique")
    return scenarios, hashlib.sha256(raw).hexdigest()


def summarize(results, expected):
    valid = [r for r in results if r.outcome == "evaluated"]
    return {"total_expected_cases": expected, "recorded_cases": len(results),
            "valid_evaluations": len(valid),
            "generation_errors": sum(r.outcome == "generation_error" for r in results),
            "evaluation_errors": sum(r.outcome == "evaluation_error" for r in results),
            "coverage": len(valid) / expected if expected else None,
            "average_score": sum(r.score for r in valid) / len(valid) if valid else None,
            "pass_rate": sum(r.decision == "passed" for r in valid) / len(valid) if valid else None,
            "quality_message": None if valid else "No valid evaluations",
            "decision": "inconclusive" if len(valid) != expected else
                        "passed" if all(r.decision == "passed" for r in valid) else "regressed"}


async def run_scenarios(scenarios: list[Scenario], adapter: Adapter, output_dir,
                        *, timeout_seconds=60.0, dataset_sha256=None, target_label="application"):
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("Timeout must be positive")
    if not scenarios or len({s.id for s in scenarios}) != len(scenarios):
        raise ValueError("Scenarios must be nonempty and have unique IDs")
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    run_id = str(uuid.uuid4())
    manifest = {"schema_version": 1, "run_id": run_id, "status": "running",
                "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None,
                "dataset_sha256": dataset_sha256, "timeout_seconds": timeout_seconds,
                "adapter": type(adapter).__name__, "target_label": target_label,
                "simulated": adapter.simulated, "scenarios": [s.model_dump() for s in scenarios]}
    results = []

    def save_manifest():
        temp = directory / "manifest.tmp"
        temp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        temp.replace(directory / "manifest.json")

    save_manifest()
    try:
        with (directory / "results.jsonl").open("x", encoding="utf-8") as stream:
            for scenario in scenarios:
                try:
                    evidence = await asyncio.wait_for(adapter.execute(scenario, run_id + ":" + scenario.id), timeout_seconds)
                    result = evaluate(scenario, evidence)
                except (ExecutionFailure, TimeoutError) as exc:
                    result = ScenarioResult(scenario_id=scenario.id, outcome="generation_error",
                                            decision="inconclusive", simulated=adapter.simulated,
                                            error_message=str(exc) if isinstance(exc, ExecutionFailure) else
                                            "Execution deadline exceeded; remote execution may still be active")
                except ValidationError:
                    result = ScenarioResult(scenario_id=scenario.id, outcome="evaluation_error",
                                            decision="inconclusive", simulated=adapter.simulated,
                                            error_message="Target evidence does not match the execution contract")
                results.append(result)
                manifest["simulated"] = manifest["simulated"] or result.simulated
                stream.write(result.model_dump_json() + "\n")
                stream.flush()
                os.fsync(stream.fileno())
                manifest["metrics"] = summarize(results, len(scenarios))
                save_manifest()
        manifest["status"] = "completed"
    except (asyncio.CancelledError, KeyboardInterrupt):
        manifest["status"] = "interrupted"
        raise
    except Exception:
        manifest["status"] = "failed"
        raise
    finally:
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        manifest["metrics"] = summarize(results, len(scenarios))
        save_manifest()
        (directory / "report.html").write_text(render_report(manifest, results), encoding="utf-8")
    return manifest
