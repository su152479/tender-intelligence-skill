# Engineering Opportunity Radar

一个可配置行业、产品和目标地区的工程机会发现 MVP。它从招标平台及政府公开网站采集公告，根据行业知识库识别潜在产品或服务需求，保存到 SQLite 并生成 Markdown 日报。

本仓库只包含通用 Python 框架和公开数据源配置，不包含真实账号、Cookie、浏览器登录状态、SQLite 数据库、采集日志、日报或网站部署代码。

## 适用场景

通过更换行业画像，同一套框架可以用于：

- 预制构件、钢结构、管材管件和防水材料
- 机电设备、工程机械、水泥与混凝土材料
- 其他能够通过关键词、施工方法和工程方向识别的产品或服务

地区不是硬编码的。`config/regions.yaml` 提供中国大陆省级行政区及常用地名映射，可选择全国或任意省份组合。

## 核心能力

- 每个平台使用独立 collector，支持 Requests、HTML 解析和 Playwright。
- 需要登录的平台采用首次人工登录并保存浏览器状态的方式。
- 登录失效时停止对应来源并提醒，不绕过验证码。
- 政府来源采用有限分页和低频串行采集，在验证码、403 或 429 时停止。
- SQLite 保存公告、原文、评分、匹配结果和来源运行状态。
- 默认使用本地规则分析，也可选择 OpenAI JSON 分析。
- 行业画像、评分规则、搜索关键词和目标地区均可配置。

## 安装

需要 Python 3.10 或更高版本。

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
radar init
```

如果 PowerShell 禁止执行激活脚本，可以直接使用虚拟环境中的 Python：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m opportunity_radar.cli init
```

## 选择行业画像

项目内置两个示例：

- `config/profiles/general_construction.yaml`：综合工程材料与设备
- `config/profiles/precast.yaml`：预制构件

在本机 `.env` 中选择：

```dotenv
RADAR_PROFILE=profiles/general_construction.yaml
```

也可以复制一个画像文件，新增自己的产品或服务。每一类机会支持：

```yaml
- name: 钢结构
  direct_keywords: [钢结构, 钢梁, 钢箱梁]
  method_keywords: [钢结构安装, 钢结构吊装]
  direction_keywords: [工业厂房, 体育场馆]
  project_types: [房建, 工业建筑]
```

采集器会从当前画像自动生成搜索关键词，分析器再按“直接证据、施工方法、工程方向”三级评分。默认每个类别选择 3 个搜索词，可通过 `RADAR_PROFILE_KEYWORDS_PER_CATEGORY` 调整，避免一次运行产生过多请求。

## 选择目标地区

在 `.env` 使用标准省级名称，以英文逗号分隔：

```dotenv
RADAR_TARGET_REGIONS=北京市,天津市,河北省
```

其他示例：

```dotenv
RADAR_TARGET_REGIONS=上海市,江苏省,浙江省
```

将 `RADAR_TARGET_REGIONS` 留空时，跨区域平台接受 `config/regions.yaml` 中全部地区。北京、河北、天津等政府 collector 仍只采集其各自官方网站覆盖的地区；可以继续增加其他省市的独立 collector。

## 运行

先验证模拟流程：

```powershell
radar run --mock
```

采集公开来源：

```powershell
radar run --source cccc --source crecg_luban
radar run --source beijing_ggzy --source hebei_ggzy --source tianjin_transport --source ccgp
```

对需要登录的平台，先完成人工登录：

```powershell
playwright install chromium
radar login cscec_yzw
radar run --source cscec_yzw
```

登录状态保存在 `data/auth/`，该目录已被 `.gitignore` 排除。请勿提交或分享其中的文件。

查看来源状态：

```powershell
radar status
```

数据库默认位于 `data/radar.sqlite3`，日报默认位于 `data/reports/`，二者均不会进入 Git。

## 可选 AI 分析

不配置 API 时，项目完全使用选定行业画像中的本地规则。启用 OpenAI 分析时：

```powershell
python -m pip install -e ".[ai]"
```

随后只在本机 `.env` 设置：

```dotenv
RADAR_AI_PROVIDER=openai
OPENAI_API_KEY=your-key-here
```

不要将真实 API Key 写入代码或提交到 Git。

## 项目结构

```text
config/
  profiles/             可切换的行业画像
  regions.yaml          全国地区名称、编码与匹配词
  sources.yaml          公开数据源配置
src/opportunity_radar/
  collectors/           独立采集器
  auth.py               Playwright 登录状态管理
  ai.py                 本地规则与可选 AI 分析
  db.py                 SQLite 数据层
  report.py             Markdown 日报
  cli.py                命令行入口
tests/                   核心流程测试
```

## 安全与合规

- 仅采集合法公开信息，并遵守目标网站的使用条款、robots 规则及访问频率要求。
- 不破解验证码，不绕过登录、访问控制或反爬措施。
- 不自动报名、投标或提交业务表单。
- 对需要登录的平台，仅复用用户本人合法建立的本地会话。
- 分析结果只是业务线索，不替代招标文件和人工判断。

## 测试

```powershell
pytest
radar run --mock
```

## 许可

当前仓库未附带开源许可证。除非仓库所有者另行授权，代码默认保留全部权利。
