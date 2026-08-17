import json, os
from datetime import date
from pathlib import Path

def generate_daily(rows, output_dir: Path) -> Path:
    minimum_score = int(os.getenv("RADAR_REPORT_MIN_SCORE", "30"))
    rows = [row for row in rows if int(row["ai_score"] or 0) >= minimum_score]
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"daily-{date.today().isoformat()}.md"
    lines = [f"# 工程项目机会雷达日报（{date.today().isoformat()}）", "", "## 今日重点机会", ""]
    for row in rows:
        a = json.loads(row["analysis_json"] or "{}")
        opportunities = a.get("潜在产品或服务", a.get("潜在预制产品", []))
        lines += [f"### {row['project_name']}", "", f"- 地区：{row['region'] or '未知'}", f"- 工程类型：{a.get('项目类型', '未识别')}", f"- 潜在产品或服务：{'、'.join(opportunities) or '无'}", f"- 评分：{row['ai_score']}", f"- 证据等级：{a.get('证据等级', '未标注')}（置信度：{a.get('置信度', '未知')}）", f"- 推荐理由：{a.get('匹配理由', '无')}", f"- 来源：[{row['source_site']}]({row['url']})", ""]
    if not rows: lines += [f"今日暂无评分达到 {minimum_score} 分的重点机会。", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
