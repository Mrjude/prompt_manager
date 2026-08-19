"""智能客服 Agent 引擎

从 agent_proj/shining_dialog_agents 整合而来，主要改造：

| 原实现 | 本实现 | 原因 |
|---|---|---|
| langchain `create_agent` + LangGraph | 轻量自研 ReAct 循环（OpenAI function calling） | prompt_manager 未引入 langchain 依赖，避免环境膨胀 |
| InMemorySaver checkpointer | SessionStore（进程内 + LRU 淘汰） | 同等的多轮记忆能力，无额外依赖 |
| 静态 system_prompt.json | agent_prompt_builder 动态组装 | 支持按 科室/平台/机器人id 切换人格与知识 |
| 工具内硬编码机构信息 | agent_tools 从数据库按 bot 检索 | 一套工具服务全部机器人 |

会话隔离：session_id 变化 或 (bot_id/department/platform) 变化都会重建会话，
避免切换机器人后沿用旧人格的历史。
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, Generator, List, Optional

import agent_prompt_builder
import agent_tools
import robot_config_service
from conversation_store import conversation_store
from llm_client import get_llm_client

logger = logging.getLogger(__name__)

MAX_REACT_ROUNDS = 5        # 单次回复内最多的工具调用轮次，防死循环
MAX_HISTORY_MESSAGES = 40   # 保留的历史消息数（不含 system）
MAX_SESSIONS = 500          # 内存中最多缓存的会话数


@dataclass
class Session:
    session_id: str
    config_key: str                     # bot_id|department|platform，用于检测配置切换
    messages: List[dict] = field(default_factory=list)   # 不含 system
    turn: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class SessionStore:
    """进程内会话存储，LRU 淘汰"""

    def __init__(self, max_sessions: int = MAX_SESSIONS):
        self._sessions: "OrderedDict[str, Session]" = OrderedDict()
        self._max = max_sessions

    def get_or_create(self, session_id: str, config_key: str) -> Session:
        sess = self._sessions.get(session_id)
        if sess and sess.config_key != config_key:
            logger.info("会话 %s 配置从 %s 切换为 %s，重置上下文", session_id, sess.config_key, config_key)
            sess = None
        if sess is None:
            sess = Session(session_id=session_id, config_key=config_key)
            self._sessions[session_id] = sess
        self._sessions.move_to_end(session_id)
        while len(self._sessions) > self._max:
            self._sessions.popitem(last=False)
        return sess

    def reset(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None

    def stats(self) -> dict:
        return {"session_count": len(self._sessions), "max_sessions": self._max}


_store = SessionStore()


def _trim(messages: List[dict]) -> List[dict]:
    """裁剪历史，但不能把 tool 消息与其 assistant 母消息切散"""
    if len(messages) <= MAX_HISTORY_MESSAGES:
        return messages
    cut = messages[-MAX_HISTORY_MESSAGES:]
    while cut and cut[0].get("role") == "tool":
        cut.pop(0)
    return cut


def _split_segments(reply: str) -> List[str]:
    """切分回复为分句

    兜底归一化（模型不完全遵守格式约束时的最后一道防线）：
      - 清理残留的 <think> 标签
      - 合并连续/首尾的 <sep>
      - 没有 <sep> 但有换行时，按换行切分
    """
    if not reply:
        return []
    text = reply.replace("\r\n", "\n")
    # 清理可能漏出的思考标签
    text = re.sub(r"</?think>", "", text)
    # 兼容模型写成 <SEP> / <sep > 等变体
    text = re.sub(r"<\s*sep\s*>", "<sep>", text, flags=re.IGNORECASE)
    # 合并连续分句符
    text = re.sub(r"(<sep>\s*)+", "<sep>", text)
    text = text.strip().strip("<sep>").strip()

    if "<sep>" not in text and "\n" in text:
        text = "<sep>".join(p.strip() for p in text.split("\n") if p.strip())
    return [s.strip() for s in text.split("<sep>") if s.strip()]


class DialogAgent:
    """智能客服 Agent：按 (bot_id, department, platform) 动态装配后执行 ReAct 对话"""

    def __init__(self, prompt_manager_url: str = "http://localhost:8900"):
        self.llm = get_llm_client(prompt_manager_url)

    # ---------------- 配置装配 ----------------
    def prepare(
        self,
        bot_id: Optional[str] = None,
        department: Optional[str] = None,
        platform: Optional[str] = None,
        turn: int = 0,
    ) -> tuple[dict, str, dict]:
        runtime = robot_config_service.resolve_runtime_config(
            bot_id=bot_id, department=department, platform=platform
        )
        system_prompt, meta = agent_prompt_builder.build_system_prompt(runtime, turn=turn)
        return runtime, system_prompt, meta

    def inspect_config(
        self,
        bot_id: Optional[str] = None,
        department: Optional[str] = None,
        platform: Optional[str] = None,
    ) -> dict:
        """供前端预览"当前筛选条件将生效的配置"，不产生对话"""
        runtime, system_prompt, meta = self.prepare(bot_id, department, platform)
        department_code = runtime.get("department")
        return {
            "runtime": runtime,
            "meta": meta,
            "system_prompt_preview": system_prompt[:2000],
            "tools": [s["function"]["name"]
                      for s in agent_tools.get_tool_schemas(department_code)],
            "skills": agent_tools.skill_registry.list_skills(department=department_code),
        }

    # ---------------- 对话主流程 ----------------
    def chat(
        self,
        message: str,
        session_id: Optional[str] = None,
        bot_id: Optional[str] = None,
        department: Optional[str] = None,
        platform: Optional[str] = None,
        temperature: float = 0.7,
        use_tools: bool = True,
        enable_thinking: bool = False,
    ) -> dict:
        session_id = session_id or uuid.uuid4().hex
        config_key = f"{bot_id or ''}|{department or ''}|{platform or ''}"
        sess = _store.get_or_create(session_id, config_key)
        sess.turn += 1

        runtime, system_prompt, meta = self.prepare(bot_id, department, platform, turn=sess.turn)
        if not runtime.get("enabled", True):
            return {
                "session_id": session_id,
                "reply": "该机器人已被禁用，请在机器人配置中启用后再试。",
                "meta": meta, "runtime": runtime,
                "tool_calls": [], "turn": sess.turn, "error": "bot_disabled",
            }

        sess.messages.append({"role": "user", "content": message})
        sess.messages = _trim(sess.messages)

        tool_schemas = agent_tools.get_tool_schemas(runtime.get("department")) if use_tools else None
        tool_trace: List[dict] = []
        reply = ""
        reasoning = ""
        error = None

        try:
            for round_idx in range(MAX_REACT_ROUNDS):
                payload = [{"role": "system", "content": system_prompt}] + sess.messages
                assistant_msg = self.llm.chat_completion(
                    messages=payload,
                    tools=tool_schemas,
                    temperature=temperature,
                    enable_thinking=enable_thinking,
                )
                tool_calls = assistant_msg.get("tool_calls") or []
                # 思考内容不进历史（避免污染上下文与放大 token 消耗），仅回传前端展示
                if assistant_msg.get("reasoning_content"):
                    reasoning = assistant_msg["reasoning_content"]

                if not tool_calls:
                    reply = (assistant_msg.get("content") or "").strip()
                    sess.messages.append({"role": "assistant", "content": reply})
                    break

                # 记录带 tool_calls 的 assistant 消息（协议要求，否则下轮报错）
                sess.messages.append({
                    "role": "assistant",
                    "content": assistant_msg.get("content") or "",
                    "tool_calls": tool_calls,
                })

                for call in tool_calls:
                    fn = call.get("function", {})
                    name = fn.get("name", "")
                    args = fn.get("arguments", "{}")
                    started = time.time()
                    result = agent_tools.execute_tool(name, args, runtime)
                    tool_trace.append({
                        "round": round_idx + 1,
                        "name": name,
                        "arguments": args,
                        "result_preview": result[:500],
                        "elapsed_ms": int((time.time() - started) * 1000),
                    })
                    sess.messages.append({
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": result,
                    })
            else:
                # 达到轮次上限仍在调工具，强制无工具收口
                logger.warning("会话 %s 达到 ReAct 轮次上限，强制收口", session_id)
                final = self.llm.chat_completion(
                    messages=[{"role": "system", "content": system_prompt}] + sess.messages,
                    tools=None,
                    temperature=temperature,
                    enable_thinking=enable_thinking,
                )
                reply = (final.get("content") or "").strip()
                sess.messages.append({"role": "assistant", "content": reply})

        except Exception as e:
            logger.exception("Agent 对话失败")
            error = str(e)
            reply = "抱歉，我这边网络有点问题，你稍等一下再说一次好吗？"

        sess.messages = _trim(sess.messages)
        sess.updated_at = time.time()

        # 持久化本轮对话（失败不影响对话返回）
        try:
            if not error:
                conversation_store.record_turn(
                    session_id=session_id,
                    user_message=message,
                    assistant_reply=reply,
                    runtime=runtime,
                    turn=sess.turn,
                    tool_calls=tool_trace,
                    meta=meta,
                )
        except Exception as e:
            logger.error("持久化对话失败: %s", e)

        return {
            "session_id": session_id,
            "reply": reply,
            "segments": _split_segments(reply),
            "reasoning": reasoning,
            "turn": sess.turn,
            "tool_calls": tool_trace,
            "meta": meta,
            "runtime": runtime,
            "error": error,
        }

    # ---------------- 历史会话 ----------------
    def load_session(self, session_id: str) -> dict:
        """从持久化存储恢复会话到内存，供前端点击对话列表后继续对话"""
        records = conversation_store.get_messages(session_id)
        if not records:
            return {"session_id": session_id, "messages": [], "found": False}

        last = records[-1]
        config_key = f"{last.get('bot_id', '')}|{last.get('department', '')}|{last.get('platform', '')}"
        sess = _store.get_or_create(session_id, config_key)
        sess.messages = [{"role": r["role"], "content": r.get("content", "")}
                         for r in records if r.get("role") in ("user", "assistant")]
        sess.messages = _trim(sess.messages)
        sess.turn = max((r.get("turn") or 0) for r in records)

        return {
            "session_id": session_id,
            "found": True,
            "turn": sess.turn,
            "bot_id": last.get("bot_id", ""),
            "department": last.get("department", ""),
            "platform": last.get("platform", ""),
            "messages": [{
                "role": r["role"],
                "content": r.get("content", ""),
                "segments": _split_segments(r.get("content", "")) if r["role"] == "assistant" else [],
                "tools": r.get("tool_calls") or [],
                "turn": r.get("turn"),
                "timestamp": r.get("timestamp"),
            } for r in records],
        }

    def reset_session(self, session_id: str) -> bool:
        return _store.reset(session_id)

    def stats(self) -> dict:
        return _store.stats()


_agent: Optional[DialogAgent] = None


def get_agent(prompt_manager_url: str = "http://localhost:8900") -> DialogAgent:
    global _agent
    if _agent is None:
        _agent = DialogAgent(prompt_manager_url)
    return _agent
