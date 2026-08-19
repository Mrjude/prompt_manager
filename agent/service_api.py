"""Agent 对外服务接口

参数与响应格式对齐 qwen-proj/llm_service/service_qwen_controller.py 的 on_post，
使已接入该控制器的外部系统可以零改造（或极小改造）切换到 Agent 服务。

两种入参风格（与控制器一致，通过是否存在 inputs 字段自动识别）：

1. utterance_rec 风格（带 inputs 包裹，KICP 平台风格）
   {
     "inputs": {"dialogId": "...", "thirdUserId": "...", "context": "[...]",
                "sentence": "用户这句话", "limit": 1},
     "robotId": "9378", "domain": "hair", ...
   }

2. 扁平风格
   {
     "dialogId": "...", "thirdUserId": "...", "robotId": "9378",
     "domain": "hair", "dialogRecord": [...], "utterance": "用户这句话", ...
   }

与控制器的差异（Agent 特有能力）：
   - 回复由 ReAct Agent 生成，会调用流程树/知识库/病种话术工具
   - 提示词由 科室+平台+机器人id 三级配置动态组装
   - 支持 enable_thinking 开关
"""
from __future__ import annotations

import ast
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("agent_service_api")

# 域名归一化：与控制器 on_post 中的处理保持一致
_DOMAIN_ALIAS = {
    "douyin_beauty": "beauty",
    "抖音医美": "beauty",
}


def _parse_dialog_record(raw: Any) -> List[dict]:
    """解析对话历史

    控制器里 context 可能是字符串形式的 list（用 ast.literal_eval 解析），
    这里同时兼容真正的 list 和 JSON 字符串。
    """
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    if isinstance(raw, str):
        for parser in (json.loads, ast.literal_eval):
            try:
                parsed = parser(raw)
                return parsed if isinstance(parsed, list) else []
            except (ValueError, SyntaxError, TypeError):
                continue
    return []


def parse_request(input_params: dict) -> dict:
    """把两种入参风格统一成内部参数字典（字段名对齐控制器）"""
    utterance_rec = "inputs" not in input_params

    if not utterance_rec:
        inputs = input_params.get("inputs") or {}
        params = {
            "utterance_rec": False,
            "dialog_id": inputs.get("dialogId", "100001"),
            "company_id": inputs.get("thirdUserId", "193929"),
            "dialog_record": _parse_dialog_record(inputs.get("context", [])),
            "query": inputs.get("sentence", ""),
            "limit": inputs.get("limit", 1),
        }
    else:
        params = {
            "utterance_rec": True,
            "dialog_id": input_params.get("dialogId", "100001"),
            "company_id": input_params.get("thirdUserId", "193929"),
            "dialog_record": _parse_dialog_record(input_params.get("dialogRecord", [])),
            "query": input_params.get("utterance", ""),
            "limit": input_params.get("limit", 1),
        }

    # 两种风格共用的字段
    domain = input_params.get("domain", "") or ""
    params.update({
        "robot_id": str(input_params.get("robotId", "") or ""),
        "domain": _DOMAIN_ALIAS.get(domain, domain),
        "platform": input_params.get("platform", "") or "",
        "keyword": input_params.get("keyword", ""),
        "intent": input_params.get("intent", "无"),
        "topic": input_params.get("topic", "无主题"),
        "action_version": input_params.get("actionVersion", "v1.0"),
        "ai_switch": input_params.get("aiSwitch", True),
        "task_type": input_params.get("task_type", "dialog"),
        "is_question": str(input_params.get("isQuestion", "0")) == "1",
        "temperature": float(input_params.get("temperature", 0.9)),
        "top_p": float(input_params.get("top_p", 0.95)),
        "max_new_tokens": int(input_params.get("max_new_tokens", 64)),
        # Agent 特有
        "enable_thinking": bool(input_params.get("enable_thinking", False)),
        "use_tools": bool(input_params.get("use_tools", True)),
        "session_id": input_params.get("sessionId") or input_params.get("session_id"),
    })
    return params


def _extract_query(params: dict) -> str:
    """取本轮用户输入；query 为空时回退到历史里最后一条访客消息"""
    query = (params.get("query") or "").strip()
    if query:
        return query
    for item in reversed(params.get("dialog_record") or []):
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or item.get("speaker") or "").lower()
        text = item.get("content") or item.get("text") or item.get("utterance") or ""
        if text and role in ("user", "visitor", "customer", "访客", "客户"):
            return str(text).strip()
    return ""


def _session_id_of(params: dict) -> str:
    """会话 id：显式传入优先，否则用 dialogId 保证同一通对话上下文连续"""
    return str(params.get("session_id") or params.get("dialog_id") or "")


def handle_dialog(params: dict, agent, robot_config_service) -> dict:
    """执行对话并组装成控制器风格的响应"""
    st = time.time()
    query = _extract_query(params)
    session_id = _session_id_of(params)
    robot_id = params.get("robot_id") or ""
    domain = params.get("domain") or ""
    platform = params.get("platform") or ""

    if not query:
        return {
            "code": 400,
            "domain": domain,
            "version": "agent-service-1.0.0",
            "session_id": session_id,
            "data": [] if params["utterance_rec"] else {"outputs": {"text": "[]"}},
            "data_ori": [],
            "history": params.get("dialog_record") or [],
            "source": params.get("task_type"),
            "error": "缺少用户输入(utterance / inputs.sentence)",
        }

    # 未显式传 domain/platform 时，由 robot_id 反查（与 agent 内部逻辑一致）
    if robot_id and (not domain or not platform):
        try:
            runtime = robot_config_service.resolve_runtime_config(bot_id=robot_id)
            domain = domain or runtime.get("department") or ""
            platform = platform or runtime.get("platform") or ""
        except Exception as e:
            logger.warning("按 robot_id=%s 解析配置失败: %s", robot_id, e)

    error = ""
    answers: List[str] = []
    reply_meta: Dict[str, Any] = {}
    try:
        result = agent.chat(
            message=query,
            session_id=session_id or None,
            bot_id=robot_id or None,
            department=domain or None,
            platform=platform or None,
            temperature=params.get("temperature", 0.9),
            use_tools=params.get("use_tools", True),
            enable_thinking=params.get("enable_thinking", False),
        )
        reply = (result.get("reply") or "").strip()
        if reply:
            answers = [reply]
        error = result.get("error") or ""
        reply_meta = {
            "turn": result.get("turn"),
            "segments": result.get("segments") or [],
            "tool_calls": [t.get("name") for t in (result.get("tool_calls") or [])],
            "reasoning": result.get("reasoning") or "",
            "prompt_name": (result.get("meta") or {}).get("prompt_name"),
            "prompt_version": (result.get("meta") or {}).get("prompt_version"),
            "department": (result.get("runtime") or {}).get("department"),
            "platform": (result.get("runtime") or {}).get("platform"),
            "company": (result.get("runtime") or {}).get("company"),
        }
        session_id = result.get("session_id") or session_id
    except Exception as e:
        logger.exception("Agent 对话失败")
        error = str(e)

    # 按 limit 截断（控制器对 dialog/smartScript 固定为 1）
    limit = 1 if params.get("task_type") in ("smartScript", "dialog") else int(params.get("limit") or 1)
    answers = answers[:max(1, limit)]

    response = {
        "code": 200 if not error else 500,
        "domain": domain,
        "version": "agent-service-1.0.0",
        "session_id": session_id,
        # 与控制器一致：utterance_rec 风格直接给数组，inputs 风格包一层 outputs.text
        "data": answers if params["utterance_rec"] else {
            "outputs": {"text": json.dumps(answers, ensure_ascii=False)}
        },
        "data_ori": [],
        "history": params.get("dialog_record") or [],
        "source": params.get("task_type"),
        "error": error,
        # Agent 扩展字段，外部可忽略
        "agent": reply_meta,
        "cost_time": round(time.time() - st, 3),
    }
    logger.info(
        "[对外服务] robot=%s domain=%s platform=%s turn=%s tools=%s cost=%.2fs",
        robot_id, domain, platform, reply_meta.get("turn"),
        reply_meta.get("tool_calls"), time.time() - st,
    )
    return response
