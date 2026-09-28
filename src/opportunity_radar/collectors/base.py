from abc import ABC, abstractmethod
from ..models import Project

class BaseCollector(ABC):
    def __init__(self, source: dict):
        self.source = source
        self.run_status = "success"
        self.run_error = ""
        self.funnel: dict[str, int] = {}

    def set_funnel(self, **metrics: int) -> None:
        for name, value in metrics.items():
            self.funnel[name] = max(0, int(value))

    def add_funnel(self, **metrics: int) -> None:
        for name, value in metrics.items():
            self.funnel[name] = self.funnel.get(name, 0) + max(0, int(value))

    def mark_partial(self, message: str) -> None:
        self.run_status = "partial"
        self.run_error = "；".join(filter(None, (self.run_error, message)))

    @abstractmethod
    def collect(self) -> list[Project]: ...
