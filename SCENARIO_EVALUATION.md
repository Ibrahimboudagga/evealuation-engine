# Evaluating agents and application AI

This extension evaluates observable application output and execution evidence.
There are now two entry points: the operator CLI executes trusted targets or
fixtures; the workspace API and Gradio page score uploaded application evidence.
Workspace runs, immutable suite versions, reviews, comparisons, exports, and
expiring report snapshots are persisted in the database.

The workspace path **imports evidence**. It does not start a remote agent or
verify the origin of an uploaded trace. Every response/report says so. Remote
execution remains operator controlled; automatic retries and schedules still
apply only to the model and pairwise runners.

## Try both reference suites without credentials

From the repository root, in PowerShell:

```powershell
.\.venv\Scripts\python.exe run_scenarios.py --adapter fixture --dataset datasets/scenarios/legal_rag.jsonl --fixtures datasets/scenarios/fixtures.json --output-dir scenario-output/legal-demo
.\.venv\Scripts\python.exe run_scenarios.py --adapter fixture --dataset datasets/scenarios/open_saas.jsonl --fixtures datasets/scenarios/fixtures.json --output-dir scenario-output/saas-demo
```

Output directories must be new. Each contains:

- `manifest.json`: stable run ID, lifecycle, timestamps, dataset SHA-256,
  timeout, adapter, operator-supplied target/version label, simulation flag,
  complete scenario definitions, and metrics.
- `results.jsonl`: flushed and fsynced after each example; outcomes, checks,
  evidence, score, and simulation labels.
- `report.html`: escaped, script-free local review of metrics, individual
  checks, errors, and application output.

Fixtures always force `simulated=true`, even if a fixture says otherwise. The
included legal response is invented test data, not a response from the legal
repository. The SaaS fixture follows its current schedule schema. Passing these
demonstrations verifies the evaluation pipeline, not the reference applications.

CLI exit codes: `0` all cases passed, `1` fully evaluated suite with regressions,
`2` inconclusive. An unexpected internal failure uses Python's error exit.

## Scenario contract

Each JSONL row is a version-1 `Scenario`. Give it a unique `id`, `task`, target
`inputs`, and at least one check. Unknown fields and contradictory tool policies
are rejected before execution.

| Check | Meaning |
| --- | --- |
| `assertions` | Named JSON Pointer checks on application output: equals, contains, exists, min, max |
| `required_tools` | Each named tool must have a successful call in a complete trace |
| `forbidden_tools` | No attempt of the tool may appear, including failed attempts |
| `max_tool_calls` | Total attempts in a complete trace |
| `expected_citation_ids` | All expected source identifiers must appear |
| `max_latency_ms` | End-to-end adapter latency for HTTP, supplied measurement for fixtures |
| `max_total_tokens` | Reported total across the application execution |
| `schedule` | Open SaaS output shape, expected task names, minimum subtasks, positive durations, total hours, priority ordering |

Example assertion: `{"name":"answer","path":"/answer","operator":"equals","value":"30 days"}`.
JSON Pointer supports object keys, array indexes, `~0` for `~`, and `~1` for `/`.
An absent expected output path is a valid failed check. An absent required trace
or usage measurement is unverified evidence. Citation identity does not establish
faithfulness or legal correctness. Priority checks verify ordering of returned
priorities; they do not infer the true priority of a user's task.

The score is the fraction of explicit checks passed. All checks must be verified
for a case to receive a score. A partially verified case keeps its individual
check results but receives `evaluation_error`, `score=null`, and `inconclusive`.
Target execution failures receive `generation_error` and no score. A fully
evaluated incorrect result receives `evaluated` and a failing quality score.

Coverage = valid cases / all expected cases. Pass rate = passing valid cases /
valid cases. Average score also uses only valid cases. If any expected case is
missing or invalid, the overall decision is inconclusive; verified failures
remain visible per case. Scenario comparisons additionally check pinned suite/scorer identity, per-case
simulation modes, coverage, minimum valid cases, and pass-rate thresholds.
They use a separate scenario policy; text evaluator/slice policies are not
silently applied to agent traces.

## Application HTTP adapter

Configure a trusted staging endpoint through `--adapter http --url ...`.
The engine sends exactly one POST per example:

```json
{
  "schema_version": 1,
  "request_id": "stable-run-id:scenario-id",
  "task": "Create today's schedule",
  "inputs": {"hours": 2}
}
```

It also sends `Idempotency-Key: <request_id>`. The receiving application must
implement deduplication if needed; the header alone is not a guarantee. Expected
response:

```json
{
  "schema_version": 1,
  "output": {"answer": "example"},
  "tool_calls": [{"name": "retrieve", "status": "succeeded"}],
  "trace_complete": true,
  "citation_ids": ["document-1"],
  "total_tokens": 120,
  "simulated": false,
  "external_run_id": "target-operation-123"
}
```

Only `output` and `simulated` are required. Never invent traces or token counts:
omit unavailable evidence or set it to null. `tool_calls=[]` plus
`trace_complete=true` asserts that zero calls actually occurred. A partial trace
must not be marked complete. Basic tool-name checks need names/statuses; argument/order/retrieval/state checks
require their additional typed fields below. Private chain-of-thought is neither
required nor collected.

Use `--token-env TARGET_EVALUATION_TOKEN` to read a bearer credential from an
environment variable. A missing explicitly requested credential is an error.
Omitting the option intentionally allows unauthenticated staging services. The
credential is not included in artifacts. Authenticated non-loopback URLs require
HTTPS. Redirects are not followed and HTTP response error bodies are not stored.

The whole example, including polling, has a bounded `--timeout` (default 60s).
There is no automatic resubmission: agents may send messages, charge credits, or
perform other side effects. A local timeout/cancellation does not cancel the
remote workflow. Reconcile remote work using the target's operational tools.
Graceful interruption preserves already written results and marks the manifest
interrupted; forced process termination can leave a running manifest. There is
no recovery/resume service for these standalone artifacts yet.

Endpoint selection is deliberately operator-only. Do not expose arbitrary target
URLs in public API requests without egress policy, DNS/IP protections, and
workspace authorization. Scenario files, output, and reports can contain client
data; store them in a controlled directory and review them before distribution.
Do not put credentials in scenario inputs or application outputs.

## Reference 1: agentic legal RAG

Source: [agentic-legal-rag-system](https://github.com/Ibrahimboudagga/agentic-legal-rag-system).
The adapter uses the routes in
[`app/client_app/main.py`](https://github.com/Ibrahimboudagga/agentic-legal-rag-system/blob/main/app/client_app/main.py):

1. POST `/agent-review/start` with `query`, `s3_paths`, and `top_k`.
2. Poll GET `/agent-review/{workflow_id}/status` and `/report`.
3. Accept a nonempty report only when the configured readiness status is reached.

The default readiness selector is `/workflow_status/status` equal to
`human_in_loop`. Verify this on the deployed revision; override `--ready-path`
and `--ready-value` if its status query differs. A nonempty partial report is not
automatically treated as finished. The native report is preserved as `output`;
write assertions against its actual deployed structure.

```powershell
.\.venv\Scripts\python.exe run_scenarios.py --adapter legal-rag --url https://your-staging-host --token-env LEGAL_STAGING_TOKEN --dataset path/to/real-legal-scenarios.jsonl --timeout 180 --target-label legal-rag-pinned-commit --output-dir scenario-output/legal-live
```

The shipped `legal_rag.jsonl` is a synthetic contract demonstration, not a ready
live test: replace its S3 path, expected fields, and expected evidence with your
controlled corpus and actual report schema.

Current integration constraints:

- The reference API queries the report only while Temporal reports RUNNING.
  COMPLETED with no report produces an execution error; it is never a pass.
- The checked-in workflow source inspected during implementation appears
  incomplete; deployed workflow readiness and report schema still require live
  verification. This evaluation engine does not repair that other repository.
- The native endpoints do not supply a complete tool-call trace or total token
  usage. These checks are inconclusive until the target exposes the standard
  evidence envelope through a bridge.
- Native citation content is retained inside the report. The adapter does not
  guess how to normalize it into citation IDs.
- The workflow may remain waiting for human review after evaluation. The adapter
  does not approve, revise, terminate, or otherwise mutate that review state.

For deeper evaluation, instrument graph node/tool boundaries and export observed
tool attempts, source identifiers, total tokens, and final structured output.
Use synthetic legal documents and independently reviewed expected answers.

## Reference 2: Open SaaS AI planner

Source: [Open SaaS](https://github.com/wasp-lang/open-saas). Its current AI feature
is `generateGptResponse` in
[`operations.ts`](https://github.com/wasp-lang/open-saas/blob/main/template/app/src/demo-ai-app/operations.ts),
with the return schema in
[`schedule.ts`](https://github.com/wasp-lang/open-saas/blob/main/template/app/src/demo-ai-app/schedule.ts).
It takes `hours`, reads the authenticated user's saved tasks, generates a
schedule, stores the response, and may consume a credit. It is a Wasp action,
not an existing `/evaluate` HTTP endpoint.

To evaluate it live:

1. Create a dedicated staging user and seed exactly the tasks in the scenario.
2. Add an authenticated staging bridge route in the Open SaaS deployment. Bind
   its allowed test identity server-side; do not accept a caller-supplied user ID.
3. Validate `inputs.hours`, invoke the existing action with its normal authenticated
   context, and return its result as `output` with `simulated=false`.
4. Keep authorization and credit checks intact. Convert failed action execution
   into an HTTP error; do not return an error object as a successful schedule.
5. Use the generic HTTP adapter against that bridge. Reset staging state between
   suites and budget for credits and provider charges.

The included suite checks the actual `tasks`/`taskItems` output structure,
three subtasks per task, total time, priority order, and unexpected task names.
It does not prove tenant isolation, credit accounting, authentication, or the
rest of the SaaS. Test those independently or expose explicit observed state
assertions in a separate controlled integration suite. No Open SaaS production
endpoint is called by the fixture demonstration.

## Typed agent evidence

Additional optional fields extend the original version-1 envelope. Old fixtures
remain valid; choosing a deeper check makes its evidence mandatory.

| Scenario field | Required observed evidence | What is verified |
| --- | --- | --- |
| tool_expectations | tool_calls with arguments and result.value | JSON Pointer assertions on a named attempt; occurrence is ordered by sequence |
| tool_order | complete tool_calls with unique sequence numbers | Successful attempts appear in the declared order |
| retrieval | retrievals with source_id/query/rank and retrieval_complete | Recall against configured relevant IDs and count of other IDs |
| grounding | observed source content and output claim | Exact configured claim and supporting quote; unreviewed references are inconclusive |
| state_checks | state_before / state_after | Explicit state assertions |
| side_effect_policy | complete side_effects with name/status/authorized | Allowed attempts, required successful effects, and observed authorization decisions |
| turns | up to 20 turns with ID/sequence/output/state and turns_complete | Declared turn ordering, outputs, and state assertions |
| revenue_ops | revenue_ops typed evidence | Hotel/date range, DAY x LT matrix, clustering inputs, selected cluster, anomaly labels, calculations, numeric narrative claims |

A tool result uses {"value": null} for an observed null; a missing result means
unavailable evidence. Tool attempt IDs/sequence and turn IDs/sequence must be
unique. HTTP requests include declared turns as {id,input}; expected assertions
are not sent to the target. The native legal adapter rejects multi-turn suites
because its target interface has no supported conversation contract.

Grounding is an exact evidence check, not a general entailment model. Source
quotes and reviewed references still need trustworthy instrumentation. An
application-provided authorized=true flag does not establish tenant isolation.
A human_reviewed reference requires a reference_id, but the engine does not
verify that the human review actually occurred.

## Revenue Ops synthetic demonstration

~~~powershell
.\.venv\Scripts\python.exe run_scenarios.py --adapter fixture --dataset datasets/scenarios/revenue_ops.jsonl --fixtures datasets/scenarios/revenue_ops_fixtures.json --target-label revenue-ops-synthetic-v1 --output-dir scenario-output/revenue-demo
~~~

The fixture has synthetic truth. Change a report calculation, hotel, matrix,
cluster or label to get a visible regression; remove required evidence to get
an inconclusive result. It does not prove real anomaly detection quality,
clustering suitability or general narrative truth.

## Workspace workflow

1. Sign in and create/select a client project.
2. Open **Agent & App Scenarios**, refresh, and upload a JSONL suite. Reusing a
   project and suite name creates a new immutable version.
3. Refresh and select the version. Upload JSON keyed by scenario ID; each value
   is an Evidence envelope. The shipped fixture JSON files use this shape.
4. Enter the target build/commit and select **Evaluate imported evidence**.
5. Refresh runs; review all cases or filter passed/regressed/inconclusive cases.
6. Select a separate baseline. Compare, export HTML/JSON, or create a read-only
   expiring share. A selected baseline decision is included in the report.
7. Save the returned share ID to revoke that link later.

Uploads are limited to 1,000 unique cases and 5 MB. Unknown evidence IDs are
rejected; omitted or malformed cases are recorded as evaluation errors.
Results/configuration/metrics and the completion audit event commit together.
There is no queued/running imported-agent execution to resume: scoring is local
and bounded, and remote side effects are never repeated by this import path.

| API | Action |
| --- | --- |
| POST /scenario-suites | Create an immutable version from project_id, name, JSONL content |
| GET /scenario-suites and /scenario-suites/{id} | List scoped versions or read a suite |
| POST /scenario-runs | Score evidence with suite_id, target_build, evidence, optional release_rules |
| GET /scenario-runs and /scenario-runs/{id} | List runs or review full evidence and checks |
| GET /scenario-runs/{id}/compare?baseline_run_id=… | Compare compatible runs |
| GET /scenario-runs/{id}/export?format=html | HTML or JSON; optional baseline_run_id |
| POST /scenario-runs/{id}/shares | Create snapshot link with expires_in_hours and optional baseline_run_id |
| DELETE /scenario-shares/{id} | Revoke a link |
| DELETE /scenario-runs/{id} and /scenario-suites/{id} | Owner deletion; delete runs before their suite |

All private routes use existing workspace membership and client project grants.
Public links return only a fixed, escaped HTML snapshot; tokens are hashed at
rest. Set PUBLIC_API_BASE in the UI environment to the externally reachable API
origin when it differs from API_BASE. The share token is returned only at
creation and is not recorded in audit metadata.

Scenario runs/cases/storage/shares contribute to workspace usage and existing
admission limits. Importing traces makes **zero engine provider calls**. These
counters are still not a billable provider-attempt ledger or atomic reservations.

Credentials must be removed at the source. Suites containing recognizable
credential fields are rejected. Retained traces redact recognized credentials
without changing whitespace; the configuration records whether redaction
occurred, that scores used original observations, and whether retained evidence
is replayable. Pattern matching cannot identify every possible secret.
Owners can delete retained runs (including their shares) and unused suites.
Project deletion is blocked while suites remain; historical scope is retained.
Existing retention maintenance also deletes expired scenario shares.

## Verification and remaining boundary

~~~powershell
.\.venv\Scripts\python.exe -m pytest tests/test_scenarios.py tests/test_scenario_trace.py tests/test_scenario_redaction.py tests/test_workspace_scenarios.py -q --basetemp=.pytest_tmp/scenarios
~~~

Tests use local fixtures and httpx.MockTransport; no live target credentials.
The three reference demonstrations validate the harness and explicit synthetic
contracts. Real hosted browser isolation, reviewed domain ground truth, remote
execution ownership/cancellation, and automatic scheduled agent execution still
need deployment/integration work. See REANALYSIS_REMEDIATION.md for the complete
implemented-versus-pending assessment.
