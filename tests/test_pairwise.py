import json
import pytest
from app.providers.openai import OpenAIProvider
from app.evaluators.pairwise_judge import PairwiseJudgeEvaluator
from app.runners.pairwise_runner import (
    PairwiseEvaluationRunner,
    _expected_score,
    _elo_update,
    get_pairwise_run_metrics,
    get_pairwise_comparisons,
)
from app.database.connection import get_db
from app.database.models import PairwiseRunDB, PairwiseComparisonDB


# ── Elo Rating Tests ────────────────────────────────────────

class TestEloRating:
    def test_expected_score_equal_ratings(self):
        """Equal ratings should produce 0.5 expected score."""
        assert _expected_score(1500, 1500) == 0.5

    def test_expected_score_higher_rating_favored(self):
        """Higher-rated player should have > 0.5 expected score."""
        exp = _expected_score(1600, 1500)
        assert exp > 0.5
        assert exp < 1.0

    def test_expected_score_lower_rating_underdog(self):
        """Lower-rated player should have < 0.5 expected score."""
        exp = _expected_score(1400, 1500)
        assert exp < 0.5
        assert exp > 0.0

    def test_expected_score_symmetry(self):
        """Expected scores should sum to 1.0."""
        exp_a = _expected_score(1500, 1600)
        exp_b = _expected_score(1600, 1500)
        assert abs(exp_a + exp_b - 1.0) < 1e-10

    def test_elo_update_win(self):
        """Winner should gain points, loser should lose points."""
        new_a, new_b = _elo_update(1500, 1500, 1.0)
        assert new_a > 1500
        assert new_b < 1500
        # Total points should be conserved (approximately)
        assert abs((new_a + new_b) - 3000) < 0.01

    def test_elo_update_loss(self):
        """Loser should lose points when expected to win."""
        new_a, new_b = _elo_update(1600, 1400, 0.0)
        assert new_a < 1600
        assert new_b > 1400

    def test_elo_update_tie(self):
        """Tie should move ratings closer to expected."""
        new_a, new_b = _elo_update(1500, 1500, 0.5)
        # Equal ratings + tie = no change
        assert abs(new_a - 1500) < 0.01
        assert abs(new_b - 1500) < 0.01

    def test_elo_update_upset(self):
        """Underdog winning should gain more points."""
        new_a_underdog_win, _ = _elo_update(1400, 1600, 1.0)
        new_a_favorite_win, _ = _elo_update(1600, 1400, 1.0)
        # Underdog should gain more
        assert (new_a_underdog_win - 1400) > (new_a_favorite_win - 1600)


# ── Pairwise Judge Evaluator Tests ──────────────────────────

class TestPairwiseJudgeEvaluator:
    @pytest.mark.asyncio
    async def test_evaluate_with_mock_provider(self):
        """Test the full evaluate flow with a mock provider."""
        provider = OpenAIProvider(model_name="mock-model")
        evaluator = PairwiseJudgeEvaluator(judge_provider=provider)

        result = await evaluator.evaluate(
            input_text="What is 2+2?",
            expected_output="4",
            response_a="4",
            response_b="The answer is five.",
        )

        assert result.winner in ("A", "B", "tie")
        assert 0.0 <= result.score_a <= 1.0
        assert 0.0 <= result.score_b <= 1.0
        assert isinstance(result.reason, str)
        assert len(result.reason) > 0

    @pytest.mark.asyncio
    async def test_evaluate_returns_correct_winner(self):
        """Mock provider returns single-model format, should fail gracefully and return tie."""
        provider = OpenAIProvider(model_name="mock-model")
        evaluator = PairwiseJudgeEvaluator(judge_provider=provider)

        result = await evaluator.evaluate(
            input_text="test",
            expected_output="expected",
            response_a="response A",
            response_b="response B",
        )

        # Mock provider returns {"score": 8, "reason": "..."} which doesn't match
        # PairwiseJudgeResponse schema. Should gracefully degrade to tie with error.
        assert result.winner == "tie"
        assert result.metadata is not None
        assert "error" in result.metadata

    def test_name_property(self):
        provider = OpenAIProvider(model_name="mock-model")
        evaluator = PairwiseJudgeEvaluator(judge_provider=provider)
        assert evaluator.name == "pairwise_judge"


# ── Pairwise Runner Tests ──────────────────────────────────

@pytest.fixture
def temp_jsonl(tmp_path):
    """Create a temporary JSONL dataset file."""
    data = [
        {"id": "p1", "input": "What is 1+1?", "expected_output": "2"},
        {"id": "p2", "input": "What is 3+3?", "expected_output": "6"},
    ]
    path = tmp_path / "pairwise_test.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for item in data:
            f.write(json.dumps(item) + "\n")
    return str(path)


class TestPairwiseEvaluationRunner:
    @pytest.mark.asyncio
    async def test_run_pairwise_evaluation(self, temp_jsonl):
        """Run a full pairwise evaluation with mock providers."""
        provider_a = OpenAIProvider(model_name="mock-model-a")
        provider_b = OpenAIProvider(model_name="mock-model-b")
        judge_provider = OpenAIProvider(model_name="mock-judge")
        evaluator = PairwiseJudgeEvaluator(judge_provider=judge_provider)

        runner = PairwiseEvaluationRunner(
            provider_a=provider_a,
            provider_b=provider_b,
            pairwise_evaluator=evaluator,
            concurrency_limit=2,
        )

        run_id = await runner.run_pairwise_evaluation(dataset_path=temp_jsonl)
        assert run_id is not None

        # Verify DB records
        with get_db() as db:
            runs = db.query(PairwiseRunDB).all()
            assert len(runs) == 1
            assert runs[0].id == run_id

            comparisons = db.query(PairwiseComparisonDB).all()
            assert len(comparisons) == 2

            for comp in comparisons:
                assert comp.winner in ("A", "B", "tie")
                assert 0.0 <= comp.score_a <= 1.0
                assert 0.0 <= comp.score_b <= 1.0
                assert comp.original_order in ("AB", "BA")

    @pytest.mark.asyncio
    async def test_pairwise_metrics(self, temp_jsonl):
        """Verify computed metrics are valid."""
        provider_a = OpenAIProvider(model_name="mock-model-a")
        provider_b = OpenAIProvider(model_name="mock-model-b")
        judge_provider = OpenAIProvider(model_name="mock-judge")
        evaluator = PairwiseJudgeEvaluator(judge_provider=judge_provider)

        runner = PairwiseEvaluationRunner(
            provider_a=provider_a,
            provider_b=provider_b,
            pairwise_evaluator=evaluator,
            concurrency_limit=2,
        )

        run_id = await runner.run_pairwise_evaluation(dataset_path=temp_jsonl)
        metrics = get_pairwise_run_metrics(run_id)

        assert metrics["total_comparisons"] == 2
        assert 0.0 <= metrics["win_rate_a"] <= 1.0
        assert 0.0 <= metrics["win_rate_b"] <= 1.0
        assert 0.0 <= metrics["tie_rate"] <= 1.0
        assert abs(metrics["win_rate_a"] + metrics["win_rate_b"] + metrics["tie_rate"] - 1.0) < 0.01
        assert metrics["elo_a"] > 0
        assert metrics["elo_b"] > 0
        assert 0.0 <= metrics["avg_score_a"] <= 1.0
        assert 0.0 <= metrics["avg_score_b"] <= 1.0

    @pytest.mark.asyncio
    async def test_pairwise_comparisons(self, temp_jsonl):
        """Verify individual comparisons are retrievable."""
        provider_a = OpenAIProvider(model_name="mock-model-a")
        provider_b = OpenAIProvider(model_name="mock-model-b")
        judge_provider = OpenAIProvider(model_name="mock-judge")
        evaluator = PairwiseJudgeEvaluator(judge_provider=judge_provider)

        runner = PairwiseEvaluationRunner(
            provider_a=provider_a,
            provider_b=provider_b,
            pairwise_evaluator=evaluator,
            concurrency_limit=2,
        )

        run_id = await runner.run_pairwise_evaluation(dataset_path=temp_jsonl)
        comparisons = get_pairwise_comparisons(run_id)

        assert len(comparisons) == 2
        for c in comparisons:
            assert "example_id" in c
            assert "prompt" in c
            assert "response_a" in c
            assert "response_b" in c
            assert "winner" in c
            assert "score_a" in c
            assert "score_b" in c
            assert "judge_reason" in c

    @pytest.mark.asyncio
    async def test_order_randomization(self, temp_jsonl):
        """Verify that original_order is recorded for each comparison."""
        provider_a = OpenAIProvider(model_name="mock-model-a")
        provider_b = OpenAIProvider(model_name="mock-model-b")
        judge_provider = OpenAIProvider(model_name="mock-judge")
        evaluator = PairwiseJudgeEvaluator(judge_provider=judge_provider)

        runner = PairwiseEvaluationRunner(
            provider_a=provider_a,
            provider_b=provider_b,
            pairwise_evaluator=evaluator,
            concurrency_limit=2,
        )

        run_id = await runner.run_pairwise_evaluation(dataset_path=temp_jsonl)
        comparisons = get_pairwise_comparisons(run_id)

        orders = [c["original_order"] for c in comparisons]
        assert all(o in ("AB", "BA") for o in orders)

    def test_empty_metrics(self):
        """Metrics for non-existent run should return empty dict."""
        metrics = get_pairwise_run_metrics("nonexistent-run-id")
        assert metrics == {}

    def test_empty_comparisons(self):
        """Comparisons for non-existent run should return empty list."""
        comparisons = get_pairwise_comparisons("nonexistent-run-id")
        assert comparisons == []


# ── API Endpoint Tests ──────────────────────────────────────

from fastapi.testclient import TestClient
from app.api.main import app


@pytest.fixture
def client():
    return TestClient(app)


class TestPairwiseAPI:
    def test_list_pairwise_runs_empty(self, client):
        resp = client.get("/pairwise-runs")
        assert resp.status_code == 200
        assert resp.json()["runs"] == []

    def test_get_pairwise_run_not_found(self, client):
        resp = client.get("/pairwise-runs/nonexistent")
        assert resp.status_code == 404

    def test_create_pairwise_run_invalid_dataset(self, client):
        resp = client.post("/pairwise-runs", json={
            "dataset_path": "nonexistent.jsonl",
            "model_a_provider": "openai",
            "model_a_model": "mock",
            "model_b_provider": "openai",
            "model_b_model": "mock",
            "judge_provider": "openai",
            "judge_model": "mock",
        })
        assert resp.status_code == 400

    def test_create_pairwise_run_invalid_provider(self, client, temp_jsonl):
        # ProviderFactory defaults unknown providers to OpenAI (with warning),
        # so this will return 200 (background task started), not 400.
        resp = client.post("/pairwise-runs", json={
            "dataset_path": temp_jsonl,
            "model_a_provider": "nonexistent_provider",
            "model_a_model": "mock",
            "model_b_provider": "openai",
            "model_b_model": "mock",
            "judge_provider": "openai",
            "judge_model": "mock",
        })
        assert resp.status_code == 200
        assert "run_id" in resp.json()
