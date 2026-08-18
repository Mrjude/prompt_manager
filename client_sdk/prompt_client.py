#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
提示词 & 知识库管理系统 - 客户端 SDK

专为模型服务设计，支持：
1. 按科室+平台+场景获取提示词
2. 按科室+平台获取知识库内容
3. 变量自动解析（公司、时间、轮次等）
4. 热更新（轮询/WebSocket）
5. 本地缓存，读取零延迟

使用示例：
    from prompt_client import PromptClient

    client = PromptClient(base_url="http://localhost:8900")

    # 获取已解析的提示词
    prompt = client.get_resolved(
        name="hair_xhs_system",
        robot_id="9125",
        current_round=3,
        action_desc="\\n回复意图：问诊、套联"
    )

    # 获取知识库
    kb_items = client.get_knowledge(department="hair", platform="xhs")
    # kb_items -> ["知识条目1", "知识条目2", ...]

    # 获取流程树描述
    flow_desc = client.get_flow_descriptions(department="hair", platform="xhs")

    # 启动后台热更新
    client.start_auto_update(interval=30)
"""

import time
import threading
import logging
import json
from typing import Optional, Dict, List, Callable
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode

logger = logging.getLogger("prompt_client")


class PromptClient:
    """
    提示词 & 知识库管理客户端

    特性：
    - 三维管理：科室 + 平台 + 场景
    - 知识库管理：科室 + 平台 唯一索引
    - 本地缓存，读取零延迟
    - 变量自动解析
    - 支持轮询和WebSocket热更新
    - 线程安全
    """

    def __init__(self, base_url: str = "http://localhost:8900"):
        # SDK 仅使用 /api/v1/* 公开端点，无需鉴权
        self.base_url = base_url.rstrip("/")
        # 提示词缓存: {name: {content, version, department, platform, scene, variables, updated_at}}
        self._cache: Dict[str, dict] = {}
        # 已解析缓存: {cache_key: (resolved_content, version)}
        self._resolved_cache: Dict[str, tuple] = {}
        # 知识库缓存: {(department, platform): {id, content, updated_at}}
        self._kb_cache: Dict[tuple, dict] = {}
        # 流程树描述缓存: {(department, platform, keyword): [record, ...]}
        self._flow_cache: Dict[tuple, List[dict]] = {}
        # 机器人配置缓存: [ {bot_id, department, platform, company, enabled, ...} ]
        self._robot_cache: List[dict] = []
        # 元数据缓存: {departments: [{key,label}], platforms: [...], kb_types: [str]}
        self._meta_cache: Dict[str, list] = {}
        self._lock = threading.Lock()
        self._running = False
        self._poll_thread: Optional[threading.Thread] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._on_update_callback: Optional[Callable] = None

    # ==================== 提示词接口 ====================

    def get(self, name: str) -> Optional[dict]:
        """获取提示词原始数据（从缓存，零延迟）"""
        with self._lock:
            if name in self._cache:
                return dict(self._cache[name])
        self._fetch_one(name)
        with self._lock:
            return dict(self._cache[name]) if name in self._cache else None

    def get_content(self, name: str, default: str = "") -> str:
        """获取提示词原始内容"""
        data = self.get(name)
        return data["content"] if data else default

    def get_resolved(
        self,
        name: str,
        robot_id: str = "",
        department: str = "",
        current_round: int = 0,
        action_desc: str = "",
        knowledge_desc: str = "",
        warmup_desc: str = "",
        connect_desc: str = "",
        extra_variables: dict = None,
    ) -> str:
        """
        获取已解析变量的提示词（最常用接口）

        内部会自动解析 {公司}, {域中文}, {时间}, {轮次} 等变量，
        并调用远程 resolve API 处理更复杂的变量替换。
        """
        extra_variables = extra_variables or {}

        # 先确保缓存中有数据
        data = self.get(name)
        if not data:
            return ""

        # 构建缓存key（含 extra_variables，避免知识库等外部变量更新后命中旧缓存）
        try:
            extra_key = json.dumps(extra_variables, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            extra_key = str(extra_variables)
        cache_key = f"{name}::{robot_id}::{department}::{current_round}::{action_desc}::{knowledge_desc}::{warmup_desc}::{connect_desc}::{extra_key}"

        with self._lock:
            if cache_key in self._resolved_cache:
                resolved, version = self._resolved_cache[cache_key]
                if version == data["version"]:
                    return resolved

        # 调用远程解析API
        try:
            payload = json.dumps({  
                "name": name,
                "robot_id": robot_id,
                "department": department,
                "current_round": current_round,
                "action_desc": action_desc,
                "knowledge_desc": knowledge_desc,
                "warmup_desc": warmup_desc,
                "connect_desc": connect_desc,
                "extra_variables": extra_variables
            }).encode()
            req = Request(
                f"{self.base_url}/api/v1/resolve",
                data=payload,
                headers={"Content-Type": "application/json"}
            )
            with urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
                resolved = result["resolved_content"]
                with self._lock:
                    self._resolved_cache[cache_key] = (resolved, result["version"])
                return resolved
        except Exception as e:
            logger.warning(f"远程解析失败，使用本地缓存: {e}")
            return data.get("content", "")

    def get_version(self, name: str) -> int:
        """获取提示词版本号"""
        with self._lock:
            return self._cache.get(name, {}).get("version", 0)

    def get_resolved_by_bot(
        self,
        bot_id: str,
        robot_id: str = "",
        department: str = "",
        current_round: int = 0,
        action_desc: str = "",
        knowledge_desc: str = "",
        warmup_desc: str = "",
        connect_desc: str = "",
        extra_variables: dict = None,
        score: bool = False,
    ) -> str:
        """按机器人配置选择提示词模板（支持 prompt_version 固定版本）后，再调用 /api/v1/resolve。

        Args:
            bot_id: 机器人 ID（必填），先查 /api/v1/robot_configs/{bot_id}/prompt_version
                决定是取固定版本还是最新版本的提示词
            其余参数同 get_resolved

        Returns:
            已填充变量的提示词内容
        """
        info = self._http_get(f"/api/v1/robot_configs/{bot_id}/prompt_version")
        if not info or not info.get("prompt_name"):
            return ""
        name = info["prompt_name"]
        if score:
            # score 模式：name 是 system 提示词名，取同名 score 提示词
            # 约定：system_prompt 为 "xhs_system" 时，score 提示词为 "xhs_score"
            # 直接从 name 推断
            base = name.rsplit("_", 1)[0] if name.endswith("_system") else name
            score_name = f"{base}_score"
            if score_name in self.get_all_names():
                name = score_name
        return self.get_resolved(
            name=name,
            robot_id=robot_id or bot_id,
            department=department,
            current_round=current_round,
            action_desc=action_desc,
            knowledge_desc=knowledge_desc,
            warmup_desc=warmup_desc,
            connect_desc=connect_desc,
            extra_variables=extra_variables,
        )

    def get_all_names(self) -> list:
        """获取所有缓存中的提示词名称"""
        with self._lock:
            return list(self._cache.keys())

    # ==================== 知识库接口 ====================

    def get_knowledge(self, department: str, platform: str, knowledge_type: str = None, bot_id: str = None) -> List[str]:
        """
        根据科室和平台获取知识库内容列表

        Args:
            department: 科室代码，如 hair, dentistry, dermatology 等
            platform: 平台代码，如 xhs, bd, dy 等
            knowledge_type: 知识类型筛选，如 "答疑"、"问诊"、"套联"、"流程"、"默认认知"、"额外"，为空返回全部
            bot_id: 机器人ID筛选，如 "9378"，为空返回全部

        Returns:
            知识条目文本列表，如 ["条目1", "条目2", ...]，未找到返回空列表
        """
        key = (department, platform)
        with self._lock:
            if key in self._kb_cache:
                content = self._kb_cache[key].get("content", "[]")
                return self._parse_kb_content(content, knowledge_type=knowledge_type, bot_id=bot_id)

        # 从服务端拉取
        self._fetch_knowledge(department, platform)
        with self._lock:
            if key in self._kb_cache:
                content = self._kb_cache[key].get("content", "[]")
                return self._parse_kb_content(content, knowledge_type=knowledge_type, bot_id=bot_id)
        return []

    def get_knowledge_detail(self, department: str, platform: str, knowledge_type: str = None, bot_id: str = None) -> List[dict]:
        """
        根据科室和平台获取知识库详情列表（含知识类型和机器人ID）

        Args:
            department: 科室代码
            platform: 平台代码
            knowledge_type: 知识类型筛选，为空返回全部
            bot_id: 机器人ID筛选，为空返回全部

        Returns:
            知识条目详情列表，如 [{"text": "条目1", "type": "答疑", "bot_id": "9378"}, ...]
        """
        key = (department, platform)
        with self._lock:
            if key in self._kb_cache:
                content = self._kb_cache[key].get("content", "[]")
                return self._parse_kb_detail(content, knowledge_type=knowledge_type, bot_id=bot_id)

        self._fetch_knowledge(department, platform)
        with self._lock:
            if key in self._kb_cache:
                content = self._kb_cache[key].get("content", "[]")
                return self._parse_kb_detail(content, knowledge_type=knowledge_type, bot_id=bot_id)
        return []

    def get_knowledge_text(self, department: str, platform: str, knowledge_type: str = None, bot_id: str = None, separator: str = "\n") -> str:
        """
        根据科室和平台获取知识库内容，合并为单个字符串

        Args:
            department: 科室代码
            platform: 平台代码
            knowledge_type: 知识类型筛选，为空返回全部
            bot_id: 机器人ID筛选，为空返回全部
            separator: 条目之间的分隔符，默认换行

        Returns:
            合并后的知识库文本
        """
        items = self.get_knowledge(department, platform, knowledge_type=knowledge_type, bot_id=bot_id)
        return separator.join(items) if items else ""

    def get_all_knowledge(self, knowledge_type: str = None, bot_id: str = None) -> Dict[str, List[str]]:
        """
        获取所有缓存中的知识库

        Args:
            knowledge_type: 知识类型筛选，为空返回全部
            bot_id: 机器人ID筛选，为空返回全部

        Returns:
            字典，key 为 "科室/平台"，value 为条目列表
        """
        result = {}
        with self._lock:
            for (dept, plat), data in self._kb_cache.items():
                result[f"{dept}/{plat}"] = self._parse_kb_content(data.get("content", "[]"), knowledge_type=knowledge_type, bot_id=bot_id)
        return result

    def refresh_knowledge(self, department: str = None, platform: str = None):
        """
        刷新知识库缓存

        Args:
            department: 科室代码，为空则刷新全部
            platform: 平台代码，为空则刷新对应科室的全部或全部
        """
        if department and platform:
            self._fetch_knowledge(department, platform)
        else:
            self._sync_all_knowledge(department=department, platform=platform)

    @staticmethod
    def _kb_item_bot_id(item: dict, default: str = "") -> str:
        """获取知识条目的机器人ID；空值表示通用记录，适用于所有机器人。"""
        value = item.get("bot_id", default)
        return "" if value is None else str(value).strip()

    @staticmethod
    def _kb_bot_matches(item_bot_id: str, bot_id: str = None) -> bool:
        """机器人ID匹配：筛选具体机器人时，同时包含 bot_id 为空的通用记录。"""
        if not bot_id:
            return True
        return item_bot_id == "" or item_bot_id == str(bot_id).strip()

    @classmethod
    def _parse_kb_content(cls, content_json: str, knowledge_type: str = None, bot_id: str = None) -> List[str]:
        """解析知识库 content JSON 为文本列表，支持按知识类型和机器人ID筛选"""
        try:
            arr = json.loads(content_json or "[]")
            if not isinstance(arr, list):
                return []
            # 兼容旧格式（字符串数组）
            results = []
            for item in arr:
                if isinstance(item, str):
                    if knowledge_type and knowledge_type != "答疑":
                        continue
                    legacy_bot_id = "9378"
                    if not cls._kb_bot_matches(legacy_bot_id, bot_id):
                        continue
                    results.append(item)
                elif isinstance(item, dict):
                    item_type = item.get("type", "答疑")
                    if knowledge_type and item_type != knowledge_type:
                        continue
                    item_bot_id = cls._kb_item_bot_id(item)
                    if not cls._kb_bot_matches(item_bot_id, bot_id):
                        continue
                    results.append(item.get("text", ""))
            return results
        except (json.JSONDecodeError, TypeError):
            return []

    @classmethod
    def _parse_kb_detail(cls, content_json: str, knowledge_type: str = None, bot_id: str = None) -> List[dict]:
        """解析知识库 content JSON 为详情列表，支持按知识类型和机器人ID筛选"""
        try:
            arr = json.loads(content_json or "[]")
            if not isinstance(arr, list):
                return []
            results = []
            for item in arr:
                if isinstance(item, str):
                    if knowledge_type and knowledge_type != "答疑":
                        continue
                    legacy_bot_id = "9378"
                    if not cls._kb_bot_matches(legacy_bot_id, bot_id):
                        continue
                    results.append({"text": item, "type": "答疑", "bot_id": legacy_bot_id})
                elif isinstance(item, dict):
                    item_type = item.get("type", "答疑")
                    if knowledge_type and item_type != knowledge_type:
                        continue
                    item_bot_id = cls._kb_item_bot_id(item)
                    if not cls._kb_bot_matches(item_bot_id, bot_id):
                        continue
                    results.append({"text": item.get("text", ""), "type": item_type, "bot_id": item_bot_id})
            return results
        except (json.JSONDecodeError, TypeError):
            return []

    def _fetch_knowledge(self, department: str, platform: str) -> bool:
        """从服务端获取单个知识库"""
        query = urlencode({"department": department, "platform": platform})
        result = self._http_get(f"/api/v1/knowledge?{query}")
        if not result or not result.get("items"):
            return False
        item = result["items"][0]
        key = (department, platform)
        with self._lock:
            old = self._kb_cache.get(key, {})
            self._kb_cache[key] = item
        if old.get("updated_at") != item.get("updated_at"):
            logger.info(f"知识库 [{department}/{platform}] 已更新")
            if self._on_update_callback:
                try:
                    self._on_update_callback(f"kb:{department}/{platform}", item)
                except Exception as e:
                    logger.error(f"知识库更新回调失败: {e}")
        return True

    def _sync_all_knowledge(self, department: str = None, platform: str = None):
        """同步所有知识库"""
        params = {}
        if department:
            params["department"] = department
        if platform:
            params["platform"] = platform
        query = urlencode(params)
        path = f"/api/v1/knowledge?{query}" if query else "/api/v1/knowledge"
        result = self._http_get(path)
        if not result:
            return
        for item in result.get("items", []):
            key = (item.get("department", ""), item.get("platform", ""))
            if not key[0] or not key[1]:
                continue
            changed = False
            with self._lock:
                old = self._kb_cache.get(key, {})
                self._kb_cache[key] = item
                if old.get("updated_at") != item.get("updated_at"):
                    changed = True
            if changed:
                logger.info(f"[同步] 知识库 [{key[0]}/{key[1]}] 已更新")
                if self._on_update_callback:
                    try:
                        self._on_update_callback(f"kb:{key[0]}/{key[1]}", item)
                    except Exception as e:
                        logger.error(f"知识库更新回调失败: {e}")

    # ==================== 流程树描述接口 ====================

    def get_flow_records(self, department: str = None, platform: str = None, keyword: str = None, bot_id: str = None) -> List[dict]:
        """
        获取流程树解析记录列表（含自然语言描述），不受后台用户权限配置影响。

        Args:
            department: 科室代码，为空返回全部
            platform: 平台代码，为空返回全部
            keyword: 关键词，可匹配流程树描述、文件名、解析描述
            bot_id: 机器人ID筛选；传具体值时包含 bot_id 为空的通用记录

        Returns:
            解析记录列表，每项包含 description/status/file_name/department/platform 等字段
        """
        key = (department or "", platform or "", keyword or "", bot_id or "")
        with self._lock:
            if key in self._flow_cache:
                return [dict(item) for item in self._flow_cache[key]]
        self.refresh_flow_records(department=department, platform=platform, keyword=keyword, bot_id=bot_id)
        with self._lock:
            return [dict(item) for item in self._flow_cache.get(key, [])]

    def get_flow_descriptions(self, department: str = None, platform: str = None, keyword: str = None, bot_id: str = None, separator: str = "\n") -> str:
        """获取流程树自然语言描述并合并为字符串。"""
        records = self.get_flow_records(department=department, platform=platform, keyword=keyword, bot_id=bot_id)
        descriptions = [r.get("description", "") for r in records if r.get("description")]
        return separator.join(descriptions)

    def refresh_flow_records(self, department: str = None, platform: str = None, keyword: str = None, bot_id: str = None):
        """刷新流程树解析记录缓存。"""
        params = {}
        if department:
            params["department"] = department
        if platform:
            params["platform"] = platform
        if keyword:
            params["keyword"] = keyword
        if bot_id:
            params["bot_id"] = bot_id
        query = urlencode(params)
        path = f"/api/v1/flow_records/search?{query}" if query else "/api/v1/flow_records/search"
        result = self._http_get(path)
        if not result:
            return
        key = (department or "", platform or "", keyword or "", bot_id or "")
        with self._lock:
            self._flow_cache[key] = result.get("items", [])

    # ==================== 元数据（科室 / 平台 / 知识类型） ====================
    def refresh_meta(self) -> dict:
        """拉取最新元数据（科室、平台、知识记录类型）。

        含运营在界面上新增的"自定义科室 / 自定义类型"。
        网络异常时保留上一次缓存，不抛异常，避免线上服务因元数据拉取失败而中断。
        """
        result = self._http_get("/api/v1/meta")
        if not result:
            logger.warning("[同步] 元数据拉取失败，沿用缓存")
            with self._lock:
                return dict(self._meta_cache)

        with self._lock:
            old = dict(self._meta_cache)
            self._meta_cache = {
                "departments": list(result.get("departments") or []),
                "platforms": list(result.get("platforms") or []),
                "kb_types": list(result.get("kb_types") or []),
            }
            new = dict(self._meta_cache)

        if old != new:
            logger.info(
                f"[同步] 元数据更新: 科室 {len(new['departments'])} 个, "
                f"平台 {len(new['platforms'])} 个, 知识类型 {len(new['kb_types'])} 个"
            )
            if self._on_update_callback:
                try:
                    self._on_update_callback("meta", new)
                except Exception as e:
                    logger.error(f"元数据更新回调失败: {e}")
        return new

    def _get_meta(self, key: str) -> list:
        """读缓存，首次访问时惰性拉取"""
        with self._lock:
            cached = self._meta_cache.get(key)
        if cached:
            return list(cached)
        self.refresh_meta()
        with self._lock:
            return list(self._meta_cache.get(key) or [])

    def get_kb_types(self, default: Optional[List[str]] = None) -> List[str]:
        """知识记录类型列表，如 ["答疑", "问诊", "套联", ...]"""
        items = self._get_meta("kb_types")
        return items if items else list(default or [])

    def get_departments(self) -> List[dict]:
        """科室列表：[{key: "hair", label: "植发科"}, ...]"""
        return self._get_meta("departments")

    def get_platforms(self) -> List[dict]:
        """平台列表：[{key: "xhs", label: "小红书"}, ...]"""
        return self._get_meta("platforms")

    def get_domain_zh2en(self, default: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """科室中文名 -> 英文 key 映射，等价于原先硬编码的 domain_zh2en"""
        items = self._get_meta("departments")
        if not items:
            return dict(default or {})
        return {d["label"]: d["key"] for d in items if d.get("label") and d.get("key")}

    def get_domain_en2zh(self, default: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """科室英文 key -> 中文名映射"""
        items = self._get_meta("departments")
        if not items:
            return dict(default or {})
        return {d["key"]: d["label"] for d in items if d.get("label") and d.get("key")}

    # ==================== 机器人配置（公司/科室/平台） ====================
    def refresh_robot_configs(self) -> List[dict]:
        """拉取最新的机器人配置列表（bot_id/department/platform/company/enabled）。

        Returns:
            最新配置列表（深拷贝）。
        """
        result = self._http_get("/api/v1/robot_configs")
        items = result.get("items", []) if result else []
        with self._lock:
            old = list(self._robot_cache)
            self._robot_cache = list(items)
        # 检测变化
        new_key = sorted(((i.get("bot_id"), i.get("department"), i.get("platform"),
                           i.get("company"), i.get("enabled")) for i in items))
        old_key = sorted(((i.get("bot_id"), i.get("department"), i.get("platform"),
                           i.get("company"), i.get("enabled")) for i in old))
        if new_key != old_key:
            logger.info(f"[同步] 机器人配置更新: {len(items)} 条")
            if self._on_update_callback:
                try:
                    self._on_update_callback("robot_configs", {"items": items, "total": len(items)})
                except Exception as e:
                    logger.error(f"机器人配置更新回调失败: {e}")
        return [dict(i) for i in items]

    def get_robot_configs(self, department: str = None, platform: str = None, enabled_only: bool = False) -> List[dict]:
        """返回当前缓存中的机器人配置。可按科室/平台过滤。"""
        with self._lock:
            items = [dict(i) for i in self._robot_cache]
        out = []
        for it in items:
            if department and it.get("department") != department:
                continue
            if platform and it.get("platform") != platform:
                continue
            if enabled_only and not it.get("enabled", True):
                continue
            out.append(it)
        return out

    # ==================== 通用接口 ====================

    def preload(self):
        """全量预加载所有提示词、知识库、流程树描述、机器人配置和元数据"""
        self._sync_all()
        self._sync_all_knowledge()
        self.refresh_flow_records()
        self.refresh_robot_configs()
        self.refresh_meta()

    def on_update(self, callback: Callable[[str, dict], None]):
        """注册更新回调: callback(key, data)，key 对提示词为名称，对知识库为 kb:科室/平台"""
        self._on_update_callback = callback

    def start_auto_update(self, interval: int = 30, names: list = None):
        """启动轮询热更新（提示词+知识库）"""
        if self._running:
            return
        self._running = True
        self._poll_thread = threading.Thread(
            target=self._poll_loop, args=(interval, names),
            daemon=True, name="prompt-poll"
        )
        self._poll_thread.start()
        logger.info(f"轮询热更新已启动，间隔 {interval}s")

    def start_ws_update(self):
        """启动WebSocket实时更新"""
        try:
            import websocket
        except ImportError:
            logger.error("需要安装 websocket-client: pip install websocket-client")
            return
        if self._ws_thread and self._ws_thread.is_alive():
            return
        self._running = True
        self._ws_thread = threading.Thread(
            target=self._ws_loop, daemon=True, name="prompt-ws"
        )
        self._ws_thread.start()

    def stop(self):
        """停止后台更新"""
        self._running = False
        if self._poll_thread:
            self._poll_thread.join(timeout=5)
        if self._ws_thread:
            self._ws_thread.join(timeout=5)
        logger.info("热更新已停止")

    # ========== 内部方法 ==========
    def _http_get(self, path: str) -> Optional[dict]:
        try:
            req = Request(f"{self.base_url}{path}")
            with urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except (URLError, HTTPError, TimeoutError) as e:
            logger.warning(f"请求失败 {path}: {e}")
            return None

    def _fetch_one(self, name: str) -> bool:
        with self._lock:
            current_version = self._cache.get(name, {}).get("version", 0)
        result = self._http_get(f"/api/v1/fetch/{name}?current_version={current_version}")
        if result is None:
            if current_version == 0:
                result = self._http_get(f"/api/v1/fetch/{name}")
                if not result:
                    return False
            else:
                return True
        with self._lock:
            old_version = self._cache.get(name, {}).get("version", 0)
            self._cache[name] = result
        if old_version != result.get("version", 0):
            logger.info(f"提示词 '{name}' 已更新到 v{result['version']}")
            if self._on_update_callback:
                try:
                    self._on_update_callback(name, result)
                except Exception as e:
                    logger.error(f"更新回调失败: {e}")
        return True

    def _sync_all(self):
        result = self._http_get("/api/v1/sync")
        if not result:
            return
        updated_items = []
        for item in result.get("prompts", []):
            name = item.get("name")
            if not name:
                continue
            with self._lock:
                old_v = self._cache.get(name, {}).get("version", 0)
                if item.get("version", 0) > old_v:
                    self._cache[name] = item
                    # 清除该提示词的已解析缓存，确保 get_resolved 重新解析新版本
                    stale = [k for k in self._resolved_cache if k.startswith(f"{name}::")]
                    for k in stale:
                        del self._resolved_cache[k]
                    updated_items.append((name, item, old_v))
        for name, item, old_v in updated_items:
            logger.info(f"[同步] '{name}' v{old_v} -> v{item['version']}")
            if self._on_update_callback:
                try:
                    self._on_update_callback(name, item)
                except Exception as e:
                    logger.error(f"提示词更新回调失败: {e}")

    def _poll_loop(self, interval, names):
        while self._running:
            try:
                if names:
                    for n in names:
                        self._fetch_one(n)
                else:
                    self._sync_all()
                self._sync_all_knowledge()
                self.refresh_flow_records()
                self.refresh_robot_configs()
                self.refresh_meta()
            except Exception as e:
                logger.error(f"轮询异常: {e}")
            for _ in range(interval):
                if not self._running:
                    break
                time.sleep(1)

    def _ws_loop(self):
        import websocket

        def on_message(ws, message):
            try:
                msg = json.loads(message)
                name = msg.get("name")
                if name and msg.get("event") in ("created", "updated", "rollback"):
                    self._fetch_one(name)
                    # 清除已解析缓存
                    with self._lock:
                        keys_to_remove = [k for k in self._resolved_cache if k.startswith(f"{name}::")]
                        for k in keys_to_remove:
                            del self._resolved_cache[k]
                # 知识库更新
                if msg.get("type") == "knowledge_updated":
                    dept = msg.get("department")
                    plat = msg.get("platform")
                    if dept and plat:
                        self._fetch_knowledge(dept, plat)
            except Exception as e:
                logger.error(f"WS消息处理失败: {e}")

        def on_error(ws, error):
            logger.warning(f"WS错误: {error}")

        def on_close(ws, *args):
            logger.info("WS连接关闭")

        ws_url = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        while self._running:
            try:
                ws = websocket.WebSocketApp(
                    f"{ws_url}/ws/updates",
                    on_message=on_message, on_error=on_error, on_close=on_close
                )
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                logger.error(f"WS异常: {e}")
            if self._running:
                time.sleep(5)

    def __repr__(self):
        with self._lock:
            return (f"PromptClient(cached_prompts={len(self._cache)}, "
                    f"cached_kb={len(self._kb_cache)}, "
                    f"cached_flow={sum(len(v) for v in self._flow_cache.values())}, "
                    f"names={list(self._cache.keys())})")
