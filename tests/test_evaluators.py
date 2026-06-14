import pytest
from app.evaluators.exact_match import ExactMatchEvaluator
from app.evaluators.similarity import SemanticSimilarityEvaluator
from app.evaluators.llm_judge import LLMAsAJudgeEvaluator
from app.providers.openai import OpenAIProvider

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
async def test_semantic_similarity_evaluator():
    evaluator = SemanticSimilarityEvaluator()
    assert evaluator.name == "semantic_similarity"
    
    # Evaluate highly similar sentences
    res1 = await evaluator.evaluate(
        "prompt", 
        "The capital of France is Paris.", 
        "Paris is France's capital city."
    )
    # Even if it falls back or runs real sentence-transformers, it should return a high similarity
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
    # Setup mock provider which returns JSON for LLM judge prompts
    mock_provider = OpenAIProvider(model_name="mock-model")
    evaluator = LLMAsAJudgeEvaluator(mock_provider)
    assert evaluator.name == "llm_judge"
    
    res = await evaluator.evaluate(
        "Who is the president?", 
        "George Washington", 
        "George Washington was the first president."
    )
    
    # Mock provider returns score 8 in mock JSON, normalized score should be 0.8
    assert res.score == 0.8
    assert "MOCK OpenAI" in res.metadata["reason"]
