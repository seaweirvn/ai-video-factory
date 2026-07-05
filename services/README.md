# services/

业务逻辑层（厚）。API 路由只做参数校验与转发，真正的逻辑在这里，按阶段落地：

- `ingest/`：素材下载 + ffprobe 元数据 + 回写飞书（阶段 1）
- `selection/`：选材引擎（角色/评分/探索比例）（阶段 2）
- `edit/`：自动剪辑（FFmpeg 拼接）+ 成片-素材映射（阶段 2）
- `content/`：AI 文案/字幕/封面（阶段 3）
- `scheduling/`：发布排期（阶段 4）
- `publishers/`：多平台发布封装（tiktok/shopee/...）（阶段 4）
- `analytics/`：数据回收（阶段 5）
- `attribution/`：按角色权重归因（阶段 6）
- `scoring/`：素材评分批量更新（阶段 6）

每个子模块对外暴露纯函数/类，由对应 `api/routes/*` 调用；长耗时任务通过 `jobs` 框架异步执行。
