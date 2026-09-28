# Reanalysis response: measurement and agent evidence

Feedback: evealuation_engine_reanalysis_2026-09-28.md, reviewing f46bf46.
Implemented on feature/reanalysis-measurement-evidence, based on the merged
stabilization change. Last verified locally: 29 September 2026 (Africa/Tunis).

## Assessment

The feedback correctly distinguishes a compatible experiment from a trustworthy
quality measurement. Two open-ended answers can both have zero exact matches
while one is materially worse. That false-pass behavior was reproduced before
the release-check changes. The separate CLI scenario workflow and schedule
dispatch delay were also confirmed.

The review is engineering input, not an instruction to certify readiness or
choose distribution terms. This change closes concrete measurement, evidence,
workflow and scheduling gaps. It does not implement every commercial or
research recommendation in the review.

## Implemented changes

| Feedback / reproduced issue | Change | Evidence and limits |
| --- | --- | --- |
| Literal evaluator version 1 could conceal code changes | Configuration schema 2 stores evaluator/base-class source hashes, dependency versions, rubric, explicit pass threshold, judge implementation/model/settings, and unverified calibration status | Legacy contracts compare as inconclusive. Candidate identity remains outside the comparison fingerprint so different candidate builds can be compared |
| A queued run could execute changed code under its original snapshot | Both runners validate their effective candidate/judge/evaluator/execution contract before provider calls | Drift or legacy queued configurations fail clearly and require a fresh submission; no history is rewritten |
| Exact match was the only quality regression gate | Average-score/pass-rate floors and maximum drops for named evaluators; minimum valid cases; metadata slice rules; saved policies propagated through direct, template and scheduled launch | Missing metrics, incompatible backends and insufficient samples remain inconclusive. Default policy protects all recorded evaluators |
| Pass threshold was implicit and reports could disagree | Per-evaluator pass thresholds are saved and used in metrics, API and report quality-failure lists | Default threshold remains 0.5; configured thresholds take precedence |
| Invalid scores could contaminate quality / release decisions | Strict normalized score validation; historical invalid scores treated as visible evaluation errors; pairwise invalid results excluded from winners and Elo | Null/non-finite/out-of-range scores cannot be ordinary wins or successful quality measurements |
| Agent evidence only captured tool names | Typed argument/result/sequence, retrieval, reference-grounding, state, side-effect and bounded multi-turn contracts | Missing required observations yield unverified checks and null quality scores; observed authorization is not independent security proof |
| Revenue Ops lacked a domain contract | Hotel/date range, DAY x LT, clustering inputs, chosen cluster, reference anomaly labels, numeric report/narrative calculations | Included fixture is synthetic; no real hotel, customer system, reviewed anomaly data or semantic narrative evaluator was supplied |
| Scenario artifacts could not be managed in the product | Immutable project-scoped suite versions, persisted imported-evidence runs, API and Gradio review/filter/compare/export/share/revoke | This is evidence import and scoring, not remote worker-owned agent execution |
| Client report scope and consistency | Existing project grants protect private routes; hashes protect expiring share tokens; fixed escaped HTML snapshots can include a selected baseline comparison | Uploaded provenance is explicitly unverified; report links never authorize other APIs |
| Trace retention could alter scored text or expose obvious keys | Preserve whitespace, mask recognizable credential fields/text, reject credential-bearing suites, record redaction/replayability | Scores use original observations. Secret pattern matching is not complete data-loss prevention |
| Long runs delayed schedules | Independent schedule dispatcher alongside the single execution worker | Still one application process / execution worker; multi-worker leases remain necessary |
| Schedule occurrence/run/audit could commit separately | One transaction with a savepoint for run creation; deterministic occurrence identity retained | Tested rollback on audit failure; true deployment crash/load validation remains pending |
| Worker health could conceal failure or shutdown could hang | Dead heartbeat/dispatcher detection; cancellation drains the dispatch transaction even if it fails | Focused asynchronous tests verify these paths |
| Deployment evidence lacked restore proof | Smoke requires two explicit empty disposable databases; tests migrations, execution, backup, restore, row counts and content hashes | SQLite passed locally; PostgreSQL CI extended but not observed on this branch |

Migration 20260928_18 adds scenario_suites, scenario_runs, and scenario_shares.
It does not transform, remove or relabel historical model/pairwise records.
Existing migration regression tests, including historical-record preservation,
continue to pass.

## Using evaluator and slice policies

Save these fields in a template or POST /runs:

~~~json
{
  "evaluator_settings": {
    "llm_judge": {"pass_threshold": 0.8}
  },
  "release_rules": {
    "coverage_minimum": 0.95,
    "minimum_valid_cases": 20,
    "evaluators": {
      "llm_judge": {
        "average_score_minimum": 0.8,
        "average_score_max_drop": 0.05,
        "pass_rate_max_drop": 0.05
      }
    },
    "slices": [
      {
        "name": "critical_cases",
        "metadata": {"risk": "critical"},
        "minimum_valid_cases": 5,
        "evaluators": {"llm_judge": {"pass_rate_minimum": 0.95}}
      }
    ]
  }
}
~~~

Metadata values come from the pinned dataset case manifest. Sample minima are
explicit admission rules; they are not confidence intervals or a calibration
study. Score/pass-rate drops are absolute differences on a 0–1 scale: 0.05 means
five percentage points. The Gradio template form accepts advanced policy and
threshold JSON. Query overrides of legacy coverage/exact-match settings remain
visible in the returned effective policy.

A legacy run cannot acquire a verified code contract retroactively. Establish a
fresh baseline on the pinned suite. Model IDs and source hashes do not pin hosted
provider aliases or downloaded embedding weights; those artifacts still require
operator-controlled revision pinning for strict reproducibility.

## Agency scenario workflow

See [SCENARIO_EVALUATION.md](SCENARIO_EVALUATION.md) for schemas, routes,
fixtures and the Gradio walkthrough.

Scenario scoring commits results, configuration, metrics and audit together.
Private routes require authenticated project access. Limits include scenario
runs/cases/storage/shares; imported traces make zero engine provider calls.
The operator CLI still writes incrementally and bounds target execution.
Remote retries, scheduling, ownership and cancellation are intentionally not
claimed for imported scenario runs.

Suite and result deletion are owner actions. Deleting a run also removes its
share snapshots. Suites with retained runs cannot be deleted; projects with
suites cannot be deleted. Existing retention maintenance removes expired
scenario shares. This does not constitute complete retention across all older
datasets, CLI artifacts, downloads and retry archives.

## Verification

Runtime: Python 3.14.6 in the existing project virtual environment.

- Full suite after the main implementation: **257 passed**, five existing
  FastAPI/Starlette deprecation warnings, 88.50 seconds.
- Final focused suite after adding selected-baseline sharing and two additional
  integration tests: **68 passed**, the same warnings, 22.00 seconds. It covers
  measurement contracts, API template propagation, scenario import/review/
  sharing/deletion/isolation, typed trace checks and redaction.
- Fresh SQLite smoke at schema 20260928_18: API/OpenAPI import, migrations,
  long-path dataset UUID, mock execution/coverage, backup, restore, row counts,
  content hashes and restored-run read **passed**.
- Revenue Ops fixture CLI: **completed, simulated=true**, one valid case,
  coverage 100%, explicit checks passed.
- Final upload/redaction checks: **13 passed** after enforcing the UTF-8 byte limit.
- Gradio UI import and Git diff whitespace check passed.

Local logs and disposable databases are under ignored .pytest_tmp; CLI demo
artifacts are under ignored scenario-output/reanalysis-revenue-final.
No production database was used for the rehearsal.

CI now covers Python 3.11/3.12/3.14, PostgreSQL 16 with an encoded password, and a
second disposable restore database. It publishes restore evidence and resolved
dependencies. These jobs have not been observed remotely for this branch.
The available Docker client could not contact its daemon; no live container,
PostgreSQL server, real provider, target application, two-browser or proxy
verification is claimed.

To repeat the SQLite deployment rehearsal, use two new paths:

~~~powershell
$env:DATABASE_URL = 'sqlite:///scenario-output/fresh-smoke.db'
$env:RESTORE_DATABASE_URL = 'sqlite:///scenario-output/fresh-restore.db'
$env:SMOKE_EVIDENCE_PATH = 'scenario-output/deployment-evidence.json'
.\.venv\Scripts\python.exe -m scripts.deployment_smoke
~~~

Create the output directory first. The command refuses nonempty source/restore
databases. PostgreSQL also needs matching pg_dump/pg_restore clients and two
empty databases; URLs must encode password special characters. Credentials are
passed to backup clients in their environment rather than command arguments.

## Remaining work, in recommended order

1. **Deployment acceptance:** observe the CI results; run the actual containers,
   two isolated browser users and intended proxy; verify downloads/logout and
   secret handling; pin a tested platform-specific dependency lock. A SQLite
   restore test is not PostgreSQL operational acceptance.
2. **Measurement validity:** commission human review of golden cases and slices;
   record calibration/adjudication, repeated judgments and uncertainty; pin
   judge/model weight revisions. The shipped regression fixtures are synthetic.
3. **Remote agent execution:** add trusted workspace target connections, trace
   provenance, remote ownership/idempotency/cancellation and safe scheduling.
   Text and scenario policies are explicit but separate; there is no universal
   release-policy or billing engine for all execution paths yet.
4. **Paid usage:** atomic cross-path quota reservations, durable provider-attempt
   ledger (including retry attempts), unknown-cost reporting and pricing.
   Current aggregate usage/admission remains insufficient for reliable billing.
5. **Identity and content lifecycle:** accepted invitations, verified recovery,
   scoped automation credentials, and retention/deletion covering every content
   and archive class. This change does not replace those remaining designs.
6. **Commercial decisions:** the repository owner still needs to select
   distribution/licensing terms and review client-data agreements. No license
   was invented or added on the owner's behalf.

The controlled-pilot demonstration is now executable with synthetic inputs:
a pinned suite, comparable baseline, deliberately regressed evidence, corrected
evidence, a visible decision and a fixed report. Tests also preserve prior retry
and interruption behavior. Real staging-system and human-reviewed business
validation remain the next proof points.
