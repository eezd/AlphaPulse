# AGENTS.md

> 本文件适用于 `/srv/AlphaPulse`，补充 AlphaPulse 的业务、验证与资源边界。通用开发行为、授权、代码质量、记忆维护及 Pi 子代理调度遵循全局 `/root/.pi/agent/AGENTS.md`；用户最新明确要求优先。

## 项目定位

- **项目名称**：AlphaPulse。
- **目标与边界**：采集、归档和检索财经新闻及经济日历，展示 Binance 行情，通过单篇分析与可复用分时／日报辅助研究，并提供 Chat 与 Skills 管理。旧交易信号和旧事件研究流程已退役；新闻高星推送、价格告警不属于已完成的当前流程。
- **技术栈与运行环境**：Python 3.13、FastAPI、SQLAlchemy 异步会话、asyncpg、Alembic、Scrapy、Playwright；PostgreSQL 18、Redis 8。前端为 Vue 3、TypeScript 6、Vite 8、Element Plus、Pinia；原生构建使用 Node 24、pnpm 12.11.2、uv，依赖分别锁定于 `backend/uv.lock` 和 `web/pnpm-lock.yaml`。
- **交付形式**：Linux systemd 六服务及同源网页；正式发布另提供六平台安装归档和 Linux 双架构 OCI 镜像。安装归档需要外部运行时及依赖安装，不是独立可执行文件。
- **核心业务不变量**：原始新闻、修订和已执行分析／报告审计必须保留。单篇自动分析冻结输入并复用结果，来源更新不自动覆盖成功版本；重新分析需显式操作。报告复用冻结分析与既定依赖，不重新读取整天原文。

## 项目结构与关键入口

| 位置 | 职责 |
| --- | --- |
| `backend/src/alphapulse/api/main.py` | FastAPI、鉴权路由、SSE／WebSocket 与编译前端挂载 |
| `backend/src/alphapulse/crawler/` | 来源调度、Scrapy 爬虫、原始响应及新闻落库 |
| `backend/src/alphapulse/briefings/` | 单篇分析、研究准入、队列、覆盖率、分时／日报和故障通知 |
| `backend/src/alphapulse/ai/` | 模型调用、提交校验、重试、进度与故障恢复 |
| `backend/src/alphapulse/calendar/`、`backend/src/alphapulse/market/` | 经济日历／金十日历及行情任务 |
| `backend/src/alphapulse/core/`、`backend/alembic/` | 配置、数据库、Redis、认证和迁移 |
| `web/src/pages/`、`web/src/common/apis/`、`web/src/router/` | 页面、接口契约与导航 |
| `backend/tests/`、`web/tests/`、`scripts/tests/` | 单测及交付脚本测试 |
| `scripts/native.py`、`scripts/maintenance.py` | 原生候选构建、维护排空、激活及恢复 |
| `scripts/smoke_*.py`、`.forgejo/workflows/`、`deploy/` | 集成验收、镜像／发布工作流和运维说明 |

- **数据流**：爬虫保存原始响应、正文与新闻修订 → 来源策略及时间边界决定分析准入 → worker 冻结输入／SKILL／规则并调用模型 → 严格证据校验与关键词处理落库 → 分时复用单篇卡片，日报复用三个分时及有限重点原文 → API 向网页提供查询与进度。
- **外部依赖及数据**：PostgreSQL 保存新闻、日历、Chat、Skills、任务和审计；Redis 保存应用状态、租约及会话；外部网站、Binance、配置的模型提供方和 Telegram 是真实网络边界。构建位于 `runtime/builds/`，当前链接为 `runtime/current`，root 服务的 Playwright 缓存位于 `runtime/ms-playwright/`，原有 `runtime/data/` 保留，私有备份与质量样本分别位于 `runtime/backups/`、`runtime/quality/`。
- **权威文档**：交付入口 `deploy/README.md`；简报契约 `deploy/briefings.md`；来源与研究准入 `deploy/research-sources.md`；关键词与评分 `deploy/keyword-importance.md`；质量评审 `deploy/analysis-quality.md`、`deploy/research-skill-cases.md`。这些文档包含明确标为历史的段落，旧研究／Docker 操作不能直接执行；当前行为须核实源码。

## 开发与验证

以下命令均从仓库根目录理解；表中注明的子目录内执行相应命令。运行前确认环境变量来源，普通测试不加载生产配置。

| 目的 | 确认过的命令或入口 | 环境要求／副作用 |
| --- | --- | --- |
| 后端依赖 | 在 `backend/` 执行 `uv sync --locked --python /usr/bin/python3.13` | 创建／更新本地虚拟环境，可能联网；不得借此更新锁文件 |
| 前端依赖 | 在 `web/` 执行 `pnpm install --frozen-lockfile` | 安装依赖，可能联网 |
| 本地 API | 在 `backend/` 执行 `uv run uvicorn alphapulse.api.main:app --host 127.0.0.1 --port 8000` | 显式设置隔离数据库、Redis 及合成配置；请求可能写入测试数据，不默认启动 worker |
| 前端开发 | 在 `web/` 执行 `pnpm dev` | Vite 监听 `127.0.0.1:5173`，将 `/api`（含 WebSocket）代理到 `127.0.0.1:8000` |
| 后端定向测试 | 在 `backend/` 执行 `uv run pytest -q tests/test_briefing_keywords.py` 等相关测试 | 使用现有 fixture 隔离 I/O；不连接生产或真实模型 |
| 后端强制检查 | 在 `backend/` 执行 `uv run pytest -q` 及 `uvx ruff==0.14.14 check --select F,E9,B src tests` | 对应原生候选的后端检查，Ruff 不自动修复 |
| 前端强制检查 | 在 `web/` 依次执行 `pnpm lint:check`、`pnpm typecheck`、`pnpm test`、`pnpm build` | 构建写入 `web/dist/`；`pnpm lint` 带 `--fix`，不能当只读检查 |
| 交付脚本测试 | `python3 -m unittest discover -s scripts/tests -q` | 具体脚本 Ruff 检查集合以 `scripts/native.py:build()` 为准 |
| 集成验收 | `scripts/smoke_briefing_keywords.py`、`scripts/smoke_research_admission.py`、`scripts/smoke_briefings.py` 等相关入口 | 会写数据库／Redis，部分测试重置数据；先读脚本，使用独占临时 PG18／Redis8、合成凭据和替身模型 |
| 原生构建及部署 | `bash /srv/AlphaPulse/start.sh` → `scripts/native.py:main()` | **构建后会维护排空并切换六个服务**；不是普通开发命令，需部署授权。`build()` 单独调用不激活，但会安装依赖、写构建产物和浏览器目录 |
| 正式发布 | Forgejo Actions → `Manual release` → Run workflow（main 控制分支） | 来源分支默认 main，SHA 可选；执行时冻结 SHA，仅发布 Forgejo，不部署本机，需发布授权 |
| 手动提升 | Forgejo Actions → `Manual promote dev to main (AlphaPulse)` → Run workflow（main 控制分支，无输入） | 仅普通快进固定远程 dev 到 main，不触发发布／同步／部署；首次安装及真实提升分别授权 |
| 对外同步 | Forgejo Actions → `Manual GitHub sync` → Run workflow（main 控制分支） | 独立手动同步获准分支、TAG 和已验收原件至 GitHub／GHCR；不重建、不强推、不删历史 |

- **最小验收**：按改动范围完成相关测试；进入原生候选必须通过上述后端、前端与交付检查。数据库／准入／队列变更还需隔离迁移和对应集成验收；页面变更检查桌面 1440px 与移动端 390px 的主要交互及溢出。
- **质量验证**：结构和逐字证据通过不代表语义准确；替身模型、保存输出重放、合成案例和真实模型评审分别记录。真实模型评审须限定样本及调用范围，不能把未知用量记为零或把 Token 下降当质量提升。
- **生成文件**：词表源为 `backend/src/alphapulse/briefings/data/lexicon/sources/`、`overrides.json`；用 `python3 scripts/build_briefing_lexicon.py` 生成 `lexicon.json`，以 `--check` 只读复核，保留来源摘要及许可证。不要直接改生成词表。`web/dist/` 与 `web/types/auto/` 由构建工具生成；运行中构建不作为源码修改入口。

## 项目特有约束

- **新闻及报告契约**：单篇冻结完整原文；超出模型上下文的全文仅可按有证据的连续分段（最多 32 段）分析并汇总一次，无法完整覆盖或汇总时明确失败，不截断后伪装完整分析。分段及在途状态持久化，未知结果不自动重放。事实和关键词引文必须在冻结标题／摘要／正文的单个字段内连续逐字出现；仅无效别名允许按 `keywords:v3` 剔除并保留原始值与审计，任一关键词无剩余别名则失败，不放宽引文验证。单篇分析结构／证据纠错共用一次额度。
- **报告预算与时间**：上海时区早间 00–09、晚间 09–18、日内 18–24，窗口结束后五分钟开始检查，日报次日 08:05。分时报最多 16 个 map 与一次汇总；日报复用三个分时及最多九条冻结重点原文；空窗口不调用模型，不递归压缩。`blocked` 不因后续补齐自动重开，手动补报属于新任务。
- **来源边界**：`collect_only` 必须在分析领取、重试、报告覆盖率及手动补报中一致生效。研究来源启用后区分历史归档与同上海发表日的新文章；以冻结准入信息决定报告资格，不凭采集时间伪造新发表。`wscn_live` 已退役，`wscn_news` 保留；CLS 仅采集获准的头条文章，不能自行扩展为电报或全站。
- **行情及重试**：报告窗口收益仅使用完整对齐的 Binance 一分钟数据，不用邻近数据或 24 小时 ticker 替代；区分现货与永续。每个模型请求独立处理无输出暂时故障：5 秒最多五次，再按 180／600／900 秒及后续每 900 秒等待；确认无输出的上游流中断也适用。已有文字、思考或工具输出后不得自动重放。队列重置仅针对精确等待 UUID，不等于取消、重排或重新调用任务。
- **数据与资源**：Redis **DB 14 与 AFFiNE 共享**，只操作本应用 `ap:*` 键；禁止 `FLUSHDB`、`FLUSHALL` 或全局清理。Docker、数据库及其他应用均非本项目独占，临时资源必须唯一命名并只清理本次创建的对象。
- **敏感信息**：原生私有配置唯一明确入口为 `/srv/AlphaPulse/runtime/config/app.env`；根目录 `.env` 可能指向历史配置，禁止隐式读取或使用。备份、模型原始响应和质量样本按私有数据处理，不复制到公开源码、日志或发布产物；授权操作需要凭据时仅在进程内使用，不打印秘密。
- **测试与生产**：生产 worker 可持续产生付费模型请求及已配置的故障通知。启动 worker、启用 AI／研究来源、批量重试／补报、发布 SKILL 或运行真实模型评测都要核实当次授权，不得用测试或修复名义隐式触发。当前通知范围为运维故障，不能顺带启用新闻／价格推送。
- **迁移与恢复**：生产数据清理、迁移前须按对应 runbook 做私有完整备份，并将实际备份恢复到隔离数据库核验。保留历史 Alembic 链，不为清理退役代码删除迁移或历史审计。加法 schema 也不等于任意旧代码可回退，核实 `scripts/maintenance.py` 的兼容对。
- **候选发布隔离**：依赖安装、测试和打包在独立 PID／文件系统的非 root 容器执行，只接收固定 SHA 导出的源码与只读控制树，不传入 `.git`、未跟踪文件、宿主 HOME／工作区挂载、Secret、Docker socket／TLS 证书或认证文件；环境变量白名单不能代替隔离。产物仅接收有限大小的固定普通文件集。资源按 run＋attempt 精确归属，控制器 finally 与工作流 always 共用幂等清理，不全局 prune。OCI 认证与临时空 authfile 匿名验收均核对顶层及 amd64／arm64 子清单实际摘要，不用默认架构 Docker pull 代替。
- **部署与回滚**：原生六个系统级 unit 以 root 运行，不依赖专用服务账号。服务为 `api`、`crawler`、`market`、`calendar`、`news-risk`、`jin10-calendar`（unit 前缀 `alphapulse-`，其中 `news-risk` 运行新 `alphapulse.briefings.worker`）。生产 API／网页共用 `127.0.0.1:21005`。脚本不自动备份或迁移数据库；失败恢复不恢复数据库内容或 SKILL 指针。`runtime/deployment.pending.json` 是恢复证据，不得直接删除以绕过检查。
- **本地 Git 与分发目标**：本地仅保留 Forgejo `origin`，fetch 及全部 push URL 均为 `https://git.134333.xyz/eezd/AlphaPulse.git`，仓库级默认推送目标 `remote.pushDefault=origin`；业务分支 main／dev 分别跟踪 origin/main／origin/dev。GitHub／GHCR 仅由 Forgejo 独立手动同步工作流按明确 HTTPS URL 与 CI Secrets 访问，不要求本地 GitHub remote、专用 helper 或登录文件。连接清理仅移除获准的仓库配置及旧远端跟踪引用，保留 Forgejo 认证；不读取／删除凭据文件，不改全局 Git／SSH／Registry 配置或 CI Secrets。不因修改 upstream 就认为引用已更新或历史可合并；缺失分支、未知映射、分叉及不完整对象先报告，不顺带下载对象、切分支、合并、重置或推送。
- **工作区与发布范围**：本目录存在跨任务未提交改动，且部分已随原生构建上线；不能仅凭 Git dirty 状态判定待发布内容。定向发布以实际运行构建为基线，冻结文件范围及摘要，防止 `start.sh` 打包整个工作区混入其他任务。新版 `.forgejo/workflows/release.yml` 在目标 Forgejo 完成接入和验收后，普通 push／tag 只更新代码；发布和 GitHub 同步各自手动执行。在新版提交尚未生效的远端，仍须盘点并防范旧自动镜像任务。正式发布、同步及可选 dev→main 提升由可信 main 上三个独立手动 Actions 工作流执行，共享 `alphapulse-delivery` 并发组，`cancel-in-progress: false`；不提供本地发布／同步／提升客户端。发布输入为来源分支及可选完整 SHA，SHA 留空在任务执行时冻结分支 HEAD，UUID 自动生成。Actions Secret `RELEASE_TOKEN` 在发布步骤映射为 `FORGEJO_RELEASE_TOKEN`，同步和提升步骤映射为 `FORGEJO_TOKEN`；仅同步步骤持有 `MIRROR_TOKEN` 并映射为 `GITHUB_TOKEN`。控制器只接受环境凭据，不读取 HOME Token、宿主 Git helper 或 Registry 登录文件；Git／Registry 分别使用本次运行的临时 0700／0600 认证配置，Git 使用精确 HTTPS 仓库路径，Registry 使用 stdin 登录，结束清理。测试必须隔离 HOME 和凭据、模拟 API；不隐式复用旧 Gitea 凭据；缺少仓库、Runner 或权限时只阻塞远端步骤。发布原件仅临时构建，完成版本不可覆盖；仅 OCI `:latest` 为获准可变别名，两端按 main 祖先关系选择唯一最新已验收 main 版，固定摘要复制完整双架构、不重建或创建 latest Git TAG；dev／历史重跑不回退，未知别名或 main 分叉须停止核对。未完成版必须证明原 SHA、终止的旧任务与产物身份后精确清理并全量重建，不使用 Generic 暂存或持久检查点。

- **手动提升契约**：`.forgejo/workflows/promote-main.yml`／`scripts/promote-main.py` 仅接受可信 main 的无输入 workflow_dispatch，固定检出 run 的 main SHA，实际执行时在临时 bare Git 冻结远程 main／dev 全 SHA；控制 SHA 必须属于冻结 main 祖先历史。一致须读回确认后零写入，否则仅 main 为 dev 祖先时允许普通快进。push 前复查两端，任何变化即失败；源 refspec 是冻结 dev SHA，只写远程 main。未尝试 push 的失败保持零写入语义；从尝试 push 起，推送／读回传输、退出码、解析或值不一致均明确“main 可能已更新”、阶段／目标／脱敏原因和人工核对，不自动重试／回滚／修复，保留异常链、不回显原始 stderr。只有两端读回符合预期才完成，不检出执行 dev、不运行应用测试或创建 merge／TAG。Actions 并发组不约束人工 push，写前复查不是原子锁；首次须另行授权将已审查文件安装到可信 main，保留本地历史，不以 reset／强推安装，也不自 dispatch。模拟验证、远端安装和真实 Actions 写入验收必须分别报告，具体步骤见 `deploy/README.md`。

## 项目记忆与接续

- 唯一项目记忆为根目录 `MEMORY.md`，目前保持本地未跟踪策略；不修改 `.gitignore`、不自动暂存或提交记忆。
- 新会话先读本文件与记忆，再核实相关源码；记忆中包含多个历史阶段，过去的 AI 开关、构建路径和验证结论不能当作当前状态。实际运行版本以 `runtime/current`、服务与只读数据库核查为准。
- `AGENTS.md` 保存稳定边界和检查入口；`MEMORY.md` 保存有日期的状态、未完成任务、已知风险及接续入口，由主代理维护。具体线上版本、队列数量和临时质量结论不固化到本文件。

## 本项目的子代理任务划分

- 已允许按需使用现有 `pi-subagents`；沿用全局角色和 Pi 已配置模型，不重新指定模型或思考等级。
- **可独立并行**：不同爬虫／来源的只读排查，后端与前端的独立审查，文件边界互不相交且接口已约定的局部实现。共享工作区仍默认单写入者。
- **必须统一协调**：API 返回结构与 `web/src/common/apis/` 类型、分析 schema／冻结输入／SKILL 模板、Alembic 链、任务领取／进度契约、锁文件、生成词表、原生维护脚本及 `MEMORY.md`。存在接口依赖先串行。
- **交接必带**：实际 cwd 和源码／构建基线、已实现与未上线部分、文件责任范围、数据库／Redis 是否独占、可执行检查、是否允许真实网络／模型调用，以及 `start.sh` 会部署、DB14 共享、根 `.env` 禁用这三项边界。子代理不自行执行生产操作或扩大评测样本。

## 维护要求

项目结构、命令、数据契约或运维边界变化时更新对应段落，并核实受影响的文档入口。删除失效描述，不把发布流水账或临时服务状态追加到本文件；通用规范仍由全局 `AGENTS.md` 维护。
