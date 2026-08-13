"""对话会话持久化存储

整合自 agent_proj/shining_dialog_agents/src/utils/conversation_logger.py，
主要差异：

| 原实现 | 本实现 | 原因 |
|---|---|---|
| 按天分文件 conversation_{date}.jsonl | 按天分文件 + 会话索引 sessions.jsonl | 前端对话列表需要按会话聚合，逐条扫描全部日志太慢 |
| 仅记录 session_id/role/content | 额外记录 bot_id/department/platform | 对话列表要支持按机器人 id 筛选 |
| 只能按 session_id / date 精确读取 | 支持关键词全文检索 + 分页 | 前端搜索框要检索对话内容 |

文件布局（默认 agent/data/conversations/）：
    sessions.jsonl                  会话索引（每行一个会话的最新快照，同 id 后写覆盖前写）
    messages_{YYYY-MM-DD}.jsonl     消息明细（追加写）
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "conversations")
STORE_DIR = os.getenv("AGENT_CONV_DIR") or _DEFAULT_DIR

# 会话标题取首条用户消息的前 N 字
TITLE_MAX_CHARS = 24


class ConversationStore:
    """线程安全的对话持久化存储（单例）"""

    _instance: Optional["ConversationStore"] = None
    _singleton_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._inited = False
        return cls._instance

    def __init__(self, store_dir: str = STORE_DIR):
        if self._inited:
            return
        self._inited = True
        self.dir = store_dir
        os.makedirs(self.dir, exist_ok=True)
        self._lock = threading.Lock()
        # 会话索引全量载入内存（会话量级为千级，内存可控）
        self._sessions: Dict[str, dict] = {}
        self._load_index()

    # ---------------- 内部工具 ----------------
    @property
    def _index_path(self) -> str:
        return os.path.join(self.dir, "sessions.jsonl")

    def _messages_path(self, date_str: Optional[str] = None) -> str:
        date_str = date_str or datetime.now().strftime("%Y-%m-%d")
        return os.path.join(self.dir, f"messages_{date_str}.jsonl")

    @staticmethod
    def _read_jsonl(path: str) -> List[dict]:
        if not os.path.exists(path):
            return []
        out = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue  # 跳过损坏行，不影响整体读取
        except Exception as e:
            logger.error("读取 %s 失败: %s", path, e)
        return out

    @staticmethod
    def _append_jsonl(path: str, record: dict):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
        except Exception as e:
            logger.error("写入 %s 失败: %s", path, e)

    def _load_index(self):
        """载入会话索引，同 session_id 以后写为准"""
        for rec in self._read_jsonl(self._index_path):
            sid = rec.get("session_id")
            if sid:
                self._sessions[sid] = rec
        logger.info("对话存储已载入 %d 个会话 (%s)", len(self._sessions), self.dir)

    def _compact_index(self):
        """索引文件重写，清理被覆盖的历史行（会话数变化时按需调用）"""
        tmp = self._index_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for rec in self._sessions.values():
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
            os.replace(tmp, self._index_path)
        except Exception as e:
            logger.error("压缩会话索引失败: %s", e)

    # ---------------- 写入 ----------------
    def record_turn(
        self,
        session_id: str,
        user_message: str,
        assistant_reply: str,
        runtime: dict,
        turn: int,
        tool_calls: Optional[List[dict]] = None,
        meta: Optional[dict] = None,
    ) -> dict:
        """记录一轮完整对话（用户消息 + agent 回复），并更新会话索引"""
        now_ms = int(time.time() * 1000)
        now_iso = datetime.now().isoformat(timespec="seconds")
        date_str = datetime.now().strftime("%Y-%m-%d")
        bot_id = str(runtime.get("bot_id") or "")

        with self._lock:
            msg_path = self._messages_path(date_str)
            base = {
                "session_id": session_id,
                "bot_id": bot_id,
                "department": runtime.get("department", ""),
                "platform": runtime.get("platform", ""),
                "turn": turn,
                "date": date_str,
            }
            self._append_jsonl(msg_path, {
                **base, "role": "user", "content": user_message,
                "timestamp": now_iso, "epoch_ms": now_ms,
            })
            self._append_jsonl(msg_path, {
                **base, "role": "assistant", "content": assistant_reply,
                "timestamp": now_iso, "epoch_ms": now_ms + 1,
                "tool_calls": tool_calls or [],
            })

            sess = self._sessions.get(session_id)
            is_new = sess is None
            if is_new:
                sess = {
                    "session_id": session_id,
                    "title": (user_message or "新对话").strip()[:TITLE_MAX_CHARS],
                    "created_at": now_iso,
                    "created_ms": now_ms,
                    "dates": [date_str],
                }
            sess.update({
                "bot_id": bot_id,
                "department": runtime.get("department", ""),
                "department_zh": runtime.get("department_zh", ""),
                "platform": runtime.get("platform", ""),
                "platform_zh": runtime.get("platform_zh", ""),
                "company": runtime.get("company", ""),
                "prompt_name": (meta or {}).get("prompt_name"),
                "prompt_version": (meta or {}).get("prompt_version"),
                "turns": turn,
                "last_message": (assistant_reply or "").replace("<sep>", " ")[:80],
                "updated_at": now_iso,
                "updated_ms": now_ms,
                "tool_call_total": sess.get("tool_call_total", 0) + len(tool_calls or []),
            })
            dates = set(sess.get("dates") or [])
            dates.add(date_str)
            sess["dates"] = sorted(dates)

            self._sessions[session_id] = sess
            self._append_jsonl(self._index_path, sess)
            return sess

    def delete_session(self, session_id: str) -> bool:
        """从索引中删除会话（消息明细保留，用于审计）"""
        with self._lock:
            if session_id not in self._sessions:
                return False
            self._sessions.pop(session_id)
            self._compact_index()
            return True

    # ---------------- 读取 ----------------
    def get_messages(self, session_id: str) -> List[dict]:
        """取某会话的全部消息，按时间排序"""
        sess = self._sessions.get(session_id)
        dates = sess.get("dates") if sess else None
        if not dates:
            # 会话索引缺失时退化为扫描全部消息文件
            dates = [f[9:-6] for f in os.listdir(self.dir)
                     if f.startswith("messages_") and f.endswith(".jsonl")]
        msgs = []
        for d in sorted(dates):
            msgs.extend(r for r in self._read_jsonl(self._messages_path(d))
                        if r.get("session_id") == session_id)
        msgs.sort(key=lambda r: r.get("epoch_ms", 0))
        return msgs

    def _match_content(self, session_id: str, keyword: str) -> bool:
        """会话内是否存在包含关键词的消息"""
        return any(keyword in str(m.get("content", "")) for m in self.get_messages(session_id))

    def list_sessions(
        self,
        bot_id: Optional[str] = None,
        department: Optional[str] = None,
        platform: Optional[str] = None,
        keyword: Optional[str] = None,
        page: int = 1,
        page_size: int = 30,
    ) -> dict:
        """会话列表：按机器人 id / 科室 / 平台筛选，按对话内容关键词检索

        keyword 先匹配标题与末条消息（快路径），未命中再回落到全文扫描（慢路径），
        避免每次搜索都读取全部消息文件。
        """
        items = list(self._sessions.values())

        if bot_id:
            bot_id = str(bot_id).strip()
            items = [s for s in items if bot_id in str(s.get("bot_id", ""))]
        if department:
            items = [s for s in items if s.get("department") == department]
        if platform:
            items = [s for s in items if s.get("platform") == platform]

        if keyword:
            kw = keyword.strip()
            fast, slow = [], []
            for s in items:
                if kw in str(s.get("title", "")) or kw in str(s.get("last_message", "")):
                    fast.append(s)
                else:
                    slow.append(s)
            deep = [s for s in slow if self._match_content(s["session_id"], kw)]
            items = fast + deep

        items.sort(key=lambda s: s.get("updated_ms", 0), reverse=True)
        total = len(items)
        page = max(1, page)
        start = (page - 1) * page_size
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": items[start:start + page_size],
        }

    def stats(self) -> dict:
        return {
            "session_total": len(self._sessions),
            "store_dir": self.dir,
        }


conversation_store = ConversationStore()
