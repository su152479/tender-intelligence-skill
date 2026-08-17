import logging
from .config import DATA_DIR, ensure_dirs

def setup_logging(level: str = "INFO") -> None:
    ensure_dirs()
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(DATA_DIR / "logs" / "radar.log", encoding="utf-8")],
        force=True,
    )
