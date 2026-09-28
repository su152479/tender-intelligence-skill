"""Small, repeatable evaluation harness for opportunity classification rules."""

from __future__ import annotations

import json
from pathlib import Path

from .ai import ANALYZER_VERSION, OpportunityAnalyzer
from .models import Project


def evaluate_fixture(analyzer: OpportunityAnalyzer, fixture: Path) -> dict:
    cases = json.loads(fixture.read_text(encoding="utf-8"))
    true_positive = false_positive = false_negative = schema_pass = exact_match = 0
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
            failures.append({"id": case["id"], "error": str(exc)})
        true_positive += len(expected & actual)
        false_positive += len(actual - expected)
        false_negative += len(expected - actual)
        exact_match += expected == actual
        if expected != actual:
            failures.append({"id": case["id"], "expected": sorted(expected), "actual": sorted(actual)})
        checks = {
            "max_opportunity_score": result.get("当前商机分0-100", 0) <= case.get("max_opportunity_score", 100),
            "min_opportunity_score": result.get("当前商机分0-100", 0) >= case.get("min_opportunity_score", 0),
            "min_lead_score": result.get("项目线索分0-100", 0) >= case.get("min_lead_score", 0),
            "record_kind": result.get("收录类型") == case.get("expected_record_kind", result.get("收录类型")),
        }
        expected_statuses = case.get("expected_product_statuses", {})
        if expected_statuses:
            evidence_by_product = {
                item.get("产品"): item for item in result.get("命中证据", [])
            }
            actual_statuses = {
                product: item.get("产品机会状态") for product, item in evidence_by_product.items()
            }
            checks["product_statuses"] = all(
                actual_statuses.get(product) == status
                for product, status in expected_statuses.items()
            )
            for expected_key, field, check_name in (
                ("expected_demand_evidence", "产品需求证据等级", "demand_evidence"),
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
    precision_denominator = true_positive + false_positive
    recall_denominator = true_positive + false_negative
    return {
        "cases": len(cases),
        "schema_pass_rate": round(schema_pass / len(cases), 4) if cases else 0,
        "exact_product_match_rate": round(exact_match / len(cases), 4) if cases else 0,
        "product_precision": round(true_positive / precision_denominator, 4) if precision_denominator else 1.0,
        "product_recall": round(true_positive / recall_denominator, 4) if recall_denominator else 1.0,
        "failures": failures,
        "semantic_pass_rate": round((len(cases) - len({item['id'] for item in failures})) / len(cases), 4) if cases else 0,
        "analyzer_version": ANALYZER_VERSION,
        "rules_version": analyzer.rules_version,
    }
