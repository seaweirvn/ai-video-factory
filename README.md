# AI Video Factory

AI 自动视频工厂。采用「控制层 + 服务层」架构：

- **控制层（n8n）**：流程调度、定时任务、飞书交互、OneDrive 交互、发布流程编排。
- **服务层（Python FastAPI）**：素材分析、视频元数据、自动剪辑、AI 文案/字幕、素材评分、视频评分、平台发布封装等所有业务逻辑，全部以 HTTP API 暴露。

飞书是唯一数据源（SSOT），OneDrive 仅存文件。业务逻辑一律封装成 API，n8n 不写业务逻辑。

## 架构分层

```
n8n (编排) --HTTP--> FastAPI (业务) --> adapters(feishu/onedrive/ffmpeg/ai)
                                    --> services(selection/edit/content/attribution/scoring/publishers/analytics)
```

长耗时任务（剪辑/转码）走异步：`POST` 返回 `job_id`，再 `GET /jobs/{id}` 轮询。

## 本地运行（阶段 0）

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- 健康检查：`GET http://localhost:8000/health`
- 接口文档：`http://localhost:8000/docs`

## Docker 运行

```bash
cd docker
docker compose up -d
```

启动 n8n（5678）与 api（8000）两个服务。

## 目录结构

- `app/`：服务入口、配置、日志、依赖
- `api/routes/`：HTTP 路由（薄，仅参数校验与转发）
- `core/`：领域模型、schema、状态机、飞书字段映射
- `services/`：业务逻辑（厚）
- `adapters/`：外部系统封装（feishu / onedrive / ffmpeg / ai_providers）
- `jobs/`：异步任务框架
- `n8n/workflows/`：workflow JSON（版本管理）
- `workspace/`：临时下载与渲染产物
- `logs/` `data/`：日志与运行数据

## 开发阶段

- 阶段 0（已完成）：项目骨架、FastAPI、配置/日志、飞书与 OneDrive adapter、异步 job、Docker。
- 阶段 1（已完成）：素材摄取 + 元数据回写。
- 阶段 2（进行中）：智能选材 + 剪辑 MVP + 成片-素材映射。
  - 选材（flexible）：`POST /selection/plan`，HOOK+CTA 必选、VALUE/PROOF 按目标时长凑。
  - 剪辑：`POST /edit/render`（异步 job），下载片段 → ffmpeg 归一化拼接 → 本地成片 →(可选)上传 OneDrive。
  - 成片-素材映射落地 `data/renders/<name>.json`（供阶段 6 归因）。
  - 成片写回飞书成片表（各国家一张表，缺列自动创建；越南表标题“越南”）。
- 阶段 3（已完成）：AI 文案。
  - `POST /content/render`：聚合成片所用素材的标签，生成标题/文案/标签并写回成片表 `标题/文案/标签` 列。
  - Provider 可切换：`OPENAI_API_KEY` 配置后走 OpenAI 兼容接口（`vi` 越南语），未配置时模板兜底跑通链路。
- 配音驱动剪辑（可选，`voiceover.enabled`；需 `OPENAI_API_KEY`）：
  - 生成口播脚本 -> OpenAI TTS 逐句合成配音 + 句级字幕(SRT) -> 用配音时长驱动选材 -> 音轨按素材「保留原声」逐段决定（勾选=配音+原声混音，未勾=只配音）-> 烧录字幕 -> 裁到配音时长。
  - 默认关闭，不影响原声拼接流程。`POST /edit/render` 传 `voiceover.enabled=true` 启用。
- 阶段 4（进行中）：排期 + 发布 + n8n 全链路编排。
  - 排期引擎：成片按账号分配，每账号每天 3~5 条，白天(默认 9-21)错峰随机时间。
  - `POST /publish/schedule` 生成发布计划(落 `data/publish/<date>.json`)，`POST /publish/run` 到点执行，`GET /publish/items` 看状态。
  - 发布器可切换：配置第三方聚合工具(`PUBLISH_BASE_URL/API_KEY`)后走真实发布，未配置用 stub 只记录。
  - **编排端点**（让 n8n 只做定时+单次 HTTP）：
    - `POST /produce`：一次把 选材→(可选)配音→剪辑→上传→文案回写 打包成异步 job。传 `product_model` 产单品，传 `products` 或都留空则批量（自动发现所有可组片产品）。
    - `GET /produce/products`：列出可组片（有 HOOK 且有 CTA）的产品型号，供 n8n 数据驱动循环。
    - `POST /publish/schedule/auto`：读成片表里 `成片状态=rendered` 的成片，按 `PUBLISH_ACCOUNTS` 排期并置为 `scheduled`，n8n 无需传成片列表。
  - 待办：接第三方工具真实发布 + 发布表/账号表(待用户提供 ID)、把发布结果回写飞书。

### n8n 定时工作流（`n8n/workflows/`）

四条链路解耦、可单独重跑，全部通过 HTTP 调 Python API：

| 文件 | 触发 | 动作 |
| --- | --- | --- |
| `01-ingest.json` | 每 6 小时 | `POST /materials/ingest` → 轮询 `/jobs/{id}` 直到完成 |
| `02-produce.json` | 每天 02:00 | `POST /produce`（批量、自动发现产品、每产品 3 条）→ 轮询 job |
| `03-schedule.json` | 每天 06:00 | `POST /publish/schedule/auto`（读成片表 + 配置账号排期）|
| `04-run.json` | 每 15 分钟 | `POST /publish/run`（执行到点发布）|

导入方式：n8n → Import from File，逐个导入上面 JSON。需在 n8n 里设置两个环境变量：

- `AIVF_BASE_URL`：API 地址，如 `http://api:8000`（Docker 内）或 `http://localhost:8000`。
- `AIVF_API_KEY`：与 `.env` 的 `API_KEY` 一致（每次请求带 `X-API-Key` 头）。

发布账号在 `.env` 的 `PUBLISH_ACCOUNTS` 里配（逗号分隔），账号表就绪后改为从飞书读。生产的条数在 `02-produce.json` 的「启动生产」节点 body 里调（`count`）。
- 阶段 5：数据回收。
- 阶段 6：归因 + 评分闭环。
- 阶段 7：自愈/规模化 + 多平台/AI 扩展。
