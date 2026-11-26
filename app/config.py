"""应用配置。"""
from __future__ import annotations

import os
from pathlib import Path

# 每边格数上限
MAX_SIDE = 512
# 支持的色版（顺序即固定编号）
COLORS = ("red", "black")
COLOR_LABELS = {"red": "红版", "black": "黑版"}

# SQLite 数据库路径，可用环境变量覆盖（测试用）
DB_PATH = Path(os.environ.get("SCREENSHOP_DB", "/data/screenshop.db"))
