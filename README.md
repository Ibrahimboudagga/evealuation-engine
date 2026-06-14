# LLM Evaluation Framework

A modular evaluation engine for comparing LLM providers on structured datasets. Supports CLI, REST API, and a Gradio web UI.

## Features

- **Multiple providers**: OpenAI, Anthropic, Gemini/Google, Cohere, and OpenAI-compatible endpoints (Groq, Hugging Face, etc.).
- **Mock mode**: Run full pipelines locally without API keys for development and testing.
- **Async evaluation**: Configurable concurrency via `asyncio` semaphore.
- **Three evaluators**: Exact match, semantic similarity (sentence-transformers), and LLM-as-a-judge with custom prompt support.
- **Structured logging**: `structlog`-based logging throughout all modules.
- **SQLite persistence**: Datasets, runs, and per-example results stored in `evals.db`.
- **REST API**: FastAPI layer for triggering runs, checking status, and listing datasets.
- **Web UI**: Gradio interface for interactive evaluation and result browsing.
- **Pydantic v2**: Typed schemas for settings, examples, results, and API request/response models.

## Project Structure

```text
app/
  config.py              # .env-backed settings via pydantic-settings
  database/
    connection.py        # SQLAlchemy engine, sessions, init_db()
    models.py            # DatasetDB, EvaluationRunDB, EvaluationResultDB
  evaluators/
    base.py              # BaseEvaluator abstract class
    exact_match.py       # Case-insensitive exact string match
    llm_judge.py         # LLM-as-a-judge with JSON parsing
    registry.py          # EvaluatorRegistry (exact_match, similarity, llm_judge)
    similarity.py        # Semantic similarity with sentence-transformers fallback
  providers/
    base.py              # BaseProvider abstract class
    openai.py            # OpenAI + OpenAI-compatible providers
    anthropic.py         # Anthropic Claude provider
    gemini.py            # Google Gemini provider
    cohere.py            # Cohere Command-R provider
    factory.py           # ProviderFactory (resolves provider names to classes)
  runners/
    eval_runner.py       # EvaluationRunner, load_dataset(), get_run_metrics()
  schemas/
    example.py           # EvaluationExample
    result.py            # EvaluationResult
  api/
    main.py              # FastAPI app (/runs, /runs/{id}, /datasets)
    schemas.py           # Pydantic v2 request/response models
  ui/
    gradio_app.py        # Gradio Blocks UI (talks to FastAPI via httpx)
datasets/
  sample.jsonl           # Sample 5-example evaluation dataset
tests/
  conftest.py            # Temporary SQLite database per test
  test_config.py
  test_evaluators.py
  test_providers.py
  test_runner.py
run_eval.py              # CLI entry point
requirements.txt
.env.example
README.md
PROJECT_DOCUMENTATION.md
```

## Getting Started

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

On Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Edit `.env` and fill in API keys for the providers you intend to use. If keys are omitted, providers fall back to mock mode automatically.

### 3. Run the CLI

```bash
python run_eval.py --dataset datasets/sample.jsonl --model openai-mock --concurrency 3
```

### 4. Run the REST API

```bash
uvicorn app.api.main:app --reload --port 8000
```

Interactive docs at `http://localhost:8000/docs`.

### 5. Run the Gradio Web UI

Start the API server first, then:

```bash
python app/ui/gradio_app.py
```

---

## CLI Usage

### All Options

| Option | Purpose | Default |
|---|---|---|
| `--dataset` | Path to JSONL dataset file | `datasets/sample.jsonl` |
| `--concurrency` | Max parallel provider calls | from `.env` (`3`) |
| `--model` | Legacy provider-model syntax (e.g. `openai-mock`) | none |
| `--candidate-provider` | Provider for generating predictions | `openai` |
| `--candidate-model` | Model for prediction generation | `mock` |
| `--candidate-api-key` | API key override for candidate provider | none |
| `--candidate-base-url` | Custom base URL for compatible endpoints | none |
| `--evaluator-provider` | Provider for LLM-as-a-judge | `openai` |
| `--evaluator-model` | Model for LLM-as-a-judge | `mock` |
| `--evaluator-api-key` | API key override for evaluator provider | none |
| `--judge-prompt-template` | Path to custom judge prompt text file | none |

### Examples

**Mock run (no API keys needed):**

```bash
python run_eval.py --model openai-mock
```

**OpenAI with gpt-4o:**

```bash
python run_eval.py \
  --candidate-provider openai \
  --candidate-model gpt-4o \
  --candidate-api-key sk-... \
  --evaluator-provider openai \
  --evaluator-model gpt-4o-mini
```

**Cohere with Command-R-Plus:**

```bash
python run_eval.py \
  --candidate-provider cohere \
  --candidate-model command-r-plus \
  --candidate-api-key YOUR_KEY
```

**Groq (OpenAI-compatible):**

```bash
python run_eval.py \
  --candidate-provider groq \
  --candidate-model llama3-8b-8192 \
  --candidate-api-key YOUR_KEY
```

**Custom judge prompt:**

```bash
python run_eval.py \
  --model openai-mock \
  --judge-prompt-template prompts/medical_judge.txt
```

---

## REST API

### `POST /runs`

Trigger a new evaluation run in the background.

**Request body:**

```json
{
  "dataset_path": "datasets/sample.jsonl",
  "candidate_provider": "openai",
  "candidate_model": "gpt-4o",
  "candidate_api_key": "sk-...",
  "evaluator_provider": "anthropic",
  "evaluator_model": "claude-3-5-sonnet-latest",
  "evaluator_api_key": "sk-...",
  "concurrency": 5,
  "judge_prompt_template": null
}
```

**Response (`200`):**

```json
{
  "run_id": "uuid-string",
  "status": "started"
}
```

### `GET /runs/{run_id}`

Get run status and aggregated metrics.

**Response (`200`):**

```json
{
  "run_id": "uuid-string",
  "status": "completed",
  "metrics": [
    {"evaluator": "exact_match", "mean_score": 0.8, "pass_rate": 0.8, "n": 10},
    {"evaluator": "semantic_similarity", "mean_score": 0.72, "pass_rate": 0.7, "n": 10},
    {"evaluator": "llm_judge", "mean_score": 0.85, "pass_rate": 0.9, "n": 10}
  ],
  "error": null
}
```

### `GET /runs`

List all tracked runs.

**Response (`200`):**

```json
{
  "runs": [
    {"run_id": "uuid-1", "status": "completed"},
    {"run_id": "uuid-2", "status": "started"}
  ]
}
```

### `GET /datasets`

List all datasets recorded in the database.

**Response (`200`):**

```json
{
  "datasets": [
    {"id": "C:\\...\\datasets\\sample.jsonl", "name": "sample.jsonl"}
  ]
}
```

---

## Gradio Web UI

The Gradio interface provides two tabs:

### Tab 1 -- Run Evaluation

Fill in all fields and click **Submit Run**. The UI sends a `POST` to the FastAPI server and displays the returned `run_id` and status.

Inputs:
- Dataset Path
- Candidate Provider (dropdown: openai, anthropic, cohere, gemini, mock)
- Candidate Model
- Candidate API Key (password field)
- Evaluator Provider (dropdown)
- Evaluator Model
- Evaluator API Key (password field)
- Concurrency (slider, 1-20)
- Judge Prompt Template (optional, multiline)

### Tab 2 -- View Results

Enter a Run ID and click **Fetch Results**. The UI queries the API and displays:
- Run status as text
- Metrics as a table with columns: Evaluator, Mean Score, Pass Rate, N

---

## Providers

| Provider | File | Default Model | Factory Identifiers |
|---|---|---|---|
| OpenAI | `app/providers/openai.py` | `gpt-4o` | `openai` |
| Anthropic | `app/providers/anthropic.py` | `claude-3-5-sonnet-latest` | `anthropic` |
| Gemini | `app/providers/gemini.py` | `gemini-1.5-flash` | `gemini`, `google` |
| Cohere | `app/providers/cohere.py` | `command-r-plus` | `cohere`, `co` |
| Groq | via `openai.py` | user-specified | `groq` |
| Hugging Face | via `openai.py` | user-specified | `huggingface`, `hf` |
| OpenAI-compatible | via `openai.py` | user-specified | `openai-compatible`, `compatible` |
| Mock | via `openai.py` | `mock-model` | `mock`, `dummy` |

Legacy syntax (`openai-gpt-4o`, `anthropic-mock`) is also supported by splitting on the first hyphen.

---

## Evaluators

| Evaluator | File | Score Range | Description |
|---|---|---|---|
| Exact Match | `app/evaluators/exact_match.py` | 0.0 or 1.0 | Case-insensitive trimmed string equality |
| Semantic Similarity | `app/evaluators/similarity.py` | 0.0 -- 1.0 | Sentence-transformers cosine similarity (falls back to token-overlap) |
| LLM-as-a-Judge | `app/evaluators/llm_judge.py` | 0.1 -- 1.0 | LLM grades predictions on a 1-10 scale, normalized to 0.1-1.0 |

All evaluators return `(text, usage)` tuples. Token usage from the judge provider is recorded per result.

---

## Configuration

All settings are in `app/config.py` and loaded from environment variables or `.env`.

| Variable | Purpose | Default |
|---|---|---|
| `DATABASE_URL` | SQLAlchemy database URL | `sqlite:///evals.db` |
| `OPENAI_API_KEY` | OpenAI API key | empty |
| `ANTHROPIC_API_KEY` | Anthropic API key | empty |
| `GEMINI_API_KEY` | Gemini API key | empty |
| `GOOGLE_API_KEY` | Alternate Gemini key | empty |
| `COHERE_API_KEY` | Cohere API key | empty |
| `DEFAULT_CANDIDATE_PROVIDER` | Default candidate provider | `openai` |
| `DEFAULT_CANDIDATE_MODEL` | Default candidate model | `mock` |
| `DEFAULT_EVALUATOR_PROVIDER` | Default evaluator provider | `openai` |
| `DEFAULT_EVALUATOR_MODEL` | Default evaluator model | `mock` |
| `DEFAULT_CONCURRENCY` | Parallel evaluation limit | `3` |
| `SIMILARITY_MODEL_NAME` | SentenceTransformer model | `all-MiniLM-L6-v2` |

---

## Database

Results are stored in `evals.db` by default. Override `DATABASE_URL` in `.env` to use a different file or another SQLAlchemy-supported backend.

Tables: `datasets`, `evaluation_runs`, `evaluation_results`.

---

## Extending the Framework

### Adding a Provider

1. Create `app/providers/your_provider.py` implementing `BaseProvider`.
2. Add a branch in `ProviderFactory.create()` in `app/providers/factory.py`.
3. Import it in `app/providers/__init__.py`.

### Adding an Evaluator

1. Create `app/evaluators/your_evaluator.py` implementing `BaseEvaluator`.
2. Register it in `EvaluatorRegistry.__init__()` in `app/evaluators/registry.py`.

```python
from app.evaluators.base import BaseEvaluator
from app.schemas.result import EvaluationResult

class LengthEvaluator(BaseEvaluator):
    @property
    def name(self) -> str:
        return "length_ratio"

    async def evaluate(self, input_text, expected_output, prediction) -> EvaluationResult:
        ratio = min(len(prediction), len(expected_output)) / max(len(prediction), len(expected_output), 1)
        return EvaluationResult(
            example_id="", prompt=input_text, prediction=prediction,
            expected_output=expected_output, score=ratio, evaluator_name=self.name,
        )
```

---

## Testing

```bash
pytest
```

Tests use a temporary SQLite database per test (via `tests/conftest.py`) and do not touch `evals.db`.

---

## Dependencies

```
pydantic>=2.0.0
pydantic-settings>=2.0.0
SQLAlchemy>=2.0.0
openai>=1.0.0
anthropic>=0.18.0
google-generativeai>=0.3.0
cohere>=5.0.0
fastapi>=0.110.0
uvicorn>=0.27.0
gradio>=4.0.0
httpx>=0.27.0
structlog>=24.0.0
pytest>=7.0.0
pytest-asyncio>=0.21.0
numpy>=1.20.0
sentence-transformers>=2.2.0
```
