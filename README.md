# LLM Evaluation Framework

A modular evaluation engine for comparing LLM providers on structured datasets. Supports CLI, REST API, and a Gradio web UI. Features single-model evaluation, pairwise model comparison, and full dataset management with versioning.

## Features

- **Multiple providers**: OpenAI, Anthropic, Gemini/Google, Cohere, and OpenAI-compatible endpoints (Groq, Hugging Face, etc.).
- **Mock mode**: Run full pipelines locally without API keys for development and testing.
- **Async evaluation**: Configurable concurrency via `asyncio` semaphore.
- **Three evaluators**: Exact match, semantic similarity (sentence-transformers), and LLM-as-a-judge with custom prompt support.
- **Pairwise evaluation**: Compare two models side-by-side with a judge model, including Elo ratings, win/loss/tie rates, and order randomization to eliminate bias.
- **Agency projects**: Group datasets and both evaluation run types by client and product, with descriptions and tags.
- **Dataset management**: Full CRUD API with versioning, file upload, tagging, project assignment, and search.
- **Structured logging**: `structlog`-based logging throughout all modules.
- **SQLite persistence**: Datasets (with versions), runs, pairwise runs, and per-example results stored in `evals.db`.
- **REST API**: FastAPI layer for triggering runs, checking status, managing datasets, and listing results.
- **Client-ready exports**: Download transparent CSV, JSON, or print-ready HTML summaries with configuration, coverage, quality, and failures.
- **Release checks**: Mark completed baselines and label compatible completed runs as passed, regressed, or inconclusive.
- **Agency demo**: Seed mock-only client data and demonstrate upload, run, review, export, and release checks without credentials.
- **Web UI**: Gradio interface with nine tabs for project setup, evaluation, detailed result review, client reports, release checks, pairwise comparison, and result browsing.
- **Pydantic v2**: Typed schemas for settings, examples, results, and API request/response models.

## Project Structure

```text
app/
  config.py              # .env-backed settings via pydantic-settings
  database/
    connection.py        # SQLAlchemy engine, sessions, init_db()
    models.py            # DatasetDB, DatasetVersionDB, EvaluationRunDB,
                         # EvaluationResultDB, PairwiseRunDB, PairwiseComparisonDB, ProjectDB
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
    project_service.py   # ProjectService (client product workspaces)
  api/
    main.py              # FastAPI app (projects, runs, pairwise-runs, datasets endpoints)
    schemas.py           # Pydantic v2 request/response models
  ui/
    gradio_app.py        # Gradio Blocks UI with 9 tabs (talks to FastAPI via httpx)
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
DEMO_GUIDE.md
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

Edit `.env` and fill in API keys for the providers you intend to use. Missing credentials stop the request with a configuration error. Use the explicit `mock` provider or `mock` model for demo mode; simulated runs are marked in the API and UI.

### Agency workspaces and provider connections

Before serving multiple agency users, generate and set `WORKSPACE_ENCRYPTION_KEY`, then bootstrap the first owner once:

```bash
curl -X POST http://localhost:8000/auth/bootstrap -H "Content-Type: application/json" -d "{\"email\":\"owner@example.com\",\"display_name\":\"Owner\",\"workspace_name\":\"Acme Agency\"}"
```

Store the returned `api_token` in a secret manager. API requests then use `Authorization: Bearer <token>`; set `WORKSPACE_API_TOKEN` for the local Gradio process and restart it. Owners can add `owner`, `editor`, `viewer`, or `client_viewer` memberships through `POST /workspace/members`. Projects, their datasets, runs, results, exports, and release checks are workspace-scoped.

Owners save a provider once using `POST /provider-connections` or the **Provider Connections** Gradio tab. The API encrypts an API key with Fernet, never returns it, and stores only a connection ID in the run configuration and reports. Workspace runs select connection IDs instead of submitting raw keys. `credential_reference` is supported as a stored reference; resolving one requires an external secret resolver in the deployment.

### Agency delivery workflow

Use **Evaluation Templates** to save the candidate and judge connections, rubric, concurrency, timeout, release rules, and report preferences. Launch a template against any dataset version with `POST /evaluation-templates/{template_id}/launch`.

Use `POST /report-shares` to create an expiring link for exactly one run or project. The returned URL is the only public capability, returns a read-only branded HTML report, and becomes unavailable after `DELETE /report-shares/{share_id}`. Shared reports do not grant API access or expose provider credentials.

`GET /projects/{project_id}/dashboard` returns the latest run, baseline/release state, coverage and quality trends, and sanitized recent failures. The Gradio **Project Dashboard** tab presents the same data.

### Hosted pilot and operational controls

Run the hosted pilot with Docker Compose and PostgreSQL:

```bash
copy .env.production.example .env
docker compose up --build
```

Complete the **Setup Wizard** at `http://localhost:7860`, save the one-time owner token as `WORKSPACE_API_TOKEN`, then recreate the UI container. Follow [HOSTED_PILOT_ONBOARDING.md](HOSTED_PILOT_ONBOARDING.md) for the mock-demo, report-sharing, backup, and restoration checklist.

`GET /health` verifies safe production configuration and database reachability. Workspace owners can review `GET /audit-events`, set retention with `PUT /operations/retention`, and explicitly run retention cleanup through `POST /operations/retention/apply`. Audit events capture the actor and safe metadata for project, dataset, run, baseline, template, export, report-share, and retention changes. Retention cleanup removes expired share links and audit records older than the configured window; it does not delete evaluation data.

### Individual sign-in

Bootstrap can now include a 12-character-or-longer password. Users then create their own expiring 12-hour bearer session with `POST /auth/sign-in`, end it with `POST /auth/sign-out`, and change their password through `PUT /auth/password`. A user who belongs to more than one workspace selects one with `X-Workspace-ID` at sign-in and on authenticated requests. Existing bootstrap tokens remain usable only to set an initial password during migration; new integrations should use individual sessions.

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
  "dataset_id": "uuid-string",
  "dataset_version_id": "uuid-string",
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

Use a registered `dataset_id` and immutable `dataset_version_id` for application submissions. `dataset_path` remains available only for legacy API and CLI automation; do not send both sources.

**Response (`200`):**

```json
{
  "run_id": "uuid-string",
  "status": "queued",
  "is_simulated": false
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
    {
      "evaluator": "exact_match",
      "total_cases": 100,
      "valid_evaluations": 80,
      "generation_errors": 12,
      "evaluation_errors": 8,
      "error_count": 20,
      "passing_evaluations": 60,
      "evaluation_coverage": 0.8,
      "mean_score": 0.75,
      "pass_rate": 0.75
    }
  ],
  "error": null
}
```

#### `GET /runs/{run_id}/results`

Review persisted per-example evaluator results. Filter with `evaluator`, `score_min`, `score_max`, and one or more `outcome` values (`evaluated`, `generation_error`, or `evaluation_error`). Each item includes the prompt, prediction, expected answer, judge explanation when available, and a sanitized error message.

#### `GET /runs/{run_id}/export`

Download a transparent report with `format=json`, `format=csv`, or `format=html`. JSON contains the full persisted results, CSV contains one row per evaluator result, and HTML is a client-ready summary with the run configuration, coverage and quality metrics, failure counts, and example-level failures.

#### `PUT /runs/{run_id}/baseline`

Mark a completed run as a release baseline. Baselines remain queryable with `GET /runs?baseline_only=true`.

#### `GET /runs/{run_id}/comparison`

Compare a completed run with a marked baseline from the same project. Query parameters include `baseline_run_id`, `coverage_minimum` (default `0.95`), and `exact_match_pass_rate_max_drop` (default `0.05`). The response labels the result `passed`, `regressed`, or `inconclusive`, and returns per-evaluator score, pass-rate, coverage, and error-count deltas.

#### `POST /demo/seed`

Idempotently creates the mock-only `Northstar Demo Client / Customer Support Copilot` project and two small sample datasets. Follow [DEMO_GUIDE.md](DEMO_GUIDE.md) for the full no-credentials walkthrough.

#### `GET /runs`

List all tracked runs.

**Response (`200`):**

```json
{
  "runs": [
    {"run_id": "uuid-1", "status": "completed"},
    {"run_id": "uuid-2", "status": "queued"}
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
  "dataset_id": "uuid-string",
  "dataset_version_id": "uuid-string",
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
  "status": "queued",
  "is_simulated": false
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
    "valid_comparisons": 45,
    "generation_errors": 3,
    "evaluation_errors": 2,
    "error_count": 5,
    "evaluation_coverage": 0.9,
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

Quality metrics are `null` when no valid evaluations complete. The UI displays **“No valid evaluations.”** in that case.

#### `GET /pairwise-runs`

List all pairwise runs.

---

### Project Endpoints

Projects create an agency workspace for one client product. A project has a name, client name, optional description, and tags. Datasets assigned with `project_id` and runs started from those datasets keep the same project ID. Deleting a project preserves its historical datasets and runs by unassigning them.

- `GET /projects` — list projects
- `POST /projects` — create a project
- `GET /projects/{project_id}` — retrieve a project
- `PUT /projects/{project_id}` — update a project
- `DELETE /projects/{project_id}` — unassign related records and delete the project

### Dataset Endpoints

#### `GET /datasets`

List all datasets with optional filtering.

**Query parameters:**
- `tag` (string, optional) — filter by tag
- `search` (string, optional) — search by name
- `project_id` (string, optional) — filter by client project

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
  "project_id": "uuid-string",
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
- `project_id` (optional) — client project that owns the dataset

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

The Gradio interface provides nine tabs:

### Tab 1 -- Projects

Create a project for each client product, with a client name, description, and tags. Project choices appear when uploading a dataset.

### Tab 2 -- Datasets

Upload a UTF-8 `.jsonl` file to create a dataset and active version. Assign it to a client project, then select an existing dataset to upload a new immutable version or set a prior version active. JSONL is validated by the API before it is stored.

### Tab 3 -- Run Evaluation

Fill in all fields and click **Submit Run**. The UI sends a `POST` to the FastAPI server and displays the returned `run_id` and status.

Inputs:
- Dataset and immutable Dataset Version selectors
- Candidate Provider (dropdown: openai, anthropic, cohere, gemini, mock)
- Candidate Model
- Candidate API Key (password field)
- Evaluator Provider (dropdown)
- Evaluator Model
- Evaluator API Key (password field)
- Concurrency (slider, 1-20)
- Judge Prompt Template (optional, multiline)

### Tab 4 -- View Results

Enter a Run ID and click **Fetch Results**. The UI queries the API and displays:
- Lifecycle status, including `queued`, `running`, `completed`, `failed`, and `interrupted`
- A visible `SIMULATED` label when any response came from a demo provider
- Any sanitized run error
- Metrics with valid-result counts, coverage, generation errors, and evaluation errors

### Tab 5 -- Review Results

Load a single-model run and filter by evaluator, score range, or result outcomes. Select a filtered item to inspect its prompt, output, expected answer, judge explanation, and sanitized failure message.

### Tab 6 -- Client Reports

Download a CSV, JSON, or print-ready HTML evaluation report to share with the client. Reports show stored configuration, coverage and quality metrics, failure counts, and example-level failures with sanitized errors.

### Tab 7 -- Release Checks

Mark a completed run as a baseline and compare a later completed run against it. The page clearly labels the configured coverage and exact-match pass-rate rules as **PASSED**, **REGRESSED**, or **INCONCLUSIVE**.

### Tab 8 -- Pairwise Evaluation

Compare two models side-by-side on the same dataset.

Inputs:
- Dataset and immutable Dataset Version selectors
- Model A: Provider, Model, API Key, Base URL (optional)
- Model B: Provider, Model, API Key, Base URL (optional)
- Judge: Provider, Model, API Key
- Concurrency (slider, 1-20)

### Tab 9 -- Pairwise Results

Enter a Pairwise Run ID and click **Fetch Results**. Displays:
- Run status with model names, simulation label, and any sanitized error
- Metrics table with valid comparisons, coverage, generation/evaluation errors, Win Rate A/B, Tie Rate, Elo A/B, and Avg Score A/B
- Per-example comparisons table: Example ID, Winner, Score A, Score B, Reason

---

## Execution and Restart Recovery

The Week 1 service runs one evaluation worker per application process. Submitted runs remain `queued` until that worker begins them, then move to `running`. Completed results are committed in batches of ten, so an interruption retains earlier batches.

Provider generation and judge calls have a 60-second timeout. A timed-out generation is saved as a `generation_error`; a timed-out judge is saved as an `evaluation_error`.

On application startup, records left `queued` or `running` by the stopped worker are marked `interrupted` with a restart message. This recovery behavior is designed for the current single-worker deployment. A multi-worker deployment must add worker ownership and heartbeats before enabling it.

---

## Reproducible Run Configuration

Every new run stores a credential-safe configuration snapshot before execution. It records the dataset registry version or legacy-file SHA-256 fingerprint, actual candidate and judge models, evaluator and prompt settings, execution concurrency, timeout, batch size, and simulation state. `GET /runs/{run_id}` and `GET /pairwise-runs/{run_id}` return this snapshot as `run_configuration`; the Gradio result pages show it under **Execution Configuration**.

Snapshots never retain API keys. Historical records created before this feature return `configuration_verified: false`; new records return `true`.

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
| `DEFAULT_CANDIDATE_PROVIDER` | Default candidate provider | `mock` |
| `DEFAULT_CANDIDATE_MODEL` | Default candidate model | `mock` |
| `DEFAULT_EVALUATOR_PROVIDER` | Default evaluator provider | `mock` |
| `DEFAULT_EVALUATOR_MODEL` | Default evaluator model | `mock` |
| `DEFAULT_CONCURRENCY` | Parallel evaluation limit | `3` |
| `SIMILARITY_MODEL_NAME` | SentenceTransformer model | `all-MiniLM-L6-v2` |

For a local OpenAI-compatible endpoint that deliberately has no authentication, use provider `compatible`, provide its base URL, and set `candidate_allow_unauthenticated` to `true` in the API request (or `--candidate-allow-unauthenticated` in the CLI). This opt-in does not apply to hosted providers.

---

## Database

Results are stored in `evals.db` by default. Override `DATABASE_URL` in `.env` to use a different file or another SQLAlchemy-supported backend.

Schema migrations run automatically when the API starts. To upgrade a database explicitly, run:

```bash
alembic upgrade head
```

When upgrading a database created before run outcomes were introduced, the migration preserves every row and labels historical result and pairwise-comparison outcomes as `unverified`. This avoids treating old zero scores or ties as confirmed evaluations.

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
