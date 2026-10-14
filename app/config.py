import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    dashscope_api_key: str = os.getenv("DASHSCOPE_API_KEY", "")
    bailian_base_url: str = os.getenv(
        "BAILIAN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    text_model: str = os.getenv("BAILIAN_TEXT_MODEL", "qwen-plus")
    vl_model: str = os.getenv("BAILIAN_VL_MODEL", "qwen3-vl-plus")

    flyai_api_key: str = os.getenv("FLYAI_API_KEY", "")
    flyai_mode: str = os.getenv("FLYAI_MODE", "live")
    flyai_bin: str = os.getenv("FLYAI_BIN", "flyai")

    amap_key: str = os.getenv("AMAP_KEY", "")

    cache_dir: Path = ROOT / ".cache"
    fixture_dir: Path = ROOT / "fixtures"


settings = Settings()
