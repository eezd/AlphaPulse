# 三阶段候选的离线案例与评审表

2026-11-15。本文件中的机构、署名、数字和结论均为**合成测试材料**，不代表这些机构的真实观点。下表是固定输入和原始规则审阅，并未对每个合成输入逐项完成真实模型评审；后续已另做真实12篇新闻和合成评分/分时→日报抽样实验，结果与本表的“待评”栏不能混算。候选为 analysis-v4、period-v3、daily-v3，输出字段和调用上限不变。

每个单篇输入共同使用：`first_seen_at=2026-11-15T02:00:00Z`、`extra.published_at_precision=date`、`extra.published_date=2026-11-15`、`extra.research.content_status=full`、首次接入 `2026-11-15T00:00:00Z`；标题为案例ID，摘要为空，正文如下。除显式例外，`research_context.mode=report`。发表日期不是事件发生时间。metadata 作者不自动变成正文别名或引文。

| ID／来源／署名 | 固定合成正文与例外 | 必须保留／重大错误界限 | 字段与候选规则 | 文本审阅／模型评审 |
| --- | --- | --- | --- | --- |
| C1／citi_insights／林甲 | Citi研究员林甲预计，若通胀连续两季下降，2027年消费增速可能为2%；工资停滞会削弱该判断。 | 归属、条件、期限、2%与工资反证；写成已增长2%属重大错误 | facts自有引文；impact归属；scenarios条件；uncertainties反证 | 已覆盖／待评 |
| C2／citi_insights／无作者 | Citi本文引述丙大学丁教授称，投资可能在2027年回升；文章同时指出订单尚未改善。 | 发布者与被引用人分开；不得推测作者、写成Citi确定预测；无作者不排除材料 | summary/facts归属；keywords主体与投资；uncertainties订单 | 已覆盖／待评 |
| J1／jpmorgan_research／周乙 | J.P. Morgan研究员周乙认为，只有融资成本下降，制造业订单才可能在2027年上半年回升。当前订单同比下降3%。 | 当前下降与未来回升分开；期限、条件、同比基准；不能合并成订单已回升 | facts拆项、自有quote；impact传导；scenarios可观察条件 | 已覆盖／待评 |
| J2／jpmorgan_research／周乙 | J.P. Morgan于2025年1月提出需求复苏假设，该假设以库存正常化为前提。 | 日期改为2025-01-06，context为archive/late_arrival；不得视作今日新结论、计入报告缺失 | 单篇时间规则；程序准入；报告只接收允许的冻结ID | 已覆盖／待评 |
| G1／goldman_insights／陈丙 | Goldman Sachs研究员陈丙预计，若电网扩建获批，铜需求可能于2027年增加；项目延期是主要风险。 | 预测非实绩；批准条件、期限、延期反证；机构名气与铜主题不自动高分 | facts归属；impact对象期限；importance_reason同尺度 | 已覆盖／待评 |
| G2／goldman_insights／无作者 | Goldman Sachs本文讨论人工智能资本开支。甲公司已批准预算10亿元；乙公司仅提出预算20亿元，尚未获批。 | 两家公司、10/20亿元、批准/提案分别记录；不相加为已投资30亿；不自动推导BTC或ETH上涨 | keywords两主体/事项；facts独立证据；impact范围约束 | 已覆盖／待评 |
| M1／普通综合新闻／无作者 | 甲机构上月公告的许可将于明年到期。乙公司今日发布业绩，同比增幅5%。丙研究员认为利率下降可能改善融资；同期BTC上涨1%，材料未提供成交量。 | 旧公告、新业绩、观点和市场表现分离；相关性不等于利率导致币价变化；不得编造量能 | summary新增信息；facts时间；impact区分系统判断；keywords三事项 | 已覆盖／待评 |
| T1／citi_insights／林甲 | 与C1相同。首次采集改为2026-11-15T16:01:00Z（上海11月16日），发表仍11月15日。 | 跨午夜只建库；未知日期和未来11月17日变体也只建库；接入前已有资料不提升 | research_context；report_news_allowed/report_analysis_allowed | 已覆盖／待评 |
| P1／period_map→period_reduce | 输入允许的C1、J1、G1三张卡；另有转载C1一张，标其源ID。每张卡保留相应研究元数据与结果。 | 不将转载算第二次确认；缩减后仍有工资/订单/延期反证、预测期限及机构归属；无行情输入不造价格 | facts/source_analysis_ids/impact/watch_points/scenarios；分片保留规则 | 已覆盖／待评 |
| D1／highlight_review→daily | 三份合成分报：早报将C1误写成“已增长2%”；晚报记为“条件预测”；盘中报无新增。复核仅给C1原文。 | 复核按原文写归属/条件，不声称看过分报；日报得到三报和复核后纠错，标原报告表述不准确、不改写历史；不凭排列杜撰机构改口 | HighlightReview四字段；daily facts/uncertainties/impact；无前日趋势假设 | 已覆盖／待评 |

重要性口径与建议参照区间现以 [keyword-importance.md](keyword-importance.md) 为准：评价发表时点的新增信息与影响，预测/提案保留状态，不仅因历史归档或尚未兑现降档。20/40/70/90边界保持不变。

每个案例填写下列人工记录，`未评`、`不确定`均不得计为通过：

```text
case_id | stage/operation | input_sha256 | old_skill_sha256 | new_skill_sha256
old_result_sha256 | new_result_sha256 | reviewer | reviewed_at
事实/状态 | 自有引文 | 关键事实覆盖 | 主体与事项检索 | 归属 | 时间/期限
条件/反证 | 传导边界 | 重要性同尺度 | 引用ID | 新增重大错误 | 说明
旧/新 state | input_tokens | output_tokens | total_tokens | elapsed_seconds | repair_count
```

评审标准：每项 pass/fail/uncertain 并附原文和输出定位。单篇还检查规范词与原文别名；分报检查归并和输出容量披露；日报检查纠错且不编造时序。零新增重大事实、归属、时间错误为必要条件；关键事实与检索覆盖不得低于旧版。分层报告成功、失败、未发送和未知用量，不能只统计成功样本，也不能以契约通过率代替语义质量。

## 真实对比执行准备

已将上表固定为 [research-cases.json](skills/research-cases.json)：8个单篇输入，加period_map、period_reduce、highlight_review、daily，共12个阶段输入。上下游资料卡、分片摘要、分报与复核均为显式合成夹具；D1故意包含错误早报，不作为真实时间线。输入中的 `input_sha256` 按UTF-8、`ensure_ascii=False,sort_keys=True,separators=(',', ':')` 的JSON计算，全部摘要和嵌套输出契约已离线核验。它们不是真实模型产出。两组对照必须使用同一份固定输入；端到端质量另评。

固定100条继续独立使用原目录和 `scripts/briefing_quality.py`，不能用这些合成例覆盖或改写真实基准。候选文件字节摘要：

| 文件 | SHA256 |
| --- | --- |
| analysis-v4.md | `060973f66a5fd0a9d1dc663990de5337567a823c14dff1649702ab0672d00dba`（`keywords:v3` 本地过滤旧伪别名；新12篇复核10篇有效、2篇高盛无输出超时，未完成模型质量验收） |
| period-v3.md | `bca21f8c7af911026f8afdca2a252b192debc85e20e5a7edea9ceb128348cc3d` |
| daily-v3.md | `a765a13feb622bf74e0901371b6d92a920eca39a5d5edd22d774f4d2fec97b6a` |

旧发布内容此前核验的摘要分别是72：`0104b7f3fff887118fd6b5dc92dc010b270b85d992be0694afaa4e1297c62966`、70：`d3680c2cd5389fdffbf3037a81122154392fdbf1d8f5d7bebdaa8efdabde82e3`、71：`9601b69a9c5fa48368c40eff0e512c7d8c7b5cde17ab4f58fe28f33200c993f3`；正式对照前重新核对导出文本，不用模型版本名代替内容摘要。文件末尾换行与实际传给模型的字符串应分别记录摘要；系统提示现在使用 `config.system_prompt(skill_content, stage)`，统一追加重要性规则及单篇关键词规则；候选已含完整规则时不重复。旧版对照应冻结其原系统提示，不能通过新组合函数悄悄给旧组加入新规则。

以下为候选摘要与基准的离线核验命令；本节保留最初的试验设计，已完成的真实模型复测及当前结论见文末和 `MEMORY.md`：

```bash
sha256sum deploy/skills/analysis-v3.md deploy/skills/analysis-v4.md deploy/skills/period-v3.md deploy/skills/daily-v3.md
backend/.venv/bin/python scripts/briefing_quality.py runtime/quality/analysis-v3-20261114
backend/.venv/bin/python -m pytest backend/tests/test_research_admission.py -q
```

另行获准开展新对照时，先从保留的发布版本72/70/71只读导出旧三份文本，冻结到私有试验目录的 `old-analysis.md`、`old-period.md`、`old-daily.md`，将三个候选复制为 `new-*.md`。记录每个文件字节SHA256、完整输入SHA256、模型/provider、reasoning_effort、max_tokens、context_window和schema哈希。两组参数与输入相同；选同一provider，禁止为了通过而截断或偷偷调大上下文。实际传输沿用 `ai.submission.complete`（既有有限结构修复及无输出重试），分别用 `NewsAnalysisResult`、`ReportResult`、`HighlightReview`。不启动生产worker、不写业务结果。

执行单元的调用形式如下；只在取得真实调用授权且建立独占试验配置后执行。`provider`须由明确选定的相同模型参数构造，不能自动读取生产默认或降级模型：

```python
import time
from alphapulse.ai import submission
from alphapulse.briefings import contracts

async def run_case(provider, prompt, frozen_input, schema):
    started = time.monotonic()
    if not submission.fits_context(provider, prompt, frozen_input, schema):
        return {'state': 'blocked', 'not_sent': True, 'output': None, 'usage': None}
    result, usage = await submission.complete(provider, prompt, frozen_input, schema)
    if schema is contracts.NewsAnalysisResult:
        contracts.validate_evidence(result, frozen_input)
    return {'state': 'done', 'not_sent': False, 'output': result.model_dump(),
            'usage': usage, 'elapsed_seconds': time.monotonic() - started}
```

调用异常应由试验驱动记录为失败、保留原因和异常附带usage，不自动跳过或改写输入。分报输出用 `contracts.validate_references` 核对引用；模型结果需留全量原始审计并绑定上述hash。比较单个阶段时上下游输入固定，端到端链路另列，不能混淆两个口径。真实100条的candidate/reviews文件格式与核验命令仍遵循 `analysis-quality.md`。

此前已授权完成12篇真实单篇v3/v4对照，私有结果见 `runtime/quality/analysis-v4-twelve-news-20261115T105032152263Z/`，使用的是追加本轮词表和评分规则前的候选。随后同12篇使用 `b5a8fc0d737a3498fb68a1db5fd123903889d65ea0cbeeffa8227b27bcac8bc6` 复测，合成评分参照和三份分时→日报链另做真实模型调用，详见 `MEMORY.md` 顶部与私有复核报告。修复真实高盛长文遗漏的 `926070016da0ff20563145a9443d06948d8e4cfbb73b2d617ce432bd24448b61` 候选在12篇回归中产生JPM 3.7%与3.4%的伪冲突，因此**未通过**。随后 `c77b...` 增加数值方向核对，先JPM/高盛后其余10篇复测：两篇通过，后10篇有1篇DOJ关键词quote非单字段连续原文、9篇通过结构/证据，因此当时候选未通过；私有结果见 `runtime/quality/sequential-quality-20261116T033923800542Z/staged-review.md`。最新 `060973...` 增加短引文逐字复制规则，DOJ复测通过，但剩余11篇中JPM消费输出不存在的别名 `U.S. consumer spending`，被证据校验拒绝；12次基础调用、无结构纠错，当前候选仍**未通过**。完整逐篇记录、调用摘要及只读生产快照见 `runtime/quality/sequential-quality-20261116T041133171508Z/quote-review.md`。之后本地`keywords:v3`只在结果处理阶段审计并剔除无法逐字核对的别名：对同12篇冻结原文及原始模型输出离线重放，JPM该别名被剔除，另11篇业务结果不变；这一回放不是新提示词或新模型调用的质量验收。程序验证及既有输出重放不等于模型语义通过；不以Token判断优劣，用量仅作调用记录。

最新 `keywords:v3` + `060973...` 限量两阶段真实复核，DOJ/JPM消费先通过，另外8篇结构/逐字证据和定向事实检查通过，但两篇高盛请求均遭上游无输出临时故障并在600秒超时，因此**12篇完整验收仍未完成**；本轮10篇有效输出没有触发无效别名剔除，剔除路径以离线重放/隔离入库测试为证据，不能宣称全面质量通过。无模型正文的失败请求用量未知，不能记零；没有自动重发或Skill发布/生产部署。私有逐篇结论、原始重试记录及前后只读快照见 `runtime/quality/sequential-quality-20261116T053320616995Z/trial-review.md`，第一阶段结果在 `runtime/quality/sequential-quality-20261116T052725440873Z/`。
