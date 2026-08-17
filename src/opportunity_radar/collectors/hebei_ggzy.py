"""河北工程招标公告（京冀公共资源交易跨区域信息专区）。"""

import os

from .beijing_ggzy import BeijingGGZYCollector


class HebeiGGZYCollector(BeijingGGZYCollector):
    """复用北京市公共资源交易服务平台公开的河北招标公告镜像。"""

    def __init__(self, source: dict):
        super().__init__(source)
        self.list_url = source.get("list_url", "https://ggzyfw.beijing.gov.cn/xtgghbzbgg/")
        self.max_pages = int(os.getenv("RADAR_HEBEI_GGZY_MAX_PAGES", "5"))

    def parse_detail(self, html: str, url: str, fallback_title: str = "", fallback_date: str = ""):
        project = super().parse_detail(html, url, fallback_title, fallback_date)
        if project:
            project.region = "河北省"
            project.stage = "施工招标公告"
        return project
