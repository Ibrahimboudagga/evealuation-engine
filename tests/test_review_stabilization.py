import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from app.database import connection
from app.database.models import EvaluationRunDB, EvaluationResultDB
from app.evaluators.registry import EvaluatorRegistry
from app.runners.eval_runner import EvaluationRunner, get_run_metrics
from app.services.dataset_service import DatasetService
from app.services.identity_service import IdentityService
from tests.fakes import DeterministicFakeProvider


CONTENT = '\n'.join(json.dumps({"id": str(i), "input": "question", "expected_output": "answer"}) for i in range(2))


def test_migrations_use_existing_connection_not_rendered_url(monkeypatch, tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'percent%25.db'))
    monkeypatch.setattr(connection, 'engine', engine)
    def upgrade(config, revision):
        assert config.attributes.get('connection') is not None
        assert config.attributes['connection'].engine is engine
    monkeypatch.setattr(connection.command, 'upgrade', upgrade)
    connection.init_db()
    engine.dispose()


def test_existing_member_password_cannot_be_replaced():
    service = IdentityService()
    owner, _ = service.bootstrap('owner@test.com', 'Owner', 'Agency', 'original-password')
    with pytest.raises(ValueError, match='existing'):
        service.add_member(owner, owner.email, 'Owner', 'owner', 'replacement-password')
    assert service.sign_in(owner.email, 'original-password')[0].user_id == owner.user_id


def test_password_change_revokes_all_tokens():
    service = IdentityService()
    owner, token = service.bootstrap('owner@test.com', 'Owner', 'Agency')
    service.change_password(owner.user_id, None, 'first-long-password')
    with pytest.raises(ValueError):
        service.authenticate(token)
    _, session, _ = service.sign_in(owner.email, 'first-long-password')
    service.change_password(owner.user_id, 'first-long-password', 'second-long-password')
    with pytest.raises(ValueError):
        service.authenticate(session)


def test_duplicate_dataset_ids_rejected():
    line = CONTENT.splitlines()[0]
    with pytest.raises(ValueError, match='unique'):
        DatasetService().create_dataset('duplicates', line + '\n' + line)


def test_coverage_includes_unprocessed_cases():
    dataset = DatasetService().create_dataset('two', CONTENT)
    runner = EvaluationRunner(DeterministicFakeProvider.successful(), EvaluatorRegistry())
    run_id = runner.create_run(dataset_id=dataset.id)
    with connection.get_db() as db:
        db.add(EvaluationResultDB(run_id=run_id, example_id='0', prompt='q', prediction='a', expected_output='a',
                                  score=1, evaluator_name='exact_match', outcome='evaluated'))
        db.commit()
    metric = get_run_metrics(run_id)['evaluators']['exact_match']
    assert metric['total_cases'] == 2 and metric['evaluation_coverage'] == .5


def test_duplicate_historical_scores_are_unverified():
    dataset = DatasetService().create_dataset('two', CONTENT)
    run_id = EvaluationRunner(DeterministicFakeProvider.successful(), EvaluatorRegistry()).create_run(dataset_id=dataset.id)
    with connection.get_db() as db:
        for _ in range(2):
            db.add(EvaluationResultDB(run_id=run_id, example_id='0', prompt='q', prediction='a', expected_output='a',
                                      score=1, evaluator_name='exact_match', outcome='evaluated'))
        db.commit()
    metric = get_run_metrics(run_id)['evaluators']['exact_match']
    assert metric['valid_evaluations'] == 0
    assert metric['evaluation_coverage'] == 0


def test_api_schema_forward_references_are_resolved():
    from app.api.schemas import AdminConsoleResponse
    assert 'health' in AdminConsoleResponse.model_json_schema()['properties']
    # The container uses eager annotation evaluation, unlike local Python 3.14.
    source = Path('app/api/schemas.py').read_text()
    assert 'from __future__ import annotations' in source or source.index('class HealthResponse') < source.index('class AdminConsoleResponse')


def test_manual_retry_preserves_partial_run():
    from app.services.queue_worker import QueueWorker
    dataset = DatasetService().create_dataset('retry', CONTENT)
    run_id = EvaluationRunner(DeterministicFakeProvider.successful(), EvaluatorRegistry()).create_run(dataset_id=dataset.id)
    with connection.get_db() as db:
        db.get(EvaluationRunDB, run_id).status = 'interrupted'
        db.add(EvaluationResultDB(run_id=run_id, example_id='0', prompt='q', prediction='a', expected_output='a',
                                  score=1, evaluator_name='exact_match', outcome='evaluated'))
        db.commit()
    retry_id = QueueWorker().retry_now('evaluation_run', run_id)
    assert retry_id and retry_id != run_id
    with connection.get_db() as db:
        assert db.get(EvaluationRunDB, run_id).status == 'interrupted'
        assert db.get(EvaluationRunDB, retry_id).parent_run_id == run_id
        assert db.query(EvaluationResultDB).filter_by(run_id=run_id).count() == 1
        assert db.query(EvaluationResultDB).filter_by(run_id=retry_id).count() == 0


@pytest.mark.asyncio
async def test_default_mock_pairwise_judge_is_valid():
    from app.providers.factory import ProviderFactory
    from app.evaluators.pairwise_judge import PairwiseJudgeEvaluator
    result = await PairwiseJudgeEvaluator(ProviderFactory.create('mock', 'mock')).evaluate('q', 'ref', 'a', 'b')
    assert result.winner == 'tie' and result.score_a is not None


def test_client_lists_follow_project_grants():
    from fastapi.testclient import TestClient
    from app.api.main import app
    from app.database.models import ProjectDB, PairwiseRunDB
    service = IdentityService()
    owner, _ = service.bootstrap('owner@test.com', 'Owner', 'Agency')
    viewer, token = service.add_member(owner, 'client@test.com', 'Client', 'client_viewer')
    with connection.get_db() as db:
        db.add(ProjectDB(id='private', name='Private', client_name='Private client', workspace_id=owner.workspace_id))
        db.commit()
    dataset = DatasetService().create_dataset('secret data', CONTENT, project_id='private')
    run_id = EvaluationRunner(DeterministicFakeProvider.successful(), EvaluatorRegistry()).create_run(dataset_id=dataset.id)
    with connection.get_db() as db:
        db.add(PairwiseRunDB(id='pair', dataset_id=dataset.id, project_id='private', model_a_name='a', model_b_name='b'))
        db.commit()
    # No lifespan worker: assertions must inspect queued records before execution.
    client = TestClient(app)
    headers = {'Authorization': 'Bearer ' + token}
    for path, key in [('/projects', 'projects'), ('/datasets', 'datasets'), ('/runs', 'runs'), ('/pairwise-runs', 'runs')]:
        assert client.get(path, headers=headers).json()[key] == []
    service.grant_project_access(owner.workspace_id, viewer.user_id, 'private')
    assert client.get('/datasets', headers=headers).json()['datasets'][0]['id'] == dataset.id


@pytest.mark.asyncio
async def test_baseline_requires_compatible_contract_and_uses_saved_rules():
    from app.services.baseline_service import BaselineService
    dataset = DatasetService().create_dataset('baseline', CONTENT)
    runner = EvaluationRunner(DeterministicFakeProvider.successful(), EvaluatorRegistry(),
                              requested_configuration={'release_rules': {'coverage_minimum': .8, 'exact_match_pass_rate_max_drop': .1}})
    first = await runner.run_evaluation(dataset_id=dataset.id)
    second = await runner.run_evaluation(dataset_id=dataset.id)
    service = BaselineService()
    service.mark_baseline(first)
    result = service.compare(second, first)
    assert result['rules']['coverage_minimum'] == .8
    assert result['status'] == 'passed'
    with connection.get_db() as db:
        db.get(EvaluationRunDB, second).is_simulated = not db.get(EvaluationRunDB, first).is_simulated
        db.commit()
    assert service.compare(second, first)['status'] == 'inconclusive'


@pytest.mark.asyncio
async def test_ui_auth_is_individual_and_does_not_fall_back_to_environment(monkeypatch):
    import asyncio
    from app.ui.session import bind_session, api_headers
    monkeypatch.setenv('WORKSPACE_API_TOKEN', 'global-owner-secret')
    with pytest.raises(ValueError):
        api_headers()
    @bind_session
    async def callback():
        await asyncio.sleep(.001)
        return api_headers()
    a, b = await asyncio.gather(callback({'access_token': 'a', 'workspace_id': 'one'}),
                                callback({'access_token': 'b', 'workspace_id': 'two'}))
    assert a == {'Authorization': 'Bearer a', 'X-Workspace-ID': 'one'}
    assert b == {'Authorization': 'Bearer b', 'X-Workspace-ID': 'two'}
    with pytest.raises(ValueError):
        api_headers()


def test_gradio_callbacks_receive_server_side_session_state(monkeypatch):
    monkeypatch.setenv('GRADIO_ANALYTICS_ENABLED', 'False')
    import app.ui.gradio_app as ui
    for fn in ui.demo.fns.values():
        if fn.fn and fn.fn.__name__ in ui._session_callbacks:
            assert ui.user_session in fn.inputs, fn.fn.__name__


def test_csv_formula_cells_are_neutralized():
    import csv, io
    from app.services.report_service import ReportService
    report = {'results': [{'example_id': '=1+1', 'prompt': '  @SUM(A1)', 'output': '-cmd', 'score': .5}]}
    row = next(csv.DictReader(io.StringIO(ReportService.to_csv(report))))
    assert row['example_id'].startswith("'") and row['prompt'].startswith("'") and row['output'].startswith("'")
    assert row['score'] == '0.5'


@pytest.mark.asyncio
async def test_candidate_usage_is_kept_separate_from_judge_usage():
    from app.providers.base import BaseProvider
    class Metered(BaseProvider):
        model_name = 'metered'
        async def generate(self, prompt):
            return 'answer', {'prompt_tokens': 21, 'completion_tokens': 7}
    dataset = DatasetService().create_dataset('usage', CONTENT)
    run_id = await EvaluationRunner(Metered(), EvaluatorRegistry()).run_evaluation(dataset_id=dataset.id)
    with connection.get_db() as db:
        result = db.query(EvaluationResultDB).filter_by(run_id=run_id, evaluator_name='exact_match').first()
        assert result.metadata_dict['candidate_usage']['prompt_tokens'] == 21
        assert result.prompt_tokens is None  # exact match uses no judge


@pytest.mark.asyncio
async def test_worker_heartbeat_continues_during_execution():
    import asyncio
    from app.services.queue_worker import QueueWorker, ClaimedRun
    worker = QueueWorker(heartbeat_interval_seconds=.01)
    started = asyncio.Event()
    calls = []
    worker._heartbeat = lambda claimed=None: calls.append(claimed.run_id if claimed else None)
    async def long_iteration():
        worker._active_claim = ClaimedRun('evaluation_run', 'long-running', None, None, {})
        started.set()
        await asyncio.sleep(10)
    worker.run_once = long_iteration
    await worker.start()
    await started.wait()
    await asyncio.sleep(.04)
    await worker.stop()
    assert calls.count('long-running') >= 2


def test_legacy_tokens_are_password_migration_only():
    from app.database.models import UserSessionDB
    service = IdentityService()
    owner, token = service.bootstrap('legacy@test.com', 'Legacy', 'Agency')
    with connection.get_db() as db:
        db.query(UserSessionDB).delete()
        db.commit()
    with pytest.raises(ValueError, match='Legacy tokens'):
        service.authenticate(token)
    assert service.authenticate(token, allow_legacy=True).user_id == owner.user_id
    service.change_password(owner.user_id, None, 'my-new-password')
    with pytest.raises(ValueError):
        service.authenticate(token, allow_legacy=True)


def test_automatic_retry_archives_results_and_keeps_exhausted_evidence():
    from app.database.models import RunAttemptDB
    from app.services.queue_worker import QueueWorker, ClaimedRun
    dataset = DatasetService().create_dataset('attempts', CONTENT)
    run_id = EvaluationRunner(DeterministicFakeProvider.successful(), EvaluatorRegistry()).create_run(dataset_id=dataset.id)
    with connection.get_db() as db:
        run = db.get(EvaluationRunDB, run_id)
        run.attempt_count = 1
        db.add(EvaluationResultDB(run_id=run_id, example_id='0', prompt='q', prediction='', expected_output='a',
                                  score=None, evaluator_name='exact_match', outcome='generation_error', error_message='timeout'))
        db.commit()
    worker = QueueWorker()
    worker._retry_or_fail(ClaimedRun('evaluation_run', run_id, None, None, {}), 'timeout')
    with connection.get_db() as db:
        assert db.query(EvaluationResultDB).filter_by(run_id=run_id).count() == 0
        archive = db.query(RunAttemptDB).filter_by(run_id=run_id).one()
        assert len(json.loads(archive.results_json)) == 1


def test_real_migrations_accept_percent_sign_path(monkeypatch, tmp_path):
    from sqlalchemy import inspect
    engine = create_engine('sqlite:///' + str(tmp_path / 'real%25.db'))
    monkeypatch.setattr(connection, 'engine', engine)
    connection.init_db()
    assert 'run_attempt_archives' in inspect(engine).get_table_names()
    assert 'parent_run_id' in {c['name'] for c in inspect(engine).get_columns('evaluation_runs')}
    engine.dispose()


@pytest.mark.asyncio
async def test_semantic_models_are_cached_by_identity_and_do_not_block_loop(monkeypatch):
    import asyncio, sys, time
    from types import SimpleNamespace
    from app.evaluators.similarity import SemanticSimilarityEvaluator
    loaded = []
    class Model:
        def __init__(self, name):
            loaded.append(name)
        def encode(self, value, convert_to_tensor):
            time.sleep(.02)
            return value
    monkeypatch.setitem(sys.modules, 'sentence_transformers', SimpleNamespace(
        SentenceTransformer=Model, util=SimpleNamespace(cos_sim=lambda *args: SimpleNamespace(item=lambda: .5))))
    task = asyncio.create_task(SemanticSimilarityEvaluator('first').evaluate('q', 'a', 'a'))
    await asyncio.sleep(.005)
    assert not task.done()
    await task
    await SemanticSimilarityEvaluator('second').evaluate('q', 'a', 'a')
    assert loaded == ['first', 'second']


def test_live_submission_cannot_use_process_keys_or_untrusted_endpoint():
    from fastapi.testclient import TestClient
    from app.api.main import app
    dataset = DatasetService().create_dataset('safe', CONTENT)
    response = TestClient(app).post('/runs', json={'dataset_id': dataset.id, 'candidate_provider': 'openai',
                                                  'evaluator_provider': 'mock', 'evaluator_model': 'mock',
                                                  'candidate_model': 'live-model', 'candidate_base_url': 'https://untrusted.test'})
    assert response.status_code == 400
    with connection.get_db() as db:
        assert db.query(EvaluationRunDB).count() == 0


@pytest.mark.asyncio
async def test_repeated_terminal_execution_does_not_change_original():
    dataset = DatasetService().create_dataset('terminal', CONTENT)
    runner = EvaluationRunner(DeterministicFakeProvider.successful(), EvaluatorRegistry())
    run_id = await runner.run_evaluation(dataset_id=dataset.id)
    with pytest.raises(ValueError, match='immutable'):
        await runner.run_evaluation(dataset_id=dataset.id, run_id=run_id)
    with connection.get_db() as db:
        assert db.get(EvaluationRunDB, run_id).status == 'completed'


def test_factory_can_forbid_process_credential_fallback():
    from app.providers.factory import ProviderFactory
    from app.providers.base import ProviderConfigurationError
    with pytest.raises(ProviderConfigurationError, match='process credentials are disabled'):
        ProviderFactory.create('openai', 'live-model', base_url='https://untrusted.test', use_default_api_key=False)


def test_production_bootstrap_requires_operator_secret(monkeypatch):
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    import app.api.main as api
    monkeypatch.setattr(api, 'get_settings', lambda: SimpleNamespace(app_environment='production', bootstrap_secret='operator-only'))
    client = TestClient(api.app)
    payload = {'email': 'owner@test.com', 'display_name': 'Owner', 'workspace_name': 'Agency', 'password': 'initial-password'}
    assert client.get('/projects').status_code == 401
    assert client.post('/auth/bootstrap', json=payload).status_code == 403
    assert client.post('/auth/bootstrap', json=payload, headers={'X-Setup-Token': 'operator-only'}).status_code == 201
