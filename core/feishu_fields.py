"""飞书字段名映射（候选名，按顺序匹配）。

飞书表列名后续可能微调或多语言，这里集中维护候选名，
adapter 解析时按候选顺序找到真实字段，避免散落硬编码。
"""

from __future__ import annotations

# 素材库表（真实列名，位于“营销”知识库多维表格）
MATERIAL_FIELDS = {
    "material_id": ["素材ID", "素材编号"],
    "product_model": ["产品", "产品型号", "型号", "Product Model"],
    "onedrive_folder": ["OneDrive文件夹", "OneDrive 文件夹"],
    "material_type": ["素材类型", "类型"],
    "content": ["拍摄内容", "内容"],
    "main_tag": ["主标签"],
    "aux_tags": ["辅助标签", "标签", "Tags"],
    "role": ["可用于位置", "素材位置", "Role", "HOOK/VALUE/PROOF/CTA"],
    "onedrive_link": ["ONEDRIVE链接", "ONEDRIVE LINK", "OneDrive Link", "ONEDRIVE_LINK"],
    # 单条时长（列名写“毫秒”，但按业务约定实际填“秒”）
    "duration": ["单条时长（毫秒）", "单条时长", "时长", "Duration"],
    # 是否已读取时长（完成标记）
    "duration_read": ["读取时长", "已读取时长"],
    "date": ["日期", "Date"],
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
