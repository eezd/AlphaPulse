# AlphaPulse CI/CD

Gitea (`https://git.134333.xyz/eezd/AlphaPulse`) is the source repository; GitHub is a public, one-way mirror of all branches, tags and history. Develop on `dev`. `main` has independent checks and no deployed instance or release workflow.

## Checks and images

Both branch workflows run Python 3.13/uv checks, Node 24/pnpm 12.11.2 checks, Linux amd64 image builds and acceptance against temporary PostgreSQL/Redis. Acceptance runs on an internal Docker network with synthetic data; no worker or external model/trading service starts there. Foreign PRs do not enter the shared Docker runner.

Only a `dev` push publishes the accepted images:

- `git.134333.xyz/eezd/dev/backend/alphapulse:YYYYMMDD-xxxxxxxx`
- `git.134333.xyz/eezd/dev/web/alphapulse:YYYYMMDD-xxxxxxxx`

The date comes from the commit's committer timestamp in UTC, and the suffix is the first eight characters of its full SHA. Existing versions are immutable. Images are public on Gitea only; private configuration and data are excluded. `/api/health` and `/version.json` identify the deployed API and web revision. `build.json` is the CI receipt, not a tracked source file.

## The single data-bearing instance

`/docker/gitea/dev/AlphaPulse` contains the runtime Compose configuration, `.env`, digest selection, deployment receipts and `.deploy.lock`. The repository-root `.env` points to that sole private configuration. The Compose project is `alphapulse-dev`; `AlphaPulse-dev` is the web container, and the other five service names append `-api`, `-crawler`, `-market`, `-calendar` or `-trader`.

This project's DEV intentionally uses the existing database, Redis DB 14 (`ap:*` only), model credentials and Demo trading configuration. The user authorized restoring all six services. It is the only instance, served at `https://alpha.134333.xyz` through `127.0.0.1:21005`. CI never uses these data for testing. Do not start a second Compose project against this configuration.

Automatic and manual updates use the same command and persistent lock:

```sh
python3 scripts/deploy.py update --build /path/to/accepted/build.json
```

The command checks image identities, current dev SHA and database migration compatibility before updating. It waits for active Chat/trading analysis, records pending state, then starts API/web before the workers. An outdated dev build is skipped. A pending deployment blocks updates. Do not invoke `docker compose up` directly to bypass the lock.

A failed update with a previous deployment restores the prior images/configuration while retaining data. With no previous deployment, the pending marker remains for diagnosis. Recovery and rollback are explicit operator commands:

```sh
python3 scripts/deploy.py recover
python3 scripts/deploy.py rollback
```

Neither command restores database contents or resets trading orders. Schema migrations require separate authorization and are never part of CI auto-deployment. No scheduled backup is installed.

## Credentials and mirroring

Gitea secrets are `ALPHAPULSE_GITEA_TOKEN` (image publication) and `ALPHAPULSE_GITHUB_SSH_KEY` (a write-enabled deploy key restricted to the GitHub mirror). Ordinary checks and the deployment job do not receive publication secrets. Credentials are removed from job-local files on exit.

Git mirroring is independent of build results. Push/create/delete events, hourly reconciliation and manual dispatch use the same `.mirror.lock` in `/docker/gitea/mirrors/AlphaPulse`. Only business heads/tags are managed; internal PR references are excluded. Missing or incomplete source reads fail rather than imply deletion. GitHub is not an independent development source. GitHub Actions is disabled on the mirror.

Only resources labeled for the current CI attempt are cleaned; there is no global Docker prune. The existing runner registration, capacity and other projects' mount allowances are preserved.
