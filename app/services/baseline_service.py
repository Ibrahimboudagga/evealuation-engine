"""Baseline marking and transparent release checks for single-model runs."""

from typing import Any, Optional

from app.database.connection import get_db
from app.database.models import EvaluationRunDB
from app.runners.eval_runner import get_run_metrics
from app.schemas.outcomes import RunStatus
from app.services.run_configuration import compatibility_fingerprint


def _delta(current: Optional[float | int], baseline: Optional[float | int]) -> Optional[float | int]:
    if current is None or baseline is None:
        return None
    return current - baseline


class BaselineService:
    """Compare a completed run with a completed, explicitly marked baseline."""

    def mark_baseline(self, run_id: str) -> EvaluationRunDB:
        with get_db() as db:
            run = db.query(EvaluationRunDB).filter(EvaluationRunDB.id == run_id).first()
            if not run:
                raise ValueError(f"Run '{run_id}' not found")
            if run.status != RunStatus.COMPLETED.value:
                raise ValueError("Only completed runs can be marked as a baseline.")
            run.is_baseline = True
            db.commit()
            db.refresh(run)
            return run

    def compare(
        self,
        run_id: str,
        baseline_run_id: str,
        coverage_minimum: Optional[float] = None,
        exact_match_pass_rate_max_drop: Optional[float] = None,
    ) -> dict[str, Any]:
        if run_id == baseline_run_id:
            raise ValueError("A run cannot be compared with itself.")

        with get_db() as db:
            current = db.query(EvaluationRunDB).filter(EvaluationRunDB.id == run_id).first()
            baseline = db.query(EvaluationRunDB).filter(EvaluationRunDB.id == baseline_run_id).first()
            if not current:
                raise ValueError(f"Run '{run_id}' not found")
            if not baseline:
                raise ValueError(f"Baseline run '{baseline_run_id}' not found")
            if not baseline.is_baseline:
                raise ValueError(f"Run '{baseline_run_id}' is not marked as a baseline")
            if current.project_id != baseline.project_id:
                raise ValueError("Runs from different agency projects cannot be compared")
            current_status = current.status
            baseline_status = baseline.status
            saved_rules = ((current.run_configuration or {}).get("request") or {}).get("release_rules") or {}
            current_fingerprint = compatibility_fingerprint(current.run_configuration) if current.configuration_verified else None
            baseline_fingerprint = compatibility_fingerprint(baseline.run_configuration) if baseline.configuration_verified else None
            compatible = (current_fingerprint is not None and current_fingerprint == baseline_fingerprint
                          and current.is_simulated == baseline.is_simulated)

        coverage_minimum = coverage_minimum if coverage_minimum is not None else saved_rules.get("coverage_minimum")
        coverage_minimum = .95 if coverage_minimum is None else coverage_minimum
        exact_match_pass_rate_max_drop = exact_match_pass_rate_max_drop if exact_match_pass_rate_max_drop is not None else saved_rules.get("exact_match_pass_rate_max_drop")
        exact_match_pass_rate_max_drop = .05 if exact_match_pass_rate_max_drop is None else exact_match_pass_rate_max_drop
        if not 0 <= coverage_minimum <= 1 or not 0 <= exact_match_pass_rate_max_drop <= 1:
            raise ValueError("Release rule thresholds must be between zero and one.")
        rules = {
            "coverage_minimum": coverage_minimum,
            "exact_match_pass_rate_max_drop": exact_match_pass_rate_max_drop,
        }
        if current_status != RunStatus.COMPLETED.value or baseline_status != RunStatus.COMPLETED.value:
            return {
                "run_id": run_id,
                "baseline_run_id": baseline_run_id,
                "status": "inconclusive",
                "rules": rules,
                "reasons": ["Both the current run and baseline must be completed before a release check can pass."],
                "comparisons": [],
            }

        if not compatible:
            return {"run_id": run_id, "baseline_run_id": baseline_run_id, "status": "inconclusive",
                    "rules": rules, "reasons": ["Incompatible or unverified experiment contracts: dataset, cases, evaluators, judge, or simulation differ."],
                    "comparisons": []}
        current_metrics = get_run_metrics(run_id).get("evaluators", {})
        baseline_metrics = get_run_metrics(baseline_run_id).get("evaluators", {})
        evaluator_names = sorted(set(current_metrics) | set(baseline_metrics))
        if not evaluator_names:
            return {
                "run_id": run_id,
                "baseline_run_id": baseline_run_id,
                "status": "inconclusive",
                "rules": rules,
                "reasons": ["Neither run has persisted evaluator results yet."],
                "comparisons": [],
            }

        comparisons = []
        reasons: list[str] = []
        inconclusive = False
        for evaluator in evaluator_names:
            current_metric = current_metrics.get(evaluator)
            baseline_metric = baseline_metrics.get(evaluator)
            if not current_metric or not baseline_metric:
                inconclusive = True
                reasons.append(f"Evaluator '{evaluator}' is missing from one of the runs.")
            comparisons.append(
                {
                    "evaluator": evaluator,
                    "baseline_average_score": baseline_metric.get("avg_score") if baseline_metric else None,
                    "current_average_score": current_metric.get("avg_score") if current_metric else None,
                    "average_score_delta": _delta(
                        current_metric.get("avg_score") if current_metric else None,
                        baseline_metric.get("avg_score") if baseline_metric else None,
                    ),
                    "baseline_pass_rate": baseline_metric.get("pass_rate") if baseline_metric else None,
                    "current_pass_rate": current_metric.get("pass_rate") if current_metric else None,
                    "pass_rate_delta": _delta(
                        current_metric.get("pass_rate") if current_metric else None,
                        baseline_metric.get("pass_rate") if baseline_metric else None,
                    ),
                    "baseline_coverage": baseline_metric.get("evaluation_coverage") if baseline_metric else None,
                    "current_coverage": current_metric.get("evaluation_coverage") if current_metric else None,
                    "coverage_delta": _delta(
                        current_metric.get("evaluation_coverage") if current_metric else None,
                        baseline_metric.get("evaluation_coverage") if baseline_metric else None,
                    ),
                    "baseline_error_count": baseline_metric.get("error_count") if baseline_metric else None,
                    "current_error_count": current_metric.get("error_count") if current_metric else None,
                    "error_count_delta": _delta(
                        current_metric.get("error_count") if current_metric else None,
                        baseline_metric.get("error_count") if baseline_metric else None,
                    ),
                }
            )
            for label, metric in (("current", current_metric), ("baseline", baseline_metric)):
                if metric and (metric["evaluation_coverage"] < coverage_minimum or metric["avg_score"] is None
                               or metric.get("unverified_cases", 0) or metric.get("unexpected_cases", 0)):
                    inconclusive = True
                    reasons.append(f"{label} {evaluator} has insufficient or ambiguous evaluation coverage (minimum {coverage_minimum:.1%}).")
            if current_metric and baseline_metric and (current_metric.get("backends") != baseline_metric.get("backends")
                    or len(current_metric.get("backends", [])) > 1 or len(baseline_metric.get("backends", [])) > 1):
                inconclusive = True
                reasons.append(f"{evaluator} used incompatible measurement backends.")

        baseline_exact = baseline_metrics.get("exact_match")
        current_exact = current_metrics.get("exact_match")
        if not baseline_exact or not current_exact:
            inconclusive = True
            reasons.append("Exact-match results are required to apply the pass-rate regression rule.")
        elif baseline_exact["pass_rate"] is None or current_exact["pass_rate"] is None:
            inconclusive = True
            reasons.append("Exact-match pass rate is unavailable because one run has no valid exact-match evaluations.")
        else:
            pass_rate_drop = baseline_exact["pass_rate"] - current_exact["pass_rate"]
            if pass_rate_drop > exact_match_pass_rate_max_drop:
                reasons.append(
                    "exact_match pass rate fell "
                    f"{pass_rate_drop:.1%}, exceeding the allowed {exact_match_pass_rate_max_drop:.1%} drop."
                )

        if inconclusive:
            status = "inconclusive"
        elif reasons:
            status = "regressed"
        else:
            status = "passed"
            reasons.append("All configured release checks passed.")

        return {
            "run_id": run_id,
            "baseline_run_id": baseline_run_id,
            "status": status,
            "rules": rules,
            "reasons": reasons,
            "comparisons": comparisons,
        }
