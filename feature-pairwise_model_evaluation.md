# Feature: Pairwise Model Evaluation

## Branch: `feature/pairwise_model_evaluation`

> A complete guide for junior developers to understand what was built, why, and how every piece fits together.

---

## Table of Contents

1. [What Is This Feature?](#1-what-is-this-feature)
2. [Why Pairwise Evaluation?](#2-why-pairwise-evaluation)
3. [Architecture Overview](#3-architecture-overview)
4. [New Files — What They Do](#4-new-files--what-they-do)
5. [Modified Files — What Changed](#5-modified-files--what-changed)
6. [Database Models](#6-database-models)
7. [The Pairwise Judge Evaluator](#7-the-pairwise-judge-evaluator)
8. [The Pairwise Runner](#8-the-pairwise-runner)
9. [Elo Rating System](#9-elo-rating-system)
10. [API Endpoints](#10-api-endpoints)
11. [Gradio UI](#11-gradio-ui)
12. [How the Pieces Connect](#12-how-the-pieces-connect)
13. [Testing](#13-testing)
14. [Backward Compatibility](#14-backward-compatibility)

---

## 1. What Is This Feature?

This feature lets you **compare two language models side-by-side** on the same dataset. Instead of just scoring each model independently, a **judge model** reads both responses and picks a winner.

**Example:**
```
Question: "What is the capital of France?"

Model A responds: "Paris"
Model B responds: "The capital of France is Paris."

Judge says: Model B wins (more complete answer)
Score A: 7/10    Score B: 9/10
```

After running through an entire dataset, you get:
- **Win/Loss/Tie rates** — how often each model wins
- **Elo rating** — a single number representing each model's strength
- **Average scores** — the judge's scores for each model

---

## 2. Why Pairwise Evaluation?

Single-model evaluation tells you "Model A scored 7.3." But that doesn't tell you if Model B would score better or worse **on the same questions**.

Pairwise evaluation solves this by:
1. Asking both models the **same question**
2. Having a judge **compare both answers directly**
3. Declaring a **winner** for each question

This is how companies like OpenAI, Anthropic, and Google actually evaluate their models in benchmarks (like Chatbot Arena).

---

## 3. Architecture Overview

Here's how all the pieces connect, from top to bottom:

```
┌─────────────────────────────────────────────────────┐
│                    GRADIO UI                         │
│  "Pairwise Evaluation" tab  →  "Pairwise Results" tab│
└──────────────────────┬──────────────────────────────┘
                       │ HTTP POST /pairwise-runs
                       ▼
┌─────────────────────────────────────────────────────┐
│                  FASTAPI ENDPOINTS                    │
│  POST /pairwise-runs    → starts background task     │
│  GET  /pairwise-runs/{id} → returns metrics          │
│  GET  /pairwise-runs    → lists all runs             │
└──────────────────────┬──────────────────────────────┘
                       │ creates runner, runs in background
                       ▼
┌─────────────────────────────────────────────────────┐
│            PAIRWISE EVALUATION RUNNER                 │
│  PairwiseEvaluationRunner                            │
│  ┌────────────────────────────────────────────────┐  │
│  │ For each example in dataset:                   │  │
│  │  1. Generate response from Model A             │  │
│  │  2. Generate response from Model B             │  │
│  │  3. Randomly swap A/B order (bias prevention)  │  │
│  │  4. Ask judge: "Which is better?"              │  │
│  │  5. Un-swap winner if order was swapped        │  │
│  │  6. Store comparison in database               │  │
│  └────────────────────────────────────────────────┘  │
│  Then: compute win rates, Elo ratings, avg scores    │
└──────────────────────┬──────────────────────────────┘
                       │ uses
          ┌────────────┴────────────┐
          ▼                         ▼
┌──────────────────┐    ┌──────────────────────────┐
│  TWO PROVIDERS   │    │  PAIRWISE JUDGE EVALUATOR │
│  provider_a      │    │  PairwiseJudgeEvaluator   │
│  provider_b      │    │  ┌──────────────────────┐ │
│  (OpenAI, etc.)  │    │  │ Sends prompt to LLM: │ │
│                  │    │  │ "Here are two answers.│ │
│                  │    │  │  Which is better?"    │ │
│                  │    │  │ Returns: winner,      │ │
│                  │    │  │   score_a, score_b,   │ │
│                  │    │  │   reason              │ │
│                  │    │  └──────────────────────┘ │
└──────────────────┘    └──────────────────────────┘
          │                         │
          ▼                         ▼
┌─────────────────────────────────────────────────────┐
│                 DATABASE (SQLite)                     │
│  pairwise_runs          pairwise_comparisons         │
│  ┌────────────────┐    ┌──────────────────────────┐ │
│  │ id             │    │ run_id (FK)              │ │
│  │ dataset_id     │    │ example_id               │ │
│  │ model_a_name   │    │ prompt                   │ │
│  │ model_b_name   │    │ response_a               │ │
│  │ created_at     │    │ response_b               │ │
│  └────────────────┘    │ winner: "A"/"B"/"tie"    │ │
│                        │ score_a, score_b          │ │
│                        │ judge_reason              │ │
│                        │ original_order            │ │
│                        └──────────────────────────┘ │
└─────────────────────────────────────────────────────┘
```

---

## 4. New Files — What They Do

### `app/evaluators/base_pairwise.py`
**Purpose:** Defines the abstract blueprint for pairwise evaluators.

**Key classes:**
- `BasePairwiseEvaluator` — Abstract class (you can't instantiate it directly). Any pairwise evaluator must implement `evaluate(input, expected_output, response_a, response_b)` and return a `PairwiseComparisonResult`.
- `PairwiseComparisonResult` — A Pydantic model (a typed dictionary) that holds the judge's output: `winner` ("A", "B", or "tie"), `score_a`, `score_b`, `reason`, and optional `metadata`.

**Why an abstract base?** This lets you swap in different judges later (e.g., a rule-based judge, a different LLM judge) without changing the runner. You just implement `BasePairwiseEvaluator` and plug it in.

### `app/evaluators/pairwise_judge.py`
**Purpose:** The actual LLM-powered pairwise judge.

**Key classes:**
- `PairwiseJudgeResponse` — Pydantic model that validates the LLM's JSON output. It expects `winner`, `score_a` (1-10), `score_b` (1-10), and `reason`.
- `PairwiseJudgeEvaluator` — Implements `BasePairwiseEvaluator`. It:
  1. Builds a prompt with both responses
  2. Sends it to the LLM
  3. Parses the JSON response (using `json_repair` for malformed output)
  4. Normalizes scores from 1-10 to 0.0-1.0
  5. Returns a `PairwiseComparisonResult`

**The prompt template** asks the LLM to:
- Score each response 1-10
- Pick a winner (A, B, or tie if scores are within 1 point)
- Explain its reasoning

### `app/runners/pairwise_runner.py`
**Purpose:** Orchestrates the entire pairwise evaluation.

**Key class:** `PairwiseEvaluationRunner`

**What it does:**
1. Takes two providers (model A and model B) and a pairwise evaluator
2. For each example in the dataset:
   - Generates responses from both models **concurrently** (using `asyncio.gather`)
   - **Randomly swaps** which model's response is labeled "A" vs "B" (eliminates order bias)
   - Asks the judge to compare them
   - **Un-swaps** the winner if the order was swapped
   - Stores the comparison in the database
3. After all examples, computes aggregate metrics

**Key functions:**
- `_expected_score(rating_a, rating_b)` — Elo math helper
- `_elo_update(rating_a, rating_b, score_a)` — Updates Elo after a comparison
- `get_pairwise_run_metrics(run_id)` — Computes all metrics from DB
- `get_pairwise_comparisons(run_id)` — Returns all individual comparisons

### `tests/test_pairwise.py`
**Purpose:** 21 tests covering every part of the pairwise system.

**Test groups:**
- `TestEloRating` — 8 tests verifying Elo math correctness
- `TestPairwiseJudgeEvaluator` — 3 tests for the LLM judge
- `TestPairwiseEvaluationRunner` — 6 tests for the full pipeline
- `TestPairwiseAPI` — 4 tests for API endpoints

---

## 5. Modified Files — What Changed

### `app/database/models.py`
**What was added:** Two new ORM models at the bottom of the file.

**`PairwiseRunDB`** — One row per pairwise evaluation run. Stores:
- `id` (UUID primary key)
- `dataset_id` (which dataset was used)
- `dataset_version_id` (which version, nullable)
- `model_a_name` (e.g., "gpt-4o")
- `model_b_name` (e.g., "claude-3-sonnet")
- `created_at`

**`PairwiseComparisonDB`** — One row per example comparison. Stores:
- `run_id` (links to PairwiseRunDB)
- `example_id`, `prompt`, `response_a`, `response_b`, `expected_output`
- `winner` ("A", "B", or "tie")
- `score_a`, `score_b` (normalized 0.0-1.0)
- `judge_reason` (the judge's explanation)
- `original_order` ("AB" or "BA" — tracks if positions were swapped)
- `metadata_json` (extra info like latency, raw scores, errors)

**Why separate tables?** Pairwise runs are fundamentally different from single-model runs. They have two models, comparisons, and different metrics. Mixing them would make the code confusing.

### `app/database/__init__.py`
**What changed:** Added `PairwiseRunDB` and `PairwiseComparisonDB` to the imports so other files can import them easily.

### `app/api/schemas.py`
**What was added:** 7 new Pydantic v2 schemas at the bottom.

| Schema | Purpose |
|--------|---------|
| `PairwiseRunRequest` | Request body for `POST /pairwise-runs` — includes config for both models and the judge |
| `PairwiseRunResponse` | Response when starting a run — returns `run_id` and `status` |
| `PairwiseMetrics` | Aggregated metrics (win rates, Elo, avg scores) |
| `PairwiseComparisonItem` | A single comparison result (for detailed responses) |
| `PairwiseRunStatusResponse` | Full status with metrics and optional comparisons |
| `PairwiseRunListItem` | Summary for listing runs |
| `PairwiseRunsListResponse` | Response for `GET /pairwise-runs` |

### `app/api/main.py`
**What changed:**

1. **New imports** — Added pairwise schemas, evaluator, runner, and metrics functions.

2. **New in-memory store** — `_pairwise_run_store` (same pattern as `_run_store` for single-model runs).

3. **3 new endpoints:**

   **`POST /pairwise-runs`** — Starts a pairwise evaluation:
   - Validates the dataset path
   - Creates providers for Model A, Model B, and the Judge
   - Creates a `PairwiseJudgeEvaluator`
   - Creates a `PairwiseEvaluationRunner`
   - Generates a placeholder run_id
   - Launches a background task via `asyncio.create_task`
   - Returns the run_id immediately

   **`GET /pairwise-runs/{run_id}`** — Gets run status and metrics:
   - Checks in-memory store first (for in-progress runs)
   - Falls back to database (for completed/CLI runs)
   - Optional `?include_comparisons=true` query param for detailed results

   **`GET /pairwise-runs`** — Lists all pairwise runs:
   - Merges in-memory store with database records

### `app/ui/gradio_app.py`
**What changed:** Added two new Gradio tabs:

**"Pairwise Evaluation" tab:**
- Model A configuration: provider dropdown, model textbox, API key, base URL
- Model B configuration: same fields
- Judge configuration: provider, model, API key
- Dataset path and concurrency
- Submit button → calls `POST /pairwise-runs`

**"Pairwise Results" tab:**
- Run ID input
- Status display
- Metrics table (Win Rate A/B, Tie Rate, Elo, Avg Scores)
- Comparisons table (per-example results)

---

## 6. Database Models

### `pairwise_runs` table

```
┌──────────────────────────────────────────────────┐
│ pairwise_runs                                     │
├──────────────────────────────────────────────────┤
│ id (PK)            │ VARCHAR(36)  │ UUID         │
│ dataset_id (FK)    │ VARCHAR(36)  │ → datasets   │
│ dataset_version_id │ VARCHAR(36)  │ nullable     │
│ model_a_name       │ VARCHAR(255) │ e.g. "gpt-4o"│
│ model_b_name       │ VARCHAR(255) │ e.g. "claude"│
│ created_at         │ DATETIME     │ UTC          │
└──────────────────────────────────────────────────┘
```

### `pairwise_comparisons` table

```
┌──────────────────────────────────────────────────┐
│ pairwise_comparisons                              │
├──────────────────────────────────────────────────┤
│ id (PK)            │ INTEGER    │ autoincrement  │
│ run_id (FK)        │ VARCHAR(36)│ → pairwise_runs│
│ example_id         │ VARCHAR(255)                │
│ prompt             │ TEXT       │ the question    │
│ response_a         │ TEXT       │ model A's answer│
│ response_b         │ TEXT       │ model B's answer│
│ expected_output    │ TEXT       │ ground truth    │
│ winner             │ VARCHAR(10)│ "A"/"B"/"tie"  │
│ score_a            │ FLOAT      │ 0.0-1.0        │
│ score_b            │ FLOAT      │ 0.0-1.0        │
│ judge_reason       │ TEXT       │ explanation     │
│ original_order     │ VARCHAR(10)│ "AB" or "BA"   │
│ metadata_json      │ TEXT       │ JSON blob       │
└──────────────────────────────────────────────────┘
```

---

## 7. The Pairwise Judge Evaluator

### How the prompt works

The judge receives a prompt like this:

```
You are an impartial AI judge evaluating two model responses side by side.

Input:
What is the capital of France?

Expected Output (Ground Truth):
Paris

Response A:
Paris

Response B:
The capital of France is Paris.

Compare both responses against the expected output. Consider correctness,
completeness, and quality.

Score each response from 1 to 10 (1 = completely wrong, 10 = perfectly correct).
Declare a winner: "A" if A is better, "B" if B is better, or "tie" if scores
are within 1 point of each other.

You MUST reply ONLY with a JSON object:
{"winner": "A", "score_a": 7, "score_b": 9, "reason": "Both are correct..."}
```

### How JSON parsing works

LLMs often return malformed JSON. The parser uses a 3-step strategy:

1. **Look for a markdown code block** — LLMs sometimes wrap JSON in ` ```json ... ``` `
2. **Look between `{` and `}`** — find the first `{` and last `}` in the text
3. **Fallback to direct repair** — use `json_repair` library to fix the raw text

If all steps fail, the judge returns a **tie** with a score of 0.0 and an error in metadata (graceful degradation — one bad response doesn't crash the whole evaluation).

---

## 8. The Pairwise Runner

### The execution flow

```python
# Simplified version of what the runner does:

async def run_pairwise_evaluation(dataset_path, examples):
    run_id = create_run_in_database()

    for example in examples:  # (actually concurrent with semaphore)
        # Step 1: Generate from both models simultaneously
        response_a, response_b = await asyncio.gather(
            provider_a.generate(example.input),
            provider_b.generate(example.input),
        )

        # Step 2: Randomly swap order to prevent bias
        if random.random() < 0.5:
            judge_a, judge_b = response_a, response_b
            original_order = "AB"
        else:
            judge_a, judge_b = response_b, response_a
            original_order = "BA"

        # Step 3: Ask the judge
        result = await judge.evaluate(
            example.input, example.expected_output, judge_a, judge_b
        )

        # Step 4: Un-swap the winner if needed
        if original_order == "BA":
            result.winner = swap_winner(result.winner)

        # Step 5: Save to database
        save_comparison(run_id, example, response_a, response_b, result)

    # Step 6: Compute aggregate metrics
    metrics = compute_metrics(run_id)
    return run_id
```

### Why randomize the order?

LLMs (and humans) tend to favor the first option presented. By randomly swapping which model's response is "A" vs "B", we ensure that each model gets an equal chance of being in the preferred first position. The `original_order` field lets us un-swap the winner back to the correct model.

---

## 9. Elo Rating System

### What is Elo?

Elo is a rating system originally designed for chess. It works like this:

- Both models start at **1500** (the default rating)
- After each comparison, ratings are updated based on the outcome
- **Win** = gain points (more if you're the underdog)
- **Loss** = lose points
- **Tie** = small adjustment toward expected outcome

### The math

**Expected score** (probability of winning):
```
E(A) = 1 / (1 + 10^((Rb - Ra) / 400))
```

**Rating update:**
```
New Ra = Ra + K × (Actual Score - Expected Score)
```

Where K = 32 (the "K-factor" — how much ratings change per game).

### Example

```
Start:  Model A = 1500,  Model B = 1500

Comparison 1: A wins
  Expected: 0.5 each
  New A: 1500 + 32 × (1.0 - 0.5) = 1516
  New B: 1500 + 32 × (0.0 - 0.5) = 1484

Comparison 2: A wins again
  Expected: ~0.56 for A (slightly favored now)
  New A: 1516 + 32 × (1.0 - 0.56) = 1530
  New B: 1484 + 32 × (0.0 - 0.44) = 1470

Final: Model A = 1530 (better),  Model B = 1470 (worse)
```

### Why Elo?

- **Single number** — easy to compare models at a glance
- **Self-correcting** — upsets move ratings more than expected results
- **Industry standard** — used by Chatbot Arena, LMSYS, and major benchmarks

---

## 10. API Endpoints

### `POST /pairwise-runs`

**Request:**
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

**Response:**
```json
{
  "run_id": "a1b2c3d4-...",
  "status": "started"
}
```

The evaluation runs in the background. Use `GET /pairwise-runs/{run_id}` to check status.

### `GET /pairwise-runs/{run_id}`

**Response (completed):**
```json
{
  "run_id": "a1b2c3d4-...",
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
  }
}
```

Add `?include_comparisons=true` to also get per-example results.

### `GET /pairwise-runs`

Lists all pairwise runs (from both in-memory store and database).

---

## 11. Gradio UI

### "Pairwise Evaluation" Tab

```
┌──────────────────────┬──────────────────────┐
│      Model A         │      Model B         │
├──────────────────────┼──────────────────────┤
│ Provider: [openai]   │ Provider: [openai]   │
│ Model: [gpt-4o]      │ Model: [gpt-4o-mini] │
│ API Key: [***]       │ API Key: [***]       │
│ Base URL: []         │ Base URL: []         │
├──────────────────────┴──────────────────────┤
│ Judge                                       │
│ Provider: [openai]   Model: [gpt-4o]        │
│ API Key: [***]                              │
├─────────────────────────────────────────────┤
│ Dataset Path: [datasets/sample.jsonl]       │
│ Concurrency: [=====5=====]                  │
│                                             │
│ [Submit Pairwise Run]                       │
└─────────────────────────────────────────────┘
```

### "Pairwise Results" Tab

```
┌─────────────────────────────────────────────┐
│ Pairwise Run ID: [________________]         │
│ [Fetch Results]                             │
├─────────────────────────────────────────────┤
│ Status: completed (gpt-4o vs gpt-4o-mini)   │
├─────────────────────────────────────────────┤
│ Pairwise Metrics       │ Individual Comparisons │
├────────────────────────┼───────────────────────┤
│ Win Rate A    70.0%    │ ID   Winner Score A B │
│ Win Rate B    20.0%    │ q1   A     0.8  0.6  │
│ Tie Rate      10.0%    │ q2   B     0.5  0.9  │
│ Elo A         1584.2   │ q3   tie   0.7  0.7  │
│ Elo B         1415.8   │ ...                   │
│ Avg Score A   0.820    │                       │
│ Avg Score B   0.610    │                       │
└────────────────────────┴───────────────────────┘
```

---

## 12. How the Pieces Connect

Here's the complete request flow when a user clicks "Submit Pairwise Run":

```
1. User fills form in Gradio, clicks Submit
         │
         ▼
2. Gradio calls submit_pairwise_run()
   → sends POST /pairwise-runs with JSON payload
         │
         ▼
3. FastAPI create_pairwise_run() receives request
   → validates dataset_path (loads JSONL file)
   → creates Model A provider via ProviderFactory
   → creates Model B provider via ProviderFactory
   → creates Judge provider via ProviderFactory
   → creates PairwiseJudgeEvaluator(judge_provider)
   → creates PairwiseEvaluationRunner(provider_a, provider_b, evaluator)
   → generates placeholder run_id
   → stores in _pairwise_run_store
   → launches asyncio.create_task(_background_pairwise_run)
   → returns {"run_id": "...", "status": "started"}
         │
         ▼
4. Background task runs _background_pairwise_run()
   → calls runner.run_pairwise_evaluation(dataset_path, examples)
         │
         ▼
5. PairwiseEvaluationRunner.run_pairwise_evaluation()
   → creates PairwiseRunDB record in database
   → for each example (concurrently):
       → provider_a.generate(input) → response_a
       → provider_b.generate(input) → response_b
       → randomly swap order
       → judge.evaluate(input, expected, judge_a, judge_b)
       → un-swap winner
       → save PairwiseComparisonDB record
   → compute metrics
   → update _pairwise_run_store
         │
         ▼
6. User polls GET /pairwise-runs/{run_id}
   → checks _pairwise_run_store → status "completed"
   → calls get_pairwise_run_metrics(run_id)
   → returns full metrics response
         │
         ▼
7. Gradio displays metrics table and comparisons
```

---

## 13. Testing

Run all tests:
```bash
python -m pytest tests/ -v
```

**94 tests total:**
- `test_config.py` — 1 test
- `test_dataset_api.py` — 24 tests
- `test_dataset_service.py` — 38 tests
- `test_evaluators.py` — 4 tests
- `test_pairwise.py` — 21 tests (new)
- `test_providers.py` — 4 tests
- `test_runner.py` — 2 tests

**Test categories in `test_pairwise.py`:**

| Category | Tests | What's tested |
|----------|-------|---------------|
| `TestEloRating` | 8 | Elo math: expected scores, updates, conservation, upsets |
| `TestPairwiseJudgeEvaluator` | 3 | LLM judge: basic flow, graceful failure, name property |
| `TestPairwiseEvaluationRunner` | 6 | Full pipeline: run, metrics, comparisons, order randomization, empty states |
| `TestPairwiseAPI` | 4 | API: list empty, not found, invalid dataset, invalid provider |

---

## 14. Backward Compatibility

**Nothing was broken.** All changes are additive:

- Existing `POST /runs`, `GET /runs`, `GET /runs/{id}` — **untouched**
- Existing `EvaluationRunner`, `EvaluatorRegistry` — **untouched**
- Existing `EvaluationRunDB`, `EvaluationResultDB` — **untouched**
- CLI `run_eval.py` — **untouched**
- Gradio "Run Evaluation" and "View Results" tabs — **untouched**
- All original tests — **still pass**

The pairwise system is a completely parallel path that shares only the database connection, provider factory, and dataset loading utilities.

---

## Quick Reference: Key Terms

| Term | Meaning |
|------|---------|
| **Pairwise** | Comparing two things side-by-side |
| **Elo Rating** | A number (starting at 1500) that represents a model's strength relative to others |
| **Win Rate** | Percentage of comparisons a model won |
| **Tie Rate** | Percentage of comparisons where neither model was clearly better |
| **Order Randomization** | Randomly swapping which response is labeled "A" vs "B" to prevent judge bias |
| **Original Order** | Tracks whether the positions were swapped ("AB" = normal, "BA" = swapped) |
| **Graceful Degradation** | When the judge fails, return a tie with error info instead of crashing |
| **json_repair** | Library that fixes malformed JSON (common in LLM outputs) |
