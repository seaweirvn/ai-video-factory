"""飞书自检脚本：列出多维表格里的表 / 某张表的字段 / 抽样记录。

用法（在项目根目录，已 copy .env 并填好 FEISHU_VN_* 后）：
  # 列出所有数据表（拿到素材表的 table_id）
  python -m scripts.inspect_feishu tables

  # 列出某张表的字段名与类型
  python -m scripts.inspect_feishu fields <table_id>

  # 抽样打印某张表前 N 条记录的字段值
  python -m scripts.inspect_feishu sample <table_id> [N]
"""

from __future__ import annotations

import sys
from pathlib import Path

from adapters.feishu import get_feishu_client
from app.config import get_settings

# Windows 控制台常是 GBK，中文表名/字段名会乱码。统一把结果写到 UTF-8 文件，
# 同时尝试打印（打印失败不影响文件）。
_OUT_FILE = Path("tmp-inspect.txt")
_lines: list[str] = []


def _emit(line: str) -> None:
    _lines.append(line)
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("utf-8", "replace").decode("ascii", "replace"))


def _flush() -> None:
    _OUT_FILE.write_text("\n".join(_lines), encoding="utf-8")
    print(f"\n[结果已写入 {_OUT_FILE.resolve()}]")


def _list_tables() -> None:
    client = get_feishu_client()
    app_token = client.app_token
    payload = client._request(
        "GET", f"/bitable/v1/apps/{app_token}/tables", params={"page_size": 100}
    )
    items = payload.get("data", {}).get("items", [])
    _emit(f"共 {len(items)} 张表：")
    for it in items:
        _emit(f"  {it.get('table_id')}  {it.get('name')}")


def _resolve_wiki_node(node_token: str) -> str:
    """把 wiki 节点 token 解析成挂载的 bitable app_token（obj_token）。"""
    client = get_feishu_client()
    payload = client._request(
        "GET", "/wiki/v2/spaces/get_node", params={"token": node_token}
    )
    node = payload.get("data", {}).get("node", {})
    _emit(f"wiki node: obj_type={node.get('obj_type')} obj_token={node.get('obj_token')} title={node.get('title')}")
    return str(node.get("obj_token") or "")


def _list_fields(table_id: str, app_token: str | None = None) -> None:
    client = get_feishu_client()
    tok = app_token or client.app_token
    payload = client._request(
        "GET", f"/bitable/v1/apps/{tok}/tables/{table_id}/fields", params={"page_size": 100}
    )
    for f in payload.get("data", {}).get("items", []):
        _emit(f"  {str(f.get('ui_type') or f.get('uiType')):<14} {f.get('field_name')}")


def _sample(table_id: str, n: int, app_token: str | None = None) -> None:
    client = get_feishu_client()
    tok = app_token or client.app_token
    payload = client._request(
        "GET",
        f"/bitable/v1/apps/{tok}/tables/{table_id}/records",
        params={"page_size": n},
    )
    for r in payload.get("data", {}).get("items", []):
        _emit("-" * 50 + " " + str(r.get("record_id")))
        for name, value in r.get("fields", {}).items():
            _emit(f"  {name}: {client.cell_text(value)!r}")


def main() -> None:
    settings = get_settings()
    if not settings.feishu_vn_app_id or not settings.feishu_vn_bitable_app_token:
        print("请先在 .env 配置 FEISHU_VN_APP_ID / FEISHU_VN_APP_SECRET / FEISHU_VN_BITABLE_APP_TOKEN")
        sys.exit(1)

    args = sys.argv[1:]
    if not args or args[0] == "tables":
        _list_tables()
    elif args[0] == "fields" and len(args) >= 2:
        _list_fields(args[1])
    elif args[0] == "sample" and len(args) >= 2:
        _sample(args[1], int(args[2]) if len(args) >= 3 else 3)
    elif args[0] == "node" and len(args) >= 2:
        _resolve_wiki_node(args[1])
    elif args[0] == "wikifields" and len(args) >= 3:
        app_token = _resolve_wiki_node(args[1])
        _list_fields(args[2], app_token)
    elif args[0] == "wikisample" and len(args) >= 3:
        app_token = _resolve_wiki_node(args[1])
        _sample(args[2], int(args[3]) if len(args) >= 4 else 3, app_token)
    else:
        print(__doc__)
        sys.exit(1)
    _flush()


if __name__ == "__main__":
    main()
