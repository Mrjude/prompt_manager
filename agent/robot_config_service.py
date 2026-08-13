"""机器人配置解析服务

职责：把「科室 + 平台 + 机器人id」三级筛选条件，解析成 agent 对话所需的完整运行时配置。

级联规则：
    department -> platform -> bot_id
    上级为空时下级返回全集；上级确定时下级只返回匹配项。

配置合并优先级（后者覆盖前者）：
    1. 全局默认（DEFAULT_RUNTIME）
    2. robot_configs 表中该 bot_id 的记录（department/platform/company/prompt_version）
    3. 调用方显式传入的 department/platform
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Dict, List, Optional

_PM_BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
if _PM_BACKEND not in sys.path:
    sys.path.insert(0, _PM_BACKEND)

import database as db  # noqa: E402

logger = logging.getLogger(__name__)

# 机器人 id -> 公司名 的兜底映射（robot_configs.company 为空时使用）
FALLBACK_COMPANY = {
    "hair": "雍禾",
    "dentistry": "唐森",
    "beauty": "碧莲盛",
}

DEFAULT_RUNTIME = {
    "department": "general",
    "platform": "general",
    "company": "",
    "prompt_version": -1,
    "enabled": True,
}


def _zh(mapping: Dict[str, str], key: str) -> str:
    return mapping.get(key, key or "")


def list_departments(only_configured: bool = False) -> List[dict]:
    """科室选项。only_configured=True 时只返回已有机器人配置的科室"""
    if only_configured:
        used = {c["department"] for c in db.list_robot_configs() if c.get("department")}
        codes = [d for d in db.DEPARTMENTS if d in used]
    else:
        codes = list(db.DEPARTMENTS)
    return [{"value": c, "label": _zh(db.DEPARTMENT_ZH, c)} for c in codes]


def list_platforms(department: Optional[str] = None, only_configured: bool = False) -> List[dict]:
    """平台选项，可按科室级联收窄"""
    if only_configured or department:
        configs = db.list_robot_configs(department=department or None)
        used = {c["platform"] for c in configs if c.get("platform")}
        codes = [p for p in db.PLATFORMS if p in used]
        # 该科室下暂无任何机器人配置时，退回全集，避免下拉框空白
        if not codes and not only_configured:
            codes = list(db.PLATFORMS)
    else:
        codes = list(db.PLATFORMS)
    return [{"value": c, "label": _zh(db.PLATFORM_ZH, c)} for c in codes]


def _sort_key(bot_id: str):
    """机器人 id 排序：数字型按数值排，非数字型排在后面按字典序"""
    s = str(bot_id)
    return (0, int(s), "") if s.isdigit() else (1, 0, s)


def list_bots(
    department: Optional[str] = None,
    platform: Optional[str] = None,
    enabled_only: bool = False,
    keyword: Optional[str] = None,
    include_unconfigured: bool = True,
) -> List[dict]:
    """机器人 id 选项（受科室 + 平台级联约束）

    实时性：每次调用都直查 SQLite（与提示词管理系统共库），不做缓存，
    因此提示词管理中新增/修改/删除机器人配置后，这里立刻可见。

    完整性：
        - enabled_only 默认 False，已禁用的机器人也会返回并带 enabled=False 标记，
          否则运营在提示词管理里禁用后，这里会直接"消失"而非"显示为禁用"
        - include_unconfigured=True 时，把只存在于 bot_ids 表、
          尚未在 robot_configs 配置科室/平台的机器人也列出（标记 configured=False），
          避免"全量机器人"漏项
    """
    configs = db.list_robot_configs(
        department=department or None,
        platform=platform or None,
        enabled_only=enabled_only,
    )
    items = []
    for cfg in configs:
        bot_id = str(cfg.get("bot_id", ""))
        if keyword and keyword not in bot_id:
            continue
        dept = cfg.get("department") or ""
        plat = cfg.get("platform") or ""
        company = cfg.get("company") or FALLBACK_COMPANY.get(dept, "")
        enabled = bool(cfg.get("enabled", True))
        label_parts = [bot_id]
        if company:
            label_parts.append(company)
        label_parts.append(f"{_zh(db.DEPARTMENT_ZH, dept)}/{_zh(db.PLATFORM_ZH, plat)}")
        if not enabled:
            label_parts.append("已禁用")
        items.append({
            "value": bot_id,
            "label": " · ".join(label_parts),
            "bot_id": bot_id,
            "department": dept,
            "platform": plat,
            "company": company,
            "enabled": enabled,
            "configured": True,
            "prompt_version": cfg.get("prompt_version", -1),
            "updated_at": cfg.get("updated_at", ""),
        })

    # 补充未配置科室/平台的机器人（只在未按科室/平台筛选时补，否则无法判断归属）
    if include_unconfigured and not department and not platform:
        configured_ids = {str(c.get("bot_id")) for c in db.list_robot_configs()}
        for bot_id in db.list_bot_ids():
            bot_id = str(bot_id)
            if bot_id in configured_ids:
                continue
            if keyword and keyword not in bot_id:
                continue
            items.append({
                "value": bot_id,
                "label": f"{bot_id} · 未配置科室/平台",
                "bot_id": bot_id,
                "department": "", "platform": "", "company": "",
                "enabled": True, "configured": False,
                "prompt_version": -1, "updated_at": "",
            })

    items.sort(key=lambda x: _sort_key(x["bot_id"]))
    return items


def get_config_signature() -> dict:
    """机器人配置指纹，供前端低成本轮询检测变更

    只要提示词管理中新增/修改/删除任一机器人配置，signature 就会变化。
    """
    configs = db.list_robot_configs()
    bot_ids = db.list_bot_ids()
    latest = max((c.get("updated_at") or "" for c in configs), default="")
    enabled_count = sum(1 for c in configs if c.get("enabled", True))
    return {
        "signature": f"{len(configs)}:{len(bot_ids)}:{enabled_count}:{latest}",
        "config_count": len(configs),
        "bot_id_count": len(bot_ids),
        "enabled_count": enabled_count,
        "latest_updated_at": latest,
    }


def get_cascade_options(
    department: Optional[str] = None,
    platform: Optional[str] = None,
) -> dict:
    """一次性返回三级联动的全部选项，减少前端请求次数"""
    return {
        "departments": list_departments(),
        "platforms": list_platforms(department=department),
        "bots": list_bots(department=department, platform=platform),
    }


def resolve_runtime_config(
    bot_id: Optional[str] = None,
    department: Optional[str] = None,
    platform: Optional[str] = None,
) -> dict:
    """解析出 agent 运行所需的完整配置

    未传 bot_id 时也可工作（走 department/platform + 最新提示词版本）。
    """
    runtime = dict(DEFAULT_RUNTIME)
    bot_id = str(bot_id).strip() if bot_id else ""
    cfg = db.get_robot_config(bot_id) if bot_id else None

    if bot_id and not cfg:
        logger.warning("机器人 %s 未在 robot_configs 中配置，使用筛选框传入的科室/平台", bot_id)

    if cfg:
        runtime.update({
            "department": cfg.get("department") or runtime["department"],
            "platform": cfg.get("platform") or runtime["platform"],
            "company": cfg.get("company") or "",
            "prompt_version": cfg.get("prompt_version", -1),
            "enabled": bool(cfg.get("enabled", True)),
        })

    # 调用方显式指定的维度优先级最高
    if department:
        runtime["department"] = department
    if platform:
        runtime["platform"] = platform
    if not runtime["company"]:
        runtime["company"] = FALLBACK_COMPANY.get(runtime["department"], "")

    runtime["bot_id"] = bot_id
    runtime["department_zh"] = _zh(db.DEPARTMENT_ZH, runtime["department"])
    runtime["platform_zh"] = _zh(db.PLATFORM_ZH, runtime["platform"])

    # 解析该机器人实际生效的提示词版本
    if bot_id:
        resolved = db.resolve_robot_prompt_version(
            bot_id=bot_id,
            department=runtime["department"],
            platform=runtime["platform"],
        )
    else:
        prompt = db.get_prompt_by_key(runtime["department"], runtime["platform"], "system_prompt")
        resolved = {
            "version": prompt.get("version", 0) if prompt else 0,
            "prompt_name": prompt.get("name") if prompt else None,
            "locked": False,
        }
    runtime["prompt_name"] = resolved.get("prompt_name")
    runtime["resolved_prompt_version"] = resolved.get("version", 0)
    runtime["prompt_version_locked"] = resolved.get("locked", False)

    # 该科室/平台是否有流程树知识可用
    tree = db.get_flow_tree_by_key(runtime["department"], runtime["platform"])
    runtime["flow_tree_id"] = tree.get("id") if tree else None
    runtime["has_flow_tree"] = bool(tree)
    runtime["config_source"] = "robot_configs" if cfg else "filter_only"
    return runtime
