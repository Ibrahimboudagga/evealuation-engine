"""Release decisions must reflect the configured measurement, including open answers."""

import json

import pytest

from app.database.connection import get_db
from app.database.models import EvaluationRunDB
from app.evaluators.registry import EvaluatorRegistry
from app.providers.base import BaseProvider
from app.runners.eval_runner import EvaluationRunner, get_run_metrics
from app.services.baseline_service import BaselineService
from app.services.dataset_service import DatasetService


class CandidateBuild(BaseProvider):
    is_mock = True

    def __init__(self, build):
        self.model_name = build

    async def generate(self, prompt):
        return ('unsupported advice' if self.model_name == 'regression' else 'supported advice'), None


class ReviewedFixtureJudge(BaseProvider):
    """Synthetic scoring fixture, not a claim of human calibration."""
    is_mock = True
    model_name = 'deterministic-judge-v1'
    generation_settings = {'temperature': 0}

    async def generate(self, prompt):
        return json.dumps({'score': 2 if 'unsupported advice' in prompt else 9,
                           'reason': 'Synthetic regression fixture'}), None


async def comparable_runs(rules=None):
    dataset = DatasetService().create_dataset('Golden synthetic suite', '\n'.join(
        json.dumps({'id': str(i), 'input': 'Give supported advice', 'expected_output': 'reference answer',
                    'metadata': {'risk': 'critical' if i == 0 else 'normal'}}) for i in range(3)))
    runs = []
    for build in ('baseline', 'regression', 'corrected'):
        runner = EvaluationRunner(CandidateBuild(build), EvaluatorRegistry(ReviewedFixtureJudge()),
                                  requested_configuration={'release_rules': rules or {
                                      'minimum_valid_cases': 3,
                                      'evaluators': {'llm_judge': {'average_score_minimum': .8,
                                                                  'average_score_max_drop': .05}}}})
        runs.append(await runner.run_evaluation(dataset_id=dataset.id))
    BaselineService().mark_baseline(runs[0])
    return runs


@pytest.mark.asyncio
async def test_open_ended_regression_is_caught_and_corrected_with_zero_exact_match():
    baseline, regressed, corrected = await comparable_runs()
    assert all(get_run_metrics(run)['evaluators']['exact_match']['pass_rate'] == 0
               for run in (baseline, regressed, corrected))
    decision = BaselineService().compare(regressed, baseline)
    assert decision['status'] == 'regressed'
    assert any('llm_judge' in reason for reason in decision['reasons'])
    assert BaselineService().compare(corrected, baseline)['status'] == 'passed'


@pytest.mark.asyncio
async def test_literal_version_one_contract_is_unverified():
    baseline, _, corrected = await comparable_runs()
    with get_db() as db:
        for run_id in (baseline, corrected):
            run = db.get(EvaluationRunDB, run_id)
            config = run.run_configuration
            config['schema_version'] = 1
            for evaluator in config['evaluators']:
                evaluator.pop('implementation_sha256', None)
                evaluator['version'] = '1'
            run.run_configuration = config
        db.commit()
    assert BaselineService().compare(corrected, baseline)['status'] == 'inconclusive'

@pytest.mark.asyncio
@pytest.mark.parametrize('rules, reason', [
    ({'minimum_valid_cases': 4}, 'minimum valid cases'),
    ({'evaluators': {'missing_domain_metric': {'average_score_minimum': .8}}}, 'missing'),
    ({'slices': [{'name': 'absent', 'metadata': {'risk': 'unknown'},
                 'evaluators': {'llm_judge': {'average_score_minimum': .8}}}]}, 'minimum valid cases'),
])
async def test_missing_metric_or_insufficient_sample_cannot_pass(rules, reason):
    baseline, _, corrected = await comparable_runs(rules)
    decision = BaselineService().compare(corrected, baseline)
    assert decision['status'] == 'inconclusive'
    assert any(reason in item for item in decision['reasons'])


@pytest.mark.asyncio
async def test_slice_regression_and_sample_denominator_are_visible():
    baseline, regressed, _ = await comparable_runs({
        'evaluators': {'exact_match': {'pass_rate_max_drop': .05}},
        'slices': [{'name': 'critical_cases', 'metadata': {'risk': 'critical'},
                    'minimum_valid_cases': 1,
                    'evaluators': {'llm_judge': {'average_score_minimum': .8}}}]})
    decision = BaselineService().compare(regressed, baseline)
    assert decision['status'] == 'regressed'
    comparison = next(item for item in decision['comparisons']
                      if item['slice_name'] == 'critical_cases' and item['evaluator'] == 'llm_judge')
    assert comparison['current_valid_evaluations'] == 1
    assert any('critical_cases/llm_judge' in reason for reason in decision['reasons'])


@pytest.mark.asyncio
async def test_backend_mismatch_is_inconclusive_even_with_high_scores():
    from app.database.models import EvaluationResultDB
    baseline, _, corrected = await comparable_runs()
    with get_db() as db:
        for result in db.query(EvaluationResultDB).filter_by(run_id=corrected, evaluator_name='semantic_similarity'):
            result.metadata_dict = {'backend': 'token_overlap'}
        db.commit()
    assert BaselineService().compare(corrected, baseline)['status'] == 'inconclusive'


def test_source_identity_changes_and_candidate_identity_is_excluded(monkeypatch):
    from app.services import run_configuration as contract
    from app.evaluators.exact_match import ExactMatchEvaluator
    config = {'schema_version': 2, 'run_type': 'single_model', 'is_simulated': True,
              'dataset': {'content_sha256': 'abc', 'expected_case_ids': ['one']},
              'candidate': {'model': 'old'}, 'evaluators': contract.evaluator_snapshots([ExactMatchEvaluator()])}
    fingerprint = contract.compatibility_fingerprint(config)
    assert fingerprint
    config['candidate']['model'] = 'new'
    assert contract.compatibility_fingerprint(config) == fingerprint
    config['evaluators'][0]['implementation_sha256'] = 'different-source-hash'
    assert contract.compatibility_fingerprint(config) != fingerprint


@pytest.mark.asyncio
async def test_pass_threshold_is_saved_and_used_by_metrics():
    dataset = DatasetService().create_dataset('threshold', json.dumps({'id': 'one', 'input': 'q', 'expected_output': 'reference'}))
    run_id = await EvaluationRunner(CandidateBuild('baseline'), EvaluatorRegistry(ReviewedFixtureJudge()),
        requested_configuration={'evaluator_settings': {'llm_judge': {'pass_threshold': .95}}}).run_evaluation(dataset_id=dataset.id)
    metric = get_run_metrics(run_id)['evaluators']['llm_judge']
    assert metric['avg_score'] == .9 and metric['pass_rate'] == 0
    with get_db() as db:
        config = db.get(EvaluationRunDB, run_id).run_configuration
    judge = next(e for e in config['evaluators'] if e['name'] == 'llm_judge')
    assert judge['settings']['pass_threshold'] == .95
    assert judge['provider']['generation_settings'] == {'temperature': 0}
    assert judge['implementation_sha256']
    assert judge['prompt_template']
    assert judge['calibration']['status'] == 'unverified'


def test_release_schema_rejects_unknown_or_empty_rules():
    from app.schemas.release import ReleaseRules
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ReleaseRules.model_validate({'evaluators': {'llm_judge': {'misspelled_score_minimum': .8}}})
    with pytest.raises(ValidationError):
        ReleaseRules.model_validate({'evaluators': {'llm_judge': {}}})

@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['rubric', 'model', 'legacy_contract'])
async def test_queued_run_refuses_changed_execution_contract_before_provider_call(change):
    from tests.fakes import DeterministicFakeProvider
    dataset = DatasetService().create_dataset('queued contract', json.dumps({'id': 'one', 'input': 'q', 'expected_output': 'a'}))
    candidate = DeterministicFakeProvider.successful()
    registry = EvaluatorRegistry(ReviewedFixtureJudge())
    runner = EvaluationRunner(candidate, registry)
    run_id = runner.create_run(dataset_id=dataset.id)
    if change == 'rubric':
        registry.get('llm_judge').prompt_template = 'Changed rubric: {prediction}'
    elif change == 'model':
        candidate.model_name = 'different-model'
    else:
        with get_db() as db:
            run = db.get(EvaluationRunDB, run_id)
            config = run.run_configuration
            config['schema_version'] = 1
            run.run_configuration = config
            db.commit()
    with pytest.raises(ValueError, match='contract'):
        await runner.run_evaluation(dataset_id=dataset.id, run_id=run_id)
    assert candidate.prompts == []
    with get_db() as db:
        assert db.get(EvaluationRunDB, run_id).status == 'failed'


@pytest.mark.asyncio
async def test_pairwise_refuses_changed_judge_before_provider_call():
    from tests.fakes import DeterministicFakeProvider
    from app.runners.pairwise_runner import PairwiseEvaluationRunner
    from app.evaluators.pairwise_judge import PairwiseJudgeEvaluator
    dataset = DatasetService().create_dataset('pairwise contract', json.dumps({'id': 'one', 'input': 'q', 'expected_output': 'a'}))
    candidate = DeterministicFakeProvider.successful()
    judge = PairwiseJudgeEvaluator(DeterministicFakeProvider.valid_pairwise_judge())
    runner = PairwiseEvaluationRunner(candidate, candidate, judge)
    run_id = runner.create_run(dataset_id=dataset.id)
    judge.prompt_template = 'Changed rubric: {response_a} {response_b}'
    with pytest.raises(ValueError, match='contract'):
        await runner.run_pairwise_evaluation(dataset_id=dataset.id, run_id=run_id)
    assert candidate.prompts == []


@pytest.mark.asyncio
@pytest.mark.parametrize('score', [float('nan'), float('inf'), -float('inf'), 1.1, -.1, None])
async def test_invalid_persisted_scores_are_visible_errors_and_never_release_pass(score):
    from app.database.models import EvaluationResultDB
    baseline, _, corrected = await comparable_runs()
    with get_db() as db:
        for result in db.query(EvaluationResultDB).filter_by(run_id=corrected, evaluator_name='llm_judge'):
            result.score = score
        db.commit()
    metric = get_run_metrics(corrected)['evaluators']['llm_judge']
    assert metric['avg_score'] is None and metric['pass_rate'] is None
    assert metric['evaluation_errors'] == metric['invalid_score_count'] == 3
    assert BaselineService().compare(corrected, baseline)['status'] == 'inconclusive'


@pytest.mark.parametrize("score", [True, False, "0.5", float("nan"), float("inf"), -1, 2])
def test_result_schema_rejects_invalid_scores_before_coercion(score):
    from pydantic import ValidationError
    from app.schemas.result import EvaluationResult
    with pytest.raises(ValidationError):
        EvaluationResult(example_id="case", prompt="q", prediction="a", expected_output="a",
                         evaluator_name="custom", score=score)


@pytest.mark.asyncio
async def test_invalid_pairwise_score_is_not_a_win_or_an_elo_update():
    from tests.fakes import DeterministicFakeProvider
    from app.runners.pairwise_runner import PairwiseEvaluationRunner, get_pairwise_run_metrics, get_pairwise_comparisons
    from app.evaluators.pairwise_judge import PairwiseJudgeEvaluator
    from app.database.models import PairwiseComparisonDB
    dataset = DatasetService().create_dataset("pairwise invalid", '{"id":"one","input":"q","expected_output":"a"}')
    candidate = DeterministicFakeProvider.successful()
    runner = PairwiseEvaluationRunner(candidate, candidate, PairwiseJudgeEvaluator(
        DeterministicFakeProvider.valid_pairwise_judge()))
    run_id = await runner.run_pairwise_evaluation(dataset_id=dataset.id)
    with get_db() as db:
        comparison = db.query(PairwiseComparisonDB).filter_by(run_id=run_id).one()
        comparison.score_a = float("inf")
        db.commit()
    metrics = get_pairwise_run_metrics(run_id)
    assert metrics["valid_comparisons"] == 0 and metrics["evaluation_errors"] == 1
    assert metrics["elo_a"] is None and metrics["win_rate_a"] is None
    review = get_pairwise_comparisons(run_id)[0]
    assert review["score_a"] is None and review["winner"] is None
    assert review["outcome"] == "evaluation_error"
