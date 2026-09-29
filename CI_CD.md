# CI/CD pipeline

The repository uses one gated GitHub Actions workflow in
`.github/workflows/tests.yml`. It validates every pull request to `main`, repeats
the same checks for pushes to `main` and `v*` tags, and continuously delivers a
deployable container after all verification stages pass.

## Pipeline stages

| Stage | Runs for | Purpose |
| --- | --- | --- |
| Python test matrix | Pull requests, `main`, `v*`, manual runs | Runs the full suite on Python 3.11, 3.12, and 3.14, checks installed dependency compatibility, and retains JUnit results |
| PostgreSQL rehearsal | Pull requests, `main`, `v*`, manual runs | Applies the current migrations to an empty PostgreSQL 16 database, executes a mock run, backs it up, restores into a second database, and compares row counts and content hashes |
| Production Compose smoke | After both validation stages | Builds the PostgreSQL, API, and Gradio stack, verifies the non-root API image, and probes API readiness plus the UI |
| CI gate | Every workflow run | Exposes one stable required status check and fails unless the matrix, database rehearsal, and Compose smoke all succeeded |
| Container publication | Successful `main` pushes and `v*` tags | Pushes a candidate image, smoke-tests that exact digest, promotes it to release tags, records an SBOM and maximal build provenance, and creates a GitHub artifact attestation |

The publish job receives `packages: write`, `id-token: write`, and
`attestations: write` only after the read-only validation jobs succeed. Pull
requests never receive registry write access.

## Image names and tags

Images are published to:

```text
ghcr.io/<repository-owner>/<repository-name>
```

Every successful delivery gets a commit-addressed `sha-<short-commit>` tag plus
an immutable digest. A push to the default branch also updates `edge`. A tag
such as `v1.4.0` produces `v1.4.0`, `1.4.0`, `1.4`, `latest`, and the commit tag.

Promote by digest so the exact tested artifact cannot move:

```bash
docker pull ghcr.io/ibrahimboudagga/evealuation-engine@sha256:<digest>
```

GitHub initially publishes a package as private unless repository or
organization settings say otherwise. Configure the target host with permission
to read that package before deployment.

## Release flow

1. Open a pull request against `main`.
2. Require the stable **CI gate** status check in the `main` branch protection
   rule. It cannot pass unless every matrix, PostgreSQL, and Compose job passed.
3. Merge only when the checks pass. The merge commit publishes `edge` and a
   commit-addressed tag; use the reported digest for immutable promotion.
4. For a versioned release, create an annotated `vMAJOR.MINOR.PATCH` tag from a
   verified `main` commit and push it.
5. Deploy the published digest to the target environment using that platform's
   secret manager and rollout mechanism.
6. Verify `/health/live` and `/health/ready` after promotion.

Example release commands:

```bash
git switch main
git pull --ff-only
git tag -a v1.0.0 -m "Evaluation Engine v1.0.0"
git push origin v1.0.0
```

## Required GitHub configuration

No provider credentials or deployment secrets are required by the pipeline.
GitHub's scoped `GITHUB_TOKEN` authenticates GHCR publication.

Recommended repository settings:

- ensure organization and repository policy permits the publish job's scoped
  `packages: write` permission;
- allow artifact attestations for the repository;
- protect `main`, require a pull request, require the pipeline checks, dismiss
  stale approvals, and block force pushes;
- keep production deployment credentials in a protected GitHub Environment when
  a hosting target is added;
- retain the workflow's minimal permissions instead of granting global write
  access.

Dependabot checks Python, GitHub Actions, and Docker dependencies weekly through
`.github/dependabot.yml`. Its pull requests follow the same validation pipeline.

Every external action is pinned to a full commit SHA, with the reviewed major
version retained in a comment for Dependabot. The base image, PostgreSQL service,
and Python requirements are not yet locked by digest or exact version. The
published image digest is immutable and is tested before promotion, but rebuilding
the same source commit later can resolve different upstream dependencies until
those inputs are locked.

## Delivery boundary

This pipeline implements continuous delivery to a trusted container registry.
It does not invent a production host or run a platform-specific deployment
command. Once the target is selected, add a deployment job that:

1. references the published digest from the `publish` job;
2. uses a protected GitHub Environment and short-lived platform credentials;
3. runs the platform's rollout command;
4. waits for liveness and readiness;
5. records or executes the platform's rollback command when verification fails.

Keep application secrets out of image-build arguments. Runtime configuration
must supply `DATABASE_URL`, `WORKSPACE_ENCRYPTION_KEY`, `BOOTSTRAP_SECRET`, and
any provider credentials through the target environment's secret manager.
