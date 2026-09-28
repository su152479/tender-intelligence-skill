"""Read-only SQLite export to a local Obsidian knowledge base."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
import sqlite3


AUTO_START = "<!-- RADAR:AUTO:START -->"
AUTO_END = "<!-- RADAR:AUTO:END -->"
MANUAL_SECTION = """## 我的跟进记录

- 跟进状态：
- 联系人：
- 下一步：
- 备注：
"""
SENSITIVE_FIELD = re.compile(
    r"(?i)(?:cookie|password|passwd|api[_ -]?key|access[_ -]?token|authorization|storage[_ -]?state|账号|密码)\s*[:=：]\s*[^\s，；,;]+"
)


def _safe_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value).strip(" .")
    return value[:90] or "未命名"


def _yaml(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _redact(value: str) -> str:
    return SENSITIVE_FIELD.sub("[敏感字段已脱敏]", value)


def _analysis(row: sqlite3.Row) -> dict:
    try:
        value = json.loads(row["analysis_json"] or "{}")
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def _region(value: str) -> str:
    if "北京" in value:
        return "北京"
    if "天津" in value:
        return "天津"
    if "河北" in value:
        return "河北"
    return value or "未知地区"


def _status_label(value: str) -> str:
    return {"OPEN": "公告可参与", "CLOSED": "公告已结束", "UNKNOWN": "公告可参与性待判断"}.get(value, "公告可参与性待判断")


def _product_states(detail: dict) -> list[dict]:
    states = []
    for item in detail.get("命中证据", []):
        product = str(item.get("产品", "")).strip()
        if product:
            states.append({
                "product": product,
                "score": int(item.get("评分", 0)),
                "intent": item.get("需求意图", "可能使用"),
                "engineering_need": item.get("工程需求", "UNKNOWN"),
                "scope_status": item.get("范围状态", "UNCERTAIN"),
                "opportunity_status": item.get("当前机会状态", "UNKNOWN"),
                "product_opportunity_status": item.get("产品机会状态", "NO_EVIDENCE"),
                "product_demand_evidence_level": item.get("产品需求证据等级", "NONE"),
                "product_demand_evidence_type": item.get("产品需求证据类型", "ENGINEERING_DIRECTION"),
                "procurement_window_status": item.get("采购窗口状态", "UNKNOWN"),
                "procurement_window_evidence": list(item.get("采购窗口证据", [])),
                "positive": list(item.get("证据句", [])),
                "negative": list(item.get("负向证据", [])),
            })
    return states


def _aggregate_status(states: list[dict]) -> str:
    values = {item["product_opportunity_status"] for item in states}
    for status in ("DIRECT", "PRE_PROCUREMENT", "FOLLOW_UP", "NO_EVIDENCE", "OUT_OF_SCOPE", "ENDED"):
        if status in values:
            return status
    return "UNKNOWN"


def _project_link(row: sqlite3.Row) -> str:
    return f"[[项目/项目-{int(row['id']):04d}|{row['project_name']}]]"


class ObsidianExporter:
    """Export public business fields while preserving user-authored note sections."""

    def __init__(self, database_path: Path, vault_path: Path):
        self.database_path = database_path.resolve()
        self.vault_path = vault_path.resolve()

    def _connect_read_only(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.database_path.as_uri() + "?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        return con

    def export(self) -> dict[str, int | str]:
        self.vault_path.mkdir(parents=True, exist_ok=True)
        for folder in ("项目", "产品", "地区", "来源", "状态", "月份", "我的笔记", "附件", ".obsidian"):
            (self.vault_path / folder).mkdir(exist_ok=True)
        self._ensure_obsidian_settings()

        with self._connect_read_only() as con:
            projects = list(con.execute("SELECT * FROM project ORDER BY publish_date DESC,id DESC"))
            source_rows = list(con.execute("""SELECT r.* FROM source_run r JOIN (
                SELECT source_id,MAX(id) id FROM source_run GROUP BY source_id
            ) latest ON latest.id=r.id ORDER BY r.source_id"""))
            current_ids = self._current_project_ids(con)
            result_parties = {
                int(item["notice_id"]): item["parties"]
                for item in con.execute("""SELECT notice_id,group_concat(raw_value,'、') parties
                FROM notice_identity_fact WHERE fact_type='RESULT_PARTY' AND is_active=1 GROUP BY notice_id""")
            }
            notice_project_links = {
                int(item["notice_id"]): int(item["engineering_project_id"])
                for item in con.execute("SELECT notice_id,engineering_project_id FROM project_notice_link")
            }

        product_groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
        region_groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
        source_groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
        status_groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
        month_groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
        supply_rows: list[sqlite3.Row] = []
        key_tracking_rows: list[sqlite3.Row] = []
        progress_rows: list[sqlite3.Row] = []
        general_rows: list[sqlite3.Row] = []
        excluded_rows: list[sqlite3.Row] = []
        valid = 0
        supply_pairs = 0

        for row in projects:
            detail = _analysis(row)
            states = _product_states(detail)
            aggregate_status = _aggregate_status(states)
            trackable = any(item["product_opportunity_status"] in {"DIRECT", "PRE_PROCUREMENT", "FOLLOW_UP"} for item in states)
            if trackable:
                valid += 1
            product_supply = [item for item in states if item["product_opportunity_status"] in {"PRE_PROCUREMENT", "FOLLOW_UP"}]
            if product_supply:
                supply_rows.append(row)
                supply_pairs += len(product_supply)
            if detail.get("项目跟踪建议", "UNKNOWN") == "KEY_FOLLOW_UP":
                key_tracking_rows.append(row)
            if detail.get("项目进展信号", "UNKNOWN") != "UNKNOWN":
                progress_rows.append(row)
            if not trackable and any(item["product_opportunity_status"] == "OUT_OF_SCOPE" for item in states):
                excluded_rows.append(row)
            elif not trackable:
                general_rows.append(row)
            for product in dict.fromkeys(item["product"] for item in states):
                product_groups[product].append(row)
            region_groups[_region(row["region"] or "")].append(row)
            source_groups[row["source_site"]].append(row)
            status_groups[_status_label(detail.get("公告可参与性", "UNKNOWN"))].append(row)
            month_groups[(row["publish_date"] or "日期未知")[:7]].append(row)
            self._write_project(row, detail, states, aggregate_status, result_parties.get(int(row["id"]), ""))

        self._write_group_indexes("产品", product_groups)
        self._write_group_indexes("地区", region_groups)
        self._write_group_indexes("来源", source_groups)
        self._write_group_indexes("状态", status_groups)
        self._write_group_indexes("月份", month_groups)
        current = [
            row for row in projects
            if int(row["id"]) in current_ids
            and _analysis(row).get("公告可参与性") == "OPEN"
            and _aggregate_status(_product_states(_analysis(row))) == "DIRECT"
        ]
        self._write_listing("01 当前直接商机.md", "当前直接商机", current, "当前公告可参与，且目标产品与本次采购直接关联。")
        self._write_listing("02 具体产品供应链机会.md", "具体产品供应链机会", supply_rows, "只包含具有产品特定证据的采购前或供应链跟进机会。")
        self._write_listing("03 重点工程跟踪.md", "重点工程跟踪", key_tracking_rows, "工程值得市场人员重点调查；进入本栏不代表任何具体产品机会已经成立。")
        self._write_listing("04 项目最新进展.md", "项目最新进展", progress_rows, "中标候选、中标结果、开工或施工进展等工程节点。")
        self._write_listing("05 一般观察.md", "一般观察", general_rows, "存在工程背景，但目标产品证据尚不足。")
        self._write_listing("06 非目标或排除.md", "非目标或排除", excluded_rows, "仅包含明确反向证据或不属于目标经营范围的记录。")
        key_entity_ids = {
            notice_project_links[int(row["id"])] for row in key_tracking_rows
            if int(row["id"]) in notice_project_links
        }
        unlinked_key_count = sum(int(row["id"]) not in notice_project_links for row in key_tracking_rows)
        self._write_home(
            projects, current, valid, supply_rows, supply_pairs, key_tracking_rows,
            len(key_entity_ids), unlinked_key_count, product_groups, source_rows,
        )
        self._write_readme()
        return {
            "projects": len(projects), "valid": valid, "current": len(current),
            "follow_ups": len(supply_rows), "supply_pairs": supply_pairs,
            "key_tracking": len(key_tracking_rows), "key_project_entities": len(key_entity_ids),
            "unlinked_key_notices": unlinked_key_count, "vault": str(self.vault_path),
        }

    def _ensure_obsidian_settings(self) -> None:
        app = self.vault_path / ".obsidian" / "app.json"
        if not app.exists():
            app.write_text(json.dumps({
                "newFileLocation": "folder", "newFileFolderPath": "我的笔记",
                "attachmentFolderPath": "附件", "alwaysUpdateLinks": True,
            }, ensure_ascii=False, indent=2), encoding="utf-8")

    def _current_project_ids(self, con: sqlite3.Connection) -> set[int]:
        today = date.today().isoformat()
        row = con.execute("""SELECT run_id,MIN(started_at) started_at FROM source_run
            WHERE started_at LIKE ? AND run_id<>'' GROUP BY run_id ORDER BY MAX(id) DESC LIMIT 1""", (today + "%",)).fetchone()
        if not row:
            return set()
        local_start = datetime.fromisoformat(row["started_at"])
        utc_start = (local_start - timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
        return {int(item[0]) for item in con.execute("SELECT id FROM project WHERE created_at>=?", (utc_start,))}

    def _write_project(self, row: sqlite3.Row, detail: dict, states: list[dict], aggregate_status: str, result_party: str) -> None:
        matched = list(detail.get("潜在预制产品", []))
        related = list(dict.fromkeys(item["product"] for item in states))
        region = _region(row["region"] or "")
        tags = [
            "工程机会", f"地区/{region}",
            f"公告状态/{_status_label(detail.get('公告可参与性', 'UNKNOWN'))}",
            f"产品机会/{aggregate_status}",
            f"项目跟踪/{detail.get('项目跟踪建议', 'UNKNOWN')}",
        ]
        tags.extend(f"产品/{item}" for item in matched)
        frontmatter = [
            "---", f"雷达ID: {int(row['id'])}", f"项目名称: {_yaml(row['project_name'])}",
            f"项目编号: {_yaml(row['project_no'] or '')}", f"发布日期: {_yaml(row['publish_date'] or '')}",
            f"地区: {_yaml(region)}", f"详细地区: {_yaml(row['region'] or '')}",
            f"工程类型: {_yaml(detail.get('项目类型', '未识别'))}", f"项目阶段: {_yaml(row['project_stage'] or '')}",
            f"匹配产品: {_yaml(matched)}", f"相关产品: {_yaml(related)}", f"机会评分: {int(row['ai_score'] or 0)}",
            f"公告可参与性: {_yaml(detail.get('公告可参与性', 'UNKNOWN'))}",
            f"项目进展信号: {_yaml(detail.get('项目进展信号', 'UNKNOWN'))}",
            f"项目跟踪建议: {_yaml(detail.get('项目跟踪建议', 'UNKNOWN'))}",
            f"产品机会状态: {_yaml(aggregate_status)}", f"来源网站: {_yaml(row['source_site'])}",
            f"原公告: {_yaml(row['url'])}", f"分析器版本: {_yaml(row['analyzer_version'] or '')}",
            f"规则版本: {_yaml(row['rules_version'] or '')}", f"tags: {_yaml(tags)}", "---", "",
        ]
        state_lines = []
        evidence_lines = []
        for item in states:
            state_lines.append(
                f"| [[产品/{_safe_name(item['product'])}|{item['product']}]] | {item['score']} | "
                f"{item['product_demand_evidence_level']} | {item['product_demand_evidence_type']} | "
                f"{item['procurement_window_status']} | {item['product_opportunity_status']} |"
            )
            for sentence in item["procurement_window_evidence"]:
                evidence_lines.append(f"- **{item['product']}（采购窗口）**：{_redact(sentence)}")
            for sentence in item["positive"]:
                evidence_lines.append(f"- **{item['product']}**：{_redact(sentence)}")
            for sentence in item["negative"]:
                evidence_lines.append(f"- **{item['product']}（反证）**：{_redact(sentence)}")
        if not state_lines:
            state_lines.append("| 未识别 | 0 | — | UNKNOWN | UNCERTAIN | UNKNOWN |")
        notice_label = _status_label(detail.get("公告可参与性", "UNKNOWN"))
        links = [f"[[地区/{_safe_name(region)}|{region}]]", f"[[来源/{_safe_name(row['source_site'])}|{row['source_site']}]]", f"[[状态/{_safe_name(notice_label)}|{notice_label}]]"]
        links.extend(f"[[产品/{_safe_name(item)}|{item}]]" for item in related)
        generated = "\n".join(frontmatter + [
            AUTO_START, f"# {row['project_name']}", "", "## 项目判断", "",
            f"- **当前商机分**：{int(row['ai_score'] or 0)}",
            f"- **项目线索分**：{int(detail.get('项目线索分0-100', 0))}",
            f"- **公告可参与性**：{detail.get('公告可参与性', 'UNKNOWN')}",
            f"- **项目进展信号**：{detail.get('项目进展信号', 'UNKNOWN')}",
            f"- **项目跟踪建议**：{detail.get('项目跟踪建议', 'UNKNOWN')}",
            f"- **项目跟踪理由**：{'；'.join(detail.get('项目跟踪理由', [])) or '未形成'}",
            f"- **中标/跟进单位**：{result_party or '未提取'}",
            f"- **建议动作**：{detail.get('建议动作', '继续核对公告与项目证据')}",
            f"- **工程方向**：{detail.get('施工方向', '未识别')}",
            f"- **推荐理由**：{detail.get('匹配理由', '未命中产品规则，保留观察。')}",
            f"- **建设单位**：{row['owner'] or '未提供'}", f"- **招标单位**：{row['tenderer'] or '未提供'}",
            f"- **原公告**：[{row['source_site']}]({row['url']})", "", "## 产品判断", "",
            "| 产品 | 证据分 | 需求证据 | 证据类型 | 采购窗口 | 产品机会 |",
            "|---|---:|---|---|---|---|", *state_lines, "", "## 关键证据", "",
            *(evidence_lines or ["- 暂无可审计产品证据。"]), "", "## 关联知识", "",
            " · ".join(links), "", AUTO_END, "",
        ])
        path = self.vault_path / "项目" / f"项目-{int(row['id']):04d}.md"
        self._write_preserving_manual(path, generated)

    def _write_preserving_manual(self, path: Path, generated: str) -> None:
        if not path.exists():
            path.write_text(generated + MANUAL_SECTION, encoding="utf-8")
            return
        existing = path.read_text(encoding="utf-8")
        start = existing.find(AUTO_START)
        end = existing.find(AUTO_END)
        generated_start = generated.find(AUTO_START)
        if start >= 0 and end >= start and generated_start >= 0:
            manual = existing[end + len(AUTO_END):].lstrip("\r\n")
            path.write_text(generated + (manual or MANUAL_SECTION), encoding="utf-8")
        else:
            path.write_text(generated + "## 原有手写内容\n\n" + existing, encoding="utf-8")

    def _write_group_indexes(self, folder: str, groups: dict[str, list[sqlite3.Row]]) -> None:
        for name, rows in groups.items():
            lines = [f"# {name}", "", f"共 {len(rows)} 条项目记录。", ""]
            lines.extend(self._row_line(row) for row in rows)
            (self.vault_path / folder / f"{_safe_name(name)}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_listing(self, filename: str, title: str, rows: list[sqlite3.Row], description: str) -> None:
        lines = [f"# {title}", "", description, "", f"共 {len(rows)} 条。", ""]
        lines.extend(self._row_line(row) for row in rows)
        (self.vault_path / filename).write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def _row_line(row: sqlite3.Row) -> str:
        products = "、".join(json.loads(row["matched_products"] or "[]")) or "观察项"
        return f"- {_project_link(row)} · {row['publish_date'] or '日期未知'} · {row['region'] or '地区未知'} · {products} · {int(row['ai_score'] or 0)}分"

    def _write_home(
        self, projects, current, valid, supply_rows, supply_pairs, key_tracking_rows,
        key_project_entities, unlinked_key_notices, product_groups, source_rows,
    ) -> None:
        top = sorted((row for row in projects if int(row["ai_score"] or 0) > 0), key=lambda row: int(row["ai_score"]), reverse=True)[:10]
        lines = [
            "# 工程机会雷达知识库", "", f"> 最近导出：{datetime.now().strftime('%Y-%m-%d %H:%M')}", "",
            "## 快速入口", "", "- [[01 当前直接商机]]", "- [[02 具体产品供应链机会]]", "- [[03 重点工程跟踪]]",
            "- [[04 项目最新进展]]", "- [[05 一般观察]]", "- [[06 非目标或排除]]", "- [[地区/北京]] · [[地区/天津]] · [[地区/河北]]", "",
            "## 数据概览", "", f"- 历史项目：**{len(projects)}**", f"- 有效线索：**{valid}**",
            f"- 今日直接商机：**{len(current)}**", f"- 产品供应链机会公告：**{len(supply_rows)}**",
            f"- 产品供应链公告×产品组合：**{supply_pairs}**", f"- 重点工程跟踪公告：**{len(key_tracking_rows)}**",
            f"- 重点工程实体：**{key_project_entities}**（仅统计已完成工程实体关联部分）",
            f"- 未关联重点公告：**{unlinked_key_notices}**", "", "## 产品知识入口", "",
        ]
        lines.extend(f"- [[产品/{_safe_name(name)}|{name}]]：{len(rows)} 条" for name, rows in sorted(product_groups.items()))
        lines += ["", "## 当前高分机会", ""]
        lines.extend(self._row_line(row) for row in top)
        lines += ["", "## 数据源状态", ""]
        lines.extend(f"- [[来源/{_safe_name(row['source_name'])}|{row['source_name']}]]：{row['health_status'] if 'health_status' in row.keys() else row['status']}，入库 {row['item_count']} 条" for row in source_rows)
        (self.vault_path / "00 雷达首页.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_readme(self) -> None:
        text = """# 使用说明

本知识库由工程机会雷达从本机 SQLite **只读导出**。自动同步不会修改雷达数据库，也不会导出账号、密码、Cookie、API Key、日志、SQLite 文件或完整公告原文。

项目笔记中 `RADAR:AUTO` 标记之间的内容会自动更新；“我的跟进记录”及其后内容由你维护，不会被覆盖。

建议从 [[00 雷达首页]] 开始浏览。
"""
        (self.vault_path / "README.md").write_text(text, encoding="utf-8")
