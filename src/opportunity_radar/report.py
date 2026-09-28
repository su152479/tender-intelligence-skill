import json, os
from pathlib import Path

from .business_time import BUSINESS_TIMEZONE_NAME, business_now


def _analysis(row) -> dict:
    try:
        return json.loads(row["analysis_json"] or "{}")
    except (json.JSONDecodeError, KeyError, TypeError):
        return {}


def _lead_score(row) -> int:
    analysis = _analysis(row)
    return int(analysis.get("项目线索分0-100", row.get("ai_score", 0) or 0))


def _render_notice(lines: list[str], row, *, updated: bool = False) -> None:
    analysis = _analysis(row)
    product_scores = analysis.get("产品证据分", {})
    score_text = "、".join(f"{name} {score}分" for name, score in product_scores.items() if score > 0) or "无"
    evidence_sentences, negative_evidence, intents, demand_states, window_states = [], [], [], [], []
    for item in analysis.get("命中证据", []):
        if item.get("产品") in analysis.get("潜在预制产品", []):
            intents.append(f"{item.get('产品')}：{item.get('需求意图', '可能使用')}")
            demand_states.append(
                f"{item.get('产品')}：{item.get('需求状态', 'UNKNOWN')} / "
                f"{item.get('范围状态', 'UNCERTAIN')} / {item.get('产品机会状态', 'NO_EVIDENCE')}"
            )
            window_states.append(
                f"{item.get('产品')}：需求证据 {item.get('产品需求证据等级', 'NONE')} / "
                f"{item.get('产品需求证据类型', 'ENGINEERING_DIRECTION')}；"
                f"采购窗口 {item.get('采购窗口状态', 'UNKNOWN')}"
            )
            evidence_sentences.extend(item.get("证据句", []))
        negative_evidence.extend(item.get("负向证据", []))
    suffix = "（今日首次发现后内容又有更新）" if row.get("content_changed_today") else ""
    lines.extend([f"### {row['project_name']}{suffix}", ""])
    if updated:
        lines.extend(["- 观察状态：内容已更新", f"- 更新时间：{row['last_observed_at']}"])
    lines.extend([
        f"- 地区：{row['region'] or '未知'}",
        f"- 工程类型：{analysis.get('项目类型', '未识别')}",
        f"- 可能产品：{'、'.join(analysis.get('潜在预制产品', [])) or '无'}",
        f"- 本次采购对象：{'、'.join(analysis.get('本次采购对象', [])) or '未识别'}",
        f"- 产品证据分：{score_text}",
        f"- 需求意图：{'、'.join(intents) or '未识别'}",
        f"- 需求/范围/机会状态：{'；'.join(demand_states) or '未识别'}",
        f"- 产品需求/采购窗口：{'；'.join(window_states) or '未识别'}",
        f"- 项目线索分：{analysis.get('项目线索分0-100', 0)}",
        f"- 当前商机分：{analysis.get('当前商机分0-100', row['ai_score'])}",
        f"- 公告可参与性：{analysis.get('公告可参与性', 'UNKNOWN')}",
        f"- 项目进展信号：{analysis.get('项目进展信号', 'UNKNOWN')}",
        f"- 项目跟踪建议：{analysis.get('项目跟踪建议', 'UNKNOWN')}",
        f"- 项目跟踪理由：{'；'.join(analysis.get('项目跟踪理由', [])) or '未形成'}",
        f"- 展示分类：{'、'.join(analysis.get('展示分类', [])) or '一般观察'}",
        f"- 收录类型：{analysis.get('收录类型', '前置项目线索')}；跟进优先级：{analysis.get('跟进优先级', '一般线索')}",
        f"- 建议动作：{analysis.get('建议动作', '继续核对公告与项目证据')}",
        f"- 评分说明：{'；'.join(analysis.get('评分说明', [])) or '无'}",
        f"- 证据等级：{analysis.get('证据等级', '未标注')}（置信度：{analysis.get('置信度', '未知')}）",
        f"- 关键证据：{'；'.join(dict.fromkeys(evidence_sentences)) or '无'}",
        f"- 负向证据：{'；'.join(dict.fromkeys(negative_evidence)) or '未发现'}",
        f"- 推荐理由：{analysis.get('匹配理由', '无')}",
        f"- 来源：[{row['source_site']}]({row['url']})", "",
    ])


def generate_daily(summary: dict, output_dir: Path, follow_ups=()) -> Path:
    """Generate a report whose daily semantics come only from notice_observation."""
    minimum_score = int(os.getenv("RADAR_REPORT_MIN_SCORE", "30"))
    report_date = summary["business_date"]
    new_rows = [row for row in summary["new"] if _lead_score(row) >= minimum_score]
    updated_rows = list(summary["updated"])
    daily_ids = {int(row["id"]) for row in summary["new"] + summary["updated"]}
    follow_ups = [row for row in follow_ups if int(row["notice_id"]) in daily_ids]

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"daily-{report_date}.md"
    lines = [
        f"# 工程项目机会雷达日报（{report_date}）", "",
        f"> 今日按 {BUSINESS_TIMEZONE_NAME} 统计，事实来源为已完成采集运行的 notice_observation。", "",
        "## 今日新发现商机", "",
    ]
    for row in new_rows:
        _render_notice(lines, row)
    if not new_rows:
        lines.extend([f"今日没有首次发现且项目线索分达到 {minimum_score} 分的商机。", ""])

    lines.extend(["## 今日更新公告", ""])
    for row in updated_rows:
        _render_notice(lines, row, updated=True)
    if not updated_rows:
        lines.extend(["今日没有历史公告发生有效内容更新。", ""])

    lines.extend([
        "## 今日再次观察", "",
        f"今日再次确认历史公告：{len(summary['seen_again'])} 条。", "",
        "## 具体产品供应链机会", "",
    ])
    for row in follow_ups:
        try:
            evidence = json.loads(row["positive_evidence"] or "[]")
        except json.JSONDecodeError:
            evidence = []
        lines.extend([
            f"### {row['project_name']} · {row['product']}", "",
            f"- 地区：{row['region'] or '未知'}",
            f"- 判断：工程需求 {row['engineering_need']}；本公告范围 {row['scope_status']}；产品机会 {row['product_opportunity_status']}",
            f"- 两步判断：产品需求 {row['product_demand_evidence_level']}（{row['product_demand_evidence_type']}）；采购窗口 {row['procurement_window_status']}",
            f"- 需求意图：{row['demand_intent']}（产品证据分 {row['evidence_score']}）",
            "- 跟踪理由：存在正向产品线索，但当前公告未形成可直接参与的目标产品机会；继续关注专项采购或中标单位供应链节点。",
            f"- 关键证据：{'；'.join(dict.fromkeys(evidence)) or '无'}",
            f"- 来源：[{row['source_site']}]({row['url']})", "",
        ])
    if not follow_ups:
        lines.extend(["今日新发现或更新公告中，没有达到产品特定证据门控的供应链机会。", ""])
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_daily_observation_snapshot(summary: dict, path: Path) -> Path:
    """Write the small, public-safe build input used by the website home page."""
    def public_row(row: dict) -> dict:
        return {
            "source_url": row["url"],
            "status": row["observation_status"],
            "first_observed_at": row["first_observed_at"],
            "last_observed_at": row["last_observed_at"],
            "content_changed_today": bool(row["content_changed_today"]),
        }

    by_url: dict[str, dict] = {}
    status_rank = {"SEEN_AGAIN": 1, "UPDATED": 2, "NEW": 3}
    for row in summary["all"]:
        item = public_row(row)
        existing = by_url.get(item["source_url"])
        if existing is None:
            by_url[item["source_url"]] = item
            continue
        existing["first_observed_at"] = min(existing["first_observed_at"], item["first_observed_at"])
        existing["last_observed_at"] = max(existing["last_observed_at"], item["last_observed_at"])
        existing["content_changed_today"] = existing["content_changed_today"] or item["content_changed_today"]
        if status_rank[item["status"]] > status_rank[existing["status"]]:
            existing["status"] = item["status"]

    payload = {
        "business_date": summary["business_date"],
        "timezone": BUSINESS_TIMEZONE_NAME,
        "generated_at": business_now().isoformat(timespec="seconds"),
        "observations": list(by_url.values()),
        "seen_again_count": len(summary["seen_again"]),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
