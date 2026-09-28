from pathlib import Path
import os, yaml
from dotenv import load_dotenv

load_dotenv()
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = Path(os.getenv("RADAR_DATA_DIR", PROJECT_ROOT / "data")).resolve()

def load_yaml(name: str) -> dict:
    with (CONFIG_DIR / name).open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def ensure_dirs() -> None:
    for path in (DATA_DIR, DATA_DIR / "auth", DATA_DIR / "reports", DATA_DIR / "logs", DATA_DIR / "documents"):
        path.mkdir(parents=True, exist_ok=True)
