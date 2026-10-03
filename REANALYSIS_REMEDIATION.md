# Reanalysis response: controlled-pilot hardening

Feedback reviewed: `evealuation_engine_reanalysis_2026-09-29.md`, covering
revision `c5d6a6c`. Implemented on `feature/reanalysis-pilot-hardening`.
Last verified locally: 3 October 2026 (Africa/Tunis).

## Assessment

The feedback is accurate about the product's position: the repository is a
credible controlled-pilot foundation, not a general multi-tenant SaaS or an
independent evaluation authority. The document was treated as review evidence,
not as authorization to choose legal terms, buy infrastructure, certify
security, or claim scientific validity.

This change addresses concrete Phase 0 defects that can be enforced and tested
inside the repository. Larger research, identity-provider, billing-ledger,
distributed-worker, and hosted-infrastructure programs remain explicit work.

## Implemented in this remediation

| Confirmed finding | Implemented behavior | Evidence and limit |
| --- | --- | --- |
| A workspace could lose its final owner | Owner memberships are locked for role/removal decisions; demoting, removing, or overwriting the last owner is rejected | PostgreSQL row locks serialize competing owner mutations; SQLite remains for single-process development |
| Password sign-in had no abuse control | Failed sign-ins use one atomically updated, persistent HMAC-keyed throttle bucket for known and unknown emails; lockout survives process restart and success clears prior failures | Production requires a non-placeholder `AUTH_THROTTLE_SECRET`; invitations, recovery, MFA and SSO remain open |
| Editors and comparison callers could shop release thresholds | Projects now store immutable model/scenario policy revisions. Editors may draft; owners approve. Runs bind to the latest approved revision ID and SHA-256 at submission. Query-time overrides return 422 | Existing embedded rules remain usable for local/unmanaged analysis; legacy project runs without approved binding are officially inconclusive |
| Baseline governance was weak | Marking a workspace baseline is owner-only. Official comparison loads rules from the bound approved database revision and verifies its hash/config snapshot | Pairwise evaluation still lacks this full baseline policy flow |
| Imported scenario evidence could report an official pass | Scenario API payloads, UI labels, exports, shares and comparisons expose analytical `quality_decision` separately; official `decision` remains `inconclusive` while provenance is unverified | Trusted signed capture/collector is not implemented |
| Retention removed only links and audit rows | Retention can be explicitly enabled for terminal model/pairwise runs, results, retry archives, scenario evidence/suites and unreferenced inactive versions. A legal hold freezes shares, audit evidence and customer content, and omitted PUT fields preserve existing safeguards | Active versions, workspace templates, CLI artifacts and external backups remain operator-managed |
| Project deletion left orphaned customer records | Owners get a non-mutating deletion preview and a legal-hold-aware, exact-name-confirmed project customer-data purge. Deletion and its content-free audit receipt commit atomically | Workspace-scoped templates are not project-linked and are excluded rather than falsely claimed as deleted |
| Notification settings claimed delivery | Status now says `preferences_saved_delivery_unavailable` until a real outbox/adapter exists | No email or Slack delivery is claimed |
| CI had no security gates | A separate least-privilege workflow adds CodeQL, `pip-audit`, full-history Gitleaks, Trivy source/misconfiguration scanning and built-image scanning, with a stable `Security gate` | Local YAML/Gitleaks checks passed; remote Actions and Docker/Trivy execution still require observation |

Migration `20260929_19` creates persistent throttle and policy-revision tables,
adds nullable policy links to model/scenario runs, and adds workspace retention
and legal-hold state. Historical records remain unchanged and policy links stay
null; the migration does not invent approval evidence for old runs.

## Governed release workflow

1. An owner or editor creates a policy draft under a client project.
2. A workspace owner approves that immutable revision.
3. A future model or scheduled run resolves the latest approved revision and
   stores its ID, version and SHA-256 in the run configuration and foreign key.
   Manual retry descendants preserve that binding.
4. An owner selects a completed baseline.
5. Comparison uses the bound policy. API/UI callers cannot supply replacement
   thresholds after seeing results.

Main endpoints:

```text
POST /projects/{project_id}/release-policy-revisions
GET  /projects/{project_id}/release-policy-revisions
POST /projects/{project_id}/release-policy-revisions/{revision_id}/approve
PUT  /runs/{run_id}/baseline
GET  /runs/{run_id}/comparison?baseline_run_id=...
```

Model rules use the existing evaluator/slice `ReleaseRules` contract. Scenario
rules cover coverage, minimum valid cases, pass-rate floor, and maximum drop.
Approval creates audit events containing revision/version/hash metadata without
copying the policy body into the audit trail.

## Retention and deletion workflow

Retention is safe by default: customer-content retention is disabled until an
owner explicitly enables it. With no legal hold, applying retention removes
expired shares and old audit events. When content retention is enabled, it also
removes old terminal run/result evidence and retry archives, old imported
scenario evidence, empty old scenario suites, and inactive dataset versions no
longer referenced by runs or schedules. An active legal hold freezes every one
of those deletion categories.

For client offboarding, an owner can preview and then confirm a project purge:

```text
GET    /projects/{project_id}/customer-data/deletion-preview
DELETE /projects/{project_id}/customer-data
       {"confirm_project_name":"exact project name"}
```

The purge deletes project grants, report snapshots, schedules/history, model and
pairwise results/runs, retry archives, scenario evidence/suites, dataset
versions/datasets, and policy revisions in one transaction. The resulting
content-free count receipt is written in that same transaction. A legal hold
returns 409.

## Verification

Runtime: Python 3.14.6 in the existing project virtual environment.

- Focused regression suite covering identity, retention, policy governance,
  migrations, scenarios, queues, and prior stabilization: **52 passed**.
- Full suite: **268 passed**, with five existing FastAPI/Starlette deprecation
  warnings, in 55.63 seconds.
- `pip check`: **No broken requirements found**.
- Alembic local database: **`20260929_19 (head)`**.
- Fresh, copied-legacy, and populated rev18 migration tests cover the new
  tables/columns, preserve historical records, confirm old policy links remain
  null, and exercise SQLite with foreign-key enforcement enabled.
- Python compilation and `git diff --check`: passed.
- Security workflow YAML and pinned-action structure were validated locally.
  Docker Desktop was stopped, so current local Gitleaks/Trivy/container
  execution is not claimed; the remote workflow remains to be observed.

## Remaining work, in priority order

1. **Identity lifecycle:** expiring invitations, verified password recovery,
   MFA/external identity, scoped service accounts, and database-enforced tenant
   policies.
2. **Scientific validity:** human labels and adjudication, judge-human
   agreement, repeated-judge/order-robustness studies, prompt-injection test
   sets, confidence intervals, and minimum-sample policies.
3. **Trusted scenario capture:** signed or mTLS-bound evidence, sequence and
   completeness verification, egress controls, remote status/cancellation, and
   immutable raw evidence.
4. **Billing-grade usage:** immutable provider-attempt/token/cost ledger plus
   transactional quota reservation/reconciliation under concurrency.
5. **Distributed operations:** leased/fenced claims, separate workers,
   horizontal-safe restart recovery, notification outbox/delivery, real staging
   deployment and rollback rehearsal.
6. **Reproducible supply chain:** reviewed hashed dependency locks, digest-pinned
   base/service images, multi-architecture builds, and pinned embedding/model
   artifacts. Security scanning reduces risk but does not make mutable inputs
   reproducible.
7. **Commercial decisions:** repository license, client terms, privacy/DPA,
   support/SLA, vulnerability disclosure, data residency and subprocessors.
   These require owner/counsel decisions and were not invented by this change.

The correct current claim is a controlled agency pilot with stronger release
governance and operational safeguards. Production SaaS readiness still depends
on the remaining evidence and infrastructure work above.
