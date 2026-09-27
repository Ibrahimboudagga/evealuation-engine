# Review analysis and stabilization changes

This document responds to `evealuation_engine_deep_review.md` (26 September
2026, reviewed revision `f995f67`). The review was treated as engineering
feedback, not as proof that every assertion or proposed design was correct.
Changes are on `feature/review-stabilization`.

## Assessment

The review's central conclusion is supported: lifecycle, identity, and
measurement integrity matter more than additional account-management screens.
The reproduced startup, migration, identity, result-counting, mock-judge, and
comparison defects were actionable. One factual correction: this repository
already had a `.dockerignore`; it has now been expanded to exclude environment
variants, local scenario output, test artifacts, and SQLite files.

The review also proposes larger architecture and research work. This change
does not claim to implement that entire roadmap or establish production
readiness. In particular, model evaluation, pairwise evaluation, and standalone
scenario artifacts remain distinct execution paths.

## Implemented changes

| Finding | Change | Evidence / limit |
| --- | --- | --- |
| R1: eager forward-reference failure | Postponed schema annotations; API/OpenAPI import regression and CI for container Python 3.11 plus local 3.14 | Python 3.11 CI is added, not yet observed remotely |
| R2: masked/interpolated migration URL | Pass the existing SQLAlchemy connection to Alembic | Real SQLite percent-path migration passes; PostgreSQL CI smoke uses an encoded password |
| R3: partial retries mix results | Manual retry creates a run with `parent_run_id`; automatic restart archives prior attempt rows transactionally before removing active rows | Original manual run stays unchanged; exhausted attempt evidence stays active |
| R4: misleading coverage | Save expected case IDs; reject duplicates/empty ingestion; calculate coverage from planned cases; exclude ambiguous historical duplicate groups | New runner writes have unique identity keys; no historical scores are silently selected or deleted |
| R5: incompatible release checks | Fingerprint dataset content/cases, evaluator settings/identity, judge configuration and simulation; reject missing/incompatible contracts as inconclusive | Candidate model may differ; actual similarity backend differences/mixed backends also block passing |
| R5: inconsistent policy | Comparison endpoints default to saved release rules; public/project report path uses the same resolver; both baseline and candidate require coverage | Explicit comparison query overrides remain visible in the returned rules |
| R6: account takeover through membership | Reject initial-password assignment for existing identities | Existing password remains valid; invitation acceptance is still future work |
| R7: immortal ordinary-access token | Newly issued setup/member tokens are expiring sessions; legacy hashes only authorize initial password migration; password changes revoke sessions and rotate legacy hashes | No scoped automation-key replacement is introduced here |
| R8: project metadata enumeration | Shared accessible-project query for project, dataset, single-run, pairwise lists; client viewers cannot list workspace provider connections/templates | Detail access continues to enforce project grants |
| R9: detached schedule response | Load execution history before returning the updated schedule | Toggle tested after session closes |
| R10: lost candidate usage | Preserve candidate usage separately in result metadata (A/B separately for pairwise), including unknown as null | This is not a provider-call ledger or reliable cost accounting; see remaining work |
| Shared UI identity | Browser-specific Gradio State supplies API credentials via a ContextVar scoped to each callback; sign-in/out and workspace selection replace environment-token identity | Component binding and concurrent callback isolation tested; real multi-browser/proxy testing remains required |
| Unsafe hosted setup/provider fallback | Production bootstrap secret; no unauthenticated pre-bootstrap production operations; live API runs require a workspace connection, which must have credentials or explicit keyless configuration | Target network egress controls remain operator responsibility |
| Mock pairwise failure | All built-in demo providers return the pairwise schema when requested | Standard mock judge tested, not just a dedicated fake |
| Similarity cache/event-loop blocking | Cache by model identity; offload loading/encoding to threads; expose actual backend in metrics | A timeout cannot kill an already running Python worker thread; hard CPU limits require process isolation |
| Operational/export safety | Independent heartbeat; queue-loop supervision; live/ready endpoints; deterministic schedule occurrence IDs; UTC timezone conversion; non-root Docker image; loopback published ports; formula-safe CSV; separate HTML quality-failure table | Still exactly one execution worker; no lease-based multi-worker recovery |
| Path IDs exceed PostgreSQL size | New file-backed datasets use UUIDs, retaining path/hash in configuration; changed files/cases are rejected at execution; registry version is pinned | Historical path IDs remain unchanged; this is not an identity-rewriting migration |

## Changed contracts

- `POST /runs/{id}/retry` and `POST /pairwise-runs/{id}/retry` return the actual
  retry run's status and ID. Failed/interrupted runs produce a new ID linked to
  the parent. A queued run is accelerated in place. `/retry-now` remains a
  deprecated alias with the same behavior. Clients must use the returned ID.
- Terminal runs cannot be executed again directly under the same ID.
- Migration `20260926_17` adds retry lineage, prior-attempt archives, and nullable
  unique result identity keys. Legacy rows retain null keys; duplicate legacy
  groups are unverified in metrics/exports. No last-row-wins conversion is used.
- Attempt archives contain the original evaluation content, not only aggregate
  counters. Apply the same access and retention controls as to active results.
- Legacy runs without a complete comparison contract return an inconclusive
  release decision. Run the pinned suite again to establish a verified baseline.
- API callers must save real credentials in workspace provider connections.
  Direct CLI provider configuration remains an operator capability. Raw keys
  and arbitrary endpoints are rejected on run submission.
- Set `BOOTSTRAP_SECRET` for production and provide it as `X-Setup-Token` on
  bootstrap (or in the setup wizard). Sign in afterward. Gradio no longer reads
  `WORKSPACE_API_TOKEN`; launch it as `python -m app.ui.gradio_app`.
- Compose publishes API/UI ports on localhost. Put a properly configured TLS
  reverse proxy in front for remote deployment. `/health/live` checks process
  responsiveness; `/health/ready` returns 503 on degraded dependencies/worker.

## Verification

Final local run: **205 tests passed**, with five existing framework deprecation
warnings. The standalone deployment-smoke script also passed against a fresh,
isolated SQLite database (API/OpenAPI import, migrations, UUID-backed file
dataset, simulated execution, and coverage).

Focused tests reproduce the original migration interpolation, password reset,
token lifetime, duplicate IDs, inflated/partial coverage, mock judge, list leak,
and rule-selection defects. Additional tests cover retry lineage and archives,
browser callback isolation, real percent-path migrations, CSV safety, candidate
usage, semantic model cache keys/event-loop responsiveness, and immutable
terminal runs. Existing feature tests were updated only where security or retry
contracts deliberately changed.

An isolated copy of the local legacy database was migrated: its **1 dataset,
4 evaluation runs, and 30 result rows** were preserved. The source database was
not changed. The copy is under the ignored `.pytest_tmp/` directory.

The GitHub Actions workflow runs tests on Python 3.11/3.14 and an isolated
PostgreSQL smoke test that exercises API schema import, migrations, an encoded
password, and a long file-dataset path. It publishes resolved dependency lists.
These CI jobs have not yet been run for this branch. The available local runtime
is Python 3.14; Docker's client is installed but the daemon is stopped. No live
provider, PostgreSQL server, legal RAG application, or Open SaaS application was
contacted during validation.

## Remaining work before trusted shared hosting/release gating

1. **Accounting and admission:** transactional reservations for concurrent
   submissions, a provider-attempt ledger, and explicit unknown-cost reporting.
   Current usage is still inferred from results and is not a billable ledger.
   Retry archives prevent evidence loss but do not automatically repair usage
   accounting. Owners can still change pilot limits; these are not immutable
   commercial entitlements.
2. **Deployment proof:** actually run the PostgreSQL/3.11 CI checks, produce a
   platform-specific dependency lock, test backup restoration, and exercise two
   real browser sessions behind the deployment proxy. The existing dependency
   lower bounds have not been replaced by an untested Windows-derived lock.
3. **Worker ownership:** one API process/worker remains required. Add leases
   before multiple workers, independent schedule dispatch for timely launches
   during long runs, and broader crash-injection tests. Deterministic occurrence
   IDs address duplicate run creation but do not make all schedule/audit writes
   a single transaction.
4. **Identity lifecycle:** accepted invitations, a verified password-reset
   channel, scoped/revocable automation keys, and hosted session/download tests.
   Existing-member password mutation is blocked now; adding a membership still
   does not require invitation acceptance.
5. **Measurement:** human-reviewed calibration, richer evaluator versions and
   provider-generation parameter snapshots, pairwise seed/order-bias analysis,
   uncertainty intervals, slice-specific rules, and verifiable RAG entailment /
   tool-argument/state-change evidence. Exact-match is not a universal quality
   gate, and a compatible experiment is not automatically a calibrated one.
6. **Product boundaries:** scenario database/UI integration with remote side-
   effect ownership; actual notification delivery; full client-data retention
   and archival policies. Current retention does not purge all prompts,
   outputs, datasets, exports, or attempt archives. Project-scoped shares remain
   mutable views; use run-scoped shares for an approved fixed delivery.

The strongest next pilot test is still the review's suggested controlled
regression: pin one dataset and scoring contract, introduce a failure, show the
evidence, fix it, then repeat with an interruption. Use synthetic client data.
