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
    # 成片表（各国家一张表，越南表标题“越南”）
    feishu_vn_render_app_token: str = ""
    feishu_vn_render_table_id: str = ""
    # 成片=发布合表：中国（同一租户 wiki「中国」多维表格；中文孪生版写这里，评分侧按 CN 读取）
    feishu_cn_render_app_token: str = ""
    feishu_cn_render_table_id: str = ""
    feishu_vn_publish_table_id: str = ""
    # 发布表 app_token（默认与成片表同库；不同则单独配）
    feishu_vn_publish_app_token: str = ""
    feishu_vn_account_table_id: str = ""
    # 账号表 app_token（默认与成片表同库；不同则单独配）
    feishu_vn_account_app_token: str = ""
    feishu_vn_product_table_id: str = ""
    # 产品中心表 app_token（默认与素材/主 bitable 同库；不同则单独配）
    feishu_vn_product_app_token: str = ""
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

    # Cloudflare R2（S3 兼容对象存储）。路径前缀待素材目录结构确认后配置；
    # 迁移完成前 storage_provider 保持 onedrive，避免后台任务误切半成品链路。
    storage_provider: str = "onedrive"  # onedrive | r2
    r2_endpoint_url: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = ""
    r2_public_domain: str = ""
    r2_material_prefix: str = ""
    r2_render_prefix: str = ""
    r2_kol_prefix: str = ""

    # KOL 原始视频下载（自 seaweir-video 迁入的独立链路：飞书创作者表 -> TikWM -> OneDrive -> 归档）。
    # 与 AI 剪辑无关：只下载、上传、回写、归档，成品走独立发布。
    feishu_vn_kol_video_table_id: str = ""  # 飞书创作者视频表 ID
    feishu_vn_kol_app_token: str = ""       # KOL 表所在 bitable app_token（留空则回退 bitable_app_token）
    kol_download_dir: Path = Path("data/kol/videos")     # 本地下载暂存目录
    kol_archive_dir: Path = Path("data/kol/archive")     # 回写成功后的归档目录
    kol_download_timeout_sec: int = 420                    # 单条下载墙钟超时，超时跳过继续
    ffprobe_path: Path | None = None  # SYSTEM 计划任务无法继承用户 PATH 时显式指定
    # KOL 原始视频上传的国家根目录；实际按月份放到 {根}/{YYYY-MM}/ 下（其他国家改这个根目录即可）
    onedrive_kol_folder: str = "/04.AI Center/KOL VIDEO/03.VN"

    # 引擎参数
    # 素材/卖点评分的复合回报权重（各分量为「同产品内分位归一」0~1）：以成交为导向，
    # GMV + 转化率(CVR) 权重最高，商品点击率(CTR)、播放为辅。合表无完播数据，completion 默认 0。
    perf_weight_gmv: float = 0.45
    perf_weight_cvr: float = 0.35
    perf_weight_ctr: float = 0.15
    perf_weight_views: float = 0.05
    perf_weight_completion: float = 0.0
    perf_weight_engagement: float = 0.0   # 兼容旧字段，默认不计入
    # 成熟度门槛：播放量达到此值的成片才纳入评分，避免新视频 0/低播放引入噪声
    score_min_views: int = 200
    selection_epsilon_start: float = 0.30
    selection_epsilon_floor: float = 0.10
    # 成片目标时长与允许的超出比例（flexible 选材用）
    selection_target_duration_sec: float = 25.0
    selection_max_overshoot: float = 1.25
    scoring_shrink_k: float = 5.0
    scoring_optimistic_init: float = 0.60

    # AI 文案（OpenAI 兼容接口；未配置 key 时自动降级为模板兜底）
    ai_provider: str = "auto"  # auto | openai | null
    # content_language：内容/字幕语言码（vi/th/ms/id...）。
    # 注意：Storyboard/字幕体系里的 "market" 字段就是 content_language 的别名（不是 content_country）；
    #       locales/<market>/subtitles.yaml 按此语言码取模板。
    content_language: str = "vi"
    # 目标国家（多国扩展用；影响文案本地化 & 以后按国家切库）。与 market 不同：VN 是国家，vi 是语言。
    content_country: str = "VN"

    # 中文孪生版：每产一条越南版后，用「同一选材/结构」再产一条中文版（只换中文字幕+中文配音，
    # 重渲染，画面完全一致），写入「中国」成片表。发布到平台暂不做（预留接口）。
    cn_twin_enabled: bool = False
    cn_content_language: str = "zh"   # 中文版内容/字幕语言码（= market）
    cn_voice_profile: str = "cn_female_01"  # 中文配音音色档（见 VOICE_PROFILES）

    # 成交视频结构算法（Conversion Director + Storyboard 驱动字幕/口播）。
    # 默认关闭：关时 produce 走原有阶段 0-4 主链路，完全不受影响。
    # 开启后 produce 用 Storyboard 解析出的 stage 文案作为口播/字幕来源（模板池为空回落原 ContentService 生成）。
    storyboard_enabled: bool = False

    # Director Engine（大脑层：先产 Content Brief 再选材/写字幕/剪辑）。
    # 默认关闭：关时 produce 走原链路（含 storyboard_enabled 路径），完全不受影响。
    # 开启需配合 voiceover（时间槽渲染）。director_strategy: heuristic（默认确定性）| llm（可选，缺 key 自动回落）。
    director_enabled: bool = False
    # director_strategy: llm（LLM 导演，主）| heuristic（纯 config 启发式）。llm 缺 key 自动回落 heuristic。
    director_strategy: str = "llm"
    # BGM 情绪槽：为空则用 Brief.audio_mood；BGM 在配音下压低混音的音量（0~1）
    director_bgm_enabled: bool = True
    director_bgm_volume: float = 0.12
    # 自动在线选曲（可选，默认关）：开启后按 国家+情绪 从在线曲库检索可商用曲并下载缓存，
    # 失败/超时/无 key 静默回落本地曲库 -> 无 BGM，绝不打断成片。provider 可插拔。
    # 默认 jamendo：免费 API + 只取 CC-BY 可商用曲（署名自动带出）；magnific 需付费订阅（备用）。
    bgm_online_enabled: bool = False
    bgm_online_provider: str = "jamendo"
    bgm_online_timeout_sec: float = 15.0
    # Jamendo 开发者应用 client_id（devportal.jamendo.com 免费注册）；缺失时在线选曲自动回落本地。
    jamendo_client_id: str = ""
    # Magnific Music API key（付费订阅，备用 provider）；缺失时自动回落本地/音乐库。
    magnific_api_key: str = ""
    magnific_base_url: str = "https://api.magnific.com"
    # 每条视频「是否加 BGM」的概率（0~1）：每次剪辑各 50% 概率开/关背景音乐。
    director_bgm_probability: float = 0.5
    # 选曲探索率 ε：ε 概率去在线拉「新曲」扩充音乐库，其余按 GMV 分复用库内高分曲（经常复用）。
    director_bgm_explore_epsilon: float = 0.25
    # 自由时长：每段时长由「口播实际长度 + 尾部留白」决定，不再拉齐到固定时间槽。
    # 每段最短展示时长（保证镜头能被看清，即使口播极短）；每句后尾部留白。
    director_beat_min_sec: float = 1.5
    director_beat_tail_sec: float = 0.4
    # 每段最长时长上限（0=不限制；口播更长时不会截断口播）。用于给「无口播/长镜头」段兜底封顶。
    director_beat_max_sec: float = 0.0

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    # 通用 LLM 模型（导演/文案包/标题等）。用更强的 gpt-4o 保证越南版&中文版整体文案地道。
    openai_model: str = "gpt-4o"
    # 字幕/口播文案专用模型：文案短但要「地道、母语、不生造词/不夹英文」，弱模型(mini)在中文域词上
    # 易翻译腔+瞎编术语。这里默认用更强的 gpt-4o（字幕 token 很少，成本增量可忽略）。留空则回落 openai_model。
    openai_caption_model: str = "gpt-4o"

    # 配音（TTS）+ 字幕；默认关闭，配音需要 openai_api_key
    openai_tts_model: str = "gpt-4o-mini-tts"
    tts_voice: str = "alloy"
    voiceover_enabled_default: bool = False
    voiceover_subtitle_default: bool = True
    # “保留原声”片段里，原声混入时的音量（0~1）。默认 1.0：原声不压低，与配音等音量混合
    voiceover_kept_original_volume: float = 1.0
    # 选材目标时长在配音时长上加的余量（秒），保证画面覆盖配音
    voiceover_tail_margin_sec: float = 1.5

    # 真人化配音（可插拔 VoiceProvider + 主播口语 + 逐句合成）
    # voice_provider: auto | openai | edge | elevenlabs | cartesia
    voice_provider: str = "auto"
    # 音色档：留空则按国家自动选（见 COUNTRY_VOICE_PROFILE）；非空则强制用该档覆盖
    voice_profile: str = ""
    # 情绪风格：normal | happy | excited | review | live（live=直播口语，默认）
    voice_style: str = "live"
    # 每句随机语速区间（1.0=原速；主播口语略快更自然）
    voice_speed_min: float = 1.0
    voice_speed_max: float = 1.12
    # 每句随机音高（百分比整数，实际取 [-x, x]；0=不随机）
    voice_pitch_random: int = 2
    # 每句停顿在 GPT 给的 pause 上加的随机抖动（毫秒，±x；0=不抖动）
    voice_pause_random: int = 60
    # 其它 Provider 密钥（用到时才需要）
    elevenlabs_api_key: str = ""
    elevenlabs_base_url: str = "https://api.elevenlabs.io/v1"
    cartesia_api_key: str = ""
    cartesia_base_url: str = "https://api.cartesia.ai"
    # MiniMax（海螺）T2A v2：Bearer 鉴权，T2A 不需要 GroupId
    minimax_api_key: str = ""
    minimax_base_url: str = "https://api.minimax.io"
    minimax_model: str = "speech-2.6-hd"
    # 语言增强，提升小语种发音（如 Vietnamese）；auto 为自动
    minimax_language_boost: str = "auto"

    # Speech Formatter：TTS 前把英文品牌/型号/缩写按 config/speech/<language>.yaml 换成本地读音。
    # 关闭则 tts_text 原样送 TTS（字幕永远用 caption 英文原文，不受影响）。
    speech_formatter_enabled: bool = True

    # 品牌名（口播/字幕里出现太频繁会像硬广）。brand_max_mentions：一条视频里「品牌(+型号)」
    # 最多出现几次；超出的自动从中间段落抹掉（保留首个 Hook 与结尾 CTA 的品牌）。0=不限制。
    brand_name: str = "SEAWEIR"
    brand_max_mentions: int = 2

    # 发布（第三方聚合工具；未配置时用 stub 只记录不真发）
    publish_provider: str = "auto"  # auto | thirdparty | bitbrowser | stub
    publish_base_url: str = ""
    publish_api_key: str = ""
    publish_platform: str = "tiktok"

    # 比特浏览器（BitBrowser 本地 API + Playwright，模拟真人网页发布）
    bitbrowser_api_url: str = "http://127.0.0.1:54345"
    # 账号 -> 比特环境ID 映射，形如 tt_vn_01:abc123,tt_vn_02:def456
    bitbrowser_account_map: str = ""
    bitbrowser_upload_url: str = "https://www.tiktok.com/tiktokstudio/upload"
    # 视频上传处理最长等待秒数
    bitbrowser_upload_timeout: int = 300
    # 每步操作的拟人随机延迟区间（毫秒）
    bitbrowser_action_delay_min_ms: int = 400
    bitbrowser_action_delay_max_ms: int = 1200
    # 发布完成后是否保留比特窗口不关闭（调试/人工复核时置 true）
    bitbrowser_keep_open: bool = False
    # 产品→窗口路由配置文件（每个窗口=一个比特环境，含 TK+Shopee 双平台）
    publish_routing_file: Path = Path("data/publish_routing.json")
    # 每账号每天发布条数区间与白天错峰时段（小时）
    publish_per_account_min: int = 3
    publish_per_account_max: int = 5
    publish_window_start_hour: int = 9
    publish_window_end_hour: int = 21
    # 账号表就绪前的临时账号列表（逗号分隔，供 /publish/schedule/auto 用）
    publish_accounts: str = ""

    @property
    def publish_account_list(self) -> list[str]:
        return [a.strip() for a in self.publish_accounts.split(",") if a.strip()]

    @property
    def bitbrowser_account_dict(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for pair in self.bitbrowser_account_map.split(","):
            pair = pair.strip()
            if not pair or ":" not in pair:
                continue
            acct, pid = pair.split(":", 1)
            if acct.strip() and pid.strip():
                out[acct.strip()] = pid.strip()
        return out

    def ensure_dirs(self) -> None:
        for path in (self.workspace_dir, self.log_dir, self.data_dir):
            Path(path).mkdir(parents=True, exist_ok=True)
        Path(self.onedrive_auth_record_path).parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
