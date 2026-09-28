from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

from .db import Database
from .project_events import ProjectEventService


LIFECYCLE_VERSION = "project-lifecycle-v1.0"

EVENT_STAGE = {
    "TENDER_ANNOUNCED": "TENDERING",
    "CONTRACTOR_CANDIDATE_SELECTED": "CONTRACTOR_CANDIDATE_SELECTED",
    "CONTRACTOR_SELECTED": "CONTRACTOR_SELECTED",
    "CONSTRUCTION_PROGRESS": "UNDER_CONSTRUCTION",
}

STAGE_RANK = {
    "UNKNOWN": 0,
    "PLANNING": 10,
    "TENDERING": 20,
    "CONTRACTOR_CANDIDATE_SELECTED": 30,
    "CONTRACTOR_SELECTED": 40,
    "UNDER_CONSTRUCTION": 50,
    "COMPLETED": 60,
}


class ProjectLifecycleAggregator:
    """Derive a read-only observed project stage from canonical event facts."""

    def __init__(self, db: Database):
        self.db = db

    def lifecycle(self, engineering_project_id: int) -> dict:
        projects = self._projects(engineering_project_id)
        if not projects:
            raise ValueError(f"工程实体不存在：{engineering_project_id}")
        events = ProjectEventService(self.db).canonical_events(
            engineering_project_id, read_only=True,
        )
        return self._derive(projects[0], events)

    def all_lifecycles(self) -> list[dict]:
        projects = self._projects()
        events = ProjectEventService(self.db).canonical_events(read_only=True)
        grouped: dict[int, list[dict]] = defaultdict(list)
        for event in events:
            grouped[int(event["engineering_project_id"])].append(event)
        return [self._derive(project, grouped.get(int(project["id"]), [])) for project in projects]

    def audit(self, sample_limit: int = 20) -> dict:
        rows = self.all_lifecycles()
        conflicts = [item for row in rows for item in row["consistency_conflicts"]]
        return {
            "lifecycle_version": LIFECYCLE_VERSION,
            "writes": 0,
            "engineering_projects": len(rows),
            "stage_distribution": dict(sorted(Counter(row["current_stage"] for row in rows).items())),
            "coverage_distribution": dict(sorted(Counter(row["coverage_scope"] for row in rows).items())),
            "consistency_conflicts": conflicts,
            "conflict_count": len(conflicts),
            "samples": self._samples(rows, max(0, sample_limit)),
        }

    def _projects(self, project_id: int | None = None) -> list[dict]:
        path = Path(self.db.path)
        if not path.exists():
            raise ValueError(f"数据库不存在：{path}")
        uri = path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as con:
            con.row_factory = sqlite3.Row
            sql = "SELECT * FROM engineering_project"
            params: tuple = ()
            if project_id is not None:
                sql += " WHERE id=?"
                params = (int(project_id),)
            sql += " ORDER BY id"
            return [dict(row) for row in con.execute(sql, params)]

    def _derive(self, project: dict, events: list[dict]) -> dict:
        factual = [event for event in events if event["event_type"] in EVENT_STAGE]
        terminations = [
            event for event in events
            if event["event_type"] == "PROJECT_TERMINATED"
            and self._scope(event)[0] == "PROJECT_LEVEL"
        ]
        ignored_terminations = [
            event for event in events
            if event["event_type"] == "PROJECT_TERMINATED" and event not in terminations
        ]
        conflicts = self._consistency_conflicts(project, factual)
        for event in ignored_terminations:
            conflicts.append({
                "code": "NARROW_TERMINATION_NOT_PROJECT_TERMINATION",
                "engineering_project_id": int(project["id"]),
                "canonical_event_key": event["canonical_event_key"],
                "event_date": event["event_date"],
                "event_scope": event["event_scope"],
                "reason": "终止事实带有标段或子工程范围，未升级为整个工程终止",
            })

        if terminations:
            current_stage = "TERMINATED"
            supporting = terminations
        elif factual:
            current_stage = max(
                (EVENT_STAGE[event["event_type"]] for event in factual),
                key=lambda stage: STAGE_RANK[stage],
            )
            supporting = [event for event in factual if EVENT_STAGE[event["event_type"]] == current_stage]
        else:
            current_stage, supporting = "UNKNOWN", []

        scope_level, coverage, detail = self._coverage(supporting)
        dates = sorted({event["event_date"] for event in supporting if event["event_date"]})
        previous_stage = self._previous_stage(factual, supporting, current_stage)
        evidence = [self._evidence(event) for event in supporting]
        source_events = [source for event in supporting for source in event.get("sources", [])]
        confidence = self._confidence(supporting)
        return {
            "engineering_project_id": int(project["id"]),
            "project_name": project["canonical_name"],
            "current_stage": current_stage,
            "observed_stage": current_stage,
            "previous_stage": previous_stage,
            "stage_since": dates[0] if dates else "",
            "stage_since_min": dates[0] if dates else "",
            "stage_since_max": dates[-1] if dates else "",
            "stage_confidence": confidence,
            "scope_level": scope_level,
            "coverage_scope": coverage,
            "coverage_detail": detail,
            "stage_evidence": evidence,
            "stage_source_events": source_events,
            "stage_reason": self._reason(current_stage, coverage, detail, len(supporting)),
            "canonical_event_count": len(events),
            "consistency_conflicts": conflicts,
            "lifecycle_version": LIFECYCLE_VERSION,
            "writes": 0,
        }

    @staticmethod
    def _scope(event: dict) -> tuple[str, str]:
        lots = [part for part in (event.get("lot") or "").split(",") if part]
        if lots:
            return "LOT_LEVEL", "LOT:" + ",".join(sorted(lots))
        scope = event.get("event_scope") or ""
        if any(label in scope for label in ("子工程:", "期次:", "站点:", "范围:")):
            return "SUBPROJECT_LEVEL", "SUBPROJECT:" + scope
        if event.get("event_type") == "PROJECT_TERMINATED" and not scope:
            return "PROJECT_LEVEL", "PROJECT"
        return "UNKNOWN_SCOPE", "UNKNOWN:" + (scope or event.get("canonical_event_key", ""))

    def _coverage(self, events: list[dict]) -> tuple[str, str, list[str]]:
        if not events:
            return "UNKNOWN_SCOPE", "UNKNOWN", []
        lots = sorted({part for event in events for part in (event.get("lot") or "").split(",") if part}, key=self._lot_sort)
        levels = {self._scope(event)[0] for event in events}
        if lots:
            return "LOT_LEVEL", "SINGLE_LOT" if len(lots) == 1 else "PARTIAL_LOTS", ["LOT " + ",".join(lots)]
        if levels == {"PROJECT_LEVEL"}:
            return "PROJECT_LEVEL", "PROJECT_WIDE", ["PROJECT"]
        scoped = sorted({event.get("event_scope") or "" for event in events if event.get("event_scope")})
        if "SUBPROJECT_LEVEL" in levels:
            return "SUBPROJECT_LEVEL", "UNKNOWN", scoped
        return "UNKNOWN_SCOPE", "UNKNOWN", scoped

    @staticmethod
    def _lot_sort(value: str) -> tuple[int, object]:
        return (0, int(value)) if value.isdigit() else (1, value)

    def _previous_stage(self, factual: list[dict], supporting: list[dict], current_stage: str) -> str:
        if current_stage in {"UNKNOWN", "TERMINATED"} or not supporting:
            return "UNKNOWN"
        support_scopes = {self._scope(event)[1] for event in supporting}
        candidates = []
        for event in factual:
            stage = EVENT_STAGE[event["event_type"]]
            if self._scope(event)[1] in support_scopes and STAGE_RANK[stage] < STAGE_RANK[current_stage]:
                candidates.append(stage)
        return max(candidates, key=lambda stage: STAGE_RANK[stage], default="UNKNOWN")

    def _consistency_conflicts(self, project: dict, events: list[dict]) -> list[dict]:
        by_scope: dict[str, list[dict]] = defaultdict(list)
        for event in events:
            level, key = self._scope(event)
            if level != "UNKNOWN_SCOPE" and event.get("event_date"):
                by_scope[key].append(event)
        conflicts = []
        for scope_key, scoped in by_scope.items():
            for advanced in scoped:
                advanced_stage = EVENT_STAGE[advanced["event_type"]]
                for earlier in scoped:
                    earlier_stage = EVENT_STAGE[earlier["event_type"]]
                    if (
                        STAGE_RANK[advanced_stage] > STAGE_RANK[earlier_stage]
                        and advanced["event_date"] < earlier["event_date"]
                    ):
                        key = (advanced["canonical_event_key"], earlier["canonical_event_key"])
                        if any(item.get("event_keys") == list(key) for item in conflicts):
                            continue
                        conflicts.append({
                            "code": "LIFECYCLE_EVENT_ORDER_CONFLICT",
                            "engineering_project_id": int(project["id"]),
                            "scope": scope_key,
                            "event_keys": list(key),
                            "reason": f"{advanced_stage}日期{advanced['event_date']}早于{earlier_stage}日期{earlier['event_date']}",
                        })
        return conflicts

    @staticmethod
    def _evidence(event: dict) -> dict:
        return {
            "canonical_event_key": event["canonical_event_key"],
            "event_type": event["event_type"],
            "event_date": event["event_date"],
            "event_date_source": event["event_date_source"],
            "event_scope": event["event_scope"],
            "lot": event["lot"],
            "party_name": event["party_name"],
            "evidence_text": event["evidence_text"],
            "source_count": event["source_count"],
        }

    @staticmethod
    def _confidence(events: list[dict]) -> str:
        if not events:
            return "LOW"
        values = {event.get("confidence", "LOW") for event in events}
        if values == {"HIGH"}:
            return "HIGH"
        return "MEDIUM" if "HIGH" in values or "MEDIUM" in values else "LOW"

    @staticmethod
    def _reason(stage: str, coverage: str, detail: list[str], count: int) -> str:
        if stage == "UNKNOWN":
            return "没有Canonical施工事实事件，未从公告标题、项目跟踪或产品机会绕过事件层推断"
        label = {
            "TENDERING": "已观察到施工、EPC或资格预审类招标事实",
            "CONTRACTOR_CANDIDATE_SELECTED": "已观察到施工中标候选人事实，尚不等同最终中标",
            "CONTRACTOR_SELECTED": "已观察到施工单位正式确定事实",
            "UNDER_CONSTRUCTION": "已观察到明确施工进展事实",
            "TERMINATED": "已观察到无较窄Scope的整个工程终止事实",
        }[stage]
        caveat = {
            "PARTIAL_LOTS": "；仅覆盖已观察到的部分标段，不代表全部标段",
            "SINGLE_LOT": "；仅覆盖一个已观察标段",
            "UNKNOWN": "；事件覆盖范围不足，不能声称全工程均处于该阶段",
            "PROJECT_WIDE": "；证据作用于工程级范围",
        }[coverage]
        return f"{label}（{count}条Canonical Event）{caveat}" + (f"：{'、'.join(detail)}" if detail else "")

    @staticmethod
    def _samples(rows: list[dict], limit: int) -> list[dict]:
        if not limit:
            return []
        ordered = sorted(rows, key=lambda row: (
            row["engineering_project_id"] not in {52, 59},
            -len(row["stage_evidence"]),
            row["current_stage"] == "UNKNOWN",
            row["engineering_project_id"],
        ))
        return ordered[:limit]
