from __future__ import annotations

from core.enums import MaterialRole


def parse_roles(value: str) -> list[MaterialRole]:
    """把“HOOK / CTA / PROOF”这类文本解析成角色列表（忽略非角色词，如“转场”/标签）。"""
    roles: list[MaterialRole] = []
    for token in str(value).replace("，", "/").replace(",", "/").split("/"):
        token = token.strip().upper()
        for role in MaterialRole:
            if role.value == token and role not in roles:
                roles.append(role)
    return roles
