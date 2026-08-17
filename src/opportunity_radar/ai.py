import json, os
from .models import Project

class OpportunityAnalyzer:
    def __init__(self, products_config: dict): self.cfg = products_config
    def analyze(self, project: Project) -> dict:
        if os.getenv("RADAR_AI_PROVIDER", "heuristic") == "openai" and os.getenv("OPENAI_API_KEY"):
            return self._openai(project)
        return self._heuristic(project)
    def _heuristic(self, project: Project) -> dict:
        title, text = project.name.lower(), f"{project.name}\n{project.raw_text}\n{project.construction_content}".lower()
        matched, reasons, types, evidence = [], [], [], []
        rules = self.cfg.get("rules", {})
        for product in self.cfg["products"]:
            direct_words = product.get("direct_keywords", product.get("keywords", []))
            method_words = product.get("method_keywords", [])
            direction_words = product.get("direction_keywords", [])
            direct_hits = [k for k in direct_words if k.lower() in text]
            method_hits = [k for k in method_words if k.lower() in text]
            direction_hits = [k for k in direction_words if k.lower() in text]
            if direct_hits:
                level, base, hits = "直接产品证据", rules.get("direct_product_score", 90), direct_hits
            elif method_hits:
                level, base, hits = "明确施工方法", rules.get("construction_method_score", 75), method_hits
            elif direction_hits:
                level, base, hits = "工程方向推断", rules.get("direction_inference_score", 55), direction_hits
            else:
                continue
            matched.append(product["name"])
            types.extend(product.get("project_types", []))
            title_hit = any(k.lower() in title for k in hits)
            evidence.append({"产品": product["name"], "等级": level, "关键词": list(dict.fromkeys(hits))})
            reasons.append(f"{level}判断{product['name']}：{'、'.join(dict.fromkeys(hits))}")
            evidence[-1]["评分"] = min(base + (rules.get("title_evidence_bonus", 3) if title_hit else 0), 100)
        score = max((item["评分"] for item in evidence), default=0)
        if len(matched) > 1:
            score += rules.get("multiple_product_bonus", 5)
        strongest = max(evidence, key=lambda item: item["评分"])["等级"] if evidence else "无有效证据"
        confidence = {"直接产品证据": "高", "明确施工方法": "中高", "工程方向推断": "中", "无有效证据": "低"}[strongest]
        return {
            "项目类型": types[0] if types else "未识别",
            "施工方向": project.construction_content or "待人工确认",
            "潜在产品或服务": matched,
            "匹配理由": "；".join(reasons) or "未命中产品知识库关键词",
            "机会评分0-100": min(score, rules.get("max_score", 100)),
            "证据等级": strongest, "置信度": confidence, "命中证据": evidence,
        }
    def _openai(self, project: Project) -> dict:
        from openai import OpenAI
        schema = '{"项目类型":"", "施工方向":"", "潜在产品或服务":[], "匹配理由":"", "机会评分0-100":0}'
        prompt = f"根据公告和行业机会知识库判断潜在业务机会。仅输出严格 JSON，结构为 {schema}\n公告：{project.raw_text[:12000]}\n知识库：{json.dumps(self.cfg, ensure_ascii=False)}"
        response = OpenAI().responses.create(model=os.getenv("OPENAI_MODEL", "gpt-5-mini"), input=prompt)
        return json.loads(response.output_text)
    def apply(self, project: Project) -> Project:
        result = self.analyze(project)
        project.ai_score = int(result["机会评分0-100"])
        project.matched_products = result.get("潜在产品或服务", result.get("潜在预制产品", []))
        project.analysis_json = json.dumps(result, ensure_ascii=False)
        return project
