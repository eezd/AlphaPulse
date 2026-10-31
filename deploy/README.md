# AlphaPulse CI/CD

Gitea (`https://git.134333.xyz/eezd/AlphaPulse`) is the source repository; GitHub is a public, one-way mirror of all branches, tags and history. Develop on `dev`. `main` has independent checks and no deployed instance or release workflow.

## Checks and images

Both branch workflows run Python 3.13/uv checks, Node 24/pnpm 12.11.2 checks, Linux amd64 image builds and acceptance against temporary PostgreSQL/Redis. Acceptance runs on an internal Docker network with synthetic data; no worker or external model/trading service starts there. Foreign PRs do not enter the shared Docker runner.

Only a `dev` push publishes the accepted images:

- `git.134333.xyz/eezd/dev/backend/alphapulse:YYYYMMDD-xxxxxxxx`
- `git.134333.xyz/eezd/dev/web/alphapulse:YYYYMMDD-xxxxxxxx`

The date comes from the commit's committer timestamp in UTC, and the suffix is the first eight characters of its full SHA. Existing versions are immutable. Images are public on Gitea only; private configuration and data are excluded. `/api/health` and `/version.json` identify the deployed API and web revision. `build.json` is the CI receipt, not a tracked source file.

## The single data-bearing instance

`/docker/gitea/dev/AlphaPulse` contains the runtime Compose configuration, `.env`, digest selection, deployment receipts and `.deploy.lock`. The repository-root `.env` points to that sole private configuration. The Compose project is `alphapulse-dev`; `AlphaPulse-dev` is the web container, and the other application containers append their service name (`-api`, `-crawler`, `-market`, `-calendar`, `-trader`, or the new `-news-risk`).

This project's DEV intentionally uses the existing database, Redis DB 14 (`ap:*` only), model credentials and Demo trading configuration. The user authorized restoring all six services. It is the only instance, served at `https://alpha.134333.xyz` through `127.0.0.1:21005`. CI never uses these data for testing. Do not start a second Compose project against this configuration.

Automatic and manual updates use the same command and persistent lock:

```sh
python3 scripts/deploy.py update --build /path/to/accepted/build.json
```

The command checks image identities, current dev SHA and database migration compatibility before updating. It waits for active Chat/trading analysis, records pending state, then starts API/web before the workers. An outdated dev build is skipped. A pending deployment blocks updates. Preparation is recorded before pausing the trader; recovery resumes a paused trader without restarting other services if updates have not begun. Do not invoke `docker compose up` directly to bypass the lock.

A failed update with a previous deployment restores the prior images/configuration while retaining data. With no previous deployment, the pending marker remains for diagnosis. Recovery and rollback are explicit operator commands:

```sh
python3 scripts/deploy.py recover
python3 scripts/deploy.py rollback
```

Neither command restores database contents or resets trading orders. Schema migrations require separate authorization and are never part of CI auto-deployment. No scheduled backup is installed.

## Credentials and mirroring

Gitea secrets are `ALPHAPULSE_GITEA_TOKEN` (image publication) and `ALL_GITHUB_TOKEN` (the account-level GitHub PAT classic with repository write permission; no repository-level override). Ordinary checks and the deployment job do not receive publication secrets. The GitHub token is read from the task environment, never stored in Git URLs or credential files. The trusted mirror task verifies the GitHub identity and repository write permission before synchronization.

Git mirroring is independent of build results. Push/create/delete events, hourly reconciliation and manual dispatch use the same `.mirror.lock` in `/docker/gitea/mirrors/AlphaPulse`. Only business heads/tags are managed; internal PR references are excluded. Missing or incomplete source reads fail rather than imply deletion. GitHub is not an independent development source. GitHub Actions is disabled on the mirror.

Only resources labeled for the current CI attempt are cleaned; there is no global Docker prune. The existing runner registration, capacity and other projects' mount allowances are preserved.

## News risk research

The independent `news-risk` service uses the backend image and never invokes trading tools. Its persistent settings default to disabled. The homepage can reuse an existing provider's API endpoint/credentials with a separate model ID. Only “test and enable” starts real model calls and the backlog beginning at 2026-10-26 00:00 Asia/Shanghai. This does not modify Chat or trading providers.

The additive `c4d7e9f021ab` migration must be reviewed and separately authorized before deployment; the schema guard intentionally rejects deployment until migration is complete. Do not apply it to the live database as part of tests. Updates add a seventh service; rollback to the previous six-service Compose removes the new worker under the deployment lock, without dropping risk tables or deleting analysis history.

Risk grades are zero (no explicit risk found) through five stars. Pending, failed and insufficient information are distinct states. Article evidence, model identity and timestamps remain available in event history. The rolling dashboard windows use publication/progress timestamps; retrospective analysis is not a historical forecast. Model/market outages retain the prior outlook with an outdated marker. No event classification changes positions or order execution.

Candidate retrieval retains the article's previous event and ranks other recent events by headline relevance and severity. Each dashboard window selects up to 100 events independently. Context packing includes source text and the tool schema; oversized articles fail individually while other articles stay queued. News details refresh pending analysis every 15 seconds while visible and stop after completion or leaving the page. Deleting an enabled news provider returns 409; pausing analysis allows deletion and clears the saved provider reference.

## Global economic calendar

The independent `jin10-calendar` service runs Chromium and Jin10's official browser SDK. It collects the macroeconomic calendar only: economic data, important events and exchange holidays. The backend runtime image installs the matching Playwright headless Chromium and OS libraries; no browser is started in the API, official calendar, news, model or trading workers.

After the separately authorized migrations through `a8b1cd465ef3` and deployment, an administrator connects Jin10 in Settings using the Jin10 App QR flow. The account token is encrypted with the existing server encryption key and never returned to the browser. Disconnect retains history and invalidates in-flight writes; reconnect starts a new connection generation. Browser or authentication failures stay visible instead of being recorded as empty days.

The collector prioritizes today, checks yesterday and the next seven days, and fills missing days from 2025-01-01 Asia/Shanghai at a limited pace. A day receives a persistent coverage checkpoint only after all three category responses pass date/structure checks. Historical dates with no checkpoint are coverage gaps, not evidence that no events occurred. A restart resumes the first gap. Failures retain raw responses without advancing coverage. No historical completeness or realtime latency is guaranteed before actual collection finishes.

Official and Jin10 observations retain separate identities and revisions. The read API merges only verified indicator IDs, definitions, units, reference periods, release stages and exact publication times. Official nonmissing fields win independently; Jin10 fills missing fields and market forecasts with explicit provenance. Unverified definitions remain distinct records. Original and revised previous values remain separate. Forecast observations captured after publication are displayed with that limitation and excluded from historical surprise judgments. Jin10's gold/USD direction flags are not BTC/ETH signals. Tentative times and all-day holidays never display an invented exact midnight time.

CI acceptance runs `scripts/smoke_calendar.py` against the isolated database using public fixtures and synthetic account material. It never starts a browser collector or connects a real Jin10 account. Local unit tests likewise isolate all external I/O. The live database migration, credential import/reconnection, history collection and dev deployment are separate operator actions; ordinary local validation does not authorize them.
