"""飞书字段名映射（候选名，按顺序匹配）。

飞书表列名后续可能微调或多语言，这里集中维护候选名，
adapter 解析时按候选顺序找到真实字段，避免散落硬编码。
"""

from __future__ import annotations

# 素材库表（真实列名，位于“营销”知识库多维表格）
MATERIAL_FIELDS = {
    "material_id": ["素材ID", "素材编号"],
    "product_model": ["产品", "商品名", "商品", "产品型号", "型号", "Product Model"],
    "onedrive_folder": ["OneDrive文件夹", "OneDrive 文件夹"],
    "material_type": ["素材类型", "类型"],
    "content": ["拍摄内容", "内容"],
    "main_tag": ["主标签"],
    "aux_tags": ["辅助标签", "标签", "Tags"],
    "role": ["可用于位置", "素材位置", "Role", "HOOK/VALUE/PROOF/CTA"],
    "onedrive_link": [
        "链接",
        "ONEDRIVE链接",
        "ONEDRIVE LINK",
        "OneDrive Link",
        "ONEDRIVE_LINK",
    ],
    # 单条时长（列名写“毫秒”，但按业务约定实际填“秒”）
    "duration": ["单条时长（毫秒）", "单条时长", "时长", "Duration"],
    # 是否已读取时长（完成标记）
    "duration_read": ["读取时长", "已读取时长"],
    # 配音模式下：勾选则该素材片段“配音+原声”同时保留，否则只用配音
    "keep_original": ["保留原声"],
    "date": ["日期", "Date"],
}

# 产品中心表（产品定位/目标人群/禁用词等背景信息，喂给 GPT 做文案接地）
PRODUCT_FIELDS = {
    "product_model": ["产品型号", "产品", "型号", "Product Model"],
    "positioning": ["产品定位", "定位", "Positioning"],
    "target_audience": ["目标人群", "人群", "Target Audience"],
    "forbidden_words": ["禁用词", "违禁词", "敏感词", "Forbidden Words"],
    # 产品中心当前形态：一行一个文案/卖点，多语言列承载本地化内容。
    # VN 是当前生产语言；CN/EN 作为备用，兼容后续结构化“核心卖点”列。
    "selling_points": ["核心卖点", "卖点", "Selling Points", "VN", "CN", "EN"],
}

# 成片表（AI 生成成片写回；各国家一张表，列名通用。value_type 用于“缺列自动建列”）
RENDER_FIELDS = {
    "render_id": ["成片ID", "Render ID"],
    "product_model": ["产品型号", "Product Model", "产品型号(AI)"],
    "onedrive_link": ["成片链接", "ONEDRIVE LINK", "OneDrive Link"],
    "duration": ["时长（秒）", "时长", "Duration"],
    "status": ["成片状态", "Status", "状态"],
    "materials": ["使用素材", "Materials", "素材ID列表"],
    "title": ["标题", "Title"],
    "caption": ["文案", "Caption"],
    "tags": ["标签", "Tags"],
    "voiceover": ["是否配音", "Voiceover"],
    "script": ["口播脚本", "Script"],
    "subtitle_language": ["字幕语言", "Subtitle Language"],
}

# 成片表各列的 ui_type（新建列时用）
RENDER_FIELD_TYPES = {
    "render_id": "text",
    "product_model": "text",
    "onedrive_link": "url",
    "duration": "number",
    "status": "text",
    "materials": "text",
    "title": "text",
    "caption": "text",
    "tags": "text",
    "voiceover": "checkbox",
    "script": "text",
    "subtitle_language": "text",
}

# 发布表
PUBLISH_FIELDS = {
    "render_id": ["成片ID", "Render ID", "ID"],
    "account": ["发布账号", "Account"],
    "platform": ["平台", "Platform"],
    "scheduled_at": ["发布时间", "Scheduled At", "计划发布时间"],
    "status": ["发布状态", "Status", "状态"],
    "post_url": ["发布链接", "Post URL"],
    "title": ["标题", "Title"],
    "caption": ["文案", "Caption"],
    "video_url": ["成片链接", "视频链接", "Video URL"],
    "error": ["错误信息", "Error"],
}

# 发布表各列 ui_type（缺列自动创建，方便扩展到其他国家表）
PUBLISH_FIELD_TYPES = {
    "render_id": "text",
    "account": "text",
    "platform": "text",
    "scheduled_at": "text",
    "status": "text",
    "post_url": "url",
    "title": "text",
    "caption": "text",
    "video_url": "url",
    "error": "text",
}

# 成片表=发布表（合表）时：发布结果按平台回写到成片所在行的这些列
# 每个平台一组「状态 + 链接」列，加新平台在这里补一行即可（扩展点）。
PUBLISH_RESULT_FIELDS = {
    "product": ["挂车商品型号", "挂车商品", "Product"],
    "published_at": ["发布时间", "Published At"],
    "tiktok_status": ["TikTok发布状态", "TikTok Status"],
    "tiktok_url": ["TikTok发布链接", "TikTok URL"],
    "tiktok_video_id": ["TK VIDEO ID", "TikTok Video ID", "TK Video ID", "TikTok视频ID"],
    "shopee_status": ["Shopee发布状态", "Shopee Status"],
    "shopee_url": ["Shopee发布链接", "Shopee URL"],
}
PUBLISH_RESULT_FIELD_TYPES = {
    "product": "text",
    "published_at": "text",
    "tiktok_status": "text",
    "tiktok_url": "url",
    "tiktok_video_id": "text",
    "shopee_status": "text",
    "shopee_url": "url",
}

# 账号表（发布账号池；就绪后替代 .env 的 PUBLISH_ACCOUNTS）
ACCOUNT_FIELDS = {
    "account": ["发布账号", "账号", "Account"],
    "platform": ["平台", "Platform"],
    "enabled": ["启用", "是否启用", "Enabled"],
    "per_day_min": ["每日最少", "每天最少", "Per Day Min"],
    "per_day_max": ["每日最多", "每天最多", "Per Day Max"],
}

# 成片-素材映射表（归因用）
RENDER_MATERIAL_MAP_FIELDS = {
    "render_id": ["成片ID", "Render ID"],
    "material_id": ["素材ID", "Material ID"],
    "role": ["素材位置", "Role"],
}

# 素材库表里的「评分」列：按国家分列 + 综合评分（每天回写）。
# 新增国家只需在 by_country 里加一行 <国家码>: ["<国家码>评分"]，其余逻辑自动扩展。
MATERIAL_SCORE_FIELDS = {
    "material_id": ["素材ID", "素材编号"],
    "composite": ["综合评分", "综合分", "Composite Score"],
    "by_country": {
        "CN": ["CN评分"],
        "VN": ["VN评分"],
        "TH": ["TH评分"],
        "MY": ["MY评分"],
        "ID": ["ID评分"],
    },
}

# 成片=发布合表里的「表现指标」列（每天更新）：用于素材/卖点评分。
# 指标列在飞书常为公式/查找列，取值会被包成单元素数组（如 [457]），读取时统一 unwrap。
SCORE_METRIC_FIELDS = {
    "render_id": ["成片ID", "Render ID", "ID"],
    "product_model": ["产品型号", "Product Model", "产品", "型号"],
    "materials": ["使用素材", "Materials", "素材ID列表"],
    "views": ["播放量", "Views", "播放"],
    "gmv": ["GMV", "成交额", "成交"],
    "orders": ["订单数", "Orders", "订单"],
    "product_clicks": ["商品点击次数", "商品点击数", "Product Clicks", "点击数", "商品点击"],
}

# 数据回收表
ANALYTICS_FIELDS = {
    "render_id": ["成片ID", "Render ID"],
    "account": ["发布账号", "Account"],
    "views": ["播放量", "Views"],
    "completion_rate": ["完播率", "Completion Rate"],
    "likes": ["点赞", "Likes"],
    "comments": ["评论", "Comments"],
    "shares": ["分享", "Shares"],
    "gmv": ["成交", "GMV", "成交额"],
}
