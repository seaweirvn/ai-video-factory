# BGM 曲库

背景音乐目录。选曲三层兜底：**在线 Provider（可选）→ 本地曲库 → 无 BGM**。
由 `config/bgm.yaml` 驱动，`services/director/bgm.py` 选曲，`adapters/ffmpeg/compose.py`
把曲子垫在口播下（音量压到 `director_bgm_volume`，自动循环并按成片长度裁剪）。

## 本地曲库（手动放文件）

- 放可商用 / 免版权音频（`.mp3` / `.m4a`）。比视频短会自动循环。
- 目录建议按国家分：`assets/bgm/vn/upbeat_01.mp3`。
- 在 `config/bgm.yaml` 里登记：
  ```yaml
  countries:
    VN:
      upbeat: [vn/upbeat_01.mp3]
  moods:            # 全局兜底（不区分国家）
    upbeat: [common/upbeat_01.mp3]
  ```

## 自动在线选曲（可选，默认关）

- 在 `.env` 配 `JAMENDO_CLIENT_ID`（devportal.jamendo.com 免费注册），
  并把 `config/bgm.yaml` 的 `online.enabled` 设为 `true`（或设 `BGM_ONLINE_ENABLED=true`）。
- 按「国家 + 情绪」检索可商用曲（默认只取 CC-BY，需署名），下载缓存到 `cache/`。
- 命中的曲目署名信息会随成片带出（`summary["bgm"]`）。CC-BY 需在成片描述/记录里署名。
- 任何失败/超时/无授权都会静默回落本地曲库 → 无 BGM，不影响成片。

`cache/` 为下载缓存，已在 `.gitignore` 忽略。
