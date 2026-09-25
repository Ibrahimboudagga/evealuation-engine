# LLM Evaluation Engine: Deep Project Guide

> Repository state covered: feature/queue-retry-foundation, September 2026.
>
> This guide explains the product, its technical design, the full evaluation
> workflow, its data model, operational controls, and the gaps still to close
> before it can operate as a fully managed agency SaaS.

## 1. Product definition

LLM Evaluation Engine is an evidence system for AI agencies. It lets an agency
store a client-specific evaluation suite, run models against it, distinguish
quality failures from technical failures, compare a new run with a baseline,
and deliver a clear report to a client.

The product is not only a score calculator. Its job is to make a model change
reviewable. A reliable report must answer all of these questions:

1. Which client, project, dataset version, model, judge, prompt, and settings
   produced this result?
2. How much of the planned evaluation completed?
3. Which examples passed, failed, or could not be evaluated?
4. Was a bad outcome caused by the candidate model, the evaluator, or provider
   infrastructure?
5. Did the new run improve, regress, or remain inconclusive against a baseline?
6. Can the agency show the client a scoped report without exposing its account
   or provider credentials?

## 2. Agency workflow

    Agency owner creates workspace
      -> creates a client project
      -> uploads JSONL dataset
      -> creates/selects immutable dataset version
      -> configures provider connections
      -> saves a reusable template
      -> launches evaluation
      -> reviews examples and metrics
      -> sets or compares a baseline
      -> exports or shares client report

A seeded mock demo supports the same broad flow without provider credentials.
Mock use is explicitly labelled as simulated so a demonstration cannot be
mistaken for real model evidence.

## 3. Domain vocabulary

| Concept | Definition | Business purpose |
| --- | --- | --- |
| Workspace | An agency account boundary. | Isolates users, clients, models, reports, and audit history. |
| User | A person who signs in. | Gives each collaborator an individual identity and audit trail. |
| Membership | A user's role in one workspace. | Controls who can administer, operate, or view agency data. |
| Project | A client engagement or product. | Keeps one client's evaluation work separate from another's. |
| Dataset | A named evaluation suite. | Represents the client's regression cases. |
| Dataset version | Immutable snapshot of uploaded JSONL content. | Lets a historical run be reproduced. |
| Provider connection | Stored provider/model endpoint configuration. | Avoids sharing or repeatedly entering raw API keys. |
| Template | Reusable evaluation configuration. | Makes standard client evaluations repeatable. |
| Run | One execution of a configuration against a dataset version. | Holds lifecycle, configuration, metrics, and results. |
| Result | One evaluator's verdict for one example. | Provides evidence behind aggregate quality metrics. |
| Pairwise comparison | A model-A/model-B judgement for one example. | Supports model selection and controlled comparisons. |
| Baseline | A completed reference run. | Enables release regression checks. |
| Report share | Expiring, revocable read-only link. | Lets a client view a report without access to the workspace. |

## 4. Architecture

The codebase is a Python application with three entry points using shared
services and persistence:

    CLI: run_eval.py
    FastAPI REST API: app/api/main.py
    Gradio operator UI: app/ui/gradio_app.py

The Gradio UI calls the API using HTTP. It does not operate the database
directly, so browser workflows use the same authorization and validation path
as integration clients.

    CLI / FastAPI / Gradio
               |
               v
    configuration, provider factory, evaluator registry
               |
               v
    single-model runner or pairwise runner
               |
               v
    SQLAlchemy models and database
               |
               v
    SQLite locally / PostgreSQL in Docker deployment

### Technology choices

- Python and asyncio run concurrent generation and evaluation work.
- FastAPI supplies typed endpoints and interactive OpenAPI documentation.
- Pydantic v2 validates configuration, request bodies, dataset records, and
  judge outputs.
- SQLAlchemy 2.x persists all application records.
- Alembic evolves live schemas while preserving historical data.
- structlog records structured application events.
- Gradio provides an internal operator interface.
- httpx is the Gradio-to-API client.
- sentence-transformers supports semantic similarity evaluation.
- json_repair helps recover well-formed JSON structures from imperfect model
  outputs, followed by strict schema validation.
- pytest and pytest-asyncio provide offline regression testing.

## 5. Dataset model

Datasets are JSONL: one JSON object per line. Every record requires input and
expected_output. An id is optional; the loader assigns a line-based ID when one
is missing.

    {"id":"refund-001","input":"Can I return an item after 30 days?","expected_output":"..."}

The upload flow validates all examples, persists their content in a
dataset_versions row, assigns a version number, and tracks the number of
examples. A run links to a dataset_version_id, rather than only to the
mutable dataset name. This is essential: later dataset changes cannot silently
change the data represented by an old report.

The API retains a legacy server-file-path path for CLI and older automation.
Agency users should upload JSONL and select a registered dataset version instead
of relying on a file path that only exists on a server.

## 6. Provider safety and connections

All provider implementations conform to a shared BaseProvider contract: accept
an input, generate output, and return text plus optional usage metadata. The
provider factory supports OpenAI, Anthropic, Gemini/Google, Cohere, and
OpenAI-compatible selections such as Groq or Hugging Face.

The provider factory enforces important safety rules:

- Unknown provider names are validation errors.
- Missing required credentials are configuration errors.
- A mock response needs an explicit mock, dummy, or demo selection.
- An unauthenticated compatible endpoint requires explicit compatible provider
  selection and a base URL.
- A run that uses a simulated candidate or judge is stored as simulated.

A workspace-level provider connection stores a name, provider, default model,
optional base URL, encrypted key or credential reference, and unauthenticated
endpoint permission. Fernet encryption requires WORKSPACE_ENCRYPTION_KEY.
The API never returns a saved credential. A credential reference is stored as a
reference only; resolving an external secret manager reference is still a
deployment extension.

## 7. Evaluators

A run can use multiple evaluators for each candidate answer.

| Evaluator | How it works | Best fit |
| --- | --- | --- |
| Exact match | Normalized, case-insensitive string equality. | Short, structured, deterministic answers. |
| Semantic similarity | Sentence-transformer similarity with a safe fallback. | Answers with acceptable wording variation. |
| LLM-as-a-judge | A judge returns a 1-10 JSON score and explanation. | Rubric-driven, qualitative criteria. |

The LLM judge tries fenced JSON, embedded JSON, and repairable JSON. It then
validates the required score and reason through Pydantic and converts the
1-10 score to 0.0-1.0. A provider error, parse error, or invalid schema produces
evaluation_error and a null score. It never converts a judge failure into an
ordinary poor answer.

### Pairwise evaluation

Pairwise evaluation generates a response from model A and model B, then asks a
judge to compare the two. Order randomization reduces presentation-order bias.
The accepted outcomes are exactly A, B, and tie. A valid tie keeps both scores
and its explanation. Any other winner label, parsing failure, or judge failure
becomes an evaluation error with no winner and no quality scores.

Valid comparisons contribute to A/B/tie rates and Elo. Failed comparisons do
not, preventing a judge outage from making one model appear to lose.

## 8. Run lifecycle and failure semantics

Run state and per-result outcome are separate concepts.

### Run status

| Status | Meaning |
| --- | --- |
| queued | Accepted and persisted but not yet executing. |
| running | Examples are being processed. |
| completed | Processing ended; result counts may still show errors. |
| failed | A fatal error prevented completion. |
| interrupted | Execution stopped before completion. |

The API creates and stores a database run before returning its ID. The
background execution uses that same ID; it does not return one ID and then
replace it later. Database records are the source of truth for status and run
listing.

Startup reconciliation marks unfinished single-worker runs as interrupted.
This makes a restart visible to an operator and preserves completed result rows
for diagnosis.

### Result outcome

| Outcome | Meaning | Quality fields |
| --- | --- | --- |
| evaluated | Candidate generation and scoring succeeded. | Score or A/B/tie present. |
| generation_error | Candidate generation failed or timed out. | Score/winner is null and judging is skipped. |
| evaluation_error | Evaluator or judge failed. | Score/winner is null. |
| unverified | A migrated historical result cannot be classified reliably. | Historical value retained but marked uncertain. |

Errors are sanitized before they are stored or presented. This avoids exposing
credentials, internal endpoints, or raw provider request details in a report.

### Incremental persistence

The runner applies a bounded timeout to provider and evaluator calls and writes
completed result batches during the run. If a worker or app process stops, work
already completed remains inspectable rather than disappearing with an
in-memory task.

## 9. Metrics and denominators

The reporting model separates quality from completeness. Quality aggregates use
only evaluated results. Coverage exposes how much of the intended evaluation
actually produced a valid verdict.

| Measurement | Definition |
| --- | --- |
| Total cases | Expected cases for an evaluator. |
| Valid evaluations | Results with outcome evaluated. |
| Generation errors | Results where candidate generation failed. |
| Evaluation errors | Results where scoring or judging failed. |
| Coverage | Valid evaluations / total cases. |
| Average score | Mean score over valid evaluations only. |
| Pass rate | Passing valid evaluations / valid evaluations. |

If no valid evaluation exists, quality metrics are null and the UI/report should
say “No valid evaluations.” A null metric avoids misleading users into reading
an infrastructure outage as a 0% quality score.

Pairwise coverage works the same way. Win rates and Elo include only valid
comparisons.

## 10. Reproducibility and baselines

Every run persists a configuration snapshot. It includes dataset version,
candidate provider/model settings, judge/evaluator settings, prompt or rubric,
concurrency, timeout, release rules, report preferences, and simulation status.
Provider secrets are intentionally omitted.

A completed run can become a baseline. A compatible new run can be compared
with it using score, pass rate, coverage, and error count. The release decision
is:

- passed: all applicable rules pass;
- regressed: a rule fails, for example an exact-match pass rate falls beyond a
  configured threshold;
- inconclusive: the comparison is not meaningful because inputs/configuration
  are incompatible or quality metrics are unavailable.

This turns a model update into a traceable decision with known dataset,
thresholds, and denominators.

## 11. Workspace access and authentication

A workspace is the tenancy boundary. Projects are linked to workspaces, and
datasets, runs, results, exports, and report shares are reached through
projects. Protected API access checks both workspace membership and project
scope.

| Role | Intended authority |
| --- | --- |
| owner | Workspace administration, provider configuration, membership management, and all project actions. |
| editor | Create and operate evaluation work. |
| viewer | Read results and workspace data. |
| client_viewer | Read only projects explicitly granted to that membership. |

The API supports first-workspace bootstrap, local password sign-in, expiring
12-hour bearer sessions, sign-out, password changes, and workspace selection by
X-Workspace-ID when a user belongs to more than one workspace. Bootstrap tokens
remain as a migration/initialization path; individual sessions are the intended
direction for ordinary users.

## 12. Templates, review, reports, and dashboards

### Templates

An evaluation template saves candidate and judge connection/model settings,
rubric or judge prompt, concurrency, timeout, release rules, and report
preferences. It lets an agency launch the same standard evaluation against a
new dataset version without rebuilding settings by hand.

### Result review

The review API and Gradio page support per-example browsing and filters for
evaluator, score, generation error, and evaluation error. A reviewer can inspect
the input prompt, generated output, expected answer, judge explanation, and
sanitized error.

### Exports and sharing

Run exports are available as CSV, JSON, and HTML. The HTML format includes run
configuration, coverage, valid-result quality metrics, failure counts, and
example-level failures.

A public share is a random capability URL with a scope of one project or one
run. It expires, can be revoked, is read-only, and does not grant access to
other workspace data or provider credentials. Branding is snapshotted in the
share record, which preserves historical visual consistency.

### Project dashboard

A project dashboard aggregates latest runs, baseline/release state, coverage and
quality trends, and recent sanitized failures. It gives an agency manager a
client-level health view without opening each individual run.

## 13. Persistence model

| Area | Tables | Purpose |
| --- | --- | --- |
| Tenancy | workspaces, users, workspace_memberships, user_sessions, project_accesses | Agency isolation, identity, sessions, roles, client project grants. |
| Agency work | projects, datasets, dataset_versions | Client/product grouping and immutable evaluation input. |
| Evaluation | evaluation_runs, evaluation_results, pairwise_runs, pairwise_comparisons | Lifecycle, configurations, outcomes, scores, explanations, Elo evidence. |
| Reuse/delivery | provider_connections, evaluation_templates, report_shares | Saved provider configurations, repeatable work, client delivery. |
| Operations | audit_events, workspace_usage_snapshots, activation_events | Traceability, aggregate usage, privacy-safe activation milestones. |

Alembic migrations provide ordered schema evolution. Migrations preserve old
rows and use SQLite batch operations where SQLite's direct ALTER TABLE support
is insufficient. Legacy records with ambiguous historic zero scores or ties are
labelled unverified instead of claiming a false modern outcome.

## 14. API families

| Area | Representative routes |
| --- | --- |
| Health/operations | GET /health, GET /setup/status, GET /audit-events, retention routes |
| Identity/members | /auth/bootstrap, /auth/sign-in, /auth/sign-out, /auth/password, /workspace/members |
| Connections | GET/POST /provider-connections and DELETE connection route |
| Templates/sharing | /evaluation-templates, template launch, /report-shares, /shared-reports/{token} |
| Demo | POST /demo/seed |
| Single-model runs | POST /runs, status/list/results/export, baseline/comparison, cancellation |
| Projects | Project CRUD and project dashboard |
| Datasets | CRUD, upload, versions, active-version selection |
| Pairwise runs | POST /pairwise-runs, status and list routes |

The FastAPI interactive contract is available at /docs while the server runs.

## 15. Deployment and operations

For local development, SQLite is the default database:

    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    .\.venv\Scripts\python.exe -m uvicorn app.api.main:app --reload --port 8000
    .\.venv\Scripts\python.exe app/ui/gradio_app.py

For a hosted pilot, Docker Compose runs PostgreSQL 16, FastAPI, and Gradio.
Production configuration requires PostgreSQL and WORKSPACE_ENCRYPTION_KEY.
Health reporting exposes safe configuration problems and database reachability
without returning secrets.

Audit events record important actions with actor identity and safe metadata.
Retention settings can remove expired shares and old audit entries; current
retention cleanup intentionally does not remove evaluation data.

## 16. Testing

The offline suite covers providers, evaluators, runners, lifecycle semantics,
migrations, datasets, projects, reports, baselines, templates/shares/dashboard,
audit operations, demo workflow, and user sign-in.

tests.fakes.DeterministicFakeProvider supports deterministic successful output,
valid judge JSON, malformed JSON, and provider exceptions. It never calls a
real provider or requires credentials.

Run the suite with:

    .\.venv\Scripts\python.exe -m pytest -q

If Windows has locked the configured .pytest_tmp directory, use an isolated
temporary base instead of treating that machine-level lock as an application
failure:

    .\.venv\Scripts\python.exe -m pytest -q --basetemp "$env:TEMP\evaluation-engine-tests"

## 17. Current status: implemented versus pending

### Implemented

- Stable database-backed run IDs and lifecycle states.
- Explicit mock mode and deterministic fake providers.
- Sanitized generation/evaluation errors and null failed quality fields.
- Valid-result coverage metrics and error-aware pairwise Elo.
- Incremental result persistence, timeouts, and restart reconciliation.
- Dataset upload, validation, versioning, and project association.
- Workspaces, roles, local sessions, provider connections, templates, report
  shares, project dashboard, audit log, retention controls, Docker deployment,
  and seeded demo workflow.
- Account persistence fields: plan, billing status, trial end, invoice contact,
  configurable limits JSON, usage snapshots, and activation events.

### Foundation only; not yet complete features

- Single-model runs have queue metadata: attempt count, maximum attempts, next
  attempt time, worker claim fields, cancellation request time, and last
  transient error. A queued run can be cancelled.
- Account and analytics tables are persisted, but no service yet calculates and
  enforces limits, generates billing-period snapshots, or serves a funnel.

### Required before production agency SaaS

1. A worker service must claim due runs, classify transient failures, apply
   jittered exponential backoff, retry safely, expose queue position, and
   respect cancellation between examples. Pairwise runs need the same support.
2. Schedules need daily/weekly template execution, execution history, and
   overdue-schedule monitoring.
3. Usage and billing need aggregation, limit enforcement, a payment-provider
   service boundary, and a pilot/manual-invoicing workflow.
4. CI/CD needs scoped automation keys, release-decision endpoints, signed
   webhooks, incoming triggers, and a GitHub Actions example.
5. Branding needs workspace name/logo/color/footer/contact/domain settings,
   safe validation, previews, and application to reports.
6. The owner console needs a single place for members, projects, templates,
   connections, usage, billing, notifications, health, and audit history.
7. Production operations need durable JSON correlation logs, error reporting,
   worker monitoring, scheduled backups, restore verification, and incident
   procedures.
8. Product analytics needs privacy-safe milestone recording and an internal
   activation funnel that never stores prompts, outputs, credentials, or report
   tokens.

## 18. Recommended paid-pilot sequence

The worker/retry/schedule foundation should be the next priority. A polished
report is not commercially credible if a provider timeout can lose or obscure a
run.

Next, calculate and enforce usage limits, then add a small billing boundary.
Start with a fixed pilot and manual invoicing, but preserve plan, trial, invoice
contact, and period usage so payment integration does not require a data-model
rewrite.

After that, add CI/CD and branded reporting. Those two features make the product
part of an agency's client-delivery process: a saved suite can run on a
deployment, block a regression, and be delivered in the agency's identity.

A first paid pilot should state the number of projects, datasets, evaluated
cases per period, retention period, support response expectations, and success
metrics. Useful success metrics include time to first completed run, coverage,
regressions detected before release, recurring runs created, and reports shared
with client stakeholders.

## 19. Key locations

| Location | Responsibility |
| --- | --- |
| app/api/main.py | FastAPI routes, authorization checks, background run launch, responses. |
| app/api/schemas.py | Request and response validation models. |
| app/database/models.py | SQLAlchemy schema. |
| alembic/versions | Ordered data-preserving migrations. |
| app/runners/eval_runner.py | Single-model orchestration, persistence, metrics. |
| app/runners/pairwise_runner.py | Pairwise orchestration, metrics, Elo. |
| app/evaluators | Exact match, similarity, single judge, pairwise judge. |
| app/providers | Provider adapters and factory safety policy. |
| app/services | Identity, project, dataset, report, baseline, demo, operations, configuration services. |
| app/ui/gradio_app.py | Gradio operator interface. |
| tests | Offline regression suite and deterministic fakes. |
| HOSTED_PILOT_ONBOARDING.md | Hosted-pilot deployment and onboarding checklist. |

