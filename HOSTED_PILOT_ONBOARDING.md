# Agency hosted-pilot onboarding

1. Copy `.env.production.example` to `.env`, set `POSTGRES_PASSWORD`, generate `WORKSPACE_ENCRYPTION_KEY`, and keep both in the agency secret manager.
2. Start the pilot with `docker compose up --build`.
3. Open `http://localhost:7860`, complete **Setup Wizard**, and store the one-time owner token.
4. Set that value as `WORKSPACE_API_TOKEN` in `.env`, then run `docker compose up -d --force-recreate ui`.
5. In **Projects**, select **Seed Agency Demo**. In **Provider Connections**, create an explicit `mock` connection with model `mock` and **Intentional unauthenticated endpoint** checked.
6. Use the demo dataset and mock connection to launch an evaluation. Confirm its simulated label, coverage, and failures.
7. In **Client Reports**, create an expiring client link. Verify it in a private browser window, then revoke it once the pilot check is complete.
8. Check `http://localhost:8000/health`; production must show `status: ok`. Schedule PostgreSQL backups using the linked guidance and test restoration before uploading client data.

The Docker Compose database volume is durable storage, not a backup. Use managed PostgreSQL backups or `pg_dump` to an access-controlled off-host location.
