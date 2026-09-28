from .base import BaseCollector
from .cccc import CCCCCollector
from .luban import CRECGLubanCollector
from .yzw import CSCECYunZhuCollector
from .beijing_ggzy import BeijingGGZYCollector
from .ccgp import CCGPCollector
from .hebei_ggzy import HebeiGGZYCollector
from .tianjin_transport import TianjinTransportCollector
from .jjj_portal import JingJinJiPortalCollector

class NotImplementedCollector(BaseCollector):
    def collect(self):
        raise NotImplementedError(f"{self.source['name']} 真实采集器尚未实现；第一阶段请使用 --mock")

class CRCCCloudCollector(NotImplementedCollector): pass
class PowerChinaCollector(NotImplementedCollector): pass

COLLECTORS = {
    "cccc": CCCCCollector, "cscec_yzw": CSCECYunZhuCollector,
    "crecg_luban": CRECGLubanCollector, "crcc_ec": CRCCCloudCollector,
    "powerchina": PowerChinaCollector, "beijing_ggzy": BeijingGGZYCollector,
    "ccgp": CCGPCollector,
    "hebei_ggzy": HebeiGGZYCollector,
    "tianjin_transport": TianjinTransportCollector,
    "jjj_portal": JingJinJiPortalCollector,
}


def collector_is_implemented(source_id: str) -> bool:
    collector = COLLECTORS.get(source_id)
    return collector is not None and not issubclass(collector, NotImplementedCollector)
