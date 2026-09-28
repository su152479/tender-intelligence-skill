# 工程项目机会雷达 MVP

面向预制构件企业的个人机会发现工具。当前版本打通“数据源配置 → 公告采集 → 规则/AI分析 → 公告观察 → 工程实体与事件 → SQLite → Markdown 日报与本地知识库”。已接入中交招采网、中建云筑网、中铁鲁班网、北京与河北公共资源平台、天津交通、中国政府采购网和京津冀协同专区，并统一只保留京津冀及雄安新区机会。

仓库根目录包含 `SKILL.md`，可直接作为 Codex Skill 安装或引用。Skill 只包含公开代码、配置模板和测试，不包含运行数据库、账号、Cookie、浏览器登录状态、附件、日志或网站部署信息。

## 已实现

- `config/sources.yaml` 管理各采集来源及其公开入口、采集方式和搜索词；未实现的来源保持禁用，不会被日常任务误调用。
- Playwright 首次人工登录并保存 `storage_state`，后续 collector 可复用 cookie/session。
- SQLite `project` 表、去重更新、原文与分析 JSON 留存。
- 默认离线启发式分析；配置 API 后可选择 OpenAI JSON 分析。
- 模拟采集、真实来源采集、Markdown 日报和 Obsidian 安全导出均可独立运行。
- 中交招采网按产品词和桥梁、盾构、装配式、管网、泵站等工程方向词调用公开搜索与详情接口，保存真实详情 URL 和公告原文，不触发登录或滑块验证。
- 中铁鲁班网读取官方公开首页，并尝试搜索公开历史公告；同时识别产品词和桥梁、路基、附属工程、隧道、管网等工程方向词，无需登录。
- 中建云筑网复用一次人工登录保存的会话，按精简的产品词和工程方向词搜索；单个搜索词超时会继续其他词，登录失效会明确提醒，不处理验证码。
- 北京、河北公共资源平台、天津交通、中国政府采购网和京津冀协同专区统一识别施工、工程总承包、桥梁、路基、隧道、管网、水工及附属工程方向。政府来源另设“城市更新”和“水利工程”调查方向：前者覆盖城中村、老旧小区、片区/街区、基础设施与地下管网更新，后者覆盖河道治理、防洪排涝、灌区、泵站、水闸、堤防、引调水、水库和水生态治理。规划编制、咨询、监理、检测、评估等非施工服务仍会排除；调查方向只负责召回，不会替代预制产品证据评分。
- 京津冀公共资源交易协同专区作为独立补漏源，覆盖工程招标计划、招标公告、中标候选人公示和中标结果；结果阶段只将公告可参与性标为 `CLOSED`、当前商机分归零，不再关闭工程线索或后续产品销售跟进。
- 附件情报闭环支持从公告页发现 PDF、DOCX、XLSX、ZIP，限制体积并检查 ZIP 条目后提取正文、工程编号和文件哈希，写入 SQLite FTS5 全文索引；附件证据不会自动改写历史机会评分。
- 政府网站使用单来源串行、1–2.5 秒随机间隔、有限分页和 403/429 熔断；不绕过验证码。北京站仅在 Python TLS 不兼容时降级到 Windows 系统 TLS，仍保留证书与主机名校验。
- 本地分析先抽取“本次采购对象”，再建立目标产品与业务动作的关系。证据分为“直接产品证据 / 工程需求证据 / 明确施工方法 / 工程方向推断”，并把项目线索价值与当前商机价值分开计分。
- AI/规则分析使用 Pydantic 严格输出契约：评分限定 0–100、证据等级和置信度使用固定枚举，任何潜在产品必须存在对应的命中证据与证据位置。
- 每条项目保存分析器版本和产品规则指纹；修改 `products.yaml` 后可识别新旧结果是否由同一套规则产生。
- 每轮采集生成唯一 `run_id`，按来源记录采集数、入库数、拒绝数、耗时、分析器版本和规则版本，便于区分真实 0 条与流程异常。
- “今日”统一按 `Asia/Shanghai` 和 `notice_observation` 计算：首次发现为 `NEW`，历史公告有效内容变化为 `UPDATED`，无变化的重复观察为 `SEEN_AGAIN`。只有已完成或可接受的部分成功来源运行参与日报；`project.created_at` 不再承担今日新增语义。
- 每个来源记录“搜索请求 → 原始列表 → 地区/时效 → 工程候选 → 校验 → 入库”采集漏斗，并将健康状态区分为 `HEALTHY / DEGRADED / SUSPECT / FAILED`。系统优先比较原始列表历史基线，不再因为最终机会为 0 就误报来源异常。阈值可通过 `RADAR_SOURCE_HEALTH_WINDOW`、`RADAR_SOURCE_HEALTH_MIN_SAMPLES` 和 `RADAR_SOURCE_ZERO_AVG_THRESHOLD` 调整。
- 每个被证据命中或被反证排除的“公告—产品”判断都会写入 `notice_product_assessment`，保留规则版本、证据、反证、工程需求、公告范围及当前机会状态，便于重评和审计。
- “另行采购、另行招标、甲供”等表述不再被当作产品不存在：当前公告得分为 0，但会进入“后续专项采购跟踪”池和日报独立板块。
- 可用 `radar feedback` 记录人工确认、无关、漏产品、错产品和重复公告，形成困难样本回归闭环。
- 提供人工标注回归评测，当前基线覆盖直接采购、工程方向推断和无关公告，可持续扩充漏报、误报案例。
- 文件和控制台日志；账号、密码、Cookie 均不写入代码或 Git。
- SQLite 启动时使用 `schema_migration` 记录当前结构基线并校验迁移名称、顺序和内容哈希；数据库版本高于程序或迁移记录被改写时会拒绝启动，避免旧程序静默修改新库。当前版本仍保留既有加法式兼容补列，首个带实际 DDL 的版本迁移将在备份/恢复流程完成后启用。
- 轻量混合采集内核会把页面响应分类为 `OK / AUTH_EXPIRED / WAF / RATE_LIMIT / STRUCTURE_CHANGED / UPSTREAM_BROKEN / ERROR`，避免把技术故障误报成真实 0 条。
- 动态页面可配置多个经过审核的 Playwright 定位方式，程序会优先复用上次成功项；所有定位均失效时标记页面结构变化，而不是盲目点击。
- 采集器输出在入库前统一校验名称、来源、URL、地区和发布日期；失败诊断仅保存在本机 `data/diagnostics/`，并对常见认证字段脱敏。

## 快速开始

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
radar init
radar --version
radar run --mock
radar run --source cccc
radar login cscec_yzw
radar run --source cscec_yzw --source crecg_luban
radar run --source beijing_ggzy --source ccgp
radar status
radar followups
radar evaluate
radar feedback 123 --label WORTH_TRACKING --product 管片 --note "人工确认值得跟踪"
radar reanalyze
radar reanalyze --apply
pip install -e ".[documents]"
radar enrich --project-no S110000A001037484009
radar documents
radar document-search "2023-188LS"
radar cleanup-documents --dry-run
radar cleanup-documents
# 建立高可信工程实体（正式工程标识或名称+地区+业主完全一致）
radar build-projects --samples 10
# 人工确认多个公告属于同一工程
radar confirm-project --notice-id 101 --notice-id 205 --canonical-name "某工程"
# 生成并查看只供人工审核的工程关联候选
radar build-project-candidates --samples 20
radar project-candidates --status PENDING --limit 20
radar review-project-candidate 18 --confirm --note "已核对官方编号"
radar review-project-candidate 19 --reject --note "建设单位不同"
radar review-project-candidate 20 --defer --note "等待正式编号"
# 审计地区/建设单位身份字段，并动态查看候选簇
radar build-identity-facts
radar identity-facts 537 --all-versions
radar identity-diff
radar identity-review-queue --fact-type OWNER --limit 30
radar identity-review 811
radar review-identity-fact 811 --confirm --note "已核对公告字段" --dry-run
radar review-identity-fact 811 --reject --note "实际为代理机构"
radar override-identity --notice-id 537 --fact-type OWNER --value "涿州市住房和城乡建设局" --note "人工核对"
radar clear-identity --notice-id 193 --fact-type REGION --note "当前资料无法确认所在地"
radar identity-review-status
radar project-identity-audit
radar project-clusters --limit 50
# 每次只明确确认一个子集或指定候选边
radar review-project-cluster 249 --confirm-notices 249 299 --note "已人工核对"
radar review-project-cluster 626 --confirm-parent-candidates 32 --note "同一上级工程"
radar review-project-cluster 626 --reject-candidates 33 --note "证据不足"
# 从正式工程关联公告建立事实事件，并查看工程时间轴
radar build-project-events
radar project-timeline 59
radar project-timeline 59 --show-sources
radar project-relation-candidates --project-id 59
radar project-event-integrity
# 只读推导当前已观察生命周期；不会写回工程实体
radar project-lifecycle 59
radar project-lifecycle 59 --show-events
radar project-lifecycle-audit --samples 20
# 搜索系统已找到附件直链时，可挂到唯一公告下
radar enrich --notice-id 539 --attachment-url "https://官方域名/初步设计.pdf"
radar export-obsidian --vault "C:\Users\你的用户名\Documents\工程机会知识库"
```

日报位于 `data/reports/`，数据库位于 `data/radar.sqlite3`。
`radar reanalyze` 只预览历史重评数量；`--apply` 会先在 `data/backups/` 创建 SQLite 一致性备份，再用当前分析器和产品规则回填历史记录及逐产品评估。
`radar export-obsidian` 使用 SQLite 只读连接生成本地 Markdown 知识库。自动区块可以重复更新，“我的跟进记录”不会被覆盖；完整原文、账号、Cookie、API Key、日志和 SQLite 文件不会进入 Vault。
`radar project-lifecycle` 只从 Canonical Project Event 动态推导“当前已观察到的最先进阶段”，不会更新已弃用的兼容字段 `engineering_project.lifecycle_stage`，也不会修改产品机会、采购窗口或项目跟踪建议。结果中的 `coverage_scope` 用于约束结论范围：`PROJECT_WIDE` 表示工程级事实，`PARTIAL_LOTS` 表示多个已观察标段，`SINGLE_LOT` 表示单一标段，`UNKNOWN` 表示事实不足以声明完整覆盖。`stage_confidence` 是证据质量等级，不是概率。
`radar enrich` 只访问公告页及其允许的官方域名附件，默认单文件不超过 100MB；遇到验证码、403、418 或 429 会停止，不做绕过。已成功解析且本地文件仍存在的附件会直接跳过。可用 `--extra-page` 补充同项目官方资料页，或用 `--attachment-url` 补充已确认的官方附件直链，再通过 `radar document-search` 检索附件正文和工程编号。文本极少的 PDF 会标记为 `needs_ocr`，本版不自动执行 OCR。
`radar cleanup-documents` 默认删除解析完成超过 2 天的本地附件原文件，只把附件状态改为 `PURGED`；SQLite 中的正文、工程编号、文件哈希、来源 URL 和 FTS5 全文索引均继续保留。命令只允许删除 `data/documents/` 内的普通文件，并可先使用 `--dry-run` 预览。保留天数可通过 `.env` 的 `RADAR_DOCUMENT_RETENTION_DAYS` 调整。
日报的“今日新发现”默认只展示项目线索分达到 30 分的首次发现公告，可通过 `.env` 的 `RADAR_REPORT_MIN_SCORE` 调整；历史公告内容更新单列，“再次观察”只显示数量。网站首页使用同一 observation 生成的公开安全快照，历史页仍展示全部累计公告。
中交每个关键词默认读取最新 5 条、保留近 30 天公告；可调整 `RADAR_CCCC_RESULTS_PER_KEYWORD` 和 `RADAR_LOOKBACK_DAYS`。该来源使用独立的持久 Edge 配置目录保存网站自己的会话 Cookie，并以可见浏览器、低频节奏运行；不会读取个人日常浏览器资料。若官方 WAF 返回 403/418 或出现 429 限流，程序立即停止该来源、保存本地诊断，并在当天熔断后续自动请求，次日才恢复单次尝试。程序不隐藏自动化标识、不破解验证码，也不绕过访问控制。

所有公开 HTTP 来源共享按域名限速、SQLite 响应缓存和有限重试策略。列表页默认缓存 5 分钟，详情页默认缓存 24 小时；连接超时和 5xx 最多尝试 2 次。403/418 不重试并熔断到次日，429 遵循 `Retry-After`（缺失时默认冷却 6 小时）。Windows curl 仅在 Python TLS 握手不兼容时使用，不再把 HTTP 403 当作 TLS 问题处理。

网络通道可通过 `.env` 的 `RADAR_PROXY_URL` 配置首选固定代理，例如本机 Clash/Mihomo 的 `http://127.0.0.1:7890`。启用 `RADAR_DIRECT_FALLBACK=true` 后，只有确认首选代理存在时才允许一次直连回退：HTTP、Playwright动态页及人工登录页在代理出现连接/TLS错误、访问限制或页面不可用时，关闭原会话并创建不继承代理环境的直连会话。直连仍失败才熔断；登录失效和验证码不会触发通道切换。程序不会轮换代理、破解验证码或循环切换网络。
商业平台在 `sources.yaml` 中用 `keywords` 保存产品知识词，用 `search_keywords` 控制更宽的站内搜索入口；结果仍会经过京津冀地域过滤和产品知识库评分。
鲁班公开历史搜索默认开启，可用 `RADAR_LUBAN_HISTORY_SEARCH=false` 临时关闭。历史页面不可用时采集器会保留首页结果，并把来源记录为“部分异常”，不再误报成“正常 0 条”。
所有已接入数据源均通过 `sources.yaml` 和本地正文校验限定北京市、天津市和河北省。云筑网采用保守过滤：卡片未出现可确认的京津冀地名时不会入库。

政府来源每天运行一次即可。`RADAR_BEIJING_GGZY_MAX_PAGES` 和 `RADAR_CCGP_MAX_PAGES` 控制最大分页，`RADAR_GOV_DELAY_MIN_SECONDS` / `RADAR_GOV_DELAY_MAX_SECONDS` 控制请求间隔。初次回溯结束后建议降低页数；遇到验证码、403 或 429 时等待或联系平台申请正式数据接口。

如需 OpenAI 分析，安装 `pip install -e ".[ai]"`，在 `.env` 设置 `RADAR_AI_PROVIDER=openai` 和 `OPENAI_API_KEY`。未配置时完全使用本地知识库规则。

## 人工登录

先安装浏览器：

```powershell
playwright install chromium
radar login cccc
```

浏览器打开后人工输入账号、密码并处理验证码，完成后回终端按 Enter。状态保存在 `data/auth/<source-id>.json`，已被 `.gitignore` 排除。不要分享该文件。当前配置为人工登录的 ID：`cccc`、`cscec_yzw`；鲁班网及政府来源使用公开入口，无需登录。铁建云采和中电建仍保留为规划来源，但在真实 Collector 完成前保持禁用。

云筑 collector 会通过 `browser.new_context(**login_manager.context_kwargs(source_id))` 复用状态；若页面跳回登录页，会停止该来源并提醒重新人工登录，不尝试破解验证码。中交和鲁班公开公告无需登录。

政府公开来源现支持北京工程招标、京冀公共资源交易跨区域信息专区（河北）、京津冀公共资源交易协同专区和天津市交通运输委员会招标公告。采集器采用限页、低频、近 30 天增量策略；遇到限流或访问失败会停止该来源并记录日志，不绕过验证码或网站访问控制。

## 项目结构

```text
config/                 数据源与产品知识库
src/opportunity_radar/
  collectors/           平台独立采集器、政府来源采集器、模拟采集器
    browser_resilience.py 响应分类、选择器缓存、本地脱敏诊断
  auth.py               Playwright 登录状态
  ai.py                 启发式/OpenAI 分析
  db.py                 SQLite
  report.py             日报
  cli.py                命令行入口
  validation.py         入库前统一结果校验
tests/                  核心流程测试
```

## 下一阶段建议

下一步优先加固 Project Event 的业务语义：区分施工、EPC、设计、监理和咨询事件，并明确哪些 Canonical Event 可以作为生命周期主事件。附件解析、全文索引和跨公告工程身份事实已经存在；继续优先完善证据语义与人工审核，不扩张未实现 Collector。动态页面仍只在必要时使用 Playwright，WAF、验证码和限流不进入 Agent 兜底。

## 测试

```powershell
pytest
radar evaluate
radar run --mock
```

`radar evaluate` 默认读取 `tests/fixtures/opportunity_cases.json`，分别输出候选产品召回指标（`candidate_*`）、产品需求证据完全匹配率、产品机会状态完全匹配率以及 DIRECT 精确率/召回率。只有显式提供相应标签的样本才进入需求证据或机会状态分母。这些都是规则回归指标，不是概率。旧字段 `exact_product_match_rate`、`product_precision`、`product_recall` 为兼容保留，分别是 `candidate_exact_match_rate`、`candidate_precision`、`candidate_recall` 的弃用别名；CLI 的 `--min-precision` / `--min-recall` 仍对候选产品指标执行门禁。发现漏报时，先把真实公告脱敏后加入该文件，再调整 `products.yaml` 或分析逻辑，避免修好一个案例却破坏其他类别。

正式产品配置以 `config/products.yaml` 为准。产品专属的弱施工方法词和在范围确认正则分别使用 `weak_method_keywords`、`in_scope_confirmation_pattern`；Python 中的旧值只用于兼容外部调用方传入的不完整内存配置。全局业务动作和通用上下游配套识别仍属于分析器语法规则，暂不迁入产品配置。

## 证据评分与机会状态 v6

`analysis_json` 同时保存“项目线索分”和“当前商机分”。旧字段 `机会评分0-100` 与 SQLite 的 `ai_score` 保留兼容，但其含义固定为当前商机分，网站商业排序不再使用工程背景分。

- 只有目标产品与采购、供应、生产、预制、加工或安装动作直接关联，才是“直接产品证据”，当前商机通常为 85–95 分。
- 目标产品仅出现在工程范围中，或只命中施工方法/工程方向时，保留 55–88 分的项目线索价值，但当前商机仅为 5–35 分。
- 本次采购对象明显是服务器、灯箱、机械租赁或产品配套材料时，不依赖排除词累加，而按“动作没有支配目标产品”硬性封顶当前商机分。
- “管片防水材料、管片螺栓”等将识别为上下游配套，不再把修饰语中的“管片”误当作管片采购。
- 既有设施、检测维护最高 20 分，不进入当前机会；明确“不含、不在本次范围”等语句排除。另行采购不属于当前公告机会，但会保留为工程需求明确的后续跟踪线索。
- 钢箱梁、现浇梁不再视为预制混凝土箱梁；普通住宅不再自动推断 PC 构件；普通排水管网不再自动推断顶管；盾构机、刀具、油脂和临电采购不再推断管片。
- “项目线索分”和“当前商机分”都是可解释规则分，不是统计概率；每条记录同时保存主要加分、降分或封顶原因。

## 公告、项目进展与产品机会 v7

系统将三个互不替代的维度分别保存：`公告可参与性` 判断当前公告能否参与；`项目进展信号`
记录施工招标、中标候选、施工单位确定或施工进展；逐产品的 `产品机会状态` 使用
`DIRECT / PRE_PROCUREMENT / FOLLOW_UP / NO_EVIDENCE / OUT_OF_SCOPE / ENDED`。
施工中标结果只关闭公告并把当前商机分归零；存在正向产品线索时仍可进入供应链跟进。
`ENDED` 不会仅因施工总承包中标自动产生。旧 `当前机会状态` 保留为兼容字段，新的业务展示和
跟踪查询以三维状态为准。分析器 v7.0 会生成新的版本化逐产品评估，旧版本仍保留用于审计。

分析器 v7.2 在产品机会内部再拆成两个独立事实：`产品需求证据等级/类型` 回答工程是否需要
目标产品，`采购窗口状态/证据` 回答目标产品当前是否可采购。采购窗口仅使用
`OPEN / UPCOMING / UNKNOWN / ENDED`；只有明确当前采购映射为 `DIRECT`，明确未来采购映射为
`PRE_PROCUREMENT`。配套材料、施工方法或强产品存在性证据在采购时间未知时只映射为
`FOLLOW_UP`，工程方向背景映射为 `NO_EVIDENCE`。因此“产品需求证据强”不再自动等同于
“目标产品即将采购”。这些字段同时写入 `analysis_json` 和版本化的
`notice_product_assessment`，旧评估不覆盖。

## 工程项目实体

`notice_identity_fact` 位于原始公告与工程关联判断之间，持久保存地区、建设单位、核心名称、
采购尾部、标段、期次、年度、站点、范围、子工程范围、结果单位和项目编号事实。每条事实保留
原文、规范值、来源类型、来源引用/位置、可信度、质量、Extractor版本及当前/历史状态；同一字段
可以同时保留标题、正文、结构化字段和既有附件编号等多个证据。`NoticeIdentity` 只从当前活动事实
选择摘要，候选、候选簇和高可信工程构建不再各自重新解析这些字段。正式编号按 namespace、语义
类型和值分类；中交方案号和云筑 tenderCode 标为 `BUSINESS_ONLY`，不会升级为工程身份编号。
旧 `engineering_project_identifier` 的唯一约束和既有记录本阶段保持不变。

`project_event` 是只读事实层的持久化结果，仅从已有 `project_notice_link` 的正式关联公告生成。
第一版支持施工招标、施工中标候选、施工单位确定、一般采购公告、目标产品采购公告/结果、施工进展
和项目终止等少量稳定事件。事件保留公告来源、标段/期次/站点/范围、Identity Fact 中的
`RESULT_PARTY`、事件日期及日期来源、证据文本、提取器版本和幂等键。公告发布日期与系统观察时间
保持分离；无法取得公告明确事件日期时会标记 `event_date_source=PUBLICATION_DATE`。
事件构建不会修改公告可参与性、项目跟踪建议、产品机会状态或任何评分，也不会根据
`FOLLOW_UP`、`PRE_PROCUREMENT` 或 `SAME_PARENT_PROJECT` 候选关系制造预测事件。

Identity v1.2仅在明确标段/合同段上下文中识别`1#标段`等LOT，并用轨道语境门控STATION，
避免把变电站、泵站、供水站、收费站等设施写成轨道车站。旧机器事实保留为非活动历史；
人工确认的旧事实继续优先。事件提取器v1.1会在Identity变化后停用旧Scope来源事件并生成
新版本事件，业务事件类型不随Scope修正而改变。

`project-timeline`默认输出动态聚合的Canonical Event Fact：严格比较工程、事件类型、LOT、
Party、Product、Scope和日期，把同一现实事实的多来源公告聚为一条；`--show-sources`可展开
全部原始`project_event`、公告URL和证据文本，默认时间轴也会显示`Sources`数量。多来源只标记
交叉验证质量，不转换成概率。工程实体之间的关系通过名称/父级名称的有界Blocking候选集只读
计算，正式编号冲突会显式保留；命令不会合并实体、
迁移事件或修改正式关系。

`notice_identity_review` 保存 `CONFIRM / REJECT / OVERRIDE / CLEAR` 人工决定。机器事实始终保留；
确认和否定指向原机器事实，覆盖时才新增 `source_type=HUMAN` 的高可信事实，清空表示当前资料不足。
同一事实或字段的旧审核改为非活动状态而不删除。Resolver按“人工CLEAR → 人工OVERRIDE →
人工CONFIRM → 未否定机器事实”处理；Extractor升级不会停用HUMAN事实，也不会让已否定的同源同值
旧证据复活。审核默认 reviewer 为 `LOCAL_USER`，不读取或保存Windows用户名。`--dry-run` 在临时
SQLite副本中重算受影响Candidate并报告正式关系冲突，生产库不写入。身份审核不会自动建立、删除
或拆分 `project_notice_link`。

现有 `project` 表继续代表公告；`engineering_project` 代表现实工程，
`project_notice_link` 保存公告归属及可审计的匹配方法。自动构建只接受两类高可信证据：
正式工程/交易项目编号完全一致，或规范化名称、地区和有效业主同时一致。中交方案号、
云筑 tenderCode、一般采购/招标编号不会用于自动合并。标段、期次、站点、区间和起止位置
不会从名称中删除；不确定时保持公告未关联。人工确认写入 `HUMAN_CONFIRMED`，自动任务
不会覆盖人工关系。工程实体构建本身不写入项目生命周期，也不将工程实体同步到网站；只读 Lifecycle Aggregator 会基于 Canonical Event 动态推导 observed stage。

`project_link_candidate` 是独立的人工审核队列，不是正式工程关系。候选生成使用地区
blocking、核心名称、建设单位、标段、期次、站点、起止范围、年度和发布时间等可解释
证据；75 分以下不进入队列，90 分以上标记 HIGH，但分数只是规则证据分，不是概率。
不同期次、站点、年度、起止范围或明确不同建设单位会被排除；不同标段只推荐为
`SAME_PARENT_PROJECT`。生成候选绝不会修改 `project_notice_link`，只有人工执行
`review-project-candidate --confirm` 才复用 `confirm-project` 建立 `HUMAN_CONFIRMED`
关系。REJECTED 会永久保留；DEFERRED 只有候选证据发生变化时才重新进入 PENDING。

`project-clusters` 只对当前活动 Pair 候选做 connected component 动态分组，不新增事实表，
也不把“图连通”当作“全部同一工程”。簇级确认必须显式选择公告子集，并逐对检查标段、
期次、年度、站点、范围、建设单位、正式编号和明确功能分区冲突。`SAME_PARENT_PROJECT`
只保存人工确认的 candidate 结论，不会错建 `project_notice_link`。

候选匹配读取人工审核后的 `NoticeIdentity`，其机器事实仍按非入侵式可靠性分级。页面结构化项目地区为 HIGH，
标题或站点栏目得到的地区为 MEDIUM，Collector 默认值或未限定文本推断为 LOW；LOW 不得作为
地区正向分。建设单位分为 `STRUCTURED_HIGH / EXTRACTED_MEDIUM / CONTAMINATED / MISSING`，
中交 `opUnitName` 另标记为 `PROCUREMENT_ORG_MEDIUM`，因其是采购/经营单位而非已证明的项目建设单位。
只有可用类型允许加分，且只有 `STRUCTURED_HIGH` 可获得强证据分。这些都是候选证据门控，
不会改写旧公告字段。

当前业务模型暂按“一条公告归属一个工程实体”运行；尚未发现必须拆成多个独立工程的
结构化样本。工程标识当前只自动接受北京公共资源平台的正式 S 编号。附件工程编号以及
未来天津、河北编号可能需要来源命名空间，在首次启用这些编号前应迁移唯一约束。

逐产品状态的含义：

- `工程需求`：`YES / LIKELY / POSSIBLE / NO / UNKNOWN`，回答项目本身是否需要该产品。
- `范围状态`：`IN_SCOPE / OUT_OF_SCOPE / SEPARATE_PROC / UNCERTAIN`，回答产品是否属于本公告工作范围。
- `当前机会状态`：`OPEN / MONITOR / CLOSED / UNKNOWN`，回答现在是否值得跟进。

日报默认展示匹配产品的独立分数、需求意图、三类状态、关键证据句和负向证据；另有“后续专项采购跟踪”板块。评分为 0 的记录仍保留在历史观察池，避免因为收紧规则而丢失原始公告。
