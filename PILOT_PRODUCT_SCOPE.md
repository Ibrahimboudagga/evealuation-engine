# Paid pilot product scope

## Offer

The first paid pilot evaluates one versioned AI feature or one bounded agent workflow in a customer's staging environment. The buyer is an agency engineering or delivery lead who needs evidence that a client release improved quality without reducing coverage, violating an explicit workflow rule, or exceeding an agreed latency or cost budget.

The pilot supports a generic HTTPS adapter. For every dataset case, the engine sends a stable idempotency key, task input, structured context, and optional turn inputs. The staging bridge returns the versioned evidence contract documented in `SCENARIO_EVALUATION.md`.

## Customer inputs

- One staging endpoint and a credential supplied through deployment secrets.
- A versioned suite of 10–1,000 representative JSONL cases.
- Deterministic acceptance checks, required and forbidden tools, safety constraints, and budgets.
- Target application, prompt, model, and build versions.
- A human-labelled calibration set when judge-based scoring is used.

## Customer outputs

- Per-case task outcome, trajectory, safety, and operational results linked to evidence checks.
- Coverage and explicit generation/evaluation failure counts.
- Candidate-versus-baseline release decision using an owner-approved policy.
- Latency, token, and measured or estimated cost evidence when the staging bridge supplies it.
- An exportable report with configuration, limitations, failures, and retained provenance.

## Success criteria

The pilot is successful when the same immutable suite evaluates a baseline and candidate build, all required evidence is present, the configured release decision is reproducible, judge calibration is reported where applicable, and an agency reviewer can explain every failed or inconclusive case from the retained record.

## Claims and boundaries

The product verifies declared checks against supplied or live-captured evidence. Imported evidence remains explicitly unverified because the engine did not observe the target execution. Citation identity checks do not establish semantic or legal truth. Exported authorization decisions do not constitute an independent access-control audit. Judge agreement applies only to the labelled calibration set and recorded judge version. The pilot does not claim to be a complete agent auditor, penetration test, legal review, or proof that an undeclared side effect did not occur.

## Data handling

Workspace and project authorization applies to suites, runs, exports, and shares. Provider and endpoint credentials remain in environment configuration or encrypted provider connections and are excluded from datasets, logs, analytics, and reports. Recognizable secrets are rejected from scenario suites and redacted from retained evidence. Owners can delete retained scenario runs and suites; shares are revocable and expiring. Pilot contracts must state the deployment location, retention period, subprocessors/model providers, deletion window, and support contact before customer data is accepted.

Synthetic or redacted cases must be used until cross-workspace isolation, export/delete, restore, and incident procedures have been rehearsed in the deployment that will host the pilot.
