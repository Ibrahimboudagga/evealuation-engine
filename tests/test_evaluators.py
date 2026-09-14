import pytest
import sys
from types import SimpleNamespace

import app.evaluators.similarity as similarity_module
from app.evaluators.exact_match import ExactMatchEvaluator
from app.evaluators.similarity import SemanticSimilarityEvaluator
from app.evaluators.llm_judge import LLMAsAJudgeEvaluator
from tests.fakes import DeterministicFakeProvider

@pytest.mark.asyncio
async def test_exact_match_evaluator():
    evaluator = ExactMatchEvaluator()
    assert evaluator.name == "exact_match"
    
    # Matching (trimmed, case insensitive)
    res1 = await evaluator.evaluate("capital of France?", "Paris", "  paris  ")
    assert res1.score == 1.0
    
    # Mismatching
    res2 = await evaluator.evaluate("capital of France?", "Paris", "London")
    assert res2.score == 0.0

@pytest.mark.asyncio
async def test_semantic_similarity_evaluator(monkeypatch):
    class FakeModel:
        def encode(self, text, convert_to_tensor):
            return text

    class FakeSimilarity:
        @staticmethod
        def cos_sim(expected, prediction):
            return SimpleNamespace(item=lambda: 0.9 if "Paris" in prediction else 0.1)

    fake_sentence_transformers = SimpleNamespace(
        SentenceTransformer=lambda _: FakeModel(),
        util=FakeSimilarity,
    )
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_sentence_transformers)
    monkeypatch.setattr(similarity_module, "_ST_MODEL", None)
    evaluator = SemanticSimilarityEvaluator()
    assert evaluator.name == "semantic_similarity"
    
    # Evaluate highly similar sentences
    res1 = await evaluator.evaluate(
        "prompt", 
        "The capital of France is Paris.", 
        "Paris is France's capital city."
    )
    # Stubbed embeddings keep this test deterministic and independent of a model download.
    assert res1.score > 0.6
    
    # Evaluate completely different sentences
    res2 = await evaluator.evaluate(
        "prompt", 
        "The capital of France is Paris.", 
        "I like to eat red apples on Tuesday."
    )
    assert res2.score < res1.score

@pytest.mark.asyncio
async def test_llm_judge_evaluator():
    # The deterministic provider returns JSON for LLM judge prompts.
    evaluator = LLMAsAJudgeEvaluator(DeterministicFakeProvider.valid_judge())
    assert evaluator.name == "llm_judge"
    
    res = await evaluator.evaluate(
        "Who is the president?", 
        "George Washington", 
        "George Washington was the first president."
    )
    
    # The deterministic judge returns score 8, normalized to 0.8.
    assert res.score == 0.8
    assert res.metadata["reason"] == "deterministic judge response"


def test_llm_judge_broken_json_parsing():
    """Verify that _extract_and_parse_json handles broken JSON via json_repair."""
    evaluator = LLMAsAJudgeEvaluator(DeterministicFakeProvider.valid_judge())

    # Trailing comma (common LLM mistake)
    result = evaluator._extract_and_parse_json('{"score": 8, "reason": "good",}')
    assert result["score"] == 8
    assert result["reason"] == "good"

    # Missing quotes around keys
    result = evaluator._extract_and_parse_json("{score: 7, reason: \"ok\"}")
    assert result["score"] == 7

    # Wrapped in markdown code block
    result = evaluator._extract_and_parse_json('```json\n{"score": 9, "reason": "great"}\n```')
    assert result["score"] == 9

    # Extra text around JSON
    result = evaluator._extract_and_parse_json('Here is the result: {"score": 6, "reason": "fine"} hope this helps')
    assert result["score"] == 6

    # Single quotes instead of double quotes
    result = evaluator._extract_and_parse_json("{'score': 5, 'reason': 'meh'}")
    assert result["score"] == 5
