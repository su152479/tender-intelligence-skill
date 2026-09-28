"""Final validation between collectors and persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

from .models import Project


@dataclass(frozen=True)
class RejectedProject:
    project: Project
    reasons: tuple[str, ...]


class ProjectValidator:
    def __init__(self, source: dict):
        self.source = source

    def validate(self, projects: list[Project]) -> tuple[list[Project], list[RejectedProject]]:
        accepted: list[Project] = []
        rejected: list[RejectedProject] = []
        seen: set[tuple[str, str]] = set()
        for project in projects:
            reasons = self._reasons(project)
            identity = (project.source_site, project.url)
            if identity in seen:
                reasons.append("本轮重复URL")
            if reasons:
                rejected.append(RejectedProject(project, tuple(reasons)))
                continue
            seen.add(identity)
            accepted.append(project)
        return accepted, rejected

    def _reasons(self, project: Project) -> list[str]:
        reasons = []
        if not project.name.strip():
            reasons.append("缺少项目名称")
        if not project.source_site.strip():
            reasons.append("缺少来源网站")
        parsed = urlparse(project.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            reasons.append("URL无效")
        allowed = tuple(self.source.get("regions", []))
        if allowed and not any(region in (project.region or "") for region in allowed):
            reasons.append("地区不在来源允许范围")
        if project.publish_date:
            try:
                datetime.fromisoformat(project.publish_date[:10])
            except ValueError:
                reasons.append("发布日期格式无效")
        return reasons
