from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


EvidenceLevel = Literal["直接产品证据", "工程需求证据", "明确施工方法", "工程方向推断", "无有效证据"]
ConfidenceLevel = Literal["高", "中高", "中", "低"]
DemandIntent = Literal["采购", "生产", "安装", "施工使用", "可能使用", "既有设施", "检测维护", "另行采购", "排除"]
DemandStatus = Literal["CONFIRMED", "LIKELY", "POSSIBLE", "EXISTING", "EXCLUDED", "SEPARATE_PROC", "UNKNOWN"]
EngineeringNeed = Literal["YES", "LIKELY", "POSSIBLE", "NO", "UNKNOWN"]
ScopeStatus = Literal["IN_SCOPE", "OUT_OF_SCOPE", "SEPARATE_PROC", "UNCERTAIN"]
CurrentOpportunityStatus = Literal["OPEN", "MONITOR", "CLOSED", "UNKNOWN"]
NoticeActionability = Literal["OPEN", "CLOSED", "UNKNOWN"]
ProjectProgressSignal = Literal[
    "CONSTRUCTION_TENDER_OPEN", "CONTRACTOR_CANDIDATE_SELECTED",
    "CONTRACTOR_SELECTED", "CONSTRUCTION_PROGRESS", "UNKNOWN",
]
ProductOpportunityStatus = Literal[
    "DIRECT", "PRE_PROCUREMENT", "FOLLOW_UP", "NO_EVIDENCE", "OUT_OF_SCOPE", "ENDED",
]
ProductDemandEvidenceLevel = Literal["NONE", "WEAK", "MEDIUM", "STRONG"]
ProductDemandEvidenceType = Literal[
    "DIRECT_TARGET_PRODUCT", "ACCESSORY_OR_SUPPORTING_PRODUCT", "CONSTRUCTION_METHOD",
    "ENGINEERING_DIRECTION", "NEGATIVE_EVIDENCE",
]
ProcurementWindowStatus = Literal["OPEN", "UPCOMING", "UNKNOWN", "ENDED"]
ProjectTrackingRecommendation = Literal[
    "KEY_FOLLOW_UP", "ACTIVE_WATCH", "LOW_PRIORITY", "ARCHIVED", "UNKNOWN",
]
EvidenceRelation = Literal["采购对象", "施工业务", "工程需求", "工程方向", "上下游配套", "排除"]
RecordKind = Literal["当前直接商机", "前置项目线索", "项目最新进展", "历史进展", "排除"]
FollowUpPriority = Literal["立即跟进", "重点跟进", "重点观察", "一般线索", "无需跟进"]


class ProductEvidence(BaseModel):
    """One auditable product match. Unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    product: str = Field(alias="产品", min_length=1)
    level: EvidenceLevel = Field(alias="等级")
    keywords: list[str] = Field(alias="关键词", default_factory=list)
    locations: list[str] = Field(alias="证据位置", default_factory=list)
    sentences: list[str] = Field(alias="证据句", default_factory=list)
    negative_evidence: list[str] = Field(alias="负向证据", default_factory=list)
    demand_intent: DemandIntent = Field(alias="需求意图", default="可能使用")
    demand_status: DemandStatus = Field(alias="需求状态", default="UNKNOWN")
    engineering_need: EngineeringNeed = Field(alias="工程需求", default="UNKNOWN")
    scope_status: ScopeStatus = Field(alias="范围状态", default="UNCERTAIN")
    opportunity_status: CurrentOpportunityStatus = Field(alias="当前机会状态", default="UNKNOWN")
    product_opportunity_status: ProductOpportunityStatus = Field(alias="产品机会状态", default="NO_EVIDENCE")
    product_demand_evidence_level: ProductDemandEvidenceLevel = Field(
        alias="产品需求证据等级", default="NONE"
    )
    product_demand_evidence_type: ProductDemandEvidenceType = Field(
        alias="产品需求证据类型", default="ENGINEERING_DIRECTION"
    )
    procurement_window_status: ProcurementWindowStatus = Field(alias="采购窗口状态", default="UNKNOWN")
    procurement_window_evidence: list[str] = Field(alias="采购窗口证据", default_factory=list)
    score: int = Field(alias="评分", ge=0, le=100)
    relation: EvidenceRelation = Field(alias="证据关系", default="工程方向")
    lead_score: int = Field(alias="项目线索分", ge=0, le=100, default=0)
    opportunity_score: int = Field(alias="当前商机分", ge=0, le=100, default=0)
    score_explanation: list[str] = Field(alias="评分说明", default_factory=list)

    @field_validator(
        "keywords", "locations", "sentences", "negative_evidence", "score_explanation",
        "procurement_window_evidence",
    )
    @classmethod
    def unique_non_empty(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class OpportunityAnalysis(BaseModel):
    """Stable AI/rules output contract persisted in project.analysis_json."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    project_type: str = Field(alias="项目类型", min_length=1)
    construction_direction: str = Field(alias="施工方向", min_length=1)
    potential_products: list[str] = Field(alias="潜在预制产品", default_factory=list)
    reason: str = Field(alias="匹配理由", min_length=1)
    score: int = Field(alias="机会评分0-100", ge=0, le=100)
    procurement_subjects: list[str] = Field(alias="本次采购对象", default_factory=list)
    lead_score: int = Field(alias="项目线索分0-100", ge=0, le=100, default=0)
    opportunity_score: int = Field(alias="当前商机分0-100", ge=0, le=100, default=0)
    record_kind: RecordKind = Field(alias="收录类型", default="前置项目线索")
    follow_up_priority: FollowUpPriority = Field(alias="跟进优先级", default="一般线索")
    notice_actionability: NoticeActionability = Field(alias="公告可参与性", default="UNKNOWN")
    project_progress_signal: ProjectProgressSignal = Field(alias="项目进展信号", default="UNKNOWN")
    project_tracking_recommendation: ProjectTrackingRecommendation = Field(
        alias="项目跟踪建议", default="UNKNOWN"
    )
    project_tracking_reasons: list[str] = Field(alias="项目跟踪理由", default_factory=list)
    display_categories: list[str] = Field(alias="展示分类", default_factory=list)
    recommended_action: str = Field(alias="建议动作", default="继续核对公告与项目证据")
    score_explanation: list[str] = Field(alias="评分说明", default_factory=list)
    evidence_level: EvidenceLevel = Field(alias="证据等级")
    confidence: ConfidenceLevel = Field(alias="置信度")
    evidence: list[ProductEvidence] = Field(alias="命中证据", default_factory=list)
    product_scores: dict[str, int] = Field(alias="产品证据分", default_factory=dict)
    analyzer_version: str = Field(alias="分析器版本", min_length=1)
    rules_version: str = Field(alias="规则版本", min_length=1)

    @field_validator(
        "potential_products", "procurement_subjects", "score_explanation", "display_categories",
        "project_tracking_reasons",
    )
    @classmethod
    def unique_products(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    def validate_evidence_consistency(self) -> "OpportunityAnalysis":
        evidence_products = {item.product for item in self.evidence}
        missing = set(self.potential_products) - evidence_products
        if missing:
            raise ValueError(f"产品缺少可审计证据：{', '.join(sorted(missing))}")
        if not self.potential_products and self.score != 0:
            raise ValueError("无匹配产品时总评分必须为 0")
        if self.score != self.opportunity_score:
            raise ValueError("兼容机会评分必须等于当前商机分")
        invalid_scores = {name: score for name, score in self.product_scores.items() if not 0 <= score <= 100}
        if invalid_scores:
            raise ValueError(f"产品证据分超出范围：{invalid_scores}")
        missing_scores = set(self.potential_products) - set(self.product_scores)
        if missing_scores:
            raise ValueError(f"潜在产品缺少独立证据分：{', '.join(sorted(missing_scores))}")
        return self

    def as_dict(self) -> dict:
        return self.model_dump(by_alias=True)

@dataclass
class Project:
    name: str
    project_no: str = ""
    publish_date: str = ""
    region: str = ""
    owner: str = ""
    tenderer: str = ""
    stage: str = ""
    construction_content: str = ""
    source_site: str = ""
    url: str = ""
    raw_text: str = ""
    ai_score: int = 0
    matched_products: list[str] = field(default_factory=list)
    analysis_json: str = "{}"
    analyzer_version: str = ""
    rules_version: str = ""
    notice_actionability: str = "UNKNOWN"
    project_progress_signal: str = "UNKNOWN"
    project_tracking_recommendation: str = "UNKNOWN"
