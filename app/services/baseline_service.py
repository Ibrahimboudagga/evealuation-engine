"""Baseline comparisons gated by pinned measurement contracts and explicit policies."""

from typing import Any, Optional

from app.database.connection import get_db
from app.database.models import EvaluationRunDB
from app.runners.eval_runner import get_run_metrics
from app.schemas.outcomes import RunStatus
from app.schemas.release import ReleaseRules
from app.services.run_configuration import compatibility_fingerprint


def _delta(current, baseline):
    return None if current is None or baseline is None else current - baseline


class BaselineService:
    def mark_baseline(self, run_id: str) -> EvaluationRunDB:
        with get_db() as db:
            run = db.get(EvaluationRunDB, run_id)
            if not run:
                raise ValueError(f"Run '{run_id}' not found")
            if run.status != RunStatus.COMPLETED.value:
                raise ValueError("Only completed runs can be marked as a baseline.")
            run.is_baseline = True
            db.commit()
            db.refresh(run)
            return run

    def compare(self, run_id: str, baseline_run_id: str,
                coverage_minimum: Optional[float] = None,
                exact_match_pass_rate_max_drop: Optional[float] = None) -> dict[str, Any]:
        if run_id == baseline_run_id:
            raise ValueError("A run cannot be compared with itself.")
        with get_db() as db:
            current = db.get(EvaluationRunDB, run_id)
            baseline = db.get(EvaluationRunDB, baseline_run_id)
            if not current or not baseline:
                raise ValueError("Current or baseline run not found")
            if not baseline.is_baseline:
                raise ValueError(f"Run '{baseline_run_id}' is not marked as a baseline")
            if current.project_id != baseline.project_id:
                raise ValueError("Runs from different agency projects cannot be compared")
            current_config = current.run_configuration or {}
            baseline_config = baseline.run_configuration or {}
            saved = dict((current_config.get('request') or {}).get('release_rules') or {})
            # Older templates used nullable legacy fields; their nulls mean defaults.
            if saved.get('coverage_minimum') is None:
                saved.pop('coverage_minimum', None)
            if coverage_minimum is not None:
                saved['coverage_minimum'] = coverage_minimum
            if exact_match_pass_rate_max_drop is not None:
                saved['exact_match_pass_rate_max_drop'] = exact_match_pass_rate_max_drop
            policy = ReleaseRules.model_validate(saved)
            rules = policy.model_dump(mode='json')
            response = {'run_id': run_id, 'baseline_run_id': baseline_run_id,
                        'status': 'inconclusive', 'rules': rules, 'reasons': [], 'comparisons': []}
            if current.status != 'completed' or baseline.status != 'completed':
                response['reasons'] = ['Both the current run and baseline must be completed before a release check can pass.']
                return response
            current_fingerprint = compatibility_fingerprint(current_config) if current.configuration_verified else None
            baseline_fingerprint = compatibility_fingerprint(baseline_config) if baseline.configuration_verified else None
            if (not current_fingerprint or current_fingerprint != baseline_fingerprint
                    or current.is_simulated != baseline.is_simulated):
                response['reasons'] = ['Incompatible or unverified experiment contracts: dataset, cases, evaluator code/settings, judge, or simulation differ.']
                return response

        current_metrics = get_run_metrics(run_id).get('evaluators', {})
        baseline_metrics = get_run_metrics(baseline_run_id).get('evaluators', {})
        evaluator_names = sorted(set(current_metrics) | set(baseline_metrics))
        if not evaluator_names:
            response['reasons'] = ['Neither run has persisted evaluator results yet.']
            return response
        # Without explicit quality rules, protect every configured measurement.
        # Exact match alone cannot guard an open-ended answer or an LLM judge.
        evaluator_rules = {name: rule.model_dump(exclude_none=True) for name, rule in policy.evaluators.items()}
        if not evaluator_rules:
            evaluator_rules = {name: {'average_score_max_drop': .05, 'pass_rate_max_drop': .05}
                               for name in evaluator_names}
        if policy.exact_match_pass_rate_max_drop is not None:
            evaluator_rules.setdefault('exact_match', {})['pass_rate_max_drop'] = policy.exact_match_pass_rate_max_drop
        rules['evaluators'] = evaluator_rules
        inconclusive = False
        regressions = False

        def check_metrics(current_values, baseline_values, selected_rules, coverage, minimum, slice_name=None):
            nonlocal inconclusive, regressions
            names = sorted(set(current_values) | set(baseline_values) | set(selected_rules))
            for evaluator in names:
                cur, base = current_values.get(evaluator), baseline_values.get(evaluator)
                label = f'{slice_name}/{evaluator}' if slice_name else evaluator
                comparison = {'evaluator': evaluator, 'slice_name': slice_name,
                              'minimum_valid_cases': minimum,
                              'baseline_valid_evaluations': base.get('valid_evaluations') if base else None,
                              'current_valid_evaluations': cur.get('valid_evaluations') if cur else None}
                for name, key in (('average_score', 'avg_score'), ('pass_rate', 'pass_rate'),
                                  ('coverage', 'evaluation_coverage'), ('error_count', 'error_count')):
                    comparison['baseline_' + name] = base.get(key) if base else None
                    comparison['current_' + name] = cur.get(key) if cur else None
                    comparison[name + '_delta'] = _delta(cur.get(key) if cur else None, base.get(key) if base else None)
                response['comparisons'].append(comparison)
                if not cur or not base:
                    inconclusive = True
                    response['reasons'].append(f"Evaluator '{label}' is missing from one of the runs.")
                    continue
                sufficient = True
                for run_label, metric in (('current', cur), ('baseline', base)):
                    if (not metric.get('denominator_verified') or metric.get('evaluation_coverage', 0) < coverage
                            or metric.get('valid_evaluations', 0) < minimum or metric.get('avg_score') is None
                            or metric.get('pass_rate') is None or metric.get('unverified_cases', 0)
                            or metric.get('unexpected_cases', 0)):
                        inconclusive = True
                        sufficient = False
                        response['reasons'].append(f'{run_label} {label} has insufficient or ambiguous evidence: '
                                                   f'minimum coverage {coverage:.1%}, minimum valid cases {minimum}.')
                cur_backends, base_backends = cur.get('backends', []), base.get('backends', [])
                if cur_backends != base_backends or len(cur_backends) != 1 or len(base_backends) != 1:
                    inconclusive = True
                    sufficient = False
                    response['reasons'].append(f'{label} used incompatible or unknown measurement backends.')
                if evaluator == 'semantic_similarity' and ('default' in cur_backends or 'default' in base_backends):
                    inconclusive = True
                    sufficient = False
                    response['reasons'].append(f'{label} has no recorded similarity backend identity.')
                if not sufficient:
                    continue
                for metric_name, key in (('average_score', 'avg_score'), ('pass_rate', 'pass_rate')):
                    rule = selected_rules.get(evaluator, {})
                    floor, drop = rule.get(metric_name + '_minimum'), rule.get(metric_name + '_max_drop')
                    if floor is not None and cur[key] + 1e-12 < floor:
                        regressions = True
                        response['reasons'].append(f'{label} {metric_name} {cur[key]:.1%} is below minimum {floor:.1%}.')
                    observed_drop = base[key] - cur[key]
                    if drop is not None and observed_drop > drop + 1e-12:
                        regressions = True
                        response['reasons'].append(f'{label} {metric_name} fell {observed_drop:.1%}, exceeding allowed {drop:.1%} drop.')

        check_metrics(current_metrics, baseline_metrics, evaluator_rules,
                      policy.coverage_minimum, policy.minimum_valid_cases)
        for slice_rule in policy.slices:
            current_slice = get_run_metrics(run_id, slice_metadata=slice_rule.metadata).get('evaluators', {})
            baseline_slice = get_run_metrics(baseline_run_id, slice_metadata=slice_rule.metadata).get('evaluators', {})
            check_metrics(current_slice, baseline_slice,
                          {name: rule.model_dump(exclude_none=True) for name, rule in slice_rule.evaluators.items()},
                          slice_rule.coverage_minimum if slice_rule.coverage_minimum is not None else policy.coverage_minimum,
                          slice_rule.minimum_valid_cases if slice_rule.minimum_valid_cases is not None else policy.minimum_valid_cases,
                          slice_rule.name)
        response['status'] = 'inconclusive' if inconclusive else ('regressed' if regressions else 'passed')
        if response['status'] == 'passed':
            response['reasons'].append('All configured release checks passed; this is not evidence of human calibration.')
        return response
