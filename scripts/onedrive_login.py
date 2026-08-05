"""OneDrive 交互式重新登录 / 刷新长期令牌。

用途：当 OneDrive 静默刷新失败（refresh token 过期/被撤销/缓存丢失，日志出现
「请在浏览器打开 https://login.microsoft.com/device 输入验证码」并最终超时）时，
运行本脚本在本机交互式重新登录一次，把新的刷新令牌写回持久缓存，之后无人值守
定时任务即可静默续期上传。

用法：
    .venv\\Scripts\\python.exe scripts\\onedrive_login.py

运行后按提示打开链接并输入验证码，在浏览器里用 seaweir@n9i0.onmicrosoft.com 登录。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loguru import logger  # noqa: E402

from adapters.onedrive.client import get_onedrive_client  # noqa: E402
from app.config import get_settings  # noqa: E402


def main() -> None:
    s = get_settings()
    rec = Path(s.onedrive_auth_record_path)

    # 备份并删除旧凭据记录，强制走「首次登录」交互分支
    if rec.exists():
        backup = rec.with_name(rec.name + ".bak")
        backup.write_text(rec.read_text(encoding="utf-8"), encoding="utf-8")
        rec.unlink()
        logger.info("已备份旧凭据 {} -> {}，并删除以触发交互登录", rec, backup)
    else:
        logger.info("未发现旧凭据 {}，将执行首次登录", rec)

    client = get_onedrive_client()
    logger.info("开始 OneDrive 交互式登录，请按下方提示在浏览器完成验证 ……")

    # 触发首次登录分支：设备码交互 + 写回 auth_record.json + 刷新令牌入持久缓存
    token = client._access_token()
    logger.info("✅ 登录成功，已获取访问令牌（长度 {}）", len(token))

    # 复验静默续期：清掉进程内实例缓存，重新用持久缓存里的刷新令牌静默取 token
    client._credential = None
    token2 = client._access_token()
    logger.info("✅ 静默续期复验通过（长度 {}）——无人值守定时任务之后可自动续期", len(token2))
    logger.info("凭据记录已写入：{}", rec)


if __name__ == "__main__":
    main()
