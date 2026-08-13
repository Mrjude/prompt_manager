"""Agent 工具集（从 shining_dialog_agents 迁移并改造为按 bot 配置动态取数）

与原项目的差异：
    原项目工具里的机构信息、病种话术是硬编码常量；
    这里全部改为从 prompt_manager 数据库按 (department, platform, bot_id) 动态检索，
    因此同一套工具可服务任意科室/平台/机器人。

工具通过 OpenAI function-calling 协议暴露，不依赖 langchain。
"""
from __future__ import annotations

import json
import logging
import os
import sys
from typing import Callable, Dict, List, Optional

_AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
_PM_BACKEND = os.path.join(_AGENT_DIR, "..", "backend")
for _p in (_AGENT_DIR, _PM_BACKEND):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import database as db  # noqa: E402
from skills.registry import skill_registry  # noqa: E402

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 工具实现：签名统一为 (runtime: dict, **kwargs) -> str
# --------------------------------------------------------------------------

def query_flow_strategy(runtime: dict, topic: str = "", node: str = "") -> str:
    """按关键词检索流程树中的对话策略"""
    department = runtime.get("department", "general")
    platform = runtime.get("platform", "general")
    keyword = (topic or node or "").strip()

    tree = db.get_flow_tree_by_key(department, platform)
    if not tree:
        return f"当前科室({runtime.get('department_zh')})/平台({runtime.get('platform_zh')})暂未配置流程树，请按通用套联策略推进对话。"

    _, records = db.list_flow_records(tree["id"], bot_id=runtime.get("bot_id") or None)
    hits = []
    for rec in records:
        desc = rec.get("description") or ""
        if not desc:
            continue
        if not keyword or keyword in desc or keyword in (rec.get("file_name") or ""):
            hits.append(f"【{rec.get('file_name') or '流程片段'}】\n{desc[:1200]}")
        if len(hits) >= 3:
            break

    if not hits:
        return f"流程树中没有找到与「{keyword}」相关的节点，请按通用策略回复并逐步引导套联。"
    return "\n\n".join(hits)


def _load_kb_items(runtime: dict) -> List[dict]:
    """读取该科室+平台知识库，过滤出"通用条目 + 当前机器人专属条目"

    knowledge_bases.content 为 JSON 数组，条目结构：{text, type, bot_id}
    bot_id 为空表示通用条目。
    """
    kb = db.get_kb_by_key(runtime.get("department", "general"), runtime.get("platform", "general"))
    if not kb:
        return []
    try:
        raw = json.loads(kb.get("content") or "[]")
    except json.JSONDecodeError:
        logger.warning("知识库 %s content 不是合法 JSON", kb.get("id"))
        return []

    bot_id = str(runtime.get("bot_id") or "").strip()
    items = []
    for entry in raw:
        if isinstance(entry, str):
            items.append({"text": entry, "type": "答疑", "bot_id": ""})
            continue
        if not isinstance(entry, dict):
            continue
        entry_bot = str(entry.get("bot_id") or "").strip()
        if entry_bot and bot_id and entry_bot != bot_id:
            continue  # 属于其他机器人的专属条目，跳过
        items.append(entry)
    return items


def _match_kb(items: List[dict], keyword: str, limit: int = 5) -> List[dict]:
    if not keyword:
        return items[:limit]
    hits = [it for it in items if keyword in str(it.get("text", ""))]
    if not hits:
        # 退化为按单字匹配，提高中文短词召回
        hits = [it for it in items if any(ch in str(it.get("text", "")) for ch in keyword if len(keyword) <= 4)]
    return hits[:limit]


def get_company_info(runtime: dict, info_type: str = "all") -> str:
    """查询机构标准化信息（地址/营业时间/医保/联系方式/简介）

    数据来源：knowledge_bases 中该科室+平台的知识条目。
    """
    info_type = (info_type or "all").strip().lower()
    company = runtime.get("company") or "我们这里"

    keyword_map = {
        "address": "地址",
        "hours": "营业时间",
        "insurance": "医保",
        "contact": "电话",
        "intro": "介绍",
        "all": "",
    }
    keyword = keyword_map.get(info_type, info_type)

    items = _load_kb_items(runtime)
    hits = _match_kb(items, keyword)

    if hits:
        return "\n".join(f"- [{it.get('type', '答疑')}] {it.get('text', '')}" for it in hits)

    return (
        f"知识库暂未录入{company}的「{keyword or '机构'}」信息。"
        "请不要编造，可回复'具体的我帮你确认一下'并引导访客留联由顾问跟进。"
    )


def search_medical_knowledge(runtime: dict, query: str = "") -> str:
    """检索医学科普/项目知识（禁止用于报价与诊断）"""
    query = (query or "").strip()
    if not query:
        return "请提供要检索的关键词。"

    items = _load_kb_items(runtime)
    hits = _match_kb(items, query, limit=3)

    if not hits:
        return (
            f"知识库中没有「{query}」的相关内容。请用通俗、模糊的方式回应，"
            "不要编造具体药物、价格和治疗方案，转而引导访客留联面诊。"
        )
    return "\n\n".join(f"【{it.get('type', '答疑')}】{str(it.get('text', ''))[:600]}" for it in hits)


def get_prompt_snippet(runtime: dict, scene: str = "") -> str:
    """按场景取提示词库中的话术片段（如 套联/问诊/异议处理）"""
    scene = (scene or "").strip()
    if not scene:
        return "请提供场景名称，如：套联、问诊、价格异议。"

    department = runtime.get("department", "general")
    platform = runtime.get("platform", "general")
    row = db.get_prompt_by_key(department, platform, scene)
    if not row:
        row = db.get_prompt_by_key(department, "general", scene)
    if not row:
        return f"提示词库中没有配置「{scene}」场景的话术，请按通用策略回复。"
    return (row.get("content") or "")[:2000]


# --------------------------------------------------------------------------
# 工具注册表：name -> (callable, openai function schema)
# --------------------------------------------------------------------------

def load_disease_skill(runtime: dict, disease: str = "") -> str:
    """加载病种专属话术 Skill（整合自 shining_dialog_agents 的 load_disease_skill）

    只在当前科室可用的 Skill 中匹配，避免跨科室误命中。
    """
    disease = (disease or "").strip()
    if not disease:
        return "请提供病种名称或用户描述。"

    department = runtime.get("department", "general")
    skill = skill_registry.match_skill(disease, department=department)
    if not skill:
        available = skill_registry.list_skills(department=department)
        if not available:
            return (
                f"{runtime.get('department_zh', '')}暂无病种话术 Skill，"
                "请按流程树策略和通用套联逻辑推进对话。"
            )
        names = [s["description"].split("（")[0] for s in available]
        return (
            f"未找到「{disease}」对应的 Skill。当前科室支持：{'、'.join(names)}。"
            "请用自然对话方式回应，并尝试引导到已支持的病种方向。"
        )
    return skill_registry.format_strategy(skill)


TOOL_REGISTRY: Dict[str, Callable] = {
    "query_flow_strategy": query_flow_strategy,
    "get_company_info": get_company_info,
    "search_medical_knowledge": search_medical_knowledge,
    "get_prompt_snippet": get_prompt_snippet,
    "load_disease_skill": load_disease_skill,
}

TOOL_SCHEMAS: List[dict] = [
    {
        "type": "function",
        "function": {
            "name": "query_flow_strategy",
            "description": (
                "查询当前科室+平台的标准对话流程话术。当需要确认'这一轮该怎么说'、"
                "'某个病种/意图该走什么流程'时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "话题或病种关键词，如：痘痘、植发价格、犹豫不留联"},
                    "node": {"type": "string", "description": "可选，流程节点名称"},
                },
                "required": ["topic"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_company_info",
            "description": "查询机构标准化信息：地址、交通、营业时间、医保政策、联系方式、机构简介。",
            "parameters": {
                "type": "object",
                "properties": {
                    "info_type": {
                        "type": "string",
                        "enum": ["address", "hours", "insurance", "contact", "intro", "all"],
                        "description": "要查询的信息类型",
                    },
                },
                "required": ["info_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_medical_knowledge",
            "description": "检索医学科普或项目知识用于答疑。禁止用它来给出具体报价、用药方案或诊断结论。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_prompt_snippet",
            "description": "按场景获取提示词库中配置的专用话术片段，如'套联'、'问诊'、'价格异议'。",
            "parameters": {
                "type": "object",
                "properties": {
                    "scene": {"type": "string", "description": "场景名称"},
                },
                "required": ["scene"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_disease_skill",
            "description": (
                "加载病种专属话术策略（含对话阶段、套联时机、禁忌、红旗警示）。"
                "当识别到访客问题涉及某个具体病种时优先调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "disease": {"type": "string", "description": "病种名称或访客原话，如'痤疮'、'脸上长痘痘'"},
                },
                "required": ["disease"],
            },
        },
    },
]

# 仅在对应科室注册了 Skill 时才暴露 load_disease_skill，避免无谓的工具噪声
_SKILL_DEPENDENT_TOOLS = {"load_disease_skill"}


def get_tool_schemas(department: Optional[str] = None) -> List[dict]:
    """按科室裁剪工具集"""
    if not department:
        return TOOL_SCHEMAS
    has_skill = bool(skill_registry.list_skills(department=department))
    if has_skill:
        return TOOL_SCHEMAS
    return [s for s in TOOL_SCHEMAS
            if s["function"]["name"] not in _SKILL_DEPENDENT_TOOLS]


def execute_tool(name: str, arguments: str | dict, runtime: dict) -> str:
    """执行工具调用，任何异常都转成可读文本回灌给模型，避免中断 ReAct 循环"""
    fn = TOOL_REGISTRY.get(name)
    if not fn:
        return f"未知工具：{name}"

    if isinstance(arguments, str):
        try:
            kwargs = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            return f"工具 {name} 的参数不是合法 JSON：{arguments[:200]}"
    else:
        kwargs = arguments or {}

    try:
        return str(fn(runtime, **kwargs))
    except TypeError as e:
        return f"工具 {name} 参数错误：{e}"
    except Exception as e:
        logger.exception("工具 %s 执行失败", name)
        return f"工具 {name} 执行失败：{e}"
