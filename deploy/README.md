# AlphaPulse CI/CD

Gitea (`https://git.134333.xyz/eezd/AlphaPulse`) is the source repository; GitHub is a public, one-way mirror of all branches, tags and history. Develop on `dev`. `main` has independent checks and no deployed instance or release workflow.

## Checks and images

Both branch workflows run Python 3.13/uv checks, Node 24/pnpm 12.11.2 checks, Linux amd64 image builds and acceptance against temporary PostgreSQL/Redis. Acceptance runs on an internal Docker network with synthetic data; no worker or external model/trading service starts there. Foreign PRs do not enter the shared Docker runner.

Only a `dev` push publishes the accepted images:

- `git.134333.xyz/eezd/dev/backend/alphapulse:YYYYMMDD-xxxxxxxx`
- `git.134333.xyz/eezd/dev/web/alphapulse:YYYYMMDD-xxxxxxxx`

Deployment, migration commands, Compose interpolation and CI outputs use these version tags. Accepted digests remain internal receipt metadata: pulls and startup verify the tag's content, full commit and version before use; an overwritten tag fails validation. SearXNG uses `docker.io/searxng/searxng:2026.10.30-ffa88e3ba`, with its accepted content checked internally as well. Historical digest-only application receipts select the corresponding version tag without discarding their identity checks. Recovery converts the known historical SearXNG digest to its verified tag and rejects unknown digest references.

The date comes from the commit's committer timestamp in UTC, and the suffix is the first eight characters of its full SHA. Existing versions are immutable. Images are public on Gitea only; private configuration and data are excluded. `/api/health` and `/version.json` identify the deployed API and web revision. `build.json` is the CI receipt, not a tracked source file.

## The single data-bearing instance

`/docker/gitea/dev/AlphaPulse` contains the runtime Compose configuration, `.env`, version-tag selection, deployment receipts (which retain accepted digests for identity checks) and `.deploy.lock`. The repository-root `.env` points to that sole private configuration. The Compose project is `alphapulse-dev`; `AlphaPulse-dev` is the web container, and the other application containers append their service name (`-api`, `-crawler`, `-market`, `-calendar`, `-trader`, or the new `-news-risk`).

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

### Start and stop this instance

```sh
bash /docker/gitea/dev/AlphaPulse/start.sh
bash /docker/gitea/dev/AlphaPulse/stop.sh
```

These Bash entrypoints use the installed `instance.py` controller (host Python 3 and
Docker Compose are required), share `.deploy.lock` with CD and fail immediately when
the lock is occupied. Start rejects a pending deployment; stop remains available
while pending. Both use the explicit `alphapulse-dev` identity and instance
`images.env`, ignoring caller `COMPOSE_*` and `ALPHAPULSE_*` overrides.

Start checks the verified `deployment.json` receipt, Compose image selection and all
local image identities before changing containers. It never builds or pulls. It
waits up to 120 seconds for services to run or become healthy, then checks the nine
services and their running image IDs before reporting success. A failed start can
leave some services running; inspect the reported failure before retrying. Stop
retains containers, networks, volumes and data. Neither operation changes stored AI
or trading settings; starting resumes workers according to those settings.

Deployment installs these three versioned files and executable Bash permissions.
For an existing instance, install only the entrypoints without starting services:

```sh
python3 scripts/deploy.py install-entrypoints
```

`entrypoints.json` records installed checksums. Installation accepts unchanged
managed files and the original skill templates, and refuses to overwrite local
customizations. Review such customizations before updating; no other instance or
global skill template is changed.

## Credentials and mirroring

Gitea secrets are `ALPHAPULSE_GITEA_TOKEN` (image publication) and `ALL_GITHUB_TOKEN` (the account-level GitHub PAT classic with repository write permission; no repository-level override). Ordinary checks and the deployment job do not receive publication secrets. The GitHub token is read from the task environment, never stored in Git URLs or credential files. The trusted mirror task verifies the GitHub identity and repository write permission before synchronization.

Git mirroring is independent of build results. Push/create/delete events, hourly reconciliation and manual dispatch use the same `.mirror.lock` in `/docker/gitea/mirrors/AlphaPulse`. Only business heads/tags are managed; internal PR references are excluded. Missing or incomplete source reads fail rather than imply deletion. GitHub is not an independent development source. GitHub Actions is disabled on the mirror.

Only resources labeled for the current CI attempt are cleaned; there is no global Docker prune. The existing runner registration, capacity and other projects' mount allowances are preserved.

## Two-stage news research

A news item has one stable package, identified by `news_items.id`. Reposts remain separate packages; a digest is never split into new packages. All original news, revisions and legacy AI tables are retained.

1. **Keyword indexing and event assignment:** read the complete news once to persist canonical TAGs, literal aliases and original supporting quotes. This stage generates no summaries or ratings. Assignment is a separate persisted subtask, so a classification retry reuses the keyword index. Packages and events have many-to-many membership; a digest can support several distinct events, and a single package may start an event. Event candidates cover all time, bounded to sixty lexical matches. Shared names or sectors alone do not establish a common occurrence. Source-content changes explicitly invalidate the index; oversized single-news input fails without truncation.
2. **Event research:** read all current member packages and originals together, producing one event summary, claims, importance (1–5), independent risk (0–5), verification, factors, uncertainty, opinions, impacts, counter-evidence, watchpoints and timeline. Every successful result and failed attempt retains input, output, rules, usage and tool evidence. Long event inputs use evidence-preserving extraction/compaction across all originals or fail explicitly. Timeline nodes identify the corresponding member packages and their original news IDs; background nodes supported only by supplementary material have an empty package list. Literal quotes and source identities are validated before publication.

An event has its own continuing timeline. Research may refer to another event's successful immutable version as background, supporting material or subject. A changed referenced event is marked for the reader and does not automatically start another paid call. AI summaries are clues, never primary evidence. Tools allow four local searches, eight local reads, two external searches and four external reads per attempt; supplied articles and cached reads are reused. Tool failures remain visible as research limitations.

Research receives each member's selected `keyword_canonicals` and complete selected keyword records from its latest successful assignment snapshot, together with `scope_quote`, `reason` and `other_scopes` for independent matters in the same news. New assignments atomically retain resolved targets in `Attempt.rules.membership_targets`. Older assignments reuse their exact output selection when the owning target is unambiguous; otherwise `scope_selection=legacy_unscoped` and `keyword_canonicals=null` explicitly identify a historical full index. Missing successful input snapshots or mismatched original revisions fail durably before model I/O. The full original remains available; TAGs locate scope and do not replace evidence. Long-input extraction also receives these boundaries. The research Skill requires summaries, facts, claims and timelines to stay within the assigned matter, including when an earlier result drifted. Independent matters may appear only as necessary, explicitly identified background in reasoning with the appropriate event reference. These instructions require real-model semantic acceptance; structural tests alone cannot prove compliance.

The research Skill reuses complete originals already supplied in `articles`, requests original reads only when needed after extraction, bundles independent evidence queries and stops when no concrete lead can resolve a consequential gap. Repeated member reads return `already_provided` with a reuse instruction and are retained in tool audit records without consuming another read budget or duplicating the full text. The tool-round ceiling and evidence requirements remain unchanged. Updating defaults does not modify already published production Skills; publication and deployment require task authorization.

Assignments select nonempty, unique `keyword_canonicals` verbatim from the input index for each target. Selection covers the relevant subject and factors throughout the original, including names outside the scope quote; unrelated digest matters are excluded. Unknown or rewritten canonical names fail validation. All aliases of selected records remain searchable. Successful assignment records store each resolved event's TAG contribution in `Attempt.rules.membership_tags` in the same transaction as memberships. Rebuilds union the latest successful assignment snapshots of current members; reindexing, pending/failed assignments and removed memberships cannot introduce new TAGs into old events. Original package keywords remain intact. Legacy assignments retain their complete historical input index; missing snapshots fail explicitly rather than reading a newer index. Deployment does not rewrite historical records or clean legacy digest noise; historical cleanup requires a separately authorized operation. Package search also matches whole ASCII name/model tokens of at least three characters inside current canonical names and aliases (`MI455X` matches `MI455X GPU`, but not `MI455X2`); stale package keywords remain excluded.

New event timeline day/month values are UTC-midnight calendar markers (first day for month precision), retaining the stated calendar date rather than converting a local midnight to a different UTC day. They do not denote precise instants. Minute precision retains its actual timezone; unknown dates stay null. Historical versions are not reparsed or rewritten. Research Skills require explicit timezone evidence for minute precision, and keep quarters/years as period text. Updated default templates must be reviewed and published as new Skill versions to change bound model instructions; changing source templates alone does not update published production Skills. Existing reviewed vocabulary remains the authority for name equivalence; no vocabulary is silently seeded.

Long-input compaction stops when the final research Skill, payload, tools and result schema fit the configured context budget. Extraction and consolidation keep their event-boundary instructions and check their own call budgets; the final fit check does not count member metadata a second time in those instructions.

Ordinary new facts, independent evidence and corrections aggregate for five minutes, with a fifteen-minute hard deadline. Urgent developments are immediately eligible. Pure reposts retain their own packages and citations without requesting a new successful event review. Indexing, assignment and research share the configured concurrency pool (1–16, default 3). Pausing stops new admissions; in-flight work drains. Interrupted tasks recover from PostgreSQL. Third-stage reading/trends, legacy queue/backfill jobs and old three-model configuration are not exposed or scheduled by this workspace.

The homepage provides event and package searches, pagination, membership details, timelines, evidence and version history. `GET /api/risk/settings` and `PUT /api/risk/settings` manage independent `index` and `research` model/effort/Skill bindings and concurrency; saving pauses admission. Clearing effort inherits provider settings. `POST /api/risk/enable` validates the two models with synthetic material, including separate index/assignment contracts. `POST /api/risk/packages` enrolls up to 500 explicit news IDs; while enabled the worker additionally admits news published since November 1, 2026, 00:00 Asia/Shanghai. Failed assignment retries reuse indexing; event retries never repeat indexing. Earlier source revisions are not shown with current revision annotations.

Create the two default Skill drafts in homepage settings, review and publish them in Skills, then bind them before enabling. Template generation never publishes or changes existing Chat/trading Skills. The additive `a71234567890` migration follows `f60123456789` and creates seven `news_research_*` tables; `b81234567890` adds the eighth table for immutable vocabulary snapshots. There is no conversion, truncation or deletion of original/legacy tables. Production schema execution, code deployment, commits and pushes require the corresponding task authorization; preparing a migration alone does not grant it.

### First rollout of two-stage research

The first rollout deploys the new workspace and binds the two published Skills while automatic news AI remains paused. The user requested a first production trial of exactly 50 frozen news items after deployment. Run only their indexing, assignment and resulting event research through the same runtime services, then leave automatic admission paused. Fifty news items require more than fifty model requests because assignment and event research are separate tasks, with bounded tool rounds and retries.

Do not use the normal enable endpoint for this trial: it makes real synthetic model calls and admits news published since November 1, 2026, 00:00 Asia/Shanghai. A read-only check on November 4 found 3,388 eligible news items; this count will grow. Explicitly enrolling 50 IDs does not limit automatic admission after enabling. The acceptance allowance of 180 requests belongs to the earlier isolated acceptance runner and does not impose a production spending limit.

The following steps are prepared operator instructions, not an instruction to run production changes without authorization:

1. Review the complete working tree, including new files, and obtain task authorization for commit/push, the independent schema migration, deployment, and publication/binding of the two news Skills. A `dev` push publishes accepted images and automatically attempts deployment. Review the proposed scope before pushing; GitHub receives a public mirror. Keep all original news and legacy AI data.
2. After the authorized commit/push, wait for the isolated check/build job and download its `build-receipt` artifact. Record the full commit, version and accepted image identities. The first automatic deploy is expected to stop at `Database migration requires separate authorization` while the database is at `f60123456789`; this check runs before stopping or replacing services. Do not disable the guard or bypass the deployment lock. If a different failure occurs, diagnose it first.
3. Under `/docker/gitea/dev/AlphaPulse/.deploy.lock`, confirm no pending deployment, the approved commit is still current `dev`, legacy news AI is paused, the database is at `f60123456789`, and the candidate has the single head `b81234567890`. Validate both image identities using `scripts/deploy.py`'s `validate_images` and the downloaded receipt. Preserve the current deployment receipt, Compose and image selection. Create a private full database backup with the owning PostgreSQL container, record its checksum, and verify restoration in a separate empty database before migrating. Preserve the existing encryption key through its private configuration mechanism; never put it in reports or Git. A file existing or `pg_restore --list` succeeding alone does not establish recoverability.
4. Still holding the same lock, use the verified candidate backend image with the existing instance `.env` and `dev_default` network to run `alembic upgrade b81234567890`. Only these two reviewed migrations may run. Check the resulting revision and eight new tables; verify original-news samples and legacy settings remain intact. The migration updates `alembic_version` as well as creating the new tables. If it fails, inspect the revision and tables before retrying; do not stamp a revision or drop tables to bypass a failure.
5. Release the migration lock, then run the existing update command with that same accepted receipt:

   ```sh
   python3 scripts/deploy.py update --build /absolute/path/to/accepted/build.json
   ```

   The updater takes the lock itself and restarts the existing nine services using their existing configuration. Verify the served frontend/API commit, all service states, restart counts and search health. A newer `dev` commit causes the update to skip; obtain its accepted receipt and reassess migrations before continuing.
6. In homepage settings, create the two default drafts through `POST /api/risk/skill-templates`. Review and publish **新闻关键词索引与事件归属** and **新闻事件整体研究** in Skills. Record their returned node IDs, published version IDs and content hashes, and verify their scope is `news` and they are enabled. The first content must equal `INDEX_SKILL + "\n" + ASSIGN_SKILL`; the second must equal `RESEARCH_SKILL` in the deployed candidate. Do not copy the sample-specific official-URL hints from isolated acceptance Skills into these production defaults. Preserve the legacy Skills and all Chat/trading bindings.
7. Bind the returned Skill IDs to `index` and `research` with the reviewed provider/model/effort values and concurrency 3. Save settings, then read them back and confirm `enabled=false`. Published content is used together with the runtime base instructions; production has no fallback to an unbound default template. Configuration/publication does not call the models. Do not click test-and-enable, call `/api/risk/enable`, or import isolated acceptance outputs during this rollout.
8. Verify homepage event/package lists, news details, vocabulary reads and authentication. Empty new event/package lists are expected before the fixed trial; originals and legacy history remain stored. Record the migration, served commit, Skill versions/bindings and paused state in the private rollout receipt.
9. For the fixed trial, freeze exactly 50 distinct original news IDs and their content hashes before processing. In the absence of another selection, use the latest 50 and order them by publication time and ID for assignment. Keep the instance deployment lock throughout execution, stop only the existing `news-risk` service through the deployment controller's Compose command, and acquire the worker's PostgreSQL advisory lock `(20261030, 1)` before dispatch. Keep the research-settings row locked and `enabled=false`, and invalidate concurrent enable validation by advancing its control revision. Reject an existing research package outside the frozen selection; do not clear it. Validate the published model/Skill bindings without calling the normal enable endpoint. Index with concurrency 3, assign chronologically and serially, then research the resulting events with concurrency 3; each event reads all its member packages. Reuse completed stages instead of repeating successful TAG extraction. Abort if a frozen original changes. Preserve JSON checkpoints, attempts, versions, evidence, usage and failures; automated completion is not semantic quality approval. In a `finally` block, retain the paused setting, release the advisory lock and restore the previously running news worker under the deployment lock. Check the stored package IDs equal the frozen 50 and the automatic setting is still paused, then review TAGs, memberships, event conclusions, evidence and model usage. General production enablement remains a separate decision; neither this bounded trial nor historical samples prove long-running production quality.

For a failure after replacement starts, `scripts/deploy.py` attempts restoration of the previous images/configuration. Use `python3 scripts/deploy.py recover` only when a pending deployment remains; use `python3 scripts/deploy.py rollback` for an explicitly authorized rollback after a successful deployment. Image rollback retains the eight new tables and their data and does not downgrade or stamp Alembic. Before rolling back an enabled system, pause news AI and check in-flight work; pause stops admission rather than cancelling requests already sent. Keep legacy news AI disabled so the old worker does not resume the three-stage flow. If new Skills have already been published, retain their versions for a later forward deployment. Do not restore the database backup over ongoing news/trading writes as a routine image rollback. The older image's Alembic CLI cannot resolve the new revision; run any subsequent schema inspection or migration using the candidate that owns it.

Uncommitted preflight images are local test artifacts, not deployable CI receipts. Build them under distinct local tags and run isolated smoke tests against those exact images. `scripts/ci.py check` identifies images by committed `HEAD` and may reuse its published images; running it on a dirty working tree cannot by itself establish that the uncommitted runtime was exercised. After committing, the normal clean-checkout CI must build and accept the committed version before rollout.

### Event boundaries, vocabulary and pinned references

An event follows one occurrence, its direct consequences, responses, investigation, corrections and remediation across months or years. A new occurrence, independent decision/action or statistical period starts another event. A market reaction explicitly attributed to a particular release belongs to that release. Shared subjects or TAGs establish candidate relevance, not membership.

Candidate recall also matches whole ASCII words from literal compound aliases against event TAGs and titles, so an existing `bybit` TAG can be found from `Bybit Hack` without regenerating historical keywords. Full-phrase matches rank first, retired events are excluded and the sixty-candidate cap remains. The model decides membership. New keyword extraction keeps independently searchable subject names alongside incident phrases. Research quotes must retain enough continuous original context to support the claim, including original line breaks around names; literal substring validation alone does not establish semantic support.

`GET/PUT /api/risk/tag-vocabulary` reads/saves the reviewed phrase vocabulary. PUT accepts `{base_revision, entries: [{canonical, aliases}]}`; stale revisions return 409, ambiguous or invalid entries return 422. Each save creates an immutable snapshot in the additive `news_research_vocabulary_versions` table (migration `b81234567890`, following `a71234567890`). The homepage provides manual editing. Vocabulary changes expand historical searches without rewriting prior model output or triggering paid reprocessing. Attempts and event research freeze their vocabulary snapshot; literal news aliases and quotes remain unchanged.

`GET /api/risk/events/{event_id}/versions/{version_id}` reads a successful pinned version, including frozen member articles and references; a mismatched event or unsuccessful version returns 404. Reference links open that snapshot. Current target changes are marked but do not enqueue referring events.

`scripts/accept_news_research.py` runs only against an empty local disposable database with CI credentials and an explicitly supplied private model configuration. Use the frozen 30-news report or `backend/tests/fixtures/news_research_scenarios.json` (`--synthetic`). Separate databases share a `--budget` counter file, which atomically limits all provider requests, including retries and repair/tool/extraction rounds, to 240. Never reset the counter for the same acceptance run. The script reports automated assertions separately from human quality review and never automatically grants quality acceptance. Synthetic external searches are controlled; real-source availability limitations remain visible. Reports retain exact inputs, outputs, rules, vocabulary, evidence and usage; private provider configuration must be removed after the run.

### Chronological scenario acceptance

`accept_news_research.py --scenario-manifest MANIFEST --credentials PRIVATE --output RUN_DIRECTORY --budget NEW_COUNTER` freezes twelve real originals and chronological batches. The manifest specifies explicit same-event, separation and pinned-reference expectations, official URLs with text anchors, and the immutable historical counter hash (199). The independent new counter permits 90 requests initially; `--resume --finish` permits the reserved continuation up to 120 additional requests total. All retries, repairs and tool rounds share this counter; never reset it. Reports and attempts are retained separately, while `state.json` holds resumable progress.

After an explicitly authorized budget extension, `--resume --finish --request-limit LIMIT` sets the cumulative supplemental cap for that continuation and records it in the new state/report. It never changes the frozen manifest or resets the counter. Omission retains the original 90/120 defaults; the option is rejected without both resume flags or below 120. A counter already above the selected cap fails explicitly. Completed-checkpoint restoration retains the extended cap from its report; paid continuation still needs the explicit authorized limit. Authorization remains an operator responsibility.

Only the disposable local CI database is writable. Published isolated Skills use the same configuration path as runtime; both the base instructions and published content must reach the model. Original news enters the test database one batch at a time. External tools are disabled during historical replay; final live verification publishes an isolated Skill version with explicitly supplied official URL hints, which are leads rather than evidence. These explicit reruns do not count as natural revisions. Known public URLs may be read directly through the same DNS, redirect, size and tool-budget checks. Tool snapshots distinguish `discovered` and `direct` reads. Search availability and successful official text reads are assessed separately.

The manifest, configuration, vocabulary and existing originals must remain frozen for resume. A diagnosed missing incident reference can use `--resume --finish --repair-references` before the final natural-update batch: retain the old version, publish a repaired isolated Skill, and retry the affected consumer without repeating successful TAG extraction. This explicit repair is retained in the audit and is not evidence of autonomous recovery. Automated checks never grant human quality acceptance; limited acceptance requires review of facts, timelines, rating rationale, original quotes and immutable references. No result establishes long-running production quality, autonomous URL discovery, or savings. Live migration, deployment and real Skill publication remain separate actions.

Official-evidence checks require a successful version of the intended event, a successful read from its expected official domain with the frozen fact anchors, and a supported/partial claim citing that read's literal text. A read retained only in a failed version cannot pass. Each event receives only the official leads associated with its member news; unrelated official pages are not required reading. Reference checks require a later natural target update without another consumer attempt and call the application reference reader to verify that the pinned version remains unchanged while `changed=true`.

Resume requires the disposable database as well as report files. If that database has been removed, `scripts/restore_news_scenario.py --report ORIGINAL_REPORT --snapshot COMPLETED_BATCH_SNAPSHOT --credentials PRIVATE --output NEW_RUN_DIRECTORY --budget ORIGINAL_NEW_COUNTER` restores a completed chronological prefix into an empty local `ci` database with synthetic CI credentials. Apply migrations to that isolated database first. The restore checks historical versions, originals, vocabulary and checkpoint consistency, preserves successful keyword attempts, and carries forward the original counter without resetting or spending requests. Historical rules stay frozen; subsequent work uses newly frozen isolated Skills and requires separate real-model validation. Old reports are never overwritten. Offline mock replay demonstrates code behavior only and cannot grant quality acceptance.

### Search and subscriptions

Compose includes a version-tagged SearXNG service on the existing internal Docker network, with no host port. Deployment creates its instance-local settings from `deploy/searxng.yml`, generates its server secret once, and checks `/healthz` through the API container. Pulls, service changes and recovery continue through `scripts/deploy.py` and `.deploy.lock`. `SEARXNG_URL` can select a different internal endpoint; its default is `http://searxng:8080`. External search availability depends on upstream engines and is reported as evidence limitations. Web reads reject private/mixed DNS answers, pin the checked address, recheck redirects and bound decoded response sizes. No external research response is inserted into the original news stream.

The crawler console accepts administrator-managed public RSS/Atom subscriptions, names, intervals and enable/disable controls. New subscriptions are paused, no default feeds are added, and the manager discovers new subscriptions without a restart. Only feed-provided content is stored; a summary is labeled as a summary. Arbitrary private-network feed URLs are rejected.

### Isolated acceptance and original-data protection

Use `scripts/smoke_news_research.py` only with disposable CI PostgreSQL credentials (`ci` / `ci-only`, database `ci`). It mocks model I/O and verifies one-news identity, multiple event memberships, indexing reuse, strict quote rejection and durable failed outputs. CI runs this after migration; all collectors and trading remain stopped.

Real-news acceptance requires explicit permission for the bounded sample and necessary model access. Export original records in a read-only repeatable-read transaction, copy only necessary provider fields to a private isolated configuration, and never copy the production `.env`. After processing, review keyword coverage, event boundaries, reposts, evidence, dates and ratings, retain a credential-free report, and remove temporary keys and test containers. The initial November 3 round used thirty news items and additive tables only; the subsequent chronological supplement uses twelve frozen originals and a separate additional request budget. Legacy sample replay/reset commands target a different schema and are not part of this workflow; do not run them for this transition. Legacy reports and task counts do not prove current quality or savings. Missing usage is not zero cost.

News transport uses at most two retries after 30/60 seconds, honoring valid Retry-After up to 120 seconds while retaining the worker slot. Partial output and permanent failures are not replayed. Structural submission repair is bounded to one correction using already available evidence. Default Skill changes are not automatically published to live nodes. External search must use a working test endpoint to establish external-verification coverage; unavailable search is explicitly reported.

## Global economic calendar

The independent `jin10-calendar` service runs Chromium and Jin10's official browser SDK. It collects the macroeconomic calendar only: economic data, important events and exchange holidays. The backend runtime image installs the matching Playwright headless Chromium and OS libraries; no browser is started in the API, official calendar, news, model or trading workers.

After the separately authorized migrations through `a8b1cd465ef3` and deployment, an administrator connects Jin10 in Settings using the Jin10 App QR flow. The account token is encrypted with the existing server encryption key and never returned to the browser. Disconnect retains history and invalidates in-flight writes; reconnect starts a new connection generation. Browser or authentication failures stay visible instead of being recorded as empty days.

The collector prioritizes today, checks yesterday and the next seven days, and fills missing days from 2025-01-01 Asia/Shanghai at a limited pace. A day receives a persistent coverage checkpoint only after all three category responses pass date/structure checks. A checkpoint captured before that local day ended is a preview and must be refreshed for historical completion, including after downtime. Historical dates without a completed snapshot are coverage gaps, not evidence that no events occurred. A restart resumes the first gap. Failed dates retain raw evidence, wait five minutes before retrying, and do not block other dates; browser reconnection preserves scheduling progress. No historical completeness or realtime latency is guaranteed before actual collection finishes.

Each complete daily response withdraws missing records only within its own day and category, retaining their values, raw evidence and a withdrawal revision. Withdrawn records are labeled explicitly and cannot supply fallback fields to an official release. A record that returns under the same source identity is restored with another revision.

Official and Jin10 observations retain separate identities and revisions. The read API merges only verified indicator IDs, definitions, units, reference periods, release stages and exact publication times. Official nonmissing fields win independently; Jin10 fills missing fields and market forecasts with explicit provenance. Unverified definitions remain distinct records. Original and revised previous values remain separate. Forecast observations captured after publication are displayed with that limitation and excluded from historical surprise judgments. Jin10's gold/USD direction flags are not BTC/ETH signals. Tentative times and all-day holidays never display an invented exact midnight time.

CI acceptance runs `scripts/smoke_calendar.py` against the isolated database using public fixtures and synthetic account material. It never starts a browser collector or connects a real Jin10 account. Local unit tests likewise isolate all external I/O. The live database migration, credential import/reconnection, history collection and dev deployment are separate operator actions; ordinary local validation does not authorize them.

## CLS retirement and full news-AI reset

CLS collection and automatic standalone news-signal labeling are retired. Original
historical migrations remain intact; the retirement migration removes source rows
only after their dependent records have been explicitly cleaned.

Preview with `python3 scripts/deploy.py remove-cls --build /path/to/build.json`.
With task-specific production authorization, append `--execute` to stop application
writers under the deployment lock, delete CLS originals/revisions/captures/logs and
all current/legacy news-AI results, migrate, then deploy the accepted version.
Other originals, shared response bodies, Skills, vocabulary and model settings are
retained. News AI remains paused. No new archive is created. Transaction failures
roll back database changes; image rollback cannot recover committed deletions.
The lock is held through cleanup, migration and activation. A durable
`deployment.pending.json` marker blocks ordinary start, update and rollback while
maintenance is incomplete. Preparation failures unpause the trader without
replacing services; `recover` can retry that unpause if it fails.
Once stopping begins, cleanup, migration or activation failures retain the marker
and do not restore old workers, which still contain automatic news labeling.
With production authorization, retry `remove-cls --execute --build /path/to/build.json`
using an accepted current dev build. Completed cleanup and migration stages are
not repeated; failed stages are retried. A newer accepted build can resume if dev
has advanced. Ordinary `recover` refuses restoration after maintenance has begun.

### US official news collectors

The US catalog adds `sec`, `cftc`, `treasury`, `ofac`, `fincen`, `whitehouse`,
`congress`, `federal_register`, `occ`, and `doj`, and enriches `fed` with full
text and H.4.1 releases. Migration `da1234567890` seeds the ten new sources
**disabled**; it does not enable news AI or run any historical collection.
Database migration and deployment still require the project's separate authorization.

For this rollout from `c91234567890` to `da1234567890`:

1. Review and authorize the feature commit/push, the one migration, and deployment
   to the existing instance. Push `dev` through the existing CI. Its schema guard
   must block activation until the independently authorized migration completes.
2. Obtain the accepted build receipt and verify the candidate images using
   `scripts/deploy.py`. Under the instance `.deploy.lock`, reject pending or stale
   deployments and confirm the current schema is exactly `c91234567890`.
3. Create a private full database backup and verify a complete restore into a
   disposable database. Do not treat a dump listing as a restoration check.
4. Still holding the lock, run only `alembic upgrade da1234567890` using the
   verified candidate backend image and the existing private instance environment.
   Verify three additive columns and ten new disabled sources. Do not modify
   existing news, source enablement, history targets, or news AI settings.
5. Release the lock and run `python3 scripts/deploy.py update --build` with that
   same accepted receipt. Verify the nine services, API/web revisions, Congress
   configuration presence, paused news AI, and disabled new sources. Report only
   key presence/match, never its value or expanded container environment.

The already enabled `fed` source keeps its enablement and uses the new full-body
adapter after deployment. New collectors and historical backfill require a
separate activation decision. SEC also requires a public contact email; absence
must remain an explicit configuration error rather than an invented address.
Rollback uses the existing deployment controller and retains additive columns
and collected data; do not downgrade or stamp the database as an image rollback.

Set `CONGRESS_API_KEY` and a public `CRAWLER_CONTACT_EMAIL` in the instance
environment before enabling Congress and SEC. Keys are sent only in the Congress
API request header and must never appear in URLs. Source access errors, including
HTTP 200 challenge pages, fail visibly; they are not empty successful collections.

After source acceptance, use the existing collection console to enable sources
and set history to `2025-01-01`. Real-time discovery runs first; historical work
issues new document requests for at most 20 seconds per batch and saves page and
pending-document cursors only after successful processing. Canceling history
preserves its cursor; restarting a completed traversal scans it again. Official
rounds wait for the previous checkpoint commit and reload current source settings
and cursors from the database; queued messages cannot restore canceled history
or bypass a newly saved failure backoff. A failed checkpoint write is retried on
subsequent triggers, with three total write attempts per round; exhausted writes
block new collection with an explicit error until the collector is restarted. An ended
list traversal does not prove complete bodies or attachments. Attachment failures
remain visible on the news item, with up to three scheduled attempts one hour apart;
exhausted gaps require explicit investigation or another traversal.

Real-time discovery follows pagination, including every Federal Register page
within a day. Only verified descending feeds stop early at a fully dated page
outside the lookback window; unknown ordering is traversed conservatively.
Congress screening reads subjects, CRS summaries, and available text before
excluding a bill whose title alone is inconclusive. Missing bill evidence remains
an unresolved gap, retried at most three times one hour apart per official update;
exhausted gaps remain visible in the collection console. No AI is called.

Original documents remain separate news packages across publishers. Body and
attachment text are searchable and versioned. Date-only releases are displayed as
dates, not precise release instants. Backfilled documents describe the version
available at capture time; they are not historical point-in-time evidence.

Validation without production credentials:

- `cd backend && uv run pytest && uvx ruff check --select F,E9,B src tests`
- `cd web && pnpm lint:check && pnpm typecheck && pnpm test && pnpm build`
- `scripts/smoke_us_news.py` requires an isolated PostgreSQL database with user
  `ci`, password `ci-only`, database `ci`, host `127.0.0.1`; it creates its own
  news tables and must run in an empty disposable database. Redis publication is mocked.

The 50 public HTML fixtures cover eight publishers. Eight additional Fed
discovery responses were captured on 2026-11-04: the 2025/2026
press, speech and testimony archives, FOMC calendars and H.4.1 release dates.
Their offline regression checks configured entry URLs and article discovery;
Fed speech archives use the singular `speech/` directory.
Additional API/PDF fixtures cover three 2025 DOJ institutional cases (OPA and USAO), their four attachments,
one excluded personal crime case, and a Federal Register daily XML containing 106
unique document numbers. The isolated database smoke checks idempotency and
revisions after API-package edits. Three additional
packages are actual GovInfo bill XML texts for GENIUS, CLARITY and STABLE;
this cross-check is separate from the captured Congress API responses.
The Congress API fixture contains 39 successful responses and produces 100
packages: GENIUS 52, CLARITY 37, STABLE 10, and one crypto committee meeting.
The smoke also replays those API packages without network access. On 2026-11-04,
the isolated PostgreSQL run finished with 156 news packages and 158 revisions;
replaying unchanged inputs created no extra revisions, and two deliberate edits
each created one revision.

DOJ discovery uses the public news API with 50 metadata records per page, then
fetches individual bodies by UUID. It traverses publication dates newest first;
old HTML discovery cursors are rebuilt without deleting news or changing the
history target. API attachment file IDs use the verified official `/media/{id}/dl`
endpoint. Calendar publication dates remain date-only; official update timestamps
are stored separately.

Federal Register daily results above 2,000 use the GovInfo issue XML to enumerate
unique document numbers, then reuse the document API/body/PDF path. The issue date
and unique count must match the daily API total; mismatch or download failure does
not advance the checkpoint. A day advances only after its pending documents finish.
Overflow tests use constructed 2,001-document data, not a claim of a real overflow day.

An independent test key completed the three bill parser chains and a selected
crypto committee meeting on 2026-11-04. This verifies the sampled API/text/action
and attachment paths, not complete historical discovery or full source quality.
The report keeps `accepted=false`: GENIUS has one undated text version, and one
meeting PDF has no extractable text. Production credentials were not used in
tests, and no key is hardcoded into the collector or saved in the fixtures.

Three Federal Register API/body/PDF documents were previously read successfully
on 2026-11-04; font encoding fidelity was not independently verified. Fixture replay
and limited live reads do not prove complete 2025 coverage or authorize production
collection activation.

The GovInfo GENIUS BILLSTATUS XML captured on 2026-11-04 confirms that the
Enrolled Bill version has an empty date. The offline cross-check preserves that
gap and validates a dated text from each of the three bills without inventing a
date. A dated version without a supported format also remains a bounded retry
gap even when other versions succeed. Meeting PDFs include both meeting documents
and witness documents, deduplicated by URL. The selected real meeting lists 16 PDF
entries with 15 unique URLs: 14 yielded text, while a Truth in Testimony form did not. The meeting
package remains `partial`; no OCR dependency or invented text/date hides the gap.

Earlier public-key probes returned HTTP 429 and stopped immediately. The
independent test key resolved that access blocker. Do not reuse production
credentials for this test.


Resumable Congress acceptance capture (no database, Redis, model, or production
configuration access):

```sh
PYTHONPATH=backend/src backend/.venv/bin/python scripts/capture_congress.py --cache /tmp/alphapulse-congress-acceptance/responses.json.gz
```

For a separate local test key, enter `CONGRESS_TEST_API_KEY` in the ignored,
mode-0600 `/srv/AlphaPulse/.env.congress-test`, then load it with uv:

```sh
PYTHONPATH=backend/src uv run --project backend --no-sync --env-file /srv/AlphaPulse/.env.congress-test python scripts/capture_congress.py --cache /tmp/alphapulse-congress-acceptance/responses.json.gz --meeting-url https://api.congress.gov/v3/committee-meeting/119/house/119541
```

The script uses `CONGRESS_TEST_API_KEY` when separately configured, otherwise the
public DEMO_KEY. It atomically saves each successful, credential-redacted response,
reuses cached responses on another invocation, and stops on HTTP 429 or its bounded
request budget (default 60, maximum 100). It never follows redirects with API
credentials. A separate compressed report records incomplete cases; an incomplete
run exits nonzero. GENIUS's known undated version remains an explicit gap rather
than a passing complete case. Meeting selection requires a crypto-relevant title
or related metadata; `--meeting-url` accepts a known official single-meeting API
URL to avoid an unrelated meeting scan. `parser_accepted` means the selected
callback chain finished, including explicit partial packages; it does not mean
full source quality passed. Exhausted candidate/request budgets remain incomplete.
Mocked tests and real captured responses are separate evidence; neither proves
complete 2025 coverage or authorizes production activation.
