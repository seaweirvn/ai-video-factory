"""每日素材维护 + 评分同步（后台任务用，默认 08:45 触发）。

先做数据准备，再评分：
0a. 下载达人(KOL)视频：抓取待下载达人视频 → 上传当前对象存储（R2）→ 回写飞书。
0b. 素材维护：为缺失素材ID的记录按「商品名+递增数字」补齐，再读取视频时长。

评分流程（按国家 → 综合）：
- 对每个「已配置」的国家，读它的「成片=发布合表」每行表现指标（GMV/订单数/播放量/商品点击次数，每天更新）。
- 成熟度门槛：播放量 ≥ SCORE_MIN_VIEWS 的成片才纳入，过滤新视频/低播放噪声。
- 派生指标：CTR=点击/播放、CVR=订单/点击，外加 播放、GMV；**按产品分位归一**到 0~1。
- 归因：从「使用素材」列解析 material_ids；卖点/结构/情绪从本地 data/renders/<id>.brief.json 补全。
- 该国 reward 按贝叶斯收缩聚合到每个素材，写回素材库表的「<国家码>评分」列。
- 综合评分：把各国证据「合池」再收缩一次（Σ回报 + k·opt）/(Σ样本 + k)，写回「综合评分」列。

扩展新国家：在 COUNTRY_CODES 里加国家码，并在 .env 配好 FEISHU_<CC>_RENDER_APP_TOKEN /
FEISHU_<CC>_RENDER_TABLE_ID 即可（当前仅 VN 有数据；其它国家未配置会自动跳过）。

评分同时驱动选材（services/selection/performance.py 惰性加载 data/performance.jsonl）：
reward = w_gmv·GMV + w_cvr·CVR + w_ctr·CTR + w_views·播放（同产品分位归一后），GMV/CVR 权重最高。
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loguru import logger  # noqa: E402

from adapters.feishu import make_feishu_client  # noqa: E402
from app.config import get_settings  # noqa: E402
from core.feishu_fields import MATERIAL_SCORE_FIELDS, SCORE_METRIC_FIELDS  # noqa: E402
from services.selection.performance import (  # noqa: E402
    PERF_PATH,
    PerformanceStore,
    get_performance_store,
)

# 参与评分的国家码（顺序即列展示顺序）。新增国家在此加一项，并配好对应飞书表。
COUNTRY_CODES = ["CN", "VN", "TH", "MY", "ID"]

# 评分超参（main() 从 settings 覆盖）：贝叶斯收缩强度 / 乐观初值 / reward 权重
SHRINK_K: float = 5.0
OPT_INIT: float = 0.6
s_weights: dict[str, float] = {"gmv": 0.45, "cvr": 0.35, "ctr": 0.15, "views": 0.05, "completion": 0.0}


def configure_file_logging(data_dir: Path) -> Path:
    """Attach the independent rotating log used by manual and scheduled runs."""
    log_dir = Path(data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_dir / "daily_score_{time:YYYY-MM-DD}.log",
        rotation="10 MB",
        retention="30 days",
        encoding="utf-8",
        enqueue=True,
    )
    return log_dir


def _get(fields: dict, candidates: list[str]):
    for c in candidates:
        if c in fields:
            return fields[c]
    return None


def _num(v) -> float:
    """飞书数字/公式列常被包成单元素数组或富文本，统一取标量数值。"""
    if isinstance(v, list):
        v = v[0] if v else 0
    if isinstance(v, dict):
        v = v.get("value") or v.get("text") or 0
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _text(v) -> str:
    if isinstance(v, list):
        parts = []
        for seg in v:
            if isinstance(seg, dict):
                parts.append(str(seg.get("text") or seg.get("name") or ""))
            else:
                parts.append(str(seg))
        return "".join(parts)
    if isinstance(v, dict):
        return str(v.get("text") or v.get("value") or "")
    return str(v or "")


def _parse_materials(s: str) -> list[str]:
    """'Z1185(HOOK), Z1147(VALUE), Z1107 (PROOF)' -> ['Z1185','Z1147','Z1107']（容忍多余空格）。"""
    out: list[str] = []
    for part in (s or "").split(","):
        mid = part.split("(")[0].strip()
        if mid:
            out.append(mid)
    return out


def _pct_norm(values: list[float]) -> list[float]:
    """同产品内分位归一到 0~1：最小→0，最大→1；全相等→全 0（无区分度，如 GMV 全为 0）。"""
    n = len(values)
    if n <= 1:
        return [0.0] * n
    uniq = sorted(set(values))
    if len(uniq) == 1:
        return [0.0] * n
    rank = {v: i for i, v in enumerate(uniq)}
    denom = len(uniq) - 1
    return [rank[v] / denom for v in values]


def _load_brief(name: str, data_dir: Path) -> dict:
    p = data_dir / "renders" / f"{name}.brief.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _country_conf(s, code: str) -> tuple[str, str, Path]:
    """取某国家的 (app_token, table_id, perf_path)；未配置返回空。VN 用主 performance.jsonl（选材直接用）。"""
    cc = code.lower()
    token = getattr(s, f"feishu_{cc}_render_app_token", "") or ""
    table = getattr(s, f"feishu_{cc}_render_table_id", "") or ""
    if cc == "vn":
        perf = PERF_PATH
    else:
        perf = PERF_PATH.with_name(f"performance.{cc}.jsonl")
    return token, table, perf


def score_country(code: str, token: str, table_id: str, perf_path: Path,
                  min_views: int, data_dir: Path) -> dict[str, list[float]]:
    """读某国合表 -> 归一 -> 写该国 perf 文件 -> 返回 {material_id: [sum_reward, n]}。"""
    client = make_feishu_client(token)
    rows = client.list_records(table_id, page_size=200)
    logger.info("[{}] 读取合表 {} 行，成熟度门槛 播放量≥{}", code, len(rows), min_views)

    parsed: list[dict] = []
    for r in rows:
        f = r.get("fields", {})
        rid = _text(_get(f, SCORE_METRIC_FIELDS["render_id"])).strip()
        product = _text(_get(f, SCORE_METRIC_FIELDS["product_model"])).strip()
        if not rid:
            continue
        views = _num(_get(f, SCORE_METRIC_FIELDS["views"]))
        if views < min_views:
            continue
        gmv = _num(_get(f, SCORE_METRIC_FIELDS["gmv"]))
        orders = _num(_get(f, SCORE_METRIC_FIELDS["orders"]))
        clicks = _num(_get(f, SCORE_METRIC_FIELDS["product_clicks"]))
        material_ids = _parse_materials(_text(_get(f, SCORE_METRIC_FIELDS["materials"])))
        if not material_ids:
            continue
        brief = _load_brief(rid, data_dir)
        parsed.append({
            "render": rid, "product": product, "material_ids": material_ids,
            "raw_views": views, "raw_gmv": gmv,
            "raw_ctr": clicks / views if views > 0 else 0.0,
            "raw_cvr": orders / clicks if clicks > 0 else 0.0,
            "selling_point": brief.get("core_selling_point", ""),
            "playbook": brief.get("playbook", ""),
            "emotion": brief.get("emotion", ""),
            "angle": brief.get("angle", ""),
            "beat_order": [b.get("name", "") for b in brief.get("beats", [])],
        })

    if not parsed:
        logger.warning("[{}] 无满足成熟度门槛的成片，跳过", code)
        # 仍写空文件，保持幂等（避免旧数据残留误导）
        perf_path.parent.mkdir(parents=True, exist_ok=True)
        perf_path.write_text("", encoding="utf-8")
        return {}

    # 按产品分位归一（views/ctr/cvr/gmv 各自在同产品内归一）
    by_product: dict[str, list[dict]] = defaultdict(list)
    for p in parsed:
        by_product[p["product"]].append(p)
    for group in by_product.values():
        for key, raw in (("views", "raw_views"), ("ctr", "raw_ctr"),
                         ("cvr", "raw_cvr"), ("gmv", "raw_gmv")):
            for g, nv in zip(group, _pct_norm([g[raw] for g in group])):
                g[key] = round(nv, 4)

    # 整表快照重写该国 perf 文件（幂等）
    perf_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({
        "render": p["render"], "product": p["product"],
        "selling_point": p["selling_point"], "material_ids": p["material_ids"],
        "completion": 0.0, "engagement": 0.0,
        "views": p["views"], "gmv": p["gmv"], "ctr": p["ctr"], "cvr": p["cvr"],
        "playbook": p["playbook"], "beat_order": p["beat_order"],
        "angle": p["angle"], "emotion": p["emotion"], "bgm_track": "",
    }, ensure_ascii=False) for p in parsed]
    perf_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 用独立 store 聚合出该国每素材的 [sum_reward, n]
    store = PerformanceStore(
        path=perf_path,
        weights={"gmv": s_weights["gmv"], "cvr": s_weights["cvr"], "ctr": s_weights["ctr"],
                 "views": s_weights["views"], "completion": s_weights["completion"]},
        shrink_k=SHRINK_K, opt_init=OPT_INIT,
    )
    store.load()
    logger.info("[{}] 纳入成片 {} 条，覆盖素材 {} 个", code, len(parsed), len(store._mat))
    return {mid: list(agg) for mid, agg in store._mat.items()}


def _shrink(sum_reward: float, n: float) -> float:
    denom = n + SHRINK_K
    return (sum_reward + SHRINK_K * OPT_INIT) / denom if denom else OPT_INIT


def writeback(material_by_country: dict[str, dict[str, list[float]]]) -> None:
    """把各国评分 + 综合评分写回素材库表（按 素材ID 匹配行）。"""
    s = get_settings()
    token = s.feishu_vn_material_app_token or s.feishu_vn_bitable_app_token
    tid = s.feishu_vn_material_table_id
    if not (token and tid):
        logger.warning("未配置素材库表（material_app_token/table_id），跳过写回")
        return
    client = make_feishu_client(token)

    # 解析真实列名
    mid_field = client.resolve_field(tid, MATERIAL_SCORE_FIELDS["material_id"])
    comp_field = client.resolve_field(tid, MATERIAL_SCORE_FIELDS["composite"])
    country_field: dict[str, str] = {}
    for cc, cands in MATERIAL_SCORE_FIELDS["by_country"].items():
        name = client.resolve_field(tid, cands)
        if name:
            country_field[cc] = name
    if not mid_field:
        logger.warning("素材库表缺少「素材ID」列，无法写回")
        return

    # 汇总所有素材：各国分 + 合池综合分
    all_mids: set[str] = set()
    for cc, mp in material_by_country.items():
        all_mids.update(mp.keys())
    scores: dict[str, dict] = {}
    for mid in all_mids:
        row: dict = {}
        pooled_sum = pooled_n = 0.0
        for cc in COUNTRY_CODES:
            agg = material_by_country.get(cc, {}).get(mid)
            if not agg:
                continue
            ssum, n = float(agg[0]), float(agg[1])
            pooled_sum += ssum
            pooled_n += n
            if cc in country_field:
                row[country_field[cc]] = f"{_shrink(ssum, n):.4f}"
        if comp_field and pooled_n > 0:
            row[comp_field] = f"{_shrink(pooled_sum, pooled_n):.4f}"
        if row:
            scores[mid] = row

    if not scores:
        logger.info("无可写回的素材评分")
        return

    # 建 素材ID -> record_id 映射（一次拉全表）
    rows = client.list_records(tid, page_size=200)
    id_to_record: dict[str, str] = {}
    for r in rows:
        mid = _text(_get(r.get("fields", {}), [mid_field])).strip()
        if mid:
            id_to_record[mid] = r.get("record_id", "")

    written = missing = 0
    for mid, fields in scores.items():
        rec_id = id_to_record.get(mid)
        if not rec_id:
            missing += 1
            continue
        try:
            client.update_record(tid, rec_id, fields)
            written += 1
        except Exception as exc:  # noqa: BLE001 - 单条失败不阻塞其它
            logger.warning("写回评分失败 - 素材{} err={}", mid, exc)
    logger.info("素材评分写回完成 - 更新 {} 行（未匹配 {}），列={}",
                written, missing, ["综合评分"] + [country_field[c] for c in country_field])


def main() -> None:
    global s_weights, SHRINK_K, OPT_INIT  # noqa: PLW0603 - 供内部辅助函数复用超参
    s = get_settings()
    data_dir = Path(s.data_dir)
    configure_file_logging(data_dir)
    logger.info("早晨维护任务开始")
    min_views = int(s.score_min_views)
    SHRINK_K = float(s.scoring_shrink_k)
    OPT_INIT = float(s.scoring_optimistic_init)
    s_weights = {
        "gmv": s.perf_weight_gmv, "cvr": s.perf_weight_cvr, "ctr": s.perf_weight_ctr,
        "views": s.perf_weight_views, "completion": s.perf_weight_completion,
    }

    # 0) 清理飞书三张表中勾选 Delete 的自有 R2 视频。
    # 只接受 R2_PUBLIC_DOMAIN 下的链接；单表/单条失败不阻塞后续维护和评分。
    try:
        from services.storage_cleanup import cleanup_deleted_videos
        cleanup_res = cleanup_deleted_videos()
        logger.info("飞书 Delete -> R2 清理完成 - {}", cleanup_res)
    except Exception:  # noqa: BLE001
        logger.exception("飞书 Delete -> R2 清理失败（不阻塞后续）")

    # 0a) 下载达人(KOL)视频：抓取待下载达人视频 -> 上传当前对象存储 -> 回写飞书（失败不阻塞后续）
    try:
        from services.kol import get_kol_download_service
        kol_res = get_kol_download_service().run()
        logger.info("达人视频下载完成 - {}", kol_res)
    except Exception:  # noqa: BLE001
        logger.exception("达人视频下载失败（不阻塞后续）")

    # 0b) 素材维护：先按「商品名+递增数字」补齐空素材ID，再读取时长。
    try:
        from services.ingest.service import get_ingest_service
        ingest = get_ingest_service()
        id_res = ingest.assign_missing_material_ids()
        logger.info("缺失素材ID补齐完成 - {}", id_res)
        res = ingest.run()
        logger.info("素材摄取(提取ID+读时长)完成 - {}", res)
    except Exception:  # noqa: BLE001
        logger.exception("素材摄取失败（不阻塞评分）")

    material_by_country: dict[str, dict[str, list[float]]] = {}
    for code in COUNTRY_CODES:
        token, table_id, perf_path = _country_conf(s, code)
        if not (token and table_id):
            logger.info("[{}] 未配置发布合表，跳过（以后扩展时在 .env 配好即可）", code)
            continue
        try:
            material_by_country[code] = score_country(
                code, token, table_id, perf_path, min_views, data_dir
            )
        except Exception:  # noqa: BLE001 - 单国失败不阻塞其它国家
            logger.exception("[{}] 评分失败", code)

    if not material_by_country:
        logger.warning("没有任何国家产出评分，结束")
        return

    writeback(material_by_country)

    # 刷新选材用的评分缓存（VN 主 performance.jsonl 已更新）
    get_performance_store.cache_clear()
    store = get_performance_store()
    store.load()
    top_mat = sorted(
        ((mid, round(store.material_score(mid), 4)) for mid in store._mat),
        key=lambda kv: kv[1], reverse=True,
    )[:15]
    logger.info("VN 选材评分刷新 - Top 素材：{}", top_mat)
    logger.info("早晨维护任务完成")


if __name__ == "__main__":
    main()
