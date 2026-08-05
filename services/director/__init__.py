"""Director Engine：视频生成系统的「大脑」层。

在所有模块之前运行：产品任务 -> Content Brief（销售方向，不含最终字幕）
-> Selection 选镜头 -> Caption 结合已选镜头写最终字幕 -> compile 装配 Timeline
-> Voiceover -> Edit -> Render。

纯新增模块，behind `director_enabled` 开关；关闭时完全不影响原链路与 n8n。
"""

from __future__ import annotations

from services.director.bgm import select_bgm, select_bgm_detailed
from services.director.captioning import generate_final_captions
from services.director.compile import compile_brief_to_storyboard
from services.director.engine import DirectorEngine, get_director_engine
from services.director.models import Beat, Brief, MaterialInventory

__all__ = [
    "Brief",
    "Beat",
    "MaterialInventory",
    "DirectorEngine",
    "get_director_engine",
    "generate_final_captions",
    "compile_brief_to_storyboard",
    "select_bgm",
    "select_bgm_detailed",
]
