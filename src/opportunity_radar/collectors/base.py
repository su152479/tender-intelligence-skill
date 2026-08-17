from abc import ABC, abstractmethod
from ..models import Project

class BaseCollector(ABC):
    def __init__(self, source: dict): self.source = source
    @abstractmethod
    def collect(self) -> list[Project]: ...
