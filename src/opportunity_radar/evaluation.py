"""Small, repeatable evaluation harness for opportunity classification rules."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from .ai import ANALYZER_VERSION, OpportunityAnalyzer
from .models import Project


def _rate(numerator: int, denominator: int, *, empty: float = 1.0) -> float:
    return round(numerator / denominator, 4) if denominator else empty


def evaluate_fixture(analyzer: OpportunityAnalyzer, fixture: Path) -> dict:
    """Evaluate candidate retrieval and labelled semantics without conflating them.

    ``expected_products`` is a candidate-retrieval label. Demand evidence and
    opportunity status are scored only on fixtures that explicitly label those
    dimensions. An analyzer exception is a failed case, not an evaluator crash.
    """
    cases = json.loads(fixture.read_text(encoding="utf-8"))
    candidate_tp = candidate_fp = candidate_fn = schema_pass = candidate_exact = 0
    demand_cases = demand_exact = 0
    demand_classes: dict[str, dict[str, int]] = defaultdict(
        lambda: {"support": 0, "correct": 0}
    )
    status_cases = status_exact = 0
    direct_tp = direct_fp = direct_fn = 0
    failures = []

    for case in cases:
        expected = set(case["expected_products"])
        try:
            result = analyzer.analyze(Project(
                name=case["name"], raw_text=case.get("raw_text", ""),
                construction_content=case.get("construction_content", ""),
            ))
            actual = set(result["潜在预制产品"])
            schema_pass += 1
        except Exception as exc:
            actual = set()
            failures.append({
                "id": case["id"], "stage": "analyze",
                "error_type": type(exc).__name__, "error": str(exc),
            })
            candidate_fn += len(expected)
            candidate_exact += expected == actual
            continue

        candidate_tp += len(expected & actual)
        candidate_fp += len(actual - expected)
        candidate_fn += len(expected - actual)
        candidate_exact += expected == actual
        if expected != actual:
            failures.append({"id": case["id"], "expected": sorted(expected), "actual": sorted(actual)})

        checks = {
            "max_opportunity_score": result.get("当前商机分0-100", 0) <= case.get("max_opportunity_score", 100),
            "min_opportunity_score": result.get("当前商机分0-100", 0) >= case.get("min_opportunity_score", 0),
            "min_lead_score": result.get("项目线索分0-100", 0) >= case.get("min_lead_score", 0),
            "record_kind": result.get("收录类型") == case.get("expected_record_kind", result.get("收录类型")),
        }
        evidence_by_product = {
            item.get("产品"): item for item in result.get("命中证据", [])
        }
        expected_statuses = case.get("expected_product_statuses", {})
        if expected_statuses:
            status_cases += 1
            actual_statuses = {
                product: evidence_by_product.get(product, {}).get("产品机会状态")
                for product in expected_statuses
            }
            status_matches = all(
                actual_statuses.get(product) == status
                for product, status in expected_statuses.items()
            )
            status_exact += status_matches
            checks["product_statuses"] = status_matches
            for product, expected_status in expected_statuses.items():
                actual_status = actual_statuses.get(product)
                if expected_status == "DIRECT" and actual_status == "DIRECT":
                    direct_tp += 1
                elif expected_status == "DIRECT":
                    direct_fn += 1
                elif actual_status == "DIRECT":
                    direct_fp += 1

        expected_demand = case.get("expected_demand_evidence", {})
        if expected_demand:
            demand_cases += 1
            demand_matches = True
            for product, expected_level in expected_demand.items():
                actual_level = evidence_by_product.get(product, {}).get("产品需求证据等级")
                matched = actual_level == expected_level
                demand_classes[expected_level]["support"] += 1
                demand_classes[expected_level]["correct"] += int(matched)
                demand_matches &= matched
            demand_exact += demand_matches
            checks["demand_evidence"] = demand_matches

        for expected_key, field, check_name in (
            ("expected_evidence_types", "产品需求证据类型", "evidence_types"),
            ("expected_procurement_windows", "采购窗口状态", "procurement_windows"),
        ):
            expected_values = case.get(expected_key, {})
            if expected_values:
                checks[check_name] = all(
                    evidence_by_product.get(product, {}).get(field) == value
                    for product, value in expected_values.items()
                )
        if "expected_project_tracking" in case:
            checks["project_tracking"] = (
                result.get("项目跟踪建议") == case["expected_project_tracking"]
            )
        subject_contains = case.get("expected_subject_contains")
        if subject_contains:
            checks["procurement_subject"] = any(
                subject_contains in value for value in result.get("本次采购对象", [])
            )
        failed_checks = [name for name, passed in checks.items() if not passed]
        if failed_checks:
            failures.append({
                "id": case["id"], "failed_checks": failed_checks,
                "lead_score": result.get("项目线索分0-100"),
                "opportunity_score": result.get("当前商机分0-100"),
                "record_kind": result.get("收录类型"),
                "subjects": result.get("本次采购对象", []),
            })

    candidate_precision = _rate(candidate_tp, candidate_tp + candidate_fp)
    candidate_recall = _rate(candidate_tp, candidate_tp + candidate_fn)
    candidate_exact_rate = _rate(candidate_exact, len(cases), empty=0)
    failed_case_count = len({item["id"] for item in failures})
    return {
        "cases": len(cases),
        "schema_pass_rate": _rate(schema_pass, len(cases), empty=0),
        "candidate_precision": candidate_precision,
        "candidate_recall": candidate_recall,
        "candidate_exact_match_rate": candidate_exact_rate,
        "demand_evidence_cases": demand_cases,
        "demand_exact_match_rate": _rate(demand_exact, demand_cases, empty=0),
        "demand_evidence_by_class": {
            level: {**counts, "accuracy": _rate(counts["correct"], counts["support"], empty=0)}
            for level, counts in sorted(demand_classes.items())
        },
        "opportunity_status_cases": status_cases,
        "opportunity_status_exact_match_rate": _rate(status_exact, status_cases, empty=0),
        "direct_precision": _rate(direct_tp, direct_tp + direct_fp),
        "direct_recall": _rate(direct_tp, direct_tp + direct_fn),
        "direct_support": direct_tp + direct_fn,
        # Deprecated compatibility aliases. These have always measured candidate
        # product retrieval, not calibrated demand or opportunity probabilities.
        "exact_product_match_rate": candidate_exact_rate,
        "product_precision": candidate_precision,
        "product_recall": candidate_recall,
        "failures": failures,
        "semantic_pass_rate": _rate(len(cases) - failed_case_count, len(cases), empty=0),
        "analyzer_version": ANALYZER_VERSION,
        "rules_version": analyzer.rules_version,
    }
