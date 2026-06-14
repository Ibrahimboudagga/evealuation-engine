import argparse
import asyncio
import sys
import structlog
from pathlib import Path
from app.config import get_settings
from app.providers.factory import ProviderFactory
from app.evaluators.registry import EvaluatorRegistry
from app.runners.eval_runner import EvaluationRunner, load_dataset, get_run_metrics

log = structlog.get_logger()

async def main():
    settings = get_settings()

    parser = argparse.ArgumentParser(description="LLM Evaluation Engine CLI")
    
    # Dataset path
    parser.add_argument(
        "--dataset", 
        type=str, 
        default="datasets/sample.jsonl", 
        help="Path to the JSONL dataset file"
    )
    
    # Concurrency limit
    parser.add_argument(
        "--concurrency", 
        type=int, 
        default=settings.default_concurrency, 
        help="Maximum parallel completions to request from the provider"
    )
    
    # --- LEGACY OPTION (For Backwards Compatibility) ---
    parser.add_argument(
        "--model", 
        type=str, 
        default=None, 
        help="Legacy model identifier (e.g. openai-gpt-4o, openai-mock)"
    )
    
    # --- CANDIDATE MODEL OPTIONS (User-provided) ---
    parser.add_argument(
        "--candidate-provider",
        type=str,
        default=settings.default_candidate_provider,
        help="Candidate provider name (e.g. openai, anthropic, google, groq, huggingface)"
    )
    parser.add_argument(
        "--candidate-model",
        type=str,
        default=settings.default_candidate_model,
        help="Candidate model ID / identifier"
    )
    parser.add_argument(
        "--candidate-api-key",
        type=str,
        default=None,
        help="Authentication key for the candidate provider"
    )
    parser.add_argument(
        "--candidate-base-url",
        type=str,
        default=None,
        help="Custom API base URL endpoint (for Groq, Hugging Face, or compatible endpoints)"
    )
    
    # --- EVALUATOR MODEL OPTIONS (Fixed Selection Judge) ---
    parser.add_argument(
        "--evaluator-provider",
        type=str,
        default=settings.default_evaluator_provider,
        help="Evaluator/judge provider name (e.g. openai, anthropic, google)"
    )
    parser.add_argument(
        "--evaluator-model",
        type=str,
        default=settings.default_evaluator_model,
        help="Evaluator/judge model ID / identifier"
    )
    parser.add_argument(
        "--evaluator-api-key",
        type=str,
        default=None,
        help="Authentication key override for the evaluator model"
    )
    parser.add_argument(
        "--judge-prompt-template",
        type=str,
        default=None,
        help="Path to a plain text file containing the judge prompt template. "
             "May contain {input}, {expected_output}, and {prediction} placeholders."
    )

    args = parser.parse_args()
    
    # Load judge prompt template if provided
    judge_prompt_template = None
    if args.judge_prompt_template:
        try:
            judge_prompt_template = Path(args.judge_prompt_template).read_text(encoding="utf-8")
        except Exception as e:
            log.error("error_loading_judge_prompt_template", error=str(e))
            sys.exit(1)
    
    # Resolve candidate details (supporting backwards compatibility of the --model flag)
    candidate_provider = args.candidate_provider
    candidate_model = args.candidate_model
    
    if args.model is not None:
        # User supplied old --model format, parse it
        parts = args.model.split("-", 1)
        candidate_provider = parts[0]
        candidate_model = parts[1] if len(parts) > 1 else "gpt-4o"
    
    log.info(
        "pipeline_start",
        dataset=args.dataset,
        candidate_model=f"{candidate_provider.upper()} ({candidate_model})",
        candidate_base_url=args.candidate_base_url,
        evaluator_model=f"{args.evaluator_provider.upper()} ({args.evaluator_model})",
        concurrency=args.concurrency,
    )
    
    # 1. Load the dataset
    try:
        examples = load_dataset(args.dataset)
        log.info("dataset_loaded", example_count=len(examples))
    except Exception as e:
        log.error("error_loading_dataset", error=str(e))
        sys.exit(1)
        
    # 2. Instantiate Candidate LLM provider
    try:
        candidate_provider_instance = ProviderFactory.create(
            provider=candidate_provider,
            model_id=candidate_model,
            api_key=args.candidate_api_key,
            base_url=args.candidate_base_url
        )
    except Exception as e:
        log.error("error_initializing_candidate_provider", error=str(e))
        sys.exit(1)
        
    # 3. Instantiate Evaluator/Judge LLM provider
    try:
        evaluator_provider_instance = ProviderFactory.create(
            provider=args.evaluator_provider,
            model_id=args.evaluator_model,
            api_key=args.evaluator_api_key
        )
    except Exception as e:
        log.error("error_initializing_evaluator_provider", error=str(e))
        sys.exit(1)
        
    # 4. Instantiate Evaluator Registry using the designated evaluator provider
    registry = EvaluatorRegistry(
        judge_provider=evaluator_provider_instance,
        judge_prompt_template=judge_prompt_template,
    )
    
    # 5. Instantiate Evaluation Runner using the candidate provider
    runner = EvaluationRunner(
        provider=candidate_provider_instance, 
        registry=registry, 
        concurrency_limit=args.concurrency
    )
    
    # 6. Run the evaluation
    log.info("generating_predictions_and_executing_evaluators")
    try:
        run_id = await runner.run_evaluation(args.dataset, examples)
        log.info("evaluation_pipeline_finished")
    except Exception as e:
        log.error("run_execution_failed", error=str(e))
        sys.exit(1)
        
    # 7. Retrieve metrics and log reports
    metrics = get_run_metrics(run_id)
    if not metrics:
        log.warning("no_evaluation_results_found", run_id=run_id)
        sys.exit(1)
        
    log.info(
        "evaluation_metrics_report",
        run_id=metrics.get("run_id"),
        total_examples=metrics.get("total_examples"),
    )
    
    eval_metrics = metrics.get("evaluators", {})
    
    if "exact_match" in eval_metrics:
        em = eval_metrics["exact_match"]
        log.info(
            "exact_match_metrics",
            accuracy_pct=em["avg_score"] * 100,
            pass_rate_pct=em["pass_rate"] * 100,
        )
        
    if "semantic_similarity" in eval_metrics:
        sim = eval_metrics["semantic_similarity"]
        log.info(
            "semantic_similarity_metrics",
            avg_score=sim["avg_score"],
            pass_rate_pct=sim["pass_rate"] * 100,
        )
        
    if "llm_judge" in eval_metrics:
        judge = eval_metrics["llm_judge"]
        log.info(
            "llm_judge_metrics",
            avg_score_1_to_10=judge["avg_score"] * 10.0,
            pass_rate_pct=judge["pass_rate"] * 100,
        )
        
    log.info("results_stored_in_database", database="evals.db")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("process_interrupted_by_user")
        sys.exit(1)
