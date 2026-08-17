# Engineering Opportunity Radar

一个面向预制构件行业的个人工程机会发现 MVP。它从公开招标平台和政府公开网站采集公告，限定北京、天津、河北区域，并依据产品知识库判断潜在的箱梁、管片、PC 构件、风塔、圆形顶管、矩形顶管和水工预制件机会。

本仓库仅包含通用 Python 框架和公开数据源配置，不包含真实账号、Cookie、浏览器登录状态、SQLite 数据库、采集日志、Markdown 日报或任何网站部署代码。

## 功能

- 每个平台使用独立 collector，支持 Requests、HTML 解析和 Playwright。
- 需要登录的平台采用首次人工登录并保存浏览器状态的方式。
- 登录失效时停止对应来源并提醒，不绕过验证码。
- 政府来源采用有限分页、低频串行采集，并在验证码、403 或 429 时停止。
- SQLite 保存项目、原文、评分、匹配产品和来源运行状态。
- 默认使用本地规则分析，可选用 OpenAI JSON 分析。
- 每次运行生成 Markdown 日报。
- 只保留北京、天津和河北项目。

## 安装

需要 Python 3.10 或更高版本。

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
radar init
```

如果 PowerShell 禁止执行激活脚本，可以不激活虚拟环境，直接运行：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m opportunity_radar.cli init
```

## 使用

先运行模拟流程：

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

浏览器状态保存在 `data/auth/`，该目录已被 `.gitignore` 排除。请勿提交或分享其中的文件。

查看来源运行状态：

```powershell
radar status
```

数据库默认位于 `data/radar.sqlite3`，日报默认位于 `data/reports/`；二者均不会进入 Git。

## 可选 AI 分析

不配置 API 时，项目完全使用 `config/products.yaml` 中的本地规则。需要启用 OpenAI 分析时：

```powershell
python -m pip install -e ".[ai]"
```

随后只在本机 `.env` 中设置：

```dotenv
RADAR_AI_PROVIDER=openai
OPENAI_API_KEY=your-key-here
```

不要把真实 API Key 写入代码或提交到 Git。

## 项目结构

```text
config/
  sources.yaml          公开数据源配置
  products.yaml         产品知识库与评分规则
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
- 不自动报名、投标或提交任何业务表单。
- 对需要登录的平台，仅复用用户本人合法建立的本地会话。
- 公告分析结果只是业务线索，不替代招标文件和人工判断。

## 测试

```powershell
pytest
radar run --mock
```

## 许可

当前仓库未附带开源许可证。除非仓库所有者另行授权，代码默认保留全部权利。

