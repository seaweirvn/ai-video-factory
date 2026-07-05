"""集中配置。所有可调参数走环境变量 / .env，业务代码只读取这里。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 服务
    app_name: str = "ai-video-factory"
    app_env: str = "dev"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    log_level: str = "INFO"
    api_key: str = "change-me"

    # 目录
    workspace_dir: Path = Path("workspace")
    log_dir: Path = Path("logs")
    data_dir: Path = Path("data")

    # 飞书（VN）
    feishu_vn_app_id: str = ""
    feishu_vn_app_secret: str = ""
    feishu_vn_bitable_app_token: str = ""
    # 素材库在“营销”知识库多维表格下，app_token 与主 bitable 不同
    feishu_vn_material_app_token: str = ""
    feishu_vn_material_table_id: str = ""
    feishu_vn_render_table_id: str = ""
    feishu_vn_publish_table_id: str = ""
    feishu_vn_account_table_id: str = ""
    feishu_vn_product_table_id: str = ""
    feishu_vn_analytics_table_id: str = ""
    feishu_vn_render_material_map_table_id: str = ""
    feishu_vn_contribution_table_id: str = ""
    feishu_vn_job_log_table_id: str = ""

    # OneDrive
    onedrive_client_id: str = "cf9e61b0-87c2-46f1-85f6-bb144b8e7085"
    onedrive_tenant_id: str = "n9i0.onmicrosoft.com"
    onedrive_target_folder: str = "/04.AI Center/KOL VIDEO/03.VN"
    onedrive_scopes: str = "https://graph.microsoft.com/Files.ReadWrite"
    onedrive_auth_record_path: Path = Path("data/onedrive/auth_record.json")
    onedrive_token_cache_name: str = "ai_video_onedrive_cache"
    onedrive_link_type: str = "view"
    onedrive_link_scope: str = "anonymous"
    # 成片上传到的 OneDrive 目录（与素材目录分开）
    onedrive_render_folder: str = "/04.AI Center/KOL VIDEO/03.VN/_AI_Renders"

    # 引擎参数
    perf_weight_completion: float = 0.30
    perf_weight_engagement: float = 0.25
    perf_weight_views: float = 0.15
    perf_weight_gmv: float = 0.30
    selection_epsilon_start: float = 0.30
    selection_epsilon_floor: float = 0.10
    # 成片目标时长与允许的超出比例（flexible 选材用）
    selection_target_duration_sec: float = 25.0
    selection_max_overshoot: float = 1.25
    scoring_shrink_k: float = 5.0
    scoring_optimistic_init: float = 0.60

    def ensure_dirs(self) -> None:
        for path in (self.workspace_dir, self.log_dir, self.data_dir):
            Path(path).mkdir(parents=True, exist_ok=True)
        Path(self.onedrive_auth_record_path).parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
