# AI Video Factory — 技术架构

本文档描述项目分层、核心数据流、外部系统对接，以及文案 / 导演 / 配音所用的**三方模型**与**本地能力**。

## 1. 设计原则

| 原则 | 说明 |
| --- | --- |
| 控制层 / 服务层分离 | **n8n** 只做定时与 HTTP 编排；**FastAPI** 承载全部业务逻辑 |
| 飞书为唯一数据源 (SSOT) | 素材、成片、发布状态等以飞书多维表格为准；**OneDrive 只存文件** |
| Adapter 隔离外部系统 | 飞书 / OneDrive / FFmpeg / AI / 发布器均在 `adapters/`，业务层不直连 SDK |
| 可插拔 Provider | 文案、配音、发布、BGM 均通过配置切换，缺 key 自动降级 |
| 长任务异步 | 摄取 / 剪辑 / 生产等返回 `job_id`，客户端轮询 `GET /jobs/{id}` |

## 2. 总览

```
┌─────────────────────────────────────────────────────────────┐
│  控制层 n8n（定时 / 编排，无业务逻辑）                         │
│  01-ingest → 02-produce → 03-schedule → 04-run              │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTP + X-API-Key
┌──────────────────────────▼──────────────────────────────────┐
│  服务层 FastAPI (app/ + api/routes/)                         │
│  routes 薄 → services 厚 → adapters 外部系统                  │
└───┬──────────┬──────────┬──────────┬──────────┬─────────────┘
    │          │          │          │          │
 飞书多维表  OneDrive   FFmpeg    AI Providers  发布器
 (SSOT)     (文件)    (本地媒体)  (LLM/TTS)   (BitBrowser等)
```

### 2.1 目录职责

| 目录 | 职责 |
| --- | --- |
| `app/` | 入口、配置 (`.env`)、日志、依赖装配 |
| `api/routes/` | HTTP 路由：参数校验与转发 |
| `services/` | 业务逻辑：摄取、选材、导演、剪辑、文案、配音、发布、评分 |
| `adapters/` | 外部系统封装 |
| `core/` | 领域模型、枚举、飞书字段映射 |
| `jobs/` | 异步任务框架 |
| `config/` | 选材/结构/BGM/语音读音等 YAML |
| `prompt_library/` | 多语言字幕/口播 Prompt 库 |
| `n8n/workflows/` | 编排工作流 JSON（版本管理） |
| `workspace/` `data/` `logs/` | 临时产物、运行数据、日志 |

### 2.2 运行形态

- **本地**：`uvicorn app.main:app --port 8000`
- **Docker**：`docker/docker-compose.yml` 启动 `api:8000` + `n8n:5678`
- 技术栈：Python 3 + FastAPI + Pydantic Settings + httpx + loguru；可选 `edge-tts`、`playwright`

## 3. 核心业务流水线

```
素材摄取                成片生产                         发布
─────────              ─────────                        ────
飞书待处理素材          POST /produce（异步 job）         排期 / 执行
  → OneDrive 下载        ├─ 选材 (HOOK/VALUE/PROOF/CTA)   POST /publish/schedule/auto
  → ffprobe 元数据       ├─ [可选] Director 出 Brief/Beats POST /publish/run
  → 回写飞书             ├─ [可选] 配音 TTS + 句级字幕
                        ├─ FFmpeg 拼接 / 混音 / 烧字幕
                        ├─ 上传 OneDrive
                        └─ 文案写回成片表
```

### 3.1 生产路径开关（互不影响）

| 开关 | 默认 | 行为 |
| --- | --- | --- |
| `voiceover` | 关 | 开启后：口播脚本 → TTS → 时长驱动选材 → 混音烧字幕 |
| `storyboard_enabled` | 关 | 用 Storyboard 阶段文案作为口播/字幕来源 |
| `director_enabled` | 关 | Director Engine：先出 Creative Brief + Story Beats，再选材/写字幕/剪辑 |
| `cn_twin_enabled` | 关 | 同画面再产中文孪生版（中文字幕+中文配音）写入中国成片表 |

### 3.2 素材角色模型

成片结构围绕四个角色（见 `core/enums.py`、`config/structure.yaml`）：

- **HOOK**：前 2–3 秒强吸引
- **VALUE**：用户利益
- **PROOF**：证明主张
- **CTA**：促成下单

选材引擎（`services/selection/`）按角色约束 + 表现分 + ε-探索凑目标时长。

### 3.3 n8n 定时链路

| 工作流 | 触发 | API |
| --- | --- | --- |
| `01-ingest.json` | 每 6 小时 | `POST /materials/ingest` |
| `02-produce.json` | 每天 02:00 | `POST /produce` |
| `03-schedule.json` | 每天 06:00 | `POST /publish/schedule/auto` |
| `04-run.json` | 每 15 分钟 | `POST /publish/run` |

## 4. 服务层模块

| 模块 | 路径 | 说明 |
| --- | --- | --- |
| 摄取 | `services/ingest/` | 下载 + 元数据 + 飞书写回 |
| 选材 | `services/selection/` | 角色/评分/探索；`POST /selection/plan` |
| 导演 | `services/director/` | Brief / Beats / BGM / 字幕编译 |
| Storyboard | `services/storyboard/` | 成交结构解析与阶段文案 |
| 剪辑 | `services/edit/` | FFmpeg 拼接、成片映射 |
| 文案 | `services/content/` | 标题/文案/标签写回 |
| 字幕 | `services/caption/` | Prompt Library + LLM 生成阶段字幕 |
| 配音 | `services/voiceover/` | 逐句 TTS、停顿、语速音高 |
| 语音格式化 | `services/speech/` | 品牌/型号本地读音（YAML） |
| 生产编排 | `services/pipeline/` | `/produce` 一条龙 |
| 发布 | `services/publish/` | 排期、路由、执行 |
| KOL | `services/kol/` | 创作者视频下载（TikWM → OneDrive），与 AI 剪辑解耦 |
| 库 / 评分 | `services/library/`、`selection/scoring` | 产品与素材分 |

## 5. Adapter 层

| Adapter | 路径 | 用途 |
| --- | --- | --- |
| 飞书 | `adapters/feishu/` | 多维表格 CRUD、字段映射 |
| OneDrive | `adapters/onedrive/` | Graph API：下载素材 / 上传成片 |
| FFmpeg | `adapters/ffmpeg/` | `ffprobe` 元数据；拼接、混音、字幕、atempo |
| AI 文案 / TTS | `adapters/ai_providers/` | ContentProvider + VoiceProvider |
| 发布器 | `adapters/publishers/` | stub / 第三方聚合 / BitBrowser / Shopee |

## 6. 模型与 AI 能力一览

项目**没有内嵌本地 LLM 权重**（无 Ollama / 本地 Whisper 等）。所谓「本地」指：本机进程内的规则/模板/启发式，以及本机二进制（FFmpeg、BitBrowser）。云端模型均通过 HTTP 调用，OpenAI 接口兼容 DeepSeek / 自建端点（改 `OPENAI_BASE_URL`）。

### 6.1 三方云端模型

#### A. 大语言模型（文案 / 导演 / 字幕）

| 用途 | 默认模型 | 配置项 | 调用位置 |
| --- | --- | --- | --- |
| 通用 LLM（标题、文案包、口播脚本、导演 Brief） | `gpt-4o` | `OPENAI_MODEL` | `OpenAIContentProvider`、`services/director/llm.py`、`director/strategy.py` |
| 字幕/口播专用 | `gpt-4o` | `OPENAI_CAPTION_MODEL`（空则回落 `OPENAI_MODEL`） | `services/caption/generator.py` |
| 协议 | OpenAI Chat Completions | `OPENAI_API_KEY` + `OPENAI_BASE_URL` | httpx 直连，不绑 SDK |

切换策略（`AI_PROVIDER`）：

- `auto`：有 key → OpenAI 兼容；无 key → **模板兜底**
- `openai`：强制云端（无 key 则降级模板）
- `null`：只用模板

#### B. 文本转语音（TTS）

| Provider | 默认模型 / 引擎 | 配置 | 说明 |
| --- | --- | --- | --- |
| **OpenAI** | `gpt-4o-mini-tts` | `OPENAI_TTS_MODEL`、`TTS_VOICE` | `/audio/speech`；`VOICE_PROVIDER=auto` 时优先 |
| **Edge TTS** | 微软神经音色（如 `vi-VN-HoaiMyNeural`） | 无需 key；需 `edge-tts` | 免费；越南/中文原生音色；变速靠 FFmpeg |
| **ElevenLabs** | `eleven_multilingual_v2` | `ELEVENLABS_API_KEY` | 多语种；音色档填 `voice_id` |
| **Cartesia** | `sonic-2` | `CARTESIA_API_KEY` | 音色档填 voice id |
| **MiniMax（海螺）** | `speech-2.6-hd` | `MINIMAX_API_KEY`、`MINIMAX_MODEL` | T2A；支持克隆音色与 `language_boost` |

`VOICE_PROVIDER`：`auto | openai | edge | elevenlabs | cartesia | minimax`。

音色档（`VOICE_PROFILES`）按国家映射，例如：

| 档名 | 国家 | OpenAI | Edge | MiniMax 等 |
| --- | --- | --- | --- | --- |
| `vn_female_02` | VN（默认） | nova | vi-VN-HoaiMyNeural | 克隆音色 id |
| `vn_female_01` | VN | shimmer | 同上 | 克隆音色 id |
| `cn_female_01` | CN | nova | zh-CN-XiaoxiaoNeural | `female-tianmei` |

情绪：`normal | happy | excited | review | live`（默认 `live`）。

#### C. BGM 在线曲库（非生成式，检索下载）

| Provider | 配置 | 说明 |
| --- | --- | --- |
| Jamendo（推荐） | `JAMENDO_CLIENT_ID` | 免费；仅 CC-BY 可商用曲 |
| Magnific | `MAGNIFIC_API_KEY` | 付费备用 |

开关：`BGM_ONLINE_ENABLED`；失败静默回落本地曲库 / 无 BGM。

#### D. 其它三方服务（非模型）

| 服务 | 用途 |
| --- | --- |
| 飞书开放平台 | 多维表格 SSOT |
| Microsoft Graph / OneDrive | 素材与成片文件 |
| TikWM | KOL 原视频下载 |
| 第三方发布聚合 API | `PUBLISH_BASE_URL` + `PUBLISH_API_KEY` |
| BitBrowser 本地 API | 模拟真人网页发 TikTok（`PUBLISH_PROVIDER=bitbrowser`） |

### 6.2 本地能力（非云端权重模型）

| 能力 | 实现 | 说明 |
| --- | --- | --- |
| **模板文案** | `TemplateContentProvider` | 无 LLM key 时用产品+标签拼标题/文案/口播占位 |
| **启发式导演** | `director_strategy=heuristic` 或 LLM 失败回落 | 纯 config + 素材库存规则出 Brief/Beats，不调 API |
| **Prompt 库回落** | `prompt_library/**/*.yaml` | 字幕 LLM 失败时用 `good_examples` 等模板 |
| **Speech Formatter** | `config/speech/<lang>.yaml` | 品牌/型号英文 → 本地读音（送 TTS 前） |
| **FFmpeg / ffprobe** | 本机 PATH | 元数据、拼接、混音、字幕烧录、atempo/音高 |
| **选材 / 评分算法** | `services/selection/` | GMV/CVR/CTR 等加权 + ε-greedy，纯本地计算 |
| **本地 BGM 库** | `assets/bgm/` + 下载缓存 | 在线选曲失败时复用 |
| **BitBrowser + Playwright** | 本机浏览器环境 | 发布自动化（非 AI 模型） |

> Edge TTS 虽跑在本机 Python 包内，音色仍由**微软云端**合成，归入三方 TTS，而非离线本地模型。

### 6.3 模型调用关系简图

```
                    ┌──────────────────────┐
  Content / Caption │  gpt-4o (Chat)       │ ← OPENAI_* / 兼容端点
  Director Brief    │  (+ caption 同模型)  │
                    └──────────▲───────────┘
                               │ 失败
                    ┌──────────┴───────────┐
                    │ 本地模板 / 启发式     │
                    └──────────────────────┘

  口播 TTS          ┌─ OpenAI gpt-4o-mini-tts
                    ├─ Edge（微软神经音色）
                    ├─ ElevenLabs multilingual_v2
                    ├─ Cartesia sonic-2
                    └─ MiniMax speech-2.6-hd
                         │
                         ▼
                    FFmpeg 变速/音高/拼接/烧字幕（本地）
```

## 7. 配置入口速查

主要环境变量见根目录 `.env.example`，代码侧集中于 `app/config.py`：

| 类别 | 关键变量 |
| --- | --- |
| 服务鉴权 | `API_KEY` |
| 飞书 / OneDrive | `FEISHU_VN_*`、`ONEDRIVE_*` |
| LLM | `AI_PROVIDER`、`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`、`OPENAI_CAPTION_MODEL` |
| TTS | `VOICE_PROVIDER`、`OPENAI_TTS_MODEL`、`MINIMAX_*`、`ELEVENLABS_*`、`CARTESIA_*` |
| 导演 / Storyboard | `DIRECTOR_ENABLED`、`DIRECTOR_STRATEGY`、`STORYBOARD_ENABLED` |
| BGM | `BGM_ONLINE_ENABLED`、`BGM_ONLINE_PROVIDER`、`JAMENDO_CLIENT_ID` |
| 发布 | `PUBLISH_PROVIDER`、`BITBROWSER_*`、`PUBLISH_ACCOUNTS` |
| 中文孪生 | `CN_TWIN_ENABLED`、`CN_VOICE_PROFILE` |

## 8. HTTP API 面（服务层）

| 前缀 / 路由 | 作用 |
| --- | --- |
| `GET /health` | 健康检查 |
| `/materials/*` | 素材摄取 |
| `/selection/*` | 选材计划 |
| `/edit/*` | 剪辑渲染（异步） |
| `/content/*` | 文案生成与回写 |
| `/produce` | 生产编排（推荐给 n8n） |
| `/publish/*` | 排期与发布 |
| `/jobs/{id}` | 异步任务状态 |
| `/analytics/*`、`/scoring/*` | 数据与评分 |
| `/kol/*` | KOL 原视频链路 |

交互式文档：服务启动后访问 `/docs`。

## 9. 数据落盘约定

| 路径 | 内容 |
| --- | --- |
| `data/renders/*.json` | 成片–素材映射（归因用） |
| `data/publish/*.json` | 日发布计划 |
| `data/onedrive/` | Graph 登录态 |
| `data/kol/` | KOL 下载与归档 |
| `workspace/` | 下载与渲染临时文件 |
| `logs/` | 应用日志 |

## 10. 演进阶段（摘要）

| 阶段 | 状态 | 内容 |
| --- | --- | --- |
| 0 | 完成 | 骨架、配置、飞书/OneDrive、Job、Docker |
| 1 | 完成 | 素材摄取 + 元数据回写 |
| 2 | 进行中 | 选材 + FFmpeg 剪辑 MVP + 成片映射 |
| 3 | 完成 | AI 文案；可选配音驱动剪辑 |
| 4 | 进行中 | 排期 + 发布 + n8n 全链路 |
| 5–7 | 规划 | 数据回收、归因评分闭环、规模化扩展 |

---

文档与代码对齐基准：`app/config.py`、`adapters/ai_providers/`、`services/director/`、`services/caption/`、`README.md`。
