import pytest
import os
import json
from pathlib import Path

from app.database.connection import get_db
from app.database.models import DatasetDB, EvaluationRunDB, EvaluationResultDB
from app.providers.factory import ProviderFactory
from app.evaluators.registry import EvaluatorRegistry
from app.runners.eval_runner import EvaluationRunner, load_dataset, get_run_metrics

@pytest.fixture
def temp_dataset(tmp_path):
    """Creates a temporary JSONL dataset file."""
    dataset_file = tmp_path / "test_dataset.jsonl"
    data = [
        {"id": "test_1", "input": "Capital of Spain?", "expected_output": "Madrid"},
        {"id": "test_2", "input": "Capital of Italy?", "expected_output": "Rome"}
    ]
    with open(dataset_file, "w", encoding="utf-8") as f:
        for item in data:
            f.write(json.dumps(item) + "\n")
    return str(dataset_file)

def test_load_dataset(temp_dataset):
    examples = load_dataset(temp_dataset)
    assert len(examples) == 2
    assert examples[0].id == "test_1"
    assert examples[0].input == "Capital of Spain?"
    assert examples[0].expected_output == "Madrid"

@pytest.mark.asyncio
async def test_evaluation_runner_pipeline(temp_dataset):
    examples = load_dataset(temp_dataset)
    
    # 1. Setup mock provider and registry
    provider = ProviderFactory.create("openai-mock")
    registry = EvaluatorRegistry(judge_provider=provider)
    
    runner = EvaluationRunner(provider=provider, registry=registry, concurrency_limit=2)
    
    # 2. Run the evaluation
    run_id = await runner.run_evaluation(temp_dataset, examples)
    assert run_id is not None
    
    # 3. Query DB directly to verify records
    with get_db() as db:
        # Check dataset record
        datasets = db.query(DatasetDB).all()
        assert len(datasets) == 1
        assert datasets[0].name == Path(temp_dataset).name
        
        # Check run record
        runs = db.query(EvaluationRunDB).all()
        assert len(runs) == 1
        assert runs[0].id == run_id
        assert runs[0].model_name == "mock"
        
        # Check results records (2 examples x 3 evaluators = 6 results)
        results = db.query(EvaluationResultDB).all()
        assert len(results) == 6
        
        # Exact match evaluation for "Madrid" vs mock prediction should be 0.0
        em_results = db.query(EvaluationResultDB).filter(
            EvaluationResultDB.evaluator_name == "exact_match"
        ).all()
        assert len(em_results) == 2
        assert em_results[0].score == 0.0

    # 4. Verify aggregated metrics
    metrics = get_run_metrics(run_id)
    assert metrics["run_id"] == run_id
    assert metrics["total_examples"] == 2
    assert "exact_match" in metrics["evaluators"]
    assert "semantic_similarity" in metrics["evaluators"]
    assert "llm_judge" in metrics["evaluators"]
