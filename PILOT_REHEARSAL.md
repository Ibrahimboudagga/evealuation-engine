# Pilot rehearsal runbook

This runbook checks the agency workflow without direct database access. It uses only the Gradio UI, documented API calls, and explicit simulated provider models. It is safe to repeat in a new pilot workspace.

## Clean Docker/PostgreSQL environment

1. Start Docker Desktop and confirm `docker info` succeeds.
2. Create a production `.env` from `.env.production.example`. Set a strong `POSTGRES_PASSWORD` and a persistent `WORKSPACE_ENCRYPTION_KEY`; leave `WORKSPACE_API_TOKEN` empty for the first boot.
3. Run `docker compose -p evaluation-pilot-rehearsal up --build -d`. The named Compose project creates its own PostgreSQL volume, so it does not reuse a development database.
4. Wait for `http://localhost:8000/health` to report a reachable PostgreSQL database and a healthy worker. Open `http://localhost:7860`.

## Rehearsal checklist

1. In **Setup Wizard**, create an owner workspace and save the one-time token in the secret manager. Set it as `WORKSPACE_API_TOKEN`, then recreate the UI container.
2. In **Agency Admin Console**, select **Seed Agency Demo**. Confirm the overview shows the demo project, datasets, activation milestones, and a `workspace.bootstrapped` audit event.
3. In **Usage & Limits**, save a small pilot cap, such as `{"runs": 10, "evaluated_cases": 100, "provider_calls": 300, "storage_bytes": 1048576, "report_shares": 5, "active_projects": 3}`. Refresh the Admin Console and confirm the usage/limit values appear.
4. Add an explicit `mock` provider connection. Use model `mock` for a normal simulated run. For the retry check, create a second explicit `mock` connection with model `mock-timeout`. That name is a test-only fault injector: it always raises a simulated timeout and is visibly marked simulated.
5. Create a template using the normal `mock` connection, create a daily schedule against a demo dataset version, and wait for the worker. In **Scheduled Evaluations**, confirm a history entry has a generated run ID; in **Worker Health**, confirm queue and schedule counters are healthy.
6. Launch a template using `mock-timeout`. In **View Results**, confirm it returns to `queued` with a next attempt timestamp and a transient error. After its configured attempts are exhausted, confirm the run is `failed` and the audit history shows `run.claimed`, `run.retry_scheduled`, and `run.retry_exhausted`.
7. Launch a normal template run, choose **Cancel** while it is queued, and confirm its status becomes `interrupted`. Verify the audit history includes `run.cancellation_requested` and `run.cancelled`.
8. Refresh **Agency Admin Console** and verify worker health, usage, billing/trial state, notifications, activation funnel, and recent audit events. The console intentionally contains no provider credentials or report tokens.
9. In **Client Reports**, export the completed normal run as HTML, create a one-hour shared report, and open the returned link in a private browser window. Revoke the link once confirmed.

## Expected evidence

- `/health` reports PostgreSQL reachability, worker heartbeat, queue depth, retry and failure counts, and overdue schedule count.
- `/audit-events` records the demo seed, limit update, schedule creation/trigger, retries, cancellation, export, and report share.
- `/workspace/usage` shows aggregate counts only.
- `/workspace/activation` shows milestone timestamps and counts only.
- The shared report is read-only and never exposes provider credentials.

## Local verification

The repository test suite covers the same lifecycle with isolated databases: queue retry and cancellation, schedule triggering, usage limits, report sharing, activation, and the owner Admin Console. Run `python -m pytest -q` before a pilot deployment.
