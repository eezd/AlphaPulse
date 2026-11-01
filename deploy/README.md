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

## Three-stage news research

The `news-risk` service runs three model stages with separate durable queues for grouping, package review, event timelines, reading and explicit reprocessing. Original news and revisions remain in their existing tables; AI packages, package versions, memberships and research evidence are stored separately.

1. **Grouping:** the selected Luna model processes every domain and content type, including political news, statements, data, explanations and opinions. Reposts of the same concrete fact share a package with original links and normalized tags. New developments and opinions form separate packages; topic similarity alone is insufficient. One article can contribute to multiple packages.
2. **Package review:** a separately selected model receives the package fact, title, category, tags and content type alongside its original articles, so separate facts within one roundup retain distinct review scopes. It records facts, timeline, claims, verification verdicts, counter-evidence, related packages, opinions and watchpoints. Reading importance (1–5) is independent of risk severity (0–5), so major positive developments can enter the important reading list. Each attempt permits four local searches, eight local reads, two external searches and four external reads, across at most eight tool rounds followed by submission. Context planning reserves the research tool definitions before extracting or compacting long articles. News/package/calendar search and SearXNG are read-only. Search snippets and other AI summaries are leads, not independent verification. Quotes must occur in a source actually supplied or read; insufficient evidence must remain explicit. Tool responses, failed attempts and previously successful versions are retained. A manual retry can reuse saved successful tool responses; old failures cannot requeue a revision that already succeeded.
3. **Reading and trends:** the independently selected advanced model ranks every successful event or independent package of importance 3–5 within the last 72 hours. Large inputs are processed in batches; truncated ranking outputs split into smaller batches, while a truncated single-package output fails explicitly. The complete reading list is retained and paginated, with strict package/version coverage checks. Up to three lower-importance packages from different domains provide expansion reading. Synthesis distinguishes facts from inference and gives market implications, 24–72-hour and 1–4-week trends, conditions, counter-evidence and watchpoints. Ordinary changes aggregate for 15 minutes; importance-5 developments can trigger earlier, with at least 60 seconds between starts. Citation-only changes do not repeat advanced-model calls; changed facts, ratings or verification verdicts still trigger updates. Reposts and analysis execution alone do not advance package source-progress timestamps. Window expiry also triggers an update. Failed refreshes retain the previous successful briefing.

An event is optional and requires at least two distinct packages describing the same developing incident or process. A standalone package remains readable and can join an event later. Review must actually read the other packages before proposing membership. Each package has at most one main event; cross-event links remain separate. Event timelines preserve known, partial and unknown occurrence times, reporting times, verification status, original quotes and exact package versions. Unknown occurrence times are never replaced by publication times. A later event refresh cannot publish over changed member versions. Successful events aggregate their represented packages into one reading entry; typed `(kind, id, revision)` references distinguish events from packages.

Package review can request atomic split/merge corrections. Corrections lock and verify input revisions, require all current source assignments and literal evidence, retain old IDs/history, and preserve the earliest ID on merges. Repeating a correction with the same source hashes and Skill rules is rejected to prevent loops. Corrections are versioned separately; superseded package URLs link to replacements. A pure repost only adds a source; new facts, independent evidence and corrections request another review. Original news rows are never rewritten by AI.

The homepage offers a publication-range reprocessing preview followed by explicit cost confirmation. Starting captures the latest source hashes, three model/effort configurations and published Skill contents; it does not reconstruct a historical information state. Jobs yield to pending realtime work, persist position and failure reasons, and resume after restart. Cancellation stops subsequent articles; an in-flight call and already queued package/event reviews may finish. Pause stops new work. Completion waits for package/event review and a reading snapshot; failures retain prior successful outputs. Failures identify their stage and object. Retrying grouping failures reruns only those news items; package review, event timeline and reading failures resume their own stages without repeating successful grouping. Retries can incur additional model costs. Pausing before a grouping call leaves the job checkpoint unchanged, even when an older successful analysis exists. Source revisions invalidate stale job items; select a new range to include the new revision. Shared packages/events may update even when some of their articles fall outside the selected range.

The first successful enable operation records a permanent start timestamp. Only source publications or source updates after that timestamp enter the pipeline; ingestion of older history does not trigger analysis. Pause/resume retains the timestamp. Historical records remain available to research tools. No trading actions are triggered.

During package review, long-article slices with no facts relevant to the package return empty facts and evidence together and are omitted from compaction. Relevant slices must retain literal source quotes; if every slice is irrelevant, review fails explicitly. Grouping extraction, compaction and final package summaries still require nonempty evidence.

Create or update the three default news Skill drafts in homepage settings, review and publish them in the Skills page's News tab, then bind the grouping, package-analysis and reading Skills. Updating appends a new draft only when the latest content differs; it preserves node IDs, previous versions and published versions. Templates never publish themselves. `POST /api/risk/skill-templates` without a body, or with `refresh_existing=false`, only creates missing templates; the homepage explicitly sends `refresh_existing=true` to prepare updated drafts. News-scoped Skills never enter Chat/trading prompts. Publishing a Skill does not enqueue a historical reanalysis.

All three stages independently select a model and reasoning effort (`low`, `medium`, `high`, `xhigh`, `max`). Model selections support provider model lists and manual IDs. Clearing a stage's effort inherits its provider setting; if both are unset, no effort parameter is sent. Overrides do not change the shared provider. Each attempt retains the selected override, effective effort, rules and model identity; changing either the override or effective settings changes its configuration fingerprint. Test-and-enable makes real validation calls and reports unsupported settings without silently lowering effort; saving paused settings makes no model calls.

The additive `c3d5e7f90123` migration follows `b2c4d6e8f012` and adds three nullable stage-effort columns. Existing settings inherit provider effort. Apply it only with separate migration authorization through the normal deployment lock; it does not update live Skill contents, publish drafts, enable AI or reprocess history.

The subsequent `d4e6f8012345` migration adds events, membership and cross-event links, grouping versions, correction records, reprocessing jobs and active article assignments. It seeds grouping history from existing analyses and pauses AI while leaving raw news and prior successful summaries intact. Both pending migrations require separate authorization under the deployment lock. Update and review the default Skill drafts before enabling the new output contracts; no historical job starts automatically. CI exercises migration and pipeline behavior using synthetic models and a disposable database, including cancelled/failed reprocessing, reposts and correction history.

### Search and subscriptions

Compose includes a digest-pinned SearXNG service on the existing internal Docker network, with no host port. Deployment creates its instance-local settings from `deploy/searxng.yml`, generates its server secret once, and checks `/healthz` through the API container. Pulls, service changes and recovery continue through `scripts/deploy.py` and `.deploy.lock`. `SEARXNG_URL` can select a different internal endpoint; its default is `http://searxng:8080`. External search availability depends on upstream engines and is reported as evidence limitations. Web reads reject private/mixed DNS answers, pin the checked address, recheck redirects and bound decoded response sizes. No external research response is inserted into the original news stream.

The crawler console accepts administrator-managed public RSS/Atom subscriptions, names, intervals and enable/disable controls. New subscriptions are paused, no default feeds are added, and the manager discovers new subscriptions without a restart. Only feed-provided content is stored; a summary is labeled as a summary. Arbitrary private-network feed URLs are rejected.

### Authorized transition only

The additive `b2c4d6e8f012` migration requires separate authorization and pauses the existing news AI setting. It adds package data, evidence, Skill scope, three-stage configuration and the source adapter; it does not delete old results. For the initial transition, stop the old worker under the instance `.deploy.lock` before applying the authorized migration. Using the accepted new-image receipt, preview legacy cleanup with:

```sh
python3 scripts/deploy.py reset-news-ai --build /path/to/accepted-build.json
```

After explicit maintenance authorization, the same command with `--confirm-delete-old-news-ai` stops the news worker and clears only `risk_analyses`, `risk_events` and `risk_snapshots` in one transaction. It refuses if the new pipeline already contains data. It preserves provider configuration, source news, revisions, calendar, Skills, Chat and trading. It uses the deployment lock and does not start the worker or enable models. Deploy through the normal `scripts/deploy.py update` path, then configure, publish and enable from the UI. Do not roll back to pre-scope code with enabled news Skills: pause news AI and disable news-scoped nodes first, since older code does not understand that boundary. Downgrading the additive migration drops the new AI tables and requires an independent data-retention decision.

The homepage displays the latest successful reading briefing and its refresh status, plus searchable package archives with time, importance, risk, tag and category filters. Details include source links, claims, tool evidence and version history. Failed tasks can be explicitly retried by stage. Local acceptance uses synthetic models/search and disposable databases; real model quality, upstream search availability and live deployment need separate operational validation.

## Global economic calendar

The independent `jin10-calendar` service runs Chromium and Jin10's official browser SDK. It collects the macroeconomic calendar only: economic data, important events and exchange holidays. The backend runtime image installs the matching Playwright headless Chromium and OS libraries; no browser is started in the API, official calendar, news, model or trading workers.

After the separately authorized migrations through `a8b1cd465ef3` and deployment, an administrator connects Jin10 in Settings using the Jin10 App QR flow. The account token is encrypted with the existing server encryption key and never returned to the browser. Disconnect retains history and invalidates in-flight writes; reconnect starts a new connection generation. Browser or authentication failures stay visible instead of being recorded as empty days.

The collector prioritizes today, checks yesterday and the next seven days, and fills missing days from 2025-01-01 Asia/Shanghai at a limited pace. A day receives a persistent coverage checkpoint only after all three category responses pass date/structure checks. A checkpoint captured before that local day ended is a preview and must be refreshed for historical completion, including after downtime. Historical dates without a completed snapshot are coverage gaps, not evidence that no events occurred. A restart resumes the first gap. Failed dates retain raw evidence, wait five minutes before retrying, and do not block other dates; browser reconnection preserves scheduling progress. No historical completeness or realtime latency is guaranteed before actual collection finishes.

Each complete daily response withdraws missing records only within its own day and category, retaining their values, raw evidence and a withdrawal revision. Withdrawn records are labeled explicitly and cannot supply fallback fields to an official release. A record that returns under the same source identity is restored with another revision.

Official and Jin10 observations retain separate identities and revisions. The read API merges only verified indicator IDs, definitions, units, reference periods, release stages and exact publication times. Official nonmissing fields win independently; Jin10 fills missing fields and market forecasts with explicit provenance. Unverified definitions remain distinct records. Original and revised previous values remain separate. Forecast observations captured after publication are displayed with that limitation and excluded from historical surprise judgments. Jin10's gold/USD direction flags are not BTC/ETH signals. Tentative times and all-day holidays never display an invented exact midnight time.

CI acceptance runs `scripts/smoke_calendar.py` against the isolated database using public fixtures and synthetic account material. It never starts a browser collector or connects a real Jin10 account. Local unit tests likewise isolate all external I/O. The live database migration, credential import/reconnection, history collection and dev deployment are separate operator actions; ordinary local validation does not authorize them.
