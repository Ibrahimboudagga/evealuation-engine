"""Client-ready exports for completed and in-progress single-model runs."""

import csv
import html
import io
import json
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import selectinload

from app.database.connection import get_db
from app.database.models import EvaluationResultDB, EvaluationRunDB
from app.errors import sanitize_error
from app.runners.eval_runner import get_run_metrics
from app.schemas.outcomes import EvaluationOutcome


_SENSITIVE_CONFIGURATION_KEY_PARTS = ("api_key", "apikey", "authorization", "password", "secret", "token")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _safe_configuration(value: Any) -> Any:
    """Defensively redact sensitive configuration keys before client export."""
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if any(part in key.lower() for part in _SENSITIVE_CONFIGURATION_KEY_PARTS)
            else _safe_configuration(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe_configuration(item) for item in value]
    return value


def _result_payload(result: EvaluationResultDB) -> dict[str, Any]:
    metadata = result.metadata_dict
    reason = metadata.get("reason")
    return {
        "example_id": result.example_id,
        "evaluator": result.evaluator_name,
        "outcome": result.outcome,
        "score": result.score,
        "prompt": result.prompt,
        "output": result.prediction,
        "expected_answer": result.expected_output,
        "judge_explanation": str(reason) if reason is not None else None,
        "sanitized_error": sanitize_error(result.error_message) if result.error_message else None,
    }


class ReportService:
    """Create transparent evaluation reports from persisted data only."""

    def build_run_report(self, run_id: str) -> dict[str, Any]:
        with get_db() as db:
            run = (
                db.query(EvaluationRunDB)
                .options(selectinload(EvaluationRunDB.project))
                .filter(EvaluationRunDB.id == run_id)
                .first()
            )
            if not run:
                raise ValueError(f"Run '{run_id}' not found")

            result_records = (
                db.query(EvaluationResultDB)
                .filter(EvaluationResultDB.run_id == run_id)
                .order_by(EvaluationResultDB.id)
                .all()
            )
            results = [_result_payload(result) for result in result_records]
            project = (
                {
                    "id": run.project.id,
                    "name": run.project.name,
                    "client_name": run.project.client_name,
                }
                if run.project
                else None
            )
            run_data = {
                "id": run.id,
                "model_name": run.model_name,
                "status": run.status,
                "created_at": _iso(run.created_at),
                "started_at": _iso(run.started_at),
                "completed_at": _iso(run.completed_at),
                "is_simulated": run.is_simulated,
                "sanitized_error": sanitize_error(run.error_message) if run.error_message else None,
                "project": project,
                "configuration_verified": run.configuration_verified,
                "configuration": _safe_configuration(run.run_configuration),
            }

        generation_errors = [
            result for result in results if result["outcome"] == EvaluationOutcome.GENERATION_ERROR.value
        ]
        evaluation_errors = [
            result for result in results if result["outcome"] == EvaluationOutcome.EVALUATION_ERROR.value
        ]
        failures = [*generation_errors, *evaluation_errors]
        metrics = get_run_metrics(run_id)
        return {
            "report_type": "evaluation_summary",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "run": run_data,
            "coverage_and_quality_metrics": list(metrics.get("evaluators", {}).values()),
            "metrics_by_evaluator": metrics.get("evaluators", {}),
            "failure_counts": {
                "total_result_records": len(results),
                "total_examples": len({result["example_id"] for result in results}),
                "generation_errors": len(generation_errors),
                "evaluation_errors": len(evaluation_errors),
                "failed_result_records": len(failures),
                "failed_examples": len({result["example_id"] for result in failures}),
            },
            "example_level_failures": failures,
            "results": results,
        }

    @staticmethod
    def to_json(report: dict[str, Any]) -> str:
        return json.dumps(report, indent=2, ensure_ascii=False)

    @staticmethod
    def to_csv(report: dict[str, Any]) -> str:
        output = io.StringIO(newline="")
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "example_id",
                "evaluator",
                "outcome",
                "score",
                "prompt",
                "output",
                "expected_answer",
                "judge_explanation",
                "sanitized_error",
            ],
        )
        writer.writeheader()
        writer.writerows(report["results"])
        return output.getvalue()

    @staticmethod
    def to_html(report: dict[str, Any]) -> str:
        """Render a standalone report that can be printed or sent to a client."""
        def text(value: Any) -> str:
            return html.escape("—" if value is None or value == "" else str(value))

        def percent(value: float | None) -> str:
            return "—" if value is None else f"{value:.1%}"

        def score(value: float | None) -> str:
            return "—" if value is None else f"{value:.3f}"

        metric_rows = "".join(
            "<tr>"
            f"<td>{text(name)}</td>"
            f"<td>{text(metric['total_cases'])}</td>"
            f"<td>{text(metric['valid_evaluations'])}</td>"
            f"<td>{percent(metric['evaluation_coverage'])}</td>"
            f"<td>{text(score(metric['avg_score']))}</td>"
            f"<td>{percent(metric['pass_rate'])}</td>"
            f"<td>{text(metric['generation_errors'])}</td>"
            f"<td>{text(metric['evaluation_errors'])}</td>"
            "</tr>"
            for name, metric in report["metrics_by_evaluator"].items()
        ) or "<tr><td colspan='8'>No valid evaluations have been recorded.</td></tr>"

        failure_rows = "".join(
            "<tr>"
            f"<td>{text(item['example_id'])}</td>"
            f"<td>{text(item['evaluator'])}</td>"
            f"<td>{text(item['outcome'])}</td>"
            f"<td><pre>{text(item['prompt'])}</pre></td>"
            f"<td><pre>{text(item['output'])}</pre></td>"
            f"<td><pre>{text(item['expected_answer'])}</pre></td>"
            f"<td>{text(item['judge_explanation'])}</td>"
            f"<td>{text(item['sanitized_error'])}</td>"
            "</tr>"
            for item in report["example_level_failures"]
        ) or "<tr><td colspan='8'>No generation or evaluation failures were recorded.</td></tr>"

        run = report["run"]
        counts = report["failure_counts"]
        configuration = html.escape(json.dumps(run["configuration"], indent=2, ensure_ascii=False))
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Evaluation Report {text(run['id'])}</title>
<style>
body {{ font-family: Arial, sans-serif; color: #172033; margin: 36px; line-height: 1.45; }}
h1, h2 {{ color: #173f5f; }} .meta, .summary {{ background: #f4f7fb; padding: 16px; border-radius: 8px; }}
table {{ border-collapse: collapse; width: 100%; margin: 16px 0; font-size: 13px; }}
th, td {{ border: 1px solid #d6deea; padding: 8px; text-align: left; vertical-align: top; }}
th {{ background: #eaf1f8; }} pre {{ white-space: pre-wrap; margin: 0; font-family: inherit; }}
@media print {{ body {{ margin: 16px; }} }}
</style></head><body>
<h1>Evaluation Summary</h1>
<p>Generated {text(report['generated_at'])}. This report uses persisted run data; errors are redacted.</p>
<div class="meta"><strong>Run:</strong> {text(run['id'])}<br><strong>Model:</strong> {text(run['model_name'])}<br>
<strong>Status:</strong> {text(run['status'])}<br><strong>Simulated:</strong> {text(run['is_simulated'])}<br>
<strong>Project:</strong> {text(run['project']['client_name'] + ' / ' + run['project']['name'] if run['project'] else None)}</div>
<h2>Coverage and Quality</h2><table><thead><tr><th>Evaluator</th><th>Total</th><th>Valid</th><th>Coverage</th><th>Average score</th><th>Pass rate</th><th>Generation errors</th><th>Evaluation errors</th></tr></thead><tbody>{metric_rows}</tbody></table>
<h2>Failure Counts</h2><div class="summary">Examples: {text(counts['total_examples'])} &nbsp; | &nbsp; Generation errors: {text(counts['generation_errors'])} &nbsp; | &nbsp; Evaluation errors: {text(counts['evaluation_errors'])} &nbsp; | &nbsp; Affected examples: {text(counts['failed_examples'])}</div>
<h2>Example-level Failures</h2><table><thead><tr><th>Example</th><th>Evaluator</th><th>Outcome</th><th>Prompt</th><th>Output</th><th>Expected answer</th><th>Judge explanation</th><th>Sanitized error</th></tr></thead><tbody>{failure_rows}</tbody></table>
<h2>Run Configuration</h2><p>Configuration verified: {text(run['configuration_verified'])}</p><pre class="meta">{configuration}</pre>
</body></html>"""

    @staticmethod
    def filename(run_id: str, extension: str) -> str:
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", run_id)
        return f"evaluation-report-{safe_id}.{extension}"
