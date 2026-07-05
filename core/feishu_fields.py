"""飞书字段名映射（候选名，按顺序匹配）。

飞书表列名后续可能微调或多语言，这里集中维护候选名，
adapter 解析时按候选顺序找到真实字段，避免散落硬编码。
"""

from __future__ import annotations

# 素材库表
MATERIAL_FIELDS = {
    "product_model": ["产品型号", "Product Model", "型号"],
    "role": ["素材位置", "Role", "位置", "HOOK/VALUE/PROOF/CTA"],
    "tags": ["标签", "Tags"],
    "onedrive_link": ["ONEDRIVE LINK", "OneDrive Link", "ONEDRIVE_LINK"],
    "enabled": ["是否启用", "Enabled", "启用"],
    "status": ["素材状态", "Status", "状态"],
    "score": ["素材评分", "Score", "评分"],
    # 视频元数据
    "duration": ["时长", "Duration", "时长(秒)"],
    "resolution": ["分辨率", "Resolution"],
    "fps": ["FPS", "帧率"],
    "size": ["文件大小", "Size", "大小"],
    "has_audio": ["是否有音频", "Has Audio", "有音频"],
}

# 成片表
RENDER_FIELDS = {
    "product_model": ["产品型号", "Product Model"],
    "status": ["成片状态", "Status", "状态"],
    "onedrive_link": ["成片链接", "ONEDRIVE LINK", "OneDrive Link"],
    "title": ["标题", "Title"],
    "caption": ["文案", "Caption"],
    "tags": ["标签", "Tags"],
    "duration": ["时长", "Duration"],
}

# 发布表
PUBLISH_FIELDS = {
    "render_id": ["成片ID", "Render ID"],
    "account": ["发布账号", "Account"],
    "platform": ["平台", "Platform"],
    "scheduled_at": ["发布时间", "Scheduled At", "计划发布时间"],
    "status": ["发布状态", "Status", "状态"],
    "post_url": ["发布链接", "Post URL"],
}

# 成片-素材映射表（归因用）
RENDER_MATERIAL_MAP_FIELDS = {
    "render_id": ["成片ID", "Render ID"],
    "material_id": ["素材ID", "Material ID"],
    "role": ["素材位置", "Role"],
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
