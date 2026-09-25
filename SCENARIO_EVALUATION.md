# Evaluating agents and application AI

This extension evaluates an application's observable result and execution evidence.
It is separate from the existing prompt-to-model evaluation runner. The first
implementation is an operator CLI with JSONL results, a run manifest, and an HTML
review report. It does not yet register scenario runs in the workspace database,
Gradio, queue, schedules, billing, or public report sharing. Existing model and
pairwise evaluation flows are unchanged.

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
remain visible per case. These are scenario decisions, not the existing
baseline/release-rule comparison service.

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
must not be marked complete. Only observable tool names/statuses are needed;
private chain-of-thought is neither required nor collected.

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

## Verification and next integration boundary

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_scenarios.py -q
```

Tests use local fixtures and `httpx.MockTransport`. They cover missing traces,
forbidden tool attempts, token/call budgets, structured schedule failures,
malformed responses, HTTP errors, deadlines, cancellation, partial report
readiness, persisted results, and HTML escaping. No real target credentials are
required. Live validation requires running reference deployments and controlled
datasets, which are not supplied by this repository.

The next integration step is database-backed scenario run/result types with
workspace scoping, scenario dataset upload/review in Gradio, and explicit remote
execution ownership/cancellation before connecting these runs to automatic
queue retries and schedules. Keep the old text evaluation contract intact.
