import json

from opportunity_radar.evaluation import evaluate_fixture


class _AnalyzerWithOneFailure:
    rules_version = "test-rules"

    def __init__(self):
        self.calls = []

    def analyze(self, project):
        self.calls.append(project.name)
        if project.name == "坏样本":
            raise RuntimeError("fixture-specific failure")
        return {
            "潜在预制产品": ["箱梁"],
            "当前商机分0-100": 90,
            "项目线索分0-100": 90,
            "收录类型": "当前直接商机",
            "本次采购对象": ["箱梁"],
            "命中证据": [{
                "产品": "箱梁", "产品机会状态": "DIRECT",
                "产品需求证据等级": "STRONG",
                "产品需求证据类型": "DIRECT_TARGET_PRODUCT",
                "采购窗口状态": "OPEN",
            }],
        }


def test_analyzer_exception_is_recorded_and_later_cases_continue(tmp_path):
    fixture = tmp_path / "cases.json"
    fixture.write_text(json.dumps([
        {"id": "broken", "name": "坏样本", "expected_products": ["管片"]},
        {
            "id": "good", "name": "好样本", "expected_products": ["箱梁"],
            "expected_product_statuses": {"箱梁": "DIRECT"},
            "expected_demand_evidence": {"箱梁": "STRONG"},
        },
    ], ensure_ascii=False), encoding="utf-8")
    analyzer = _AnalyzerWithOneFailure()

    result = evaluate_fixture(analyzer, fixture)

    assert analyzer.calls == ["坏样本", "好样本"]
    assert result["cases"] == 2
    assert result["schema_pass_rate"] == 0.5
    assert result["semantic_pass_rate"] == 0.5
    failure = next(item for item in result["failures"] if item["id"] == "broken")
    assert failure == {
        "id": "broken", "stage": "analyze", "error_type": "RuntimeError",
        "error": "fixture-specific failure",
    }


def test_metric_dimensions_and_compatibility_aliases(tmp_path):
    fixture = tmp_path / "cases.json"
    fixture.write_text(json.dumps([{
        "id": "labelled", "name": "好样本", "expected_products": ["箱梁"],
        "expected_product_statuses": {"箱梁": "DIRECT"},
        "expected_demand_evidence": {"箱梁": "STRONG"},
    }], ensure_ascii=False), encoding="utf-8")

    result = evaluate_fixture(_AnalyzerWithOneFailure(), fixture)

    assert result["candidate_exact_match_rate"] == 1
    assert result["demand_exact_match_rate"] == 1
    assert result["opportunity_status_exact_match_rate"] == 1
    assert result["direct_precision"] == result["direct_recall"] == 1
    assert result["product_precision"] == result["candidate_precision"]
    assert result["product_recall"] == result["candidate_recall"]
    assert result["exact_product_match_rate"] == result["candidate_exact_match_rate"]
