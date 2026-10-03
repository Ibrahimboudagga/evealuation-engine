# CI/CD pipeline

The repository uses a delivery workflow in `.github/workflows/tests.yml` and a
security workflow in `.github/workflows/security.yml`. They validate every pull
request to `main`. The delivery workflow repeats its checks for pushes to `main`
and `v*` tags, while the security workflow also runs on `main` and every Monday.

## Pipeline stages

| Stage | Runs for | Purpose |
| --- | --- | --- |
| Python test matrix | Pull requests, `main`, `v*`, manual runs | Runs the full suite on Python 3.11, 3.12, and 3.14, checks installed dependency compatibility, and retains JUnit results |
| PostgreSQL rehearsal | Pull requests, `main`, `v*`, manual runs | Applies the current migrations to an empty PostgreSQL 16 database, executes a mock run, backs it up, restores into a second database, and compares row counts and content hashes |
| Production Compose smoke | After both validation stages | Builds the PostgreSQL, API, and Gradio stack, verifies the non-root API image, and probes API readiness plus the UI |
| CI gate | Every workflow run | Exposes one stable required status check and fails unless the matrix, database rehearsal, and Compose smoke all succeeded |
| Container publication | Successful `main` pushes and `v*` tags | Pushes a candidate image, smoke-tests that exact digest, promotes it to release tags, records an SBOM and maximal build provenance, and creates a GitHub artifact attestation |

The separate security workflow adds these blocking checks:

| Security check | Scope | Failure policy |
| --- | --- | --- |
| CodeQL | Python application code and GitHub Actions workflows | Uploads extended security results to GitHub code scanning; configure a code-scanning ruleset to block newly introduced high or critical alerts |
| `pip-audit` | The dependency graph resolved from `requirements.txt` | Fails when the Python Packaging Authority advisory data reports a known vulnerability |
| Gitleaks | Complete Git history, including pull-request commits | Fails when a credential-like secret is detected; the read-only checkout is scanned in a network-isolated, digest-pinned container |
| Trivy filesystem scan | Dependency manifests and repository configuration | Fails on fixable high or critical vulnerabilities or unsafe configuration |
| Trivy container scan | The production image's operating-system and Python packages | Fails on fixable high or critical vulnerabilities |
| Security gate | All checks above | Exposes one stable branch-protection status and fails if any scan did not succeed |

All security actions are pinned to reviewed commit SHAs, and the Gitleaks image
is pinned by digest. Scans run on the unprivileged `pull_request` event and
receive no application secrets. Only the CodeQL job gets
`security-events: write`, which GitHub requires to upload its results; every
other security job uses the workflow's `contents: read` default.

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
2. Require both stable **CI gate** and **Security gate** status checks in the
   `main` branch protection rule. They cover runtime verification and the
   security scans described above.
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
- protect `main`, require a pull request, require branches to be current with
  `main`, require both gate checks, dismiss stale approvals, and block force
  pushes;
- enable GitHub code scanning and add a code-scanning ruleset that rejects new
  high or critical CodeQL alerts; the CodeQL action uploads findings but GitHub
  rulesets decide whether a finding blocks a merge;
- keep production deployment credentials in a protected GitHub Environment when
  a hosting target is added;
- retain the workflow's minimal permissions instead of granting global write
  access.

Gitleaks is held at scanner version 8.30.0 and runs without a GitHub token or a
vendor license. The three entries in
`.gitleaksignore` identify exact historical fingerprints for deterministic,
non-production Fernet keys used by tests and CI; the path is not broadly
excluded, so any newly committed credential-like value still fails the scan.

Dependabot checks Python, GitHub Actions, and Docker dependencies weekly through
`.github/dependabot.yml`. Its pull requests follow the same validation pipeline.

Every external action is pinned to a full commit SHA, with the reviewed major
version retained in a comment for Dependabot. The base image, PostgreSQL service,
and Python requirements are not yet locked by digest or exact version. The
published image digest is immutable and is tested before promotion, but rebuilding
the same source commit later can resolve different upstream dependencies until
those inputs are locked.

The vulnerability jobs currently ignore findings for which the upstream project
offers no fix and fail on fixable high or critical findings. Unfixed findings
remain a release-risk review item; change `ignore-unfixed` only through a reviewed
policy update. Because `requirements.txt` declares lower bounds instead of a
fully hashed lock, the dependency audit checks what resolves at scan time. Adding
a reviewed, hashed production lock remains necessary for reproducible dependency
selection.

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
