# LLM Evaluation Framework

A modular evaluation engine for comparing LLM providers on structured datasets. Supports CLI, REST API, and a Gradio web UI. Features single-model evaluation, pairwise model comparison, and full dataset management with versioning.

## Features

- **Multiple providers**: OpenAI, Anthropic, Gemini/Google, Cohere, and OpenAI-compatible endpoints (Groq, Hugging Face, etc.).
- **Mock mode**: Run full pipelines locally without API keys for development and testing.
- **Async evaluation**: Configurable concurrency via `asyncio` semaphore.
- **Three evaluators**: Exact match, semantic similarity (sentence-transformers), and LLM-as-a-judge with custom prompt support.
- **Pairwise evaluation**: Compare two models side-by-side with a judge model, including Elo ratings, win/loss/tie rates, and order randomization to eliminate bias.
- **Dataset management**: Full CRUD API with versioning, file upload, tagging, and search.
- **Structured logging**: `structlog`-based logging throughout all modules.
- **SQLite persistence**: Datasets (with versions), runs, pairwise runs, and per-example results stored in `evals.db`.
- **REST API**: FastAPI layer for triggering runs, checking status, managing datasets, and listing results.
- **Web UI**: Gradio interface with four tabs for interactive evaluation, pairwise comparison, and result browsing.
- **Pydantic v2**: Typed schemas for settings, examples, results, and API request/response models.

## Project Structure

```text
app/
  config.py              # .env-backed settings via pydantic-settings
  database/
    connection.py        # SQLAlchemy engine, sessions, init_db()
    models.py            # DatasetDB, DatasetVersionDB, EvaluationRunDB,
                         # EvaluationResultDB, PairwiseRunDB, PairwiseComparisonDB
  evaluators/
    base.py              # BaseEvaluator abstract class
    base_pairwise.py     # BasePairwiseEvaluator abstract class
    exact_match.py       # Case-insensitive exact string match
    llm_judge.py         # LLM-as-a-judge with JSON parsing
    pairwise_judge.py    # Pairwise LLM judge for model comparison
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
    pairwise_runner.py   # PairwiseEvaluationRunner, Elo ratings, pairwise metrics
  schemas/
    example.py           # EvaluationExample
    result.py            # EvaluationResult
  services/
    dataset_service.py   # DatasetService (CRUD, versioning, upload, search)
  api/
    main.py              # FastAPI app (runs, pairwise-runs, datasets endpoints)
    schemas.py           # Pydantic v2 request/response models
  ui/
    gradio_app.py        # Gradio Blocks UI with 4 tabs (talks to FastAPI via httpx)
datasets/
  sample.jsonl           # Sample 5-example evaluation dataset
tests/
  conftest.py            # Temporary SQLite database per test
  test_config.py
  test_dataset_api.py    # Dataset API endpoint tests
  test_dataset_service.py # Dataset service unit tests
  test_evaluators.py
  test_pairwise.py       # Pairwise evaluation tests
  test_providers.py
  test_runner.py
run_eval.py              # CLI entry point
requirements.txt
.env.example
README.md
PROJECT_DOCUMENTATION.md
feature-pairwise_model_evaluation.md
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

### Run Endpoints

#### `POST /runs`

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

#### `GET /runs/{run_id}`

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

#### `GET /runs`

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

---

### Pairwise Run Endpoints

#### `POST /pairwise-runs`

Trigger a new pairwise evaluation run comparing two models.

**Request body:**

```json
{
  "dataset_path": "datasets/sample.jsonl",
  "model_a_provider": "openai",
  "model_a_model": "gpt-4o",
  "model_a_api_key": "sk-...",
  "model_b_provider": "openai",
  "model_b_model": "gpt-4o-mini",
  "model_b_api_key": "sk-...",
  "judge_provider": "openai",
  "judge_model": "gpt-4o",
  "judge_api_key": "sk-...",
  "concurrency": 5
}
```

**Response (`200`):**

```json
{
  "run_id": "uuid-string",
  "status": "started"
}
```

#### `GET /pairwise-runs/{run_id}`

Get pairwise run status, metrics, and optional per-example comparisons.

**Query parameters:**
- `include_comparisons` (boolean, default `false`) — include detailed per-example results

**Response (`200`):**

```json
{
  "run_id": "uuid-string",
  "model_a_name": "gpt-4o",
  "model_b_name": "gpt-4o-mini",
  "status": "completed",
  "metrics": {
    "total_comparisons": 50,
    "wins_a": 35,
    "wins_b": 10,
    "ties": 5,
    "win_rate_a": 0.70,
    "win_rate_b": 0.20,
    "tie_rate": 0.10,
    "elo_a": 1584.2,
    "elo_b": 1415.8,
    "avg_score_a": 0.82,
    "avg_score_b": 0.61
  },
  "comparisons": null
}
```

#### `GET /pairwise-runs`

List all pairwise runs.

---

### Dataset Endpoints

#### `GET /datasets`

List all datasets with optional filtering.

**Query parameters:**
- `tag` (string, optional) — filter by tag
- `search` (string, optional) — search by name

**Response (`200`):**

```json
{
  "datasets": [
    {
      "id": "uuid-string",
      "name": "sample.jsonl",
      "description": "A sample dataset",
      "tags": ["geography", "sample"],
      "latest_version_number": 1,
      "created_at": "2025-01-01T00:00:00Z",
      "updated_at": "2025-01-01T00:00:00Z",
      "active_version": {
        "id": "uuid-string",
        "version_number": 1,
        "example_count": 5,
        "is_active": true,
        "created_at": "2025-01-01T00:00:00Z"
      }
    }
  ]
}
```

#### `GET /datasets/{dataset_id}`

Get full dataset details including version history.

**Response (`200`):** Same as above plus `versions` array with all versions.

#### `POST /datasets`

Create a new dataset from JSONL content.

**Request body:**

```json
{
  "name": "my-dataset",
  "description": "Test dataset",
  "tags": ["test"],
  "content": "{\"input\": \"What is 2+2?\", \"expected_output\": \"4\"}\n{\"input\": \"Capital of France?\", \"expected_output\": \"Paris\"}"
}
```

**Response (`201`):** Dataset response object.

#### `POST /datasets/upload`

Upload a JSONL file as a dataset.

**Request:** `multipart/form-data` with fields:
- `file` — JSONL file
- `name` — dataset name
- `description` (optional) — description
- `tags` (optional) — comma-separated tags

**Response (`201`):** Dataset response object.

#### `POST /datasets/{dataset_id}/versions`

Add a new version to an existing dataset.

**Request body:**

```json
{
  "content": "{\"input\": \"New question?\", \"expected_output\": \"New answer\"}"
}
```

**Response (`201`):** Version response object.

#### `PUT /datasets/{dataset_id}/active-version`

Set a specific version as active.

**Request body:**

```json
{
  "version_id": "uuid-string"
}
```

**Response (`200`):** Version response object.

#### `DELETE /datasets/{dataset_id}`

Delete a dataset and all its versions.

**Response (`200`):**

```json
{
  "message": "Dataset deleted successfully",
  "id": "uuid-string"
}
```

---

## Gradio Web UI

The Gradio interface provides four tabs:

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

### Tab 3 -- Pairwise Evaluation

Compare two models side-by-side on the same dataset.

Inputs:
- Dataset Path
- Model A: Provider, Model, API Key, Base URL (optional)
- Model B: Provider, Model, API Key, Base URL (optional)
- Judge: Provider, Model, API Key
- Concurrency (slider, 1-20)

### Tab 4 -- Pairwise Results

Enter a Pairwise Run ID and click **Fetch Results**. Displays:
- Run status with model names
- Metrics table: Win Rate A/B, Tie Rate, Elo A/B, Avg Score A/B, Total Comparisons
- Per-example comparisons table: Example ID, Winner, Score A, Score B, Reason

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
| Pairwise Judge | `app/evaluators/pairwise_judge.py` | 0.0 -- 1.0 | LLM compares two model responses, declares winner (A/B/tie) with scores |

All single-model evaluators return `(text, usage)` tuples. Token usage from the judge provider is recorded per result. The pairwise judge returns a `PairwiseComparisonResult` with winner, scores, and reason.

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

### Tables

| Table | Description |
|---|---|
| `datasets` | Dataset identity, description, tags, and version tracking |
| `dataset_versions` | Immutable content snapshots for each dataset |
| `evaluation_runs` | Single-model evaluation run metadata |
| `evaluation_results` | Per-example evaluator results with token usage |
| `pairwise_runs` | Pairwise comparison run metadata (two models) |
| `pairwise_comparisons` | Per-example pairwise comparison results with Elo tracking |

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

### Adding a Pairwise Evaluator

1. Create `app/evaluators/your_pairwise_evaluator.py` implementing `BasePairwiseEvaluator`.
2. Use it with `PairwiseEvaluationRunner`.

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
json_repair>=0.60.0
```
