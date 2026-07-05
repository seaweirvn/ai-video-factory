"""手动运行素材摄取（下载 -> ffprobe -> 回写时长）。

用法：
  python -m scripts.run_ingest [limit]

首次运行会触发 OneDrive 设备码登录（终端打印登录地址与验证码）。
不带 limit 处理全部待处理素材；带数字只处理前 N 条（建议先用 1 验证）。
"""

from __future__ import annotations

import sys

from app.logging import setup_logging
from app.config import get_settings
from services.ingest import get_ingest_service


def main() -> None:
    s = get_settings()
    setup_logging(s.log_dir, s.log_level)
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    summary = get_ingest_service().run(limit=limit)
    print("SUMMARY:", summary)


if __name__ == "__main__":
    main()
