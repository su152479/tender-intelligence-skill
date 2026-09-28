from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations

from .db import Database
from .identity import NoticeIdentity, NoticeIdentityService, identity_text


@dataclass(frozen=True)
class EntityFeature:
    project_id: int
    canonical_name: str
    names: frozenset[str]
    parent_names: frozenset[str]
    regions: frozenset[str]
    owners: frozenset[str]
    identifiers: frozenset[str]
    lots: frozenset[str]
    phases: frozenset[str]
    stations: frozenset[str]
    ranges: frozenset[str]
    source_platforms: frozenset[str]
    event_timeline: tuple[str, ...]
    notice_ids: tuple[int, ...]
    publish_dates: tuple[str, ...]


class EngineeringProjectRelationCandidateService:
    """Compute project-to-project relation candidates without writing or merging entities."""

    def __init__(self, db: Database):
        self.db = db

    def candidates(self, minimum_score: int = 60) -> list[dict]:
        features = self._features()
        result = []
        by_id = {item.project_id: item for item in features}
        for left_id, right_id in self._candidate_pairs(features):
            candidate = self._compare(by_id[left_id], by_id[right_id])
            if candidate and candidate["evidence_score"] >= minimum_score:
                result.append(candidate)
        return sorted(result, key=lambda row: (-row["evidence_score"], row["project_id_a"], row["project_id_b"]))

    def candidate_for(self, project_id_a: int, project_id_b: int) -> dict | None:
        wanted = {int(project_id_a), int(project_id_b)}
        features = [item for item in self._features() if item.project_id in wanted]
        if len(features) != 2:
            return None
        return self._compare(*sorted(features, key=lambda item: item.project_id))

    def _features(self) -> list[EntityFeature]:
        self.db.init()
        with self.db.connect() as con:
            con.row_factory = sqlite3.Row
            projects = con.execute("SELECT * FROM engineering_project ORDER BY id").fetchall()
            links = con.execute(
                """SELECT l.engineering_project_id,p.id notice_id,p.publish_date,p.source_site
                FROM project_notice_link l JOIN project p ON p.id=l.notice_id
                ORDER BY l.engineering_project_id,p.id"""
            ).fetchall()
            identifiers = con.execute(
                """SELECT engineering_project_id,identifier_type,identifier_value
                FROM engineering_project_identifier ORDER BY engineering_project_id,id"""
            ).fetchall()
            events = con.execute(
                """SELECT engineering_project_id,event_date,event_type,lot,party_name,product
                FROM project_event WHERE is_active=1
                ORDER BY engineering_project_id,COALESCE(event_date,'9999-12-31'),id"""
            ).fetchall()
        notice_ids = [int(row["notice_id"]) for row in links]
        identities = NoticeIdentityService(self.db).current_identities(notice_ids)
        links_by_project: dict[int, list[sqlite3.Row]] = {}
        ids_by_project: dict[int, set[str]] = {}
        for row in links:
            links_by_project.setdefault(int(row["engineering_project_id"]), []).append(row)
        for row in identifiers:
            ids_by_project.setdefault(int(row["engineering_project_id"]), set()).add(
                f"{row['identifier_type']}:{row['identifier_value']}"
            )
        events_by_project: dict[int, list[str]] = defaultdict(list)
        for row in events:
            events_by_project[int(row["engineering_project_id"])].append("|".join((
                row["event_date"] or "", row["event_type"] or "", row["lot"] or "",
                row["party_name"] or "", row["product"] or "",
            )))
        result = []
        for project in projects:
            project_id = int(project["id"])
            project_links = links_by_project.get(project_id, [])
            values = [identities.get(int(row["notice_id"]), NoticeIdentity(int(row["notice_id"]))) for row in project_links]
            canonical = project["canonical_name"] or ""
            result.append(EntityFeature(
                project_id=project_id,
                canonical_name=canonical,
                names=frozenset(filter(None, {identity_text(canonical), *(identity_text(v.core_name) for v in values)})),
                parent_names=frozenset(filter(None, (identity_text(v.parent_core_name) for v in values))),
                regions=frozenset(filter(None, {project["region"] or "", *(v.region_value for v in values)})),
                owners=frozenset(filter(None, {project["owner"] or "", *(v.owner for v in values)})),
                identifiers=frozenset(ids_by_project.get(project_id, set())),
                lots=frozenset(value for v in values for value in v.lots),
                phases=frozenset(value for v in values for value in v.phases),
                stations=frozenset(value for v in values for value in v.stations),
                ranges=frozenset(value for v in values for value in v.ranges),
                source_platforms=frozenset(filter(None, (row["source_site"] or "" for row in project_links))),
                event_timeline=tuple(events_by_project.get(project_id, [])),
                notice_ids=tuple(int(row["notice_id"]) for row in project_links),
                publish_dates=tuple(sorted(filter(None, (row["publish_date"] or "" for row in project_links)))),
            ))
        return result

    @staticmethod
    def _candidate_pairs(features: list[EntityFeature]) -> list[tuple[int, int]]:
        """Block by identity anchors instead of comparing every project pair."""
        exact_blocks: dict[str, set[int]] = defaultdict(set)
        fuzzy_blocks: dict[str, set[int]] = defaultdict(set)
        for feature in features:
            anchors = feature.names | feature.parent_names
            for anchor in anchors:
                if not anchor:
                    continue
                exact_blocks[anchor].add(feature.project_id)
                if len(anchor) >= 8:
                    for index in range(len(anchor) - 7):
                        fuzzy_blocks[anchor[index:index + 8]].add(feature.project_id)
        pairs: set[tuple[int, int]] = set()
        for project_ids in exact_blocks.values():
            if len(project_ids) < 2:
                continue
            pairs.update(combinations(sorted(project_ids), 2))
        # Very common eight-character fragments are poor identity anchors and
        # would recreate an unbounded all-pairs comparison inside one bucket.
        for project_ids in fuzzy_blocks.values():
            if not 2 <= len(project_ids) <= 20:
                continue
            pairs.update(combinations(sorted(project_ids), 2))
        return sorted(pairs)

    @staticmethod
    def _compare(left: EntityFeature, right: EntityFeature) -> dict | None:
        exact_name = bool(left.names & right.names)
        common_parent = bool((left.parent_names | left.names) & (right.parent_names | right.names))
        contains_name = any(
            min(len(a), len(b)) >= 8 and (a in b or b in a)
            for a in left.names for b in right.names
        )
        if not (exact_name or common_parent or contains_name):
            return None

        evidence, conflicts, score = [], [], 0
        if exact_name:
            evidence.append("canonical/core name完全一致")
            score += 55
        elif common_parent:
            evidence.append("PARENT_CORE_NAME一致")
            score += 40
        elif contains_name:
            evidence.append("一个规范名称完整包含另一个")
            score += 30

        common_ids = left.identifiers & right.identifiers
        different_ids = bool(left.identifiers and right.identifiers and not common_ids)
        if common_ids:
            evidence.append("存在相同正式identifier")
            score += 30
        elif different_ids:
            conflicts.append("different_strong_identifier")

        if left.regions and right.regions:
            if left.regions & right.regions:
                evidence.append("地区一致")
                score += 10
            else:
                conflicts.append("different_region")
                score -= 25
        if left.owners and right.owners:
            if {identity_text(v) for v in left.owners} & {identity_text(v) for v in right.owners}:
                evidence.append("建设单位一致")
                score += 10
            else:
                conflicts.append("different_owner")
                score -= 20
        for name, a, b, penalty in (
            ("different_phase", left.phases, right.phases, 10),
            ("different_station", left.stations, right.stations, 20),
            ("different_range", left.ranges, right.ranges, 15),
        ):
            if a and b:
                if a & b:
                    evidence.append(name.replace("different_", "common_") + "一致")
                    score += 5
                else:
                    conflicts.append(name)
                    score -= penalty
        different_lot = bool(left.lots and right.lots and not (left.lots & right.lots))
        if different_lot:
            conflicts.append("different_lot")
        elif left.lots & right.lots:
            evidence.append("标段一致")
            score += 5

        if common_ids:
            relation = "SAME_PROJECT"
        elif different_ids or different_lot:
            relation = "SAME_PARENT_PROJECT" if exact_name or common_parent else "RELATED"
        elif contains_name and not exact_name:
            relation = "PARENT_CHILD"
        elif exact_name and not {"different_owner", "different_station", "different_range"} & set(conflicts):
            relation = "SAME_PROJECT"
        else:
            relation = "UNCERTAIN"
        return {
            "project_id_a": left.project_id,
            "project_id_b": right.project_id,
            "relation_candidate": relation,
            "evidence_score": max(0, min(100, score)),
            "evidence": evidence,
            "conflicts": conflicts,
            "project_a": {
                "canonical_name": left.canonical_name, "identifiers": sorted(left.identifiers),
                "regions": sorted(left.regions), "owners": sorted(left.owners),
                "lots": sorted(left.lots), "phases": sorted(left.phases),
                "stations": sorted(left.stations), "ranges": sorted(left.ranges),
                "publication_window": list(left.publish_dates),
                "source_platforms": sorted(left.source_platforms),
                "event_timeline": list(left.event_timeline), "notice_ids": list(left.notice_ids),
            },
            "project_b": {
                "canonical_name": right.canonical_name, "identifiers": sorted(right.identifiers),
                "regions": sorted(right.regions), "owners": sorted(right.owners),
                "lots": sorted(right.lots), "phases": sorted(right.phases),
                "stations": sorted(right.stations), "ranges": sorted(right.ranges),
                "publication_window": list(right.publish_dates),
                "source_platforms": sorted(right.source_platforms),
                "event_timeline": list(right.event_timeline), "notice_ids": list(right.notice_ids),
            },
            "action": "人工审核候选；未执行工程合并",
        }


def direct_unlinked_notices(db: Database) -> list[dict]:
    db.init()
    with db.connect() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """SELECT p.id notice_id,p.project_name,p.source_site,p.publish_date,a.product,
            a.opportunity_score,a.product_opportunity_status
            FROM project p
            JOIN notice_product_assessment a ON a.notice_id=p.id
            LEFT JOIN project_notice_link l ON l.notice_id=p.id
            WHERE l.id IS NULL AND a.product_opportunity_status='DIRECT'
              AND a.id=(SELECT MAX(b.id) FROM notice_product_assessment b
                        WHERE b.notice_id=a.notice_id AND b.product=a.product)
            ORDER BY a.opportunity_score DESC,p.publish_date DESC,p.id"""
        ).fetchall()
        candidate_notice_ids = {
            int(value) for row in con.execute(
                """SELECT notice_id_a,notice_id_b FROM project_link_candidate
                WHERE is_active=1 AND candidate_status IN ('PENDING','DEFERRED')"""
            ).fetchall() for value in row
        }
    identities = NoticeIdentityService(db).current_identities([int(row["notice_id"]) for row in rows])
    result = []
    for row in rows:
        notice_id = int(row["notice_id"])
        identity = identities.get(notice_id, NoticeIdentity(notice_id))
        reasons = []
        if notice_id in candidate_notice_ids:
            reasons.append("CANDIDATE_PENDING_REVIEW")
        if not any(item.strength == "STRONG" for item in identity.identifiers):
            reasons.append("NO_FORMAL_IDENTIFIER")
        if not identity.owner:
            reasons.append("OWNER_MISSING")
        if not identity.core_name:
            reasons.append("CORE_NAME_MISSING")
        if not reasons:
            reasons.append("NOT_BUILT_OR_INSUFFICIENT_IDENTITY")
        result.append({**dict(row), "reason": reasons[0], "reasons": reasons})
    return result
