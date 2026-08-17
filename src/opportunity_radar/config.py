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

def load_profile() -> dict:
    name = os.getenv("RADAR_PROFILE", "profiles/general_construction.yaml")
    return load_yaml(name)

def profile_keywords(profile: dict) -> list[str]:
    words: list[str] = []
    limit = max(1, int(os.getenv("RADAR_PROFILE_KEYWORDS_PER_CATEGORY", "3")))
    for item in profile.get("products", []):
        words.append(item["name"])
        words.extend(item.get("direct_keywords", item.get("keywords", []))[:limit - 1])
    return list(dict.fromkeys(words))

def ensure_dirs() -> None:
    for path in (DATA_DIR, DATA_DIR / "auth", DATA_DIR / "reports", DATA_DIR / "logs"):
        path.mkdir(parents=True, exist_ok=True)
