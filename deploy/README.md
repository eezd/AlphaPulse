# AlphaPulse delivery and operations

Gitea (`https://git.134333.xyz/eezd/AlphaPulse`) is the source. `main` is the default stable branch and `dev` is the development branch. GitHub is a public one-way mirror of all business branches, tags, history and existing LFS objects. Normal pushes and tags **only synchronize Git**; they do not test, build, deploy or release. Main was advanced once to the framework migration; future updates/releases do not merge branches.

## Native development instance

Run from any directory as root:

```sh
bash /srv/AlphaPulse/start.sh
bash /srv/AlphaPulse/stop.sh
bash /srv/AlphaPulse/status.sh
```

`start.sh` builds the current workspace with Python 3.13 (`/usr/bin/python3.13`), uv 0.13.6, Node 24 and pnpm 12.11.2. It installs locked dependencies and runs backend pytest/ruff, frontend lint/typecheck/tests/build, and delivery-script tests. It prepares an isolated candidate before touching running services. The candidate records the full Git SHA, UTC commit-date version, build time and start/end dirty state; local dirty builds are not formal releases.

Only these six foreground services are managed: `alphapulse-api.service`, `alphapulse-crawler.service`, `alphapulse-market.service`, `alphapulse-calendar.service`, `alphapulse-news-risk.service`, `alphapulse-jin10-calendar.service`. They run as the dedicated `alphapulse` system account, are enabled at boot and restart on failure. API and compiled frontend share **127.0.0.1:21005**. Existing nginx and `https://alpha.134333.xyz` remain unchanged; SSE and market WebSockets use the same API.

Private environment configuration lives in `runtime/config/app.env` (root:alphapulse, 0640); the root `.env` points there. Immutable builds are `runtime/builds/<unique-id>`, selected by `runtime/current`. Persistent browser files and service HOME are under `runtime/data`. Logs are in `journalctl -u alphapulse-api` (or another exact unit name). None of these runtime files is tracked or packaged. PostgreSQL and Redis remain in their existing containers and retain all data; native configuration reaches their host ports. Redis DB 14 is shared: never FLUSHDB.

Start/stop serialize on `runtime/.deploy.lock`. Stop retains units, builds, configuration and data. A deployment checks the current Alembic revision against both candidate and previous code; **no automatic database migrations or backups**. Before updates, AI new admissions pause and in-flight research/Chat drain (maximum one hour); success restores the prior enabled state only if the control revision and configuration still match. No provider validation or model request is made for deployment. Avoid initiating new Chat requests during maintenance. Research worker SIGTERM stops new admissions and drains active tasks.

Activation verifies service state and both health/version endpoints. Failure restores the previous current link, unit contents and original active/enabled state; it never restores database contents. `runtime/deployment.pending.json` retains interrupted/failed maintenance evidence and blocks further starts. Inspect it and service health before manually recovering; do not delete it merely to bypass a failure. No deployment is safe across an independently executed incompatible schema migration.

The old `/docker/gitea/dev/AlphaPulse` application instance is retired after the one-time authorized cutover. Its stopped containers and private files remain for diagnosis, with restart disabled; its old start entrypoint rejects startup. Do not run its Compose files against the live database. Gitea, runner and shared database infrastructure still use Docker.

## Explicit formal release

```sh
GITEA_TOKEN=... bash /srv/AlphaPulse/release.sh dev
# Any existing remote branch is allowed, including feature/example.
```

The client requires Bash/Python 3 and only a Gitea token; it does not inspect local Git or local changes. It fixes the remote full SHA, dispatches `release.yml` from trusted main, and follows that unique run. It never commits, pushes local code, merges or deploys. An uncertain dispatch POST is not automatically repeated.

The workflow checks the fixed source and creates six Python **installation archives**: Linux/Darwin/Windows × amd64/arm64. These include original backend source/lock, compiled frontend and a platform-checking `install.py`. They require Python 3.13 and uv, network access to install dependencies/browser, external PostgreSQL/Redis, and configured environment variables. Linux also needs Chromium system libraries. Windows arm64 may need Rust, MSVC and OpenSSL to compile cryptography; macOS/Windows installation is not natively tested on this Linux runner. A locked dependency dry-run is reported separately from runtime acceptance; these are not self-contained executables.

Linux archives feed one Linux amd64/arm64 OCI image build using a project-isolated BuildKit builder (with bundled arm64 emulation). It verifies imports and browser launch on each architecture; the build probe uses single-process/no-GPU flags to accommodate QEMU, while runtime browser acceptance uses normal settings. Linux amd64 API, UI, SSE, WebSocket, schema and queue acceptance run against synthetic PostgreSQL/Redis on an internal network, without workers or model calls. No production configuration/data is supplied. Builders and temporary resources are scoped to this run, not global prune. Archives and compressed OCI blobs must each be below 90,000,000 bytes.

Version is the selected commit's UTC committer date plus its first eight SHA characters: `YYYYMMDD-xxxxxxxx`. A repository-wide persistent lock/record under `/docker/gitea/mirrors/AlphaPulse/releases` fixes source, baseline, notes and artifact hashes. First release on a branch has exact notes `init`; subsequent notes use the nearest successfully completed ancestor on the **same** branch. Existing historical auto-build tags lack completion records and are not baselines. Same-SHA requests reuse the original version. Conflicts fail; partial releases retain originals and drafts for retry, without rebuilding over published content.

Original six archives, SHA256SUMS, packages.json and build.json are published identically to Gitea and GitHub Releases. Images use immutable `git.134333.xyz/eezd/alphapulse:<version>` and `ghcr.io/eezd/alphapulse:<version>` tags; digest-preserving multiarch copying and both endpoints are verified before the completion record is written. No latest channel is introduced.

Gitea secret `ALPHAPULSE_GITEA_TOKEN` needs repository, Release and registry publication access. Account secret `ALL_GITHUB_TOKEN` needs identity `eezd`, repository write, workflow and write:packages scopes. No secret is read from the Gitea database. The release preflight verifies identity/scopes; missing permissions fail before publication. GitHub Actions on the mirror remains disabled. Ordinary Git mirror operations are independent of builds and share their own `.mirror.lock`; no release token is needed locally for native development.

## Two-stage news research

A news item has one stable package, identified by `news_items.id`. Reposts remain separate packages; a digest is never split into new packages. Original news, revisions and archived news-research tables are retained; dedicated trading signals are removed by the retirement migration below.

1. **Keyword indexing and event assignment:** read the complete news once to persist canonical TAGs, literal aliases and original supporting quotes. This stage generates no summaries or ratings. Assignment is a separate persisted subtask, so a classification retry reuses the keyword index. Packages and events have many-to-many membership; a digest can support several distinct events, and a single package may start an event. Event candidates cover all time, bounded to sixty lexical matches. Shared names or sectors alone do not establish a common occurrence. Source-content changes explicitly invalidate the index; oversized single-news input fails without truncation.
2. **Event research:** read all current member packages and originals together, producing one event summary, claims, importance (1–5), independent risk (0–5), verification, factors, uncertainty, opinions, impacts, counter-evidence, watchpoints and timeline. Every successful result and failed attempt retains input, output, rules, usage and tool evidence. Long event inputs use evidence-preserving extraction/compaction across all originals or fail explicitly. Timeline nodes identify the corresponding member packages and their original news IDs; background nodes supported only by supplementary material have an empty package list. Literal quotes and source identities are validated before publication.

An event has its own continuing timeline. Research may refer to another event's successful immutable version as background, supporting material or subject. A changed referenced event is marked for the reader and does not automatically start another paid call. AI summaries are clues, never primary evidence. Tools allow four local searches, eight local reads, two external searches and four external reads per attempt; supplied articles and cached reads are reused. Tool failures remain visible as research limitations.

Both stage Skills require generated prose in Simplified Chinese regardless of source language. Canonical TAGs and entity names use established Chinese names; proper names without a reliable Chinese equivalent, product identifiers and standard abbreviations remain intact. Assignment titles/reasons and all event research prose, including long-input extraction/compaction facts, follow this rule. Literal `aliases`, `quote`, `time_text` and source `period_text` preserve their original language and characters; Chinese explanations accompany evidence through the existing research fields. Assignment copies indexed TAGs verbatim, including historical English TAGs. Search terms may use the source language, and protocol keys, IDs, URLs, dates and enum values remain unchanged. No original news is translated or rewritten. Publish new versions of the bound Skills to apply these defaults; deploying templates alone is insufficient, and publication does not enable or backfill research.

Research receives each member's selected `keyword_canonicals` and complete selected keyword records from its latest successful assignment snapshot, together with `scope_quote`, `reason` and `other_scopes` for independent matters in the same news. New assignments atomically retain resolved targets in `Attempt.rules.membership_targets`. Older assignments reuse their exact output selection when the owning target is unambiguous; otherwise `scope_selection=legacy_unscoped` and `keyword_canonicals=null` explicitly identify a historical full index. Missing successful input snapshots or mismatched original revisions fail durably before model I/O. The full original remains available; TAGs locate scope and do not replace evidence. Long-input extraction also receives these boundaries. The research Skill requires summaries, facts, claims and timelines to stay within the assigned matter, including when an earlier result drifted. Independent matters may appear only as necessary, explicitly identified background in reasoning with the appropriate event reference. These instructions require real-model semantic acceptance; structural tests alone cannot prove compliance.

The research Skill reuses complete originals already supplied in `articles`, requests original reads only when needed after extraction, bundles independent evidence queries and stops when no concrete lead can resolve a consequential gap. Repeated member reads return `already_provided` with a reuse instruction and are retained in tool audit records without consuming another read budget or duplicating the full text. The tool-round ceiling and evidence requirements remain unchanged. Updating defaults does not modify already published production Skills; publication and deployment require task authorization.

Assignments select nonempty, unique `keyword_canonicals` verbatim from the input index for each target. Selection covers the relevant subject and factors throughout the original, including names outside the scope quote; unrelated digest matters are excluded. Unknown or rewritten canonical names fail validation. All aliases of selected records remain searchable. Successful assignment records store each resolved event's TAG contribution in `Attempt.rules.membership_tags` in the same transaction as memberships. Rebuilds union the latest successful assignment snapshots of current members; reindexing, pending/failed assignments and removed memberships cannot introduce new TAGs into old events. Original package keywords remain intact. Legacy assignments retain their complete historical input index; missing snapshots fail explicitly rather than reading a newer index. Deployment does not rewrite historical records or clean legacy digest noise; historical cleanup requires a separately authorized operation. Package search also matches whole ASCII name/model tokens of at least three characters inside current canonical names and aliases (`MI455X` matches `MI455X GPU`, but not `MI455X2`); stale package keywords remain excluded.

New event timeline day/month values are UTC-midnight calendar markers (first day for month precision), retaining the stated calendar date rather than converting a local midnight to a different UTC day. They do not denote precise instants. Minute precision retains its actual timezone; unknown dates stay null. Historical versions are not reparsed or rewritten. Research Skills require explicit timezone evidence for minute precision, and keep quarters/years as period text. Updated default templates must be reviewed and published as new Skill versions to change bound model instructions; changing source templates alone does not update published production Skills. Canonical names and literal aliases are generated by the indexing Skill and retained per package.

Long-input compaction stops when the final research Skill, payload, tools and result schema fit the configured context budget. Extraction and consolidation keep their event-boundary instructions and check their own call budgets; the final fit check does not count member metadata a second time in those instructions.

Facts, independent evidence and corrections aggregate per event; urgent developments become ready immediately. Completed assignments do not wait for an unrelated batch before research. Pure reposts retain their own packages and citations without requesting a new successful event review. Indexing, assignment and research share the configured concurrency pool (1–16, default 3). Pausing stops new admissions; in-flight work drains. Interrupted tasks recover from PostgreSQL. Third-stage reading/trends, the legacy queue/backfill jobs, old three-model configuration, API, ORM models and unused frontend components have been removed. Reused model submission, evidence, time and retrieval helpers now belong to `news_research`; the public HTTP reader belongs to `core.public_http`. Historical Alembic migrations and inactive legacy tables are retained; autogeneration excludes those archived tables. The three retired news Skills and their old bindings are removed. There is no legacy reset command.

The homepage provides event and package searches, pagination, membership details, timelines, evidence and version history. `GET /api/risk/settings` and `PUT /api/risk/settings` manage independent `index` and `research` model/effort/Skill bindings and concurrency; saving pauses admission. Clearing effort inherits provider settings. `POST /api/risk/enable` validates the two models with synthetic material, including separate index/assignment contracts. `POST /api/risk/packages` enrolls up to 500 explicit news IDs; while enabled the worker admits eligible news using the saved historical start and persistent realtime boundary. Failed assignment retries reuse indexing; event retries never repeat indexing. Earlier source revisions are not shown with current revision annotations.

Create the two default Skill drafts in homepage settings, review and publish them in Skills, then bind them before enabling. Template generation never publishes or changes existing shared Chat Skills. The additive `a71234567890` migration follows `f60123456789` and creates seven `news_research_*` tables; the historical `b81234567890` migration added vocabulary snapshots, retired by `ef6789012345`. There is no conversion, truncation or deletion of original/legacy tables. Production schema execution, code deployment, commits and pushes require the corresponding task authorization; preparing a migration alone does not grant it.

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
6. In homepage settings, create the two default drafts through `POST /api/risk/skill-templates`. Review and publish **新闻关键词索引与事件归属** and **新闻事件整体研究** in Skills. Record their returned node IDs, published version IDs and content hashes, and verify their scope is `news` and they are enabled. The first content must equal `INDEX_SKILL + "\n" + ASSIGN_SKILL`; the second must equal `RESEARCH_SKILL` in the deployed candidate. Do not copy the sample-specific official-URL hints from isolated acceptance Skills into these production defaults. Preserve all shared Skills and Chat bindings.
7. Bind the returned Skill IDs to `index` and `research` with the reviewed provider/model/effort values and concurrency 3. Save settings, then read them back and confirm `enabled=false`. Published content is used together with the runtime base instructions; production has no fallback to an unbound default template. Configuration/publication does not call the models. Do not click test-and-enable, call `/api/risk/enable`, or import isolated acceptance outputs during this rollout.
8. Verify homepage event/package lists, news details and authentication. Empty new event/package lists are expected before the fixed trial; originals and legacy history remain stored. Record the migration, served commit, Skill versions/bindings and paused state in the private rollout receipt.
9. For the fixed trial, freeze exactly 50 distinct original news IDs and their content hashes before processing. In the absence of another selection, use the latest 50 and order them by publication time and ID for assignment. Keep the instance deployment lock throughout execution, stop only the existing `news-risk` service through the deployment controller's Compose command, and acquire the worker's PostgreSQL advisory lock `(20261030, 1)` before dispatch. Keep the research-settings row locked and `enabled=false`, and invalidate concurrent enable validation by advancing its control revision. Reject an existing research package outside the frozen selection; do not clear it. Validate the published model/Skill bindings without calling the normal enable endpoint. Index with concurrency 3, assign chronologically and serially, then research the resulting events with concurrency 3; each event reads all its member packages. Reuse completed stages instead of repeating successful TAG extraction. Abort if a frozen original changes. Preserve JSON checkpoints, attempts, versions, evidence, usage and failures; automated completion is not semantic quality approval. In a `finally` block, retain the paused setting, release the advisory lock and restore the previously running news worker under the deployment lock. Check the stored package IDs equal the frozen 50 and the automatic setting is still paused, then review TAGs, memberships, event conclusions, evidence and model usage. General production enablement remains a separate decision; neither this bounded trial nor historical samples prove long-running production quality.

For a failure after replacement starts, `scripts/deploy.py` attempts restoration of the previous images/configuration. Use `python3 scripts/deploy.py recover` only when a pending deployment remains; use `python3 scripts/deploy.py rollback` for an explicitly authorized rollback after a successful deployment. Image rollback never downgrades or stamps Alembic and requires the target image to match the current schema. Before rolling back an enabled system, pause news AI and check in-flight work; pause stops admission rather than cancelling requests already sent. Keep legacy news AI disabled so the old worker does not resume the three-stage flow. If new Skills have already been published, retain their versions for a later forward deployment. Do not restore the database backup over ongoing news writes as a routine image rollback. The older image's Alembic CLI cannot resolve the new revision; run any subsequent schema inspection or migration using the candidate that owns it.

Uncommitted preflight images are local test artifacts, not deployable CI receipts. Build them under distinct local tags and run isolated smoke tests against those exact images. `scripts/ci.py check` identifies images by committed `HEAD` and may reuse its published images; running it on a dirty working tree cannot by itself establish that the uncommitted runtime was exercised. After committing, the normal clean-checkout CI must build and accept the committed version before rollout.

### Event boundaries, keyword retrieval and pinned references

An event follows one occurrence, its direct consequences, responses, investigation, corrections and remediation across months or years. A new occurrence, independent decision/action or statistical period starts another event. A market reaction explicitly attributed to a particular release belongs to that release. Shared subjects or TAGs establish candidate relevance, not membership.

Candidate recall also matches whole ASCII words from literal compound aliases against event TAGs and titles, so an existing `bybit` TAG can be found from `Bybit Hack` without regenerating historical keywords. Full-phrase matches rank first, retired events are excluded and the sixty-candidate cap remains. The model decides membership. New keyword extraction keeps independently searchable subject names alongside incident phrases. Research quotes must retain enough continuous original context to support the claim, including original line breaks around names; literal substring validation alone does not establish semantic support.

The shared phrase vocabulary and its homepage editor and API have been removed. Package keywords, Chinese canonical names and literal source aliases still come from the indexing Skill and drive candidate recall and local search. There is no shared synonym override or automatic vocabulary accumulation. Migration `ef6789012345` follows `de5678901234` and drops only the retired `news_research_vocabulary_versions` table; original news, package TAGs and immutable historical attempt/version rule snapshots remain intact. Downgrade recreates an empty vocabulary table and cannot recover deleted entries. Applying this migration to production is a separate deployment step requiring task authorization.

`GET /api/risk/events/{event_id}/versions/{version_id}` reads a successful pinned version, including frozen member articles and references; a mismatched event or unsuccessful version returns 404. Reference links open that snapshot. Current target changes are marked but do not enqueue referring events.

`scripts/accept_news_research.py` runs only against an empty local disposable database with CI credentials and an explicitly supplied private model configuration. Use the frozen 30-news report or `backend/tests/fixtures/news_research_scenarios.json` (`--synthetic`). Separate databases share a `--budget` counter file, which atomically limits all provider requests, including retries and repair/tool/extraction rounds, to 240. Never reset the counter for the same acceptance run. The script reports automated assertions separately from human quality review and never automatically grants quality acceptance. Synthetic external searches are controlled; real-source availability limitations remain visible. Reports retain exact inputs, outputs, rules, evidence and usage; private provider configuration must be removed after the run.

### Chronological scenario acceptance

`accept_news_research.py --scenario-manifest MANIFEST --credentials PRIVATE --output RUN_DIRECTORY --budget NEW_COUNTER` freezes twelve real originals and chronological batches. The manifest specifies explicit same-event, separation and pinned-reference expectations, official URLs with text anchors, and the immutable historical counter hash (199). The independent new counter permits 90 requests initially; `--resume --finish` permits the reserved continuation up to 120 additional requests total. All retries, repairs and tool rounds share this counter; never reset it. Reports and attempts are retained separately, while `state.json` holds resumable progress.

After an explicitly authorized budget extension, `--resume --finish --request-limit LIMIT` sets the cumulative supplemental cap for that continuation and records it in the new state/report. It never changes the frozen manifest or resets the counter. Omission retains the original 90/120 defaults; the option is rejected without both resume flags or below 120. A counter already above the selected cap fails explicitly. Completed-checkpoint restoration retains the extended cap from its report; paid continuation still needs the explicit authorized limit. Authorization remains an operator responsibility.

Only the disposable local CI database is writable. Published isolated Skills use the same configuration path as runtime; both the base instructions and published content must reach the model. Original news enters the test database one batch at a time. External tools are disabled during historical replay; final live verification publishes an isolated Skill version with explicitly supplied official URL hints, which are leads rather than evidence. These explicit reruns do not count as natural revisions. Known public URLs may be read directly through the same DNS, redirect, size and tool-budget checks. Tool snapshots distinguish `discovered` and `direct` reads. Search availability and successful official text reads are assessed separately.

The manifest, configuration and existing originals must remain frozen for resume. A diagnosed missing incident reference can use `--resume --finish --repair-references` before the final natural-update batch: retain the old version, publish a repaired isolated Skill, and retry the affected consumer without repeating successful TAG extraction. This explicit repair is retained in the audit and is not evidence of autonomous recovery. Automated checks never grant human quality acceptance; limited acceptance requires review of facts, timelines, rating rationale, original quotes and immutable references. No result establishes long-running production quality, autonomous URL discovery, or savings. Live migration, deployment and real Skill publication remain separate actions.

Official-evidence checks require a successful version of the intended event, a successful read from its expected official domain with the frozen fact anchors, and a supported/partial claim citing that read's literal text. A read retained only in a failed version cannot pass. Each event receives only the official leads associated with its member news; unrelated official pages are not required reading. Reference checks require a later natural target update without another consumer attempt and call the application reference reader to verify that the pinned version remains unchanged while `changed=true`.

Resume requires the disposable database as well as report files. If that database has been removed, `scripts/restore_news_scenario.py --report ORIGINAL_REPORT --snapshot COMPLETED_BATCH_SNAPSHOT --credentials PRIVATE --output NEW_RUN_DIRECTORY --budget ORIGINAL_NEW_COUNTER` restores a completed chronological prefix into an empty local `ci` database with synthetic CI credentials. Apply migrations to that isolated database first. The restore checks historical versions, originals and checkpoint consistency, preserves successful keyword attempts, and carries forward the original counter without resetting or spending requests. Historical rules (including any retired vocabulary snapshots) stay frozen; subsequent work uses package TAGs and newly frozen isolated Skills and requires separate real-model validation. Old reports are never overwritten. Offline mock replay demonstrates code behavior only and cannot grant quality acceptance.

### Search and subscriptions

News research uses only stored news, packages, event versions and economic releases. External search and webpage-reading tools, their settings and the SearXNG service are removed. Collector HTTP access is unchanged. Historical external evidence remains readable but cannot be restored as evidence read by a new research attempt.

The worker continuously fills the shared pool from durable news, package and event
states. Indexing and ready research run alongside serial assignment; no batch
barrier delays new arrivals. Successful stages survive pauses and restarts, and
failed or uncertain calls require explicit retry. Settings return queue counts
and a null `batch` field for compatibility. Historical batch records are retained.
Deployment keeps research paused; enabling remains an explicit action.

The crawler console accepts administrator-managed public RSS/Atom subscriptions, names, intervals and enable/disable controls. New subscriptions are paused, no default feeds are added, and the manager discovers new subscriptions without a restart. Only feed-provided content is stored; a summary is labeled as a summary. Arbitrary private-network feed URLs are rejected.

### Isolated acceptance and original-data protection

Use `scripts/smoke_news_research.py` only with disposable CI PostgreSQL credentials (`ci` / `ci-only`, database `ci`). It mocks model I/O and verifies one-news identity, multiple event memberships, indexing reuse, strict quote rejection and durable failed outputs. CI runs this after migration; all collectors remain stopped.

Real-news acceptance requires explicit permission for the bounded sample and necessary model access. Export original records in a read-only repeatable-read transaction, copy only necessary provider fields to a private isolated configuration, and never copy the production `.env`. After processing, review keyword coverage, event boundaries, reposts, evidence, dates and ratings, retain a credential-free report, and remove temporary keys and test containers. The initial November 3 round used thirty news items and additive tables only; the subsequent chronological supplement uses twelve frozen originals and a separate additional request budget. Legacy sample replay/reset commands target a different schema and are not part of this workflow; do not run them for this transition. Legacy reports and task counts do not prove current quality or savings. Missing usage is not zero cost.

All AI generation requests share one transport retry owner; SDK retries are disabled. A single request retries after 60 and then 180 seconds while retaining its task slot. Partial text, thinking or tool arguments, ambiguous timeouts and permanent errors are not replayed. Three distinct transiently failing requests within sixty seconds without a completed success open a Redis-coordinated circuit scoped to protocol, normalized endpoint and credential fingerprint. It waits 180, 600 and then 900 seconds, repeating 900-second waits until recovery. Only one real request probes recovery across processes; successful completion closes the circuit. An expiring, renewed lease recovers crashed probes. Normal concurrent successes clear the failure window without invalidating peers that subsequently fail. Complete responses and known usage remain available when post-response bookkeeping or lease cleanup fails; admission for new requests still requires Redis coordination. These fixed intervals replace upstream Retry-After scheduling, including five-hour limit hints. Redis coordination failure blocks outgoing generation rather than bypassing protection.

Chat emits additive `retry` SSE events and cancellation stops waiting. `GET /api/settings/provider-availability` reports normal/waiting/probing per configured provider without credentials. News queue status adds `waiting_retry`; waiting requests retain their accepted task positions. Trading retries refresh market input without replaying previous tools or trades. Synchronous connection tests and news enable validation fail promptly while the shared circuit is waiting; they do not enable processing after a failed validation. CI runs `scripts/smoke_ai_recovery.py` against its disposable Redis, using `AI_RECOVERY_TEST_REDIS_URL`. Structural submission repair is bounded to one correction using already available evidence. Default Skill changes are not automatically published to live nodes. Research tools are restricted to local sources; evidence gaps are reported without attempting external access.

## Global economic calendar

The independent `jin10-calendar` service runs Chromium and Jin10's official browser SDK. It collects the macroeconomic calendar only: economic data, important events and exchange holidays. The backend runtime image installs the matching Playwright headless Chromium and OS libraries; no browser is started in the API, official calendar, news or model workers.

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
Other originals, shared response bodies, Skills and model settings are
retained. News AI remains paused. No new archive is created. Transaction failures
roll back database changes; image rollback cannot recover committed deletions.
The lock is held through cleanup, migration and activation. A durable
`deployment.pending.json` marker blocks ordinary start, update and rollback while
maintenance is incomplete. Preparation failures retain the running services; `recover` can clear a preparation marker after the failure is resolved.
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

Article and DOJ document failures also remain explicit gaps, with at most three
scheduled attempts one hour apart, measured from the actual failure time.
Real-time and history lanes share the document retry quota; a document retry is
requested only once per round. Due body and attachment retries remain eligible
after their document leaves the real-time lookback window.
A failed round may reuse completed document
reads and retain retry evidence, but cannot advance its lane or history position;
a pipeline error prevents saving completed reads. HTTP 403/429 and detected
challenge pages stop the round for at least fifteen minutes; a longer valid
`Retry-After` is honored and subsequent blocks cannot shorten that deadline.
CFTC document requests are serialized with a three-second
delay. The console distinguishes active collection, scheduled waiting, failure
backoff, stopped processes and unresolved document gaps.

Real-time discovery follows pagination, including every Federal Register page
within a day. Only verified descending feeds stop early at a fully dated page
outside the lookback window; unknown ordering is traversed conservatively.
Treasury currently returns the first page again for subsequent page requests.
A dated real-time page can end discovery at the lookback boundary; a repeated
historical page still fails explicitly and cannot establish historical coverage.
Congress screening reads subjects, CRS summaries, and available text before
excluding a bill whose title alone is inconclusive. Missing bill evidence remains
an unresolved gap, retried at most three times one hour apart per official update;
exhausted gaps remain visible in the collection console. No AI is called.

Original documents remain separate news packages across publishers. Body and
attachment text are searchable and versioned. Date-only releases are displayed as
dates, not precise release instants. Backfilled documents describe the version
available at capture time; they are not historical point-in-time evidence.

Selection rule `us-policy-v2` uses explicit national budget, continuing/consolidated
appropriations, discount-rate and reserve-policy phrases instead of standalone
`budget`, `fiscal` or `quantitative`. Federal Register macro screening uses the title
to avoid incidental references in unrelated regulations; crypto screening still
reads the complete body and extracted attachments.

Image attachments remain an explicit text-extraction gap, including when their
format is PNG, JPEG or GIF. They do not establish completeness or irrelevance.
Relevant documents remain `partial`; documents whose relevance is still unknown
are kept in bounded attachment retries rather than inserted as unrelated news.
Congress meeting attachment failures retain the same retry evidence as articles.
Bill and attachment retry records retain an optional `error` explaining missing
dates, unavailable text formats or extraction failures. The console shows these
reasons, original links, attempt counts and pending/exhausted state even while a
new round runs. Historical records without a reason remain visible as such; this
change neither resets their retry quota nor rewrites historical records.
`unresolved_bills`, `bill_retry_pending` and `bill_retry_exhausted` are derived from
the persisted bill retry cursor, independently of the latest round's statistics.
The source API derives `attachment_gaps`, `attachment_retry_pending` and
`attachment_retry_exhausted` from the persisted cursor, independently of the latest
round's statistics. The console keeps these document counts visible across rounds;
success clears the gap, while three failed attempts remain visible for investigation.

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
Three captured policy bodies cover implementation notes, economic projections
and meeting minutes. Minutes use the calendar's explicit release date, with its
source URL recorded, rather than the meeting date encoded in the document URL.
Date-only documents remain eligible on the cutoff day; older discovery links
are filtered before fetching their bodies in both realtime and history lanes.
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

### Bounded collection failures (November 2026)

SEC, DOJ and Congress use one durable retry layer (three failed attempts, at least
one hour apart). EIA, DOL, Michigan and ADP retain the same budget per calendar
release in `collection_failures` / `collection_retired_at`. Cooling does not spend
an attempt. Retirement retains evidence and values; new releases are independent.
A source is disabled only after three failed discovery/schedule checks, not for
one document gap. Explicit official photo captions can exclude illustrations from
text extraction. Missing official dates are never replaced by update timestamps.

Migration `db2345678901` is additive and must precede activation. With the crawler
stopped under the deployment lock, preview `python -m
alphapulse.crawler.repair_collection`; append `--execute` once to establish the
repair budget for existing news gaps. It preserves the previous retry evidence
and records a durable initialization marker. Repeating it cannot renew the budget.
Set the authorized SEC contact in the private instance environment before enabling
SEC; do not put the address or credentials in tracked configuration.

### Authorized 2025 history backfill

`scripts/backfill_sources.py` previews the current baseline without writing data.
`--execute` creates or resumes `/docker/gitea/dev/AlphaPulse/backfill-20250101/state.json`;
`--once` runs one eligible pass. The frozen start is 2025-01-01 Asia/Shanghai.
Both WallstreetCN sources are excluded. Existing US official history lanes are
activated without resetting document retry budgets. Nine other news sources use
real archive captures and the existing idempotent importer; all thirteen official
calendar sources use the archive backfill. Jin10 retains its existing daily gap
scheduler and validated checkpoints. News AI must remain paused.

Each external archive phase retains its reports and capture files. Actual failed
phases wait at least one hour (or a longer Retry-After) and stop after three
attempts; rerunning the driver preserves this budget. Already collected full news
and retired calendar results are retained. A source can remain limited even when
some documents were imported: ECB dynamic index version coverage, inaccessible
BoE speeches and CoinDesk access blocks are reported rather than labeled complete.
The deployment lock serializes writes; crawler/calendar processes are restored in
finally blocks. Do not start a second driver or remove its state to renew quotas.

### Realtime priority and historical AI backfill

Migration `dd4567890123` adds the persistent realtime boundary and pending-event
priority, plus partial unique indexes for active package attempts and event
versions. Pause and drain news AI before applying it; duplicate active records
must be investigated rather than deleted to force migration success. Preserve
originals, attempts and version history. Deployment requires the candidate's
schema head and must leave automatic processing paused during verification.

The model/Skills drawer saves the historical start time with model settings via
**Save and pause**. New settings default to 2026-11-05 00:00 Asia/Shanghai; existing
saved values remain intact. Settings responses expose `control_revision`,
`realtime_from`, `timezone` and queue counts. PUT accepts optional `start_at` and
`base_revision`; version conflicts return 409. Enable uses the saved start only;
a differing legacy enable body returns 422. The first successful enable fixes
`realtime_from`; later saves, pauses and restarts never move it.

Publication time, not ingestion time, determines realtime membership. Realtime
work takes the next available slot without cancelling accepted calls. Historical
work continuously fills free slots from durable news/package/event states, with
no thirty-news admission barrier or batch phase. Assignment remains globally
serial and chronological within a priority lane; a preceding indexing task must
finish before later news is assigned. Failed revisions do not block the lane.
Within each lane, assignment is selected first, then ready event research, then
indexing; realtime work and its in-range known dependencies precede historical
work. Completion wakes dispatch immediately; new arrivals and timers are polled
at most once per second. Existing calls finish normally when concurrency shrinks.

Saving a new start pauses dispatch; the next enable uses the saved scope without
repeating successful stages. Shrinking history does not exclude realtime backlog.
Out-of-range or failed members block event research without dropping evidence.
Legacy batch tables remain historical records, with no new batches created.
Current-revision deferred records and superseded research require explicit retry;
a new event revision can proceed. Settings retain a null `batch` compatibility
field, while `queue` exposes capacity, running_by_stage, per-lane event states and
idle_reasons. A pending event with an active older research version is counted
as waiting_previous, with an explicit wait reason, rather than as ready work.
Missing and stale originals are included in queue counts. Coverage
remains a read-only report.
Ordinary event research uses the existing five-minute quiet window with a
15-minute maximum; urgent events become ready immediately. A running research
call is not duplicated when newer members arrive. New changes start another
aggregation window and obsolete output cannot overwrite current conclusions.
After a worker interruption, started calls become failed with an unknown-result
message and need manual retry; unclaimed work resumes normally. Strict realtime
priority can starve historical work under sustained overload.

Release isolation runs `scripts/smoke_dynamic_queue.py` and `scripts/smoke_priority_queue.py` in its disposable internal
network with synthetic credentials and no model calls. It covers preemption,
late historical ingestion, stable realtime eligibility, range changes,
dependencies, aggregation, control revisions and interrupted-call recovery.


## Simulated trading retirement

The `/trading` page, `/api/trading/*`, `/api/settings/exchange` and its test endpoint
are retired. The API returns 404; the old page address uses the standard not-found
page. There is no exchange credential form or trading worker. Market/K-lines,
news, research results, Chat, shared Skills and model providers remain supported.
Historical migrations remain unchanged.

Migration `f07890123456` follows `ef6789012345` and permanently drops only
`trading_orders`, `trading_events`, `trading_fills`, `trading_symbols`,
`trading_credentials`, `trading_account`, `trading_analyses` and `news_signals`.
It drops children first without CASCADE. Downgrade explicitly fails; neither old
data nor credentials can be recovered by image rollback. No backup is created.

The retirement was executed before the native-service migration. Its current head is `f07890123456`. Historical schema execution procedures and automatic Compose controllers are retired. New schema changes require their own reviewed migration and explicit execution authorization; neither native start nor formal release migrates the live database.
