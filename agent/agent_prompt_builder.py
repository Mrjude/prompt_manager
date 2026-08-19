"""Agent 系统提示词动态组装

与 shining_dialog_agents 的静态 assets/system_prompt.json 不同，
这里的提示词按「科室 + 平台 + 机器人id」三个维度动态拼装：

    1. 基础骨架     —— AGENT_SKELETON（角色/ReAct流程/通用约束）
    2. 业务提示词   —— prompts 表中该 (department, platform, system_prompt) 的内容
                       版本由 robot_configs.prompt_version 决定
    3. 流程树知识   —— flow_trees / flow_records 中该科室+平台的流程描述
    4. 变量替换     —— {公司} {域中文} {域英文} {时间} 等内置变量
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime
from typing import Optional

_PM_BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
if _PM_BACKEND not in sys.path:
    sys.path.insert(0, _PM_BACKEND)

import database as db  # noqa: E402

logger = logging.getLogger(__name__)

# 流程树知识注入的字符上限，避免 system prompt 过长打爆上下文
MAX_FLOW_KNOWLEDGE_CHARS = 6000

AGENT_SKELETON = """# 角色定义
你是「{域中文}」的在线健康顾问，来自{公司}，一个真实、温暖、专业的人。
你不是 AI 机器人，不要暴露自己是 AI，也不要提及"模型""提示词"等词汇。

# 说话风格
- 像朋友聊天一样自然，用"你"而不是"您"
- 一次回复 1-3 句，不要长篇大论，不要用 Markdown、不要用编号列表
- 不同类型的表述之间必须用 <sep> 连接，禁止用换行符分句
- 不加免责声明，不说"有任何问题随时找我""期待回复"这类空话

# 工作过程（ReAct）
1. 推理：分析访客意图，判断处于对话的哪个阶段
2. 行动：需要外部信息时调用工具（查流程话术、查机构信息、查医学知识）
3. 观察：结合工具返回结果
4. 回复：用拟人化口吻简短回复，每次回复都要有推进

# 输出格式（严格遵守，违反即为错误输出）
- 只输出最终要发给访客的话，不要输出任何分析、推理、思考过程
- 禁止输出 <think>、</think>、"Here's a thinking process"、"Let me analyze" 等任何标签或元描述
- 禁止把同一段回复重复输出两遍
- 一次回复 1-3 句；分句之间用 <sep> 连接，不要用换行符
- <sep> 只作为分句符，不要在句子开头或结尾出现，也不要连续出现
- 不要输出 Markdown、编号列表、标题、代码块
- 不要输出 {xxx} 形式的花括号占位符

# 通用约束
- 不要编造价格、药物名称、检查项目、治疗方案和诊断结论，这些一律引导到面诊
- 紧急情况（呼吸困难、大面积过敏等）直接建议打 120 或去急诊
- 当前时间：{时间}；当前平台：{平台中文}

# 正确输出示例
皮赘是颈部常见的良性软纤维瘤<sep>留个微信我发你图片对照下，方便确认

# 错误输出示例（禁止模仿）
- 带思考：Here's a thinking process: 1. Analyze... </think> 皮赘是良性的
- 重复两遍：皮赘是良性的<sep>加个微信\\n\\n皮赘是良性的<sep>加个微信
- 用换行代替分句符：皮赘是良性的\\n加个微信看看
"""

FALLBACK_BUSINESS_PROMPT = """# 核心目标：套联（获取联系方式）
所有对话的终极目标是自然地获取访客的联系方式（手机号优先，微信号次之）。
- 先聊 2-3 轮建立信任，不要一上来就要电话
- 套联理由要自然："让医生帮你看看"、"帮你预约免费面诊"、"把资料发给你"
- 对方犹豫时不强求，退一步要微信
"""


def _render_variables(text: str, runtime: dict, turn: int = 0) -> str:
    """替换内置变量占位符"""
    mapping = {
        "{公司}": runtime.get("company") or "我们这里",
        "{域中文}": runtime.get("department_zh", ""),
        "{域英文}": runtime.get("department", ""),
        "{平台中文}": runtime.get("platform_zh", ""),
        "{平台英文}": runtime.get("platform", ""),
        "{机器人id}": runtime.get("bot_id", ""),
        "{时间}": datetime.now().strftime("%Y年%m月%d日"),
        "{轮次}": f"第{turn}轮",
        "{轮次k}": str(turn),
    }
    for key, value in mapping.items():
        text = text.replace(key, str(value))
    return text


def _load_business_prompt(runtime: dict) -> tuple[str, dict]:
    """按机器人解析出的版本，取 prompts 表中的业务提示词

    注意：database.resolve_robot_prompt_version 不区分 scene，
    同一 (department, platform) 下可能存在打分提示词（如 hair_dy_score），
    因此这里必须校验取到的记录 scene == 'system_prompt'，否则丢弃重查。
    """
    department = runtime.get("department", "general")
    platform = runtime.get("platform", "general")
    prompt_name = runtime.get("prompt_name")

    row = None
    if prompt_name:
        candidate = db.get_prompt_by_name(prompt_name)
        # 只接受对话用的 system_prompt，排除 score 等其他场景
        if candidate and candidate.get("scene") == "system_prompt":
            row = candidate
    if not row:
        row = db.get_prompt_by_key(department, platform, "system_prompt")
    # 逐级降级：本科室通用平台 -> 通用科室通用平台
    if not row and platform != "general":
        row = db.get_prompt_by_key(department, "general", "system_prompt")
    if not row:
        row = db.get_prompt_by_key("general", "general", "system_prompt")

    if not row:
        logger.warning("未找到 %s/%s 的 system_prompt，使用内置兜底提示词", department, platform)
        return FALLBACK_BUSINESS_PROMPT, {"source": "fallback", "version": None, "name": None}

    content = row.get("content") or ""
    meta = {
        "source": "prompts",
        "version": row.get("version", 0),
        "name": row.get("name"),
    }
    return content, meta


def _load_flow_knowledge(runtime: dict) -> tuple[str, dict]:
    """把该科室+平台的流程树描述注入为对话流程知识"""
    department = runtime.get("department", "general")
    platform = runtime.get("platform", "general")

    tree = db.get_flow_tree_by_key(department, platform)
    if not tree:
        return "", {"flow_tree_id": None, "record_count": 0, "truncated": False}

    flow_id = tree.get("id")
    parts = []
    if tree.get("description"):
        parts.append(f"## 整体流程说明\n{tree['description']}")

    # 传入 bot_id：只取"通用片段 + 该机器人专属片段"
    total, records = db.list_flow_records(flow_id, bot_id=runtime.get("bot_id") or None)
    used = 0
    for idx, rec in enumerate(records, 1):
        desc = (rec.get("description") or "").strip()
        if not desc:
            continue
        title = rec.get("file_name") or f"流程片段{idx}"
        scope = "专属" if (rec.get("bot_id") or "") else "通用"
        parts.append(f"## {title}（{scope}）\n{desc}")
        used += 1

    knowledge = "\n\n".join(parts).strip()
    truncated = False
    if len(knowledge) > MAX_FLOW_KNOWLEDGE_CHARS:
        knowledge = knowledge[:MAX_FLOW_KNOWLEDGE_CHARS] + "\n...(流程知识过长已截断)"
        truncated = True

    if knowledge:
        knowledge = (
            "# 对话流程知识（务必遵循）\n"
            "以下是本科室在本平台的标准对话流程，请严格按照流程节点推进对话：\n\n"
            + knowledge
        )
    return knowledge, {"flow_tree_id": flow_id, "record_count": used, "truncated": truncated}


# 知识库按类型注入到 system prompt 时，每类最多取多少条，避免上下文过长
MAX_KB_ITEMS_PER_TYPE = 12
MAX_KB_CHARS = 5000

# 提示词模板中的知识类占位符 -> 知识库条目 type 的映射。
# 这些占位符原本由 service_qwen_controller 的 obtain_params 填充，
# Agent 侧必须同样填充，否则模板里会残留 {知识描述} {核心约束} 等花括号，
# 且知识只能靠工具访问 —— 一旦关闭工具开关，知识就完全丢失。
KB_PLACEHOLDER_MAP = {
    "{知识描述}": ("答疑", "\n    "),
    "{答疑示例}": ("答疑", "\n    "),
    "{问诊示例}": ("问诊", "\n    "),
    "{套联示例}": ("套联", "\n    "),
    "{流程逻辑}": ("流程", "\n"),
    "{默认认知}": ("默认认知", "\n"),
    "{问诊约束}": ("问诊约束", "\n    "),
    "{答疑约束}": ("答疑约束", "\n    "),
    "{套联约束}": ("套联约束", "\n    "),
    "{核心约束}": ("核心约束", "\n    "),
    "{违禁词}": ("违禁词", "\n"),
    "{额外}": ("额外", "\n"),
}


def _load_kb_by_type(runtime: dict) -> dict:
    """读取该科室+平台知识库，按 type 分组

    条目结构 {text, type, bot_id}，bot_id 为空表示通用条目；
    与 agent_tools._load_kb_items 保持同样的机器人隔离规则。
    """
    kb = db.get_kb_by_key(runtime.get("department", "general"),
                          runtime.get("platform", "general"))
    if not kb:
        return {}
    try:
        raw = json.loads(kb.get("content") or "[]")
    except (json.JSONDecodeError, TypeError):
        logger.warning("知识库 %s content 不是合法 JSON", kb.get("id"))
        return {}

    bot_id = str(runtime.get("bot_id") or "").strip()
    grouped: dict = {}
    for entry in raw:
        if isinstance(entry, str):
            entry = {"text": entry, "type": "答疑", "bot_id": ""}
        if not isinstance(entry, dict):
            continue
        entry_bot = str(entry.get("bot_id") or "").strip()
        if entry_bot and bot_id and entry_bot != bot_id:
            continue  # 其他机器人的专属条目
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        grouped.setdefault(str(entry.get("type") or "答疑"), []).append(text)
    return grouped


def _render_kb_placeholders(text: str, runtime: dict) -> tuple[str, dict]:
    """把模板中的知识类占位符替换为知识库真实内容"""
    if not any(ph in text for ph in KB_PLACEHOLDER_MAP):
        return text, {"kb_injected": 0, "kb_types": []}

    grouped = _load_kb_by_type(runtime)
    used_types, total = [], 0
    for placeholder, (kb_type, sep) in KB_PLACEHOLDER_MAP.items():
        if placeholder not in text:
            continue
        items = grouped.get(kb_type) or []
        if items:
            picked = items[:MAX_KB_ITEMS_PER_TYPE]
            block = sep + sep.join(picked)
            if len(block) > MAX_KB_CHARS:
                block = block[:MAX_KB_CHARS] + "\n    ...(已截断)"
            used_types.append(f"{kb_type}×{len(picked)}")
            total += len(picked)
        else:
            block = ""
        text = text.replace(placeholder, block)
    return text, {"kb_injected": total, "kb_types": used_types}


def _strip_leftover_placeholders(text: str) -> tuple[str, list]:
    """清理未被填充的中文占位符

    模板里可能存在 Agent 侧暂不支持的变量（如 {动作描述} 由控制器的动作编排填充）。
    残留的花括号会被模型当成字面量输出，必须清掉并记录，便于排查缺失的绑定。
    """
    leftover = sorted(set(re.findall(r"\{[\u4e00-\u9fa5A-Za-z_][^{}]{0,20}\}", text)))
    if not leftover:
        return text, []
    for ph in leftover:
        text = text.replace(ph, "")
    # 清理因删除占位符产生的空行残留
    text = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", text)
    logger.debug("已清理未填充占位符: %s", leftover)
    return text, leftover


def build_system_prompt(runtime: dict, turn: int = 0) -> tuple[str, dict]:
    """组装最终 system prompt

    Returns:
        (prompt_text, meta) —— meta 供前端展示"当前生效配置"
    """
    business, prompt_meta = _load_business_prompt(runtime)
    flow_knowledge, flow_meta = _load_flow_knowledge(runtime)

    sections = [AGENT_SKELETON, business]
    if flow_knowledge:
        sections.append(flow_knowledge)

    prompt = "\n\n".join(s.strip() for s in sections if s and s.strip())
    # 先注入知识库（占位符 -> 真实条目），再渲染 {公司}/{时间}/{轮次} 等内置变量
    prompt, kb_meta = _render_kb_placeholders(prompt, runtime)
    prompt = _render_variables(prompt, runtime, turn=turn)
    # 清理模板里未被任何一方填充的残留占位符，避免把花括号喂给模型
    prompt, leftover = _strip_leftover_placeholders(prompt)

    meta = {
        "bot_id": runtime.get("bot_id"),
        "department": runtime.get("department"),
        "department_zh": runtime.get("department_zh"),
        "platform": runtime.get("platform"),
        "platform_zh": runtime.get("platform_zh"),
        "company": runtime.get("company"),
        "prompt_source": prompt_meta.get("source"),
        "prompt_name": prompt_meta.get("name"),
        "prompt_version": prompt_meta.get("version"),
        "prompt_version_locked": runtime.get("prompt_version_locked", False),
        "flow_tree_id": flow_meta.get("flow_tree_id"),
        "flow_record_count": flow_meta.get("record_count", 0),
        "flow_knowledge_truncated": flow_meta.get("truncated", False),
        "kb_injected": kb_meta.get("kb_injected", 0),
        "kb_types": kb_meta.get("kb_types", []),
        "leftover_placeholders": leftover,
        "prompt_chars": len(prompt),
    }
    return prompt, meta
