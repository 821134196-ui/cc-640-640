"""应用配置。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("HOUSEKEEPING_DB", BASE_DIR / "data" / "housekeeping.db"))
DB_URL = f"sqlite:///{DB_PATH}"

# 前端静态目录
FRONTEND_DIR = BASE_DIR.parent / "frontend"

CURRENCY = "CNY"
