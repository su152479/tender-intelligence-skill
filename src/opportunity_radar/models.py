from dataclasses import dataclass, field

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
