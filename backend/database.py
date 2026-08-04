#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
提示词管理系统 - 数据库层

特性：
1. 按科室+平台+场景三维管理提示词
2. 支持变量占位符（如 {公司}, {域中文}, {时间} 等）
3. 完整版本管理与回滚
4. 内存缓存 + 热更新
5. 变量解析引擎，支持动态填充
6. 知识库管理
"""
import sqlite3
import os
import json
import re
import secrets
import hashlib
from datetime import datetime, timedelta
from contextlib import contextmanager
from typing import Optional, List, Dict, Tuple, Any

DB_PATH = os.environ.get(
    "PROMPT_DB_PATH",
    os.path.join(os.path.dirname(__file__), "prompt_manager.db")
)

# 内存缓存: {cache_key: (version, content, variables, updated_at)}
# cache_key = f"{department}::{platform}::{scene}"
_version_cache: Dict[str, Tuple[int, str, str, str]] = {}

# 科室定义
DEPARTMENTS = [
    "hair", "dentistry", "dermatology", "ophthalmology", "pediatrics", "beauty",
    "thyroid", "psychiatry", "andrology", "gynaecology",
    "general",
]
DEPARTMENT_ZH = {
    "hair": "植发科", "dentistry": "口腔科", "dermatology": "皮肤科",
    "ophthalmology": "眼科", "pediatrics": "儿科", "beauty": "医美",
    "thyroid": "甲状腺科", "psychiatry": "精神科", "andrology": "男科", "gynaecology": "妇科",
    "general": "通用",
}
# 平台定义
PLATFORMS = ["xhs", "bd", "dy", "kuaishou", "wechat", "general"]
PLATFORM_ZH = {
    "xhs": "小红书", "bd": "百度", "dy": "抖音",
    "kuaishou": "快手", "wechat": "微信", "general": "通用"
}
# 场景定义
SCENES = ["system_prompt", "warmup", "knowledge", "action_desc", "score", "general"]
SCENE_ZH = {
    "system_prompt": "系统提示词", "warmup": "暖场语", "knowledge": "知识模板",
    "action_desc": "动作描述", "score": "评分", "general": "通用"
}
# 知识类型定义
KB_TYPES = ["答疑", "问诊", "套联", "流程", "默认认知", "额外", "问诊约束", "答疑约束", "套联约束", "核心约束", "违禁词"]

# 机器人ID列表（与 ROBOT_COMPANY_MAP 的 key 保持一致）
BOT_IDS = ["7422", "8714", "8686", "8542", "8771", "9125", "9378", "10122", "10569", "9352", "9358", "9682"]

# 内置变量定义
BUILTIN_VARIABLES = {
    "{公司}": "根据robot_id自动填充：雍禾/碧莲盛/唐森等",
    "{域中文}": "科室中文名，如：植发科",
    "{域英文}": "科室英文名，如：hair",
    "{时间}": "当前日期，如：2026年4月16日",
    "{轮次}": "当前对话轮次，如：第3轮",
    "{轮次k}": "当前对话轮次数字，如：3",
    "{套联描述}": "根据科室和轮次自动填充的套联福利描述",
    "{动作描述}": "动作意图描述注入点",
    "{知识描述}": "知识模板注入点",
    "{暖场描述}": "暖场语描述注入点",
}


@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """初始化数据库"""
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS prompts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                department TEXT NOT NULL DEFAULT 'general',
                platform TEXT NOT NULL DEFAULT 'general',
                scene TEXT NOT NULL DEFAULT 'system_prompt',
                content TEXT NOT NULL,
                variables TEXT DEFAULT '{}',
                description TEXT DEFAULT '',
                tags TEXT DEFAULT '',
                is_active INTEGER DEFAULT 1,
                version INTEGER DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(name)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS prompt_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prompt_id INTEGER NOT NULL,
                version INTEGER NOT NULL,
                content TEXT NOT NULL,
                change_log TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (prompt_id) REFERENCES prompts(id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_prompts_dept 
            ON prompts(department, platform, scene)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_prompt_versions_pid 
            ON prompt_versions(prompt_id, version)
        """)
        # 迁移：确保旧表有 variables 列
        try:
            conn.execute("SELECT variables FROM prompts LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE prompts ADD COLUMN variables TEXT DEFAULT '{}'")
        # 迁移：确保有 variable_bindings 列
        try:
            conn.execute("SELECT variable_bindings FROM prompts LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE prompts ADD COLUMN variable_bindings TEXT DEFAULT '{}'")
        # 知识库表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_bases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                department TEXT NOT NULL,
                platform TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL,
                UNIQUE(department, platform)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_kb_dept_plat
            ON knowledge_bases(department, platform)
        """)
        # 流程树库
        conn.execute("""
            CREATE TABLE IF NOT EXISTS flow_trees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                department TEXT NOT NULL,
                platform TEXT NOT NULL,
                description TEXT DEFAULT '',
                updated_at TEXT NOT NULL,
                UNIQUE(department, platform)
            )
        """)
        # 流程树解析记录
        conn.execute("""
            CREATE TABLE IF NOT EXISTS flow_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                flow_id INTEGER NOT NULL,
                file_name TEXT NOT NULL,
                file_type TEXT NOT NULL DEFAULT 'image',
                file_path TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                structure TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (flow_id) REFERENCES flow_trees(id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_flow_records_fid
            ON flow_records(flow_id)
        """)
        try:
            conn.execute("SELECT bot_id FROM flow_records LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE flow_records ADD COLUMN bot_id TEXT NOT NULL DEFAULT ''")
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_flow_records_bot_id
            ON flow_records(bot_id)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
        """)
        # 机器人ID表（持久化存储，支持动态新增）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_ids (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS robot_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id TEXT NOT NULL UNIQUE,
                department TEXT NOT NULL,
                platform TEXT NOT NULL,
                company TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_robot_configs_filter
            ON robot_configs(department, platform, enabled)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'normal',
                can_prompt_view INTEGER NOT NULL DEFAULT 1,
                can_prompt_edit INTEGER NOT NULL DEFAULT 0,
                can_prompt_delete INTEGER NOT NULL DEFAULT 0,
                can_knowledge_view INTEGER NOT NULL DEFAULT 1,
                can_knowledge_edit INTEGER NOT NULL DEFAULT 0,
                can_knowledge_delete INTEGER NOT NULL DEFAULT 0,
                can_flow_view INTEGER NOT NULL DEFAULT 1,
                can_flow_edit INTEGER NOT NULL DEFAULT 0,
                can_flow_delete INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        # 旧库兼容：增加 managed_departments 列（JSON 数组）
        try:
            conn.execute("SELECT managed_departments FROM users LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE users ADD COLUMN managed_departments TEXT NOT NULL DEFAULT '[]'")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS auth_sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id)")
        for column, default in (
            ("can_prompt_view", 1),
            ("can_prompt_edit", 0),
            ("can_prompt_delete", 0),
            ("can_knowledge_view", 1),
            ("can_knowledge_edit", 0),
            ("can_knowledge_delete", 0),
            ("can_flow_view", 1),
            ("can_flow_edit", 0),
            ("can_flow_delete", 0),
        ):
            try:
                conn.execute(f"SELECT {column} FROM users LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute(f"ALTER TABLE users ADD COLUMN {column} INTEGER NOT NULL DEFAULT {default}")
    _ensure_default_admin()
    _migrate_kb_content()
    _ensure_bot_ids_seeded()
    _refresh_cache()


def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return f"{salt}${digest}"


def _verify_password(password: str, password_hash: str) -> bool:
    try:
        salt, digest = password_hash.split("$", 1)
    except ValueError:
        return False
    actual = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return secrets.compare_digest(actual, digest)


def _public_user(row: dict) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "can_prompt_view": bool(row["can_prompt_view"]),
        "can_prompt_edit": bool(row["can_prompt_edit"]),
        "can_prompt_delete": bool(row["can_prompt_delete"]),
        "can_knowledge_view": bool(row["can_knowledge_view"]),
        "can_knowledge_edit": bool(row["can_knowledge_edit"]),
        "can_knowledge_delete": bool(row["can_knowledge_delete"]),
        "can_flow_view": bool(row["can_flow_view"]),
        "can_flow_edit": bool(row["can_flow_edit"]),
        "can_flow_delete": bool(row["can_flow_delete"]),
        "is_active": bool(row["is_active"]),
        "managed_departments": _parse_md(row.get("managed_departments")),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _parse_md(raw) -> List[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return [str(x) for x in v] if isinstance(v, list) else []
    except Exception:
        return []


def _ensure_default_admin():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        row = conn.execute("SELECT id FROM users WHERE username = ?", ("admin",)).fetchone()
        if not row:
            conn.execute(
                """INSERT INTO users (
                       username, password_hash, role,
                       can_prompt_view, can_prompt_edit, can_prompt_delete,
                       can_knowledge_view, can_knowledge_edit, can_knowledge_delete,
                       can_flow_view, can_flow_edit, can_flow_delete,
                       is_active, created_at, updated_at
                   ) VALUES (?, ?, 'admin', 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, ?, ?)""",
                ("admin", _hash_password("admin123456"), now, now)
            )
        else:
            conn.execute(
                """UPDATE users SET role = 'admin', can_prompt_view = 1,
                   can_prompt_edit = 1, can_prompt_delete = 1,
                   can_knowledge_view = 1, can_knowledge_edit = 1, can_knowledge_delete = 1,
                   can_flow_view = 1, can_flow_edit = 1, can_flow_delete = 1,
                   updated_at = ? WHERE username = 'admin'""",
                (now,)
            )


def authenticate_user(username: str, password: str) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? AND is_active = 1",
            (username,)
        ).fetchone()
    if not row or not _verify_password(password, row["password_hash"]):
        return None
    return _public_user(dict(row))


def create_session(user_id: int, hours: int = 24 * 30) -> dict:
    now = datetime.now()
    token = secrets.token_urlsafe(32)
    expires_at = (now + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    created_at = now.strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO auth_sessions (token, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (token, user_id, expires_at, created_at)
        )
    return {"token": token, "expires_at": expires_at}


def get_user_by_token(token: str) -> Optional[dict]:
    if not token:
        return None
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        row = conn.execute(
            """SELECT u.* FROM auth_sessions s
               JOIN users u ON u.id = s.user_id
               WHERE s.token = ? AND s.expires_at > ? AND u.is_active = 1""",
            (token, now)
        ).fetchone()
    return _public_user(dict(row)) if row else None


def delete_session(token: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM auth_sessions WHERE token = ?", (token,))


def list_users() -> List[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY role ASC, id ASC").fetchall()
        return [_public_user(dict(r)) for r in rows]


def get_user_by_id(user_id: int) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _public_user(dict(row)) if row else None


def create_user(
    username: str,
    password: str,
    role: str = "normal",
    can_prompt_view: bool = True,
    can_prompt_edit: bool = False,
    can_prompt_delete: bool = False,
    can_knowledge_view: bool = True,
    can_knowledge_edit: bool = False,
    can_knowledge_delete: bool = False,
    can_flow_view: bool = True,
    can_flow_edit: bool = False,
    can_flow_delete: bool = False,
    managed_departments: Optional[List[str]] = None,
) -> dict:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    md_json = json.dumps(list(managed_departments or []), ensure_ascii=False)
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO users (
                   username, password_hash, role,
                   can_prompt_view, can_prompt_edit, can_prompt_delete,
                   can_knowledge_view, can_knowledge_edit, can_knowledge_delete,
                   can_flow_view, can_flow_edit, can_flow_delete,
                   is_active, created_at, updated_at, managed_departments
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (
                username, _hash_password(password), role,
                1 if can_prompt_view else 0,
                1 if can_prompt_edit else 0,
                1 if can_prompt_delete else 0,
                1 if can_knowledge_view else 0,
                1 if can_knowledge_edit else 0,
                1 if can_knowledge_delete else 0,
                1 if can_flow_view else 0,
                1 if can_flow_edit else 0,
                1 if can_flow_delete else 0,
                now, now, md_json
            )
        )
        user_id = cursor.lastrowid
    return get_user_by_id(user_id)


def update_user(
    user_id: int,
    password: Optional[str] = None,
    role: Optional[str] = None,
    can_prompt_view: Optional[bool] = None,
    can_prompt_edit: Optional[bool] = None,
    can_prompt_delete: Optional[bool] = None,
    can_knowledge_view: Optional[bool] = None,
    can_knowledge_edit: Optional[bool] = None,
    can_knowledge_delete: Optional[bool] = None,
    can_flow_view: Optional[bool] = None,
    can_flow_edit: Optional[bool] = None,
    can_flow_delete: Optional[bool] = None,
    is_active: Optional[bool] = None,
    managed_departments: Optional[List[str]] = None,
) -> Optional[dict]:
    if not get_user_by_id(user_id):
        return None
    fields, params = [], []
    if password:
        fields.append("password_hash = ?")
        params.append(_hash_password(password))
    if role is not None:
        fields.append("role = ?")
        params.append(role)
    if can_prompt_view is not None:
        fields.append("can_prompt_view = ?")
        params.append(1 if can_prompt_view else 0)
    if can_prompt_edit is not None:
        fields.append("can_prompt_edit = ?")
        params.append(1 if can_prompt_edit else 0)
    if can_prompt_delete is not None:
        fields.append("can_prompt_delete = ?")
        params.append(1 if can_prompt_delete else 0)
    if can_knowledge_view is not None:
        fields.append("can_knowledge_view = ?")
        params.append(1 if can_knowledge_view else 0)
    if can_knowledge_edit is not None:
        fields.append("can_knowledge_edit = ?")
        params.append(1 if can_knowledge_edit else 0)
    if can_knowledge_delete is not None:
        fields.append("can_knowledge_delete = ?")
        params.append(1 if can_knowledge_delete else 0)
    if can_flow_view is not None:
        fields.append("can_flow_view = ?")
        params.append(1 if can_flow_view else 0)
    if can_flow_edit is not None:
        fields.append("can_flow_edit = ?")
        params.append(1 if can_flow_edit else 0)
    if can_flow_delete is not None:
        fields.append("can_flow_delete = ?")
        params.append(1 if can_flow_delete else 0)
    if is_active is not None:
        fields.append("is_active = ?")
        params.append(1 if is_active else 0)
    if managed_departments is not None:
        fields.append("managed_departments = ?")
        params.append(json.dumps(list(managed_departments), ensure_ascii=False))
    if not fields:
        return get_user_by_id(user_id)
    fields.append("updated_at = ?")
    params.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    params.append(user_id)
    with get_connection() as conn:
        conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", params)
        if is_active is False:
            conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))
    return get_user_by_id(user_id)


def delete_user(user_id: int) -> bool:
    user = get_user_by_id(user_id)
    if not user or user["username"] == "admin":
        return False
    with get_connection() as conn:
        conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return True


def user_has_permission(user_id: int, permission: str) -> bool:
    allowed = {
        "can_prompt_view", "can_prompt_edit", "can_prompt_delete",
        "can_knowledge_view", "can_knowledge_edit", "can_knowledge_delete",
        "can_flow_view", "can_flow_edit", "can_flow_delete",
    }
    if permission not in allowed:
        return False
    with get_connection() as conn:
        row = conn.execute(f"SELECT {permission} FROM users WHERE id = ? AND is_active = 1", (user_id,)).fetchone()
    if not row:
        return False
    return bool(row[permission])


def _migrate_kb_content():
    """迁移知识库内容：字符串数组 -> 对象数组（增加知识类型标签和机器人ID）"""
    with get_connection() as conn:
        rows = conn.execute("SELECT id, content FROM knowledge_bases").fetchall()
        for row in rows:
            try:
                content = json.loads(row["content"] or "[]")
                if not isinstance(content, list) or not content:
                    continue
                changed = False
                if isinstance(content[0], str):
                    # 旧格式：纯字符串数组 -> 对象数组
                    content = [{"text": item, "type": "答疑", "bot_id": "9378"} for item in content if isinstance(item, str)]
                    changed = True
                elif isinstance(content[0], dict):
                    # 已是对象数组，不再为缺少 bot_id 的记录强制补默认机器人ID；空 bot_id 表示通用记录
                    pass
                if changed:
                    conn.execute(
                        "UPDATE knowledge_bases SET content = ? WHERE id = ?",
                        (json.dumps(content, ensure_ascii=False), row["id"])
                    )
                # 同步已有知识库内容中的 bot_id 到 bot_ids 表
                _sync_bot_ids_from_content(json.dumps(content, ensure_ascii=False))
            except (json.JSONDecodeError, TypeError):
                continue


def _cache_key(department: str, platform: str, scene: str) -> str:
    return f"{department}::{platform}::{scene}"


def _refresh_cache():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT department, platform, scene, version, content, variables, updated_at FROM prompts WHERE is_active=1"
        ).fetchall()
        _version_cache.clear()
        for row in rows:
            key = _cache_key(row["department"], row["platform"], row["scene"])
            _version_cache[key] = (row["version"], row["content"], row["variables"], row["updated_at"])


# ==================== CRUD ====================
def create_prompt(
        name: str,
        department: str,
        platform: str,
        scene: str,
        content: str,
        variables: str = "{}",
        variable_bindings: str = "{}",
        description: str = "",
        tags: str = ""
    ) -> dict:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO prompts (name, department, platform, scene, content, variables, variable_bindings, description, tags, is_active, version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?)""",
            (name, department, platform, scene, content, variables, variable_bindings, description, tags, now, now)
        )
        prompt_id = cursor.lastrowid
        conn.execute(
            "INSERT INTO prompt_versions (prompt_id, version, content, change_log, created_at) VALUES (?, 1, ?, '初始版本', ?)",
            (prompt_id, content, now)
        )
    _refresh_cache()
    return get_prompt_by_id(prompt_id)


def get_prompt_by_id(prompt_id: int) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
        return dict(row) if row else None


def get_prompt_by_name(name: str) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM prompts WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None


def get_prompt_by_key(department: str, platform: str, scene: str) -> Optional[dict]:
    """根据科室+平台+场景精确查找"""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM prompts WHERE department=? AND platform=? AND scene=? AND is_active=1",
            (department, platform, scene)
        ).fetchone()
        return dict(row) if row else None


def list_prompts(
        department: Optional[str] = None,
        platform: Optional[str] = None,
        scene: Optional[str] = None,
        keyword: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
        departments: Optional[List[str]] = None,
    ) -> Tuple[int, List[dict]]:
    conditions, params = [], []
    if departments is not None:
        # 普通用户：只看到自己被授权的科室
        if not departments:
            return 0, []
        placeholders = ",".join("?" * len(departments))
        conditions.append(f"department IN ({placeholders})")
        params.extend(departments)
    if department:
        conditions.append("department = ?")
        params.append(department)
    if platform:
        conditions.append("platform = ?")
        params.append(platform)
    if scene:
        conditions.append("scene = ?")
        params.append(scene)
    if keyword:
        conditions.append("(name LIKE ? OR description LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])

    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    with get_connection() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM prompts{where}", params).fetchone()[0]
        offset = (page - 1) * page_size
        rows = conn.execute(
            f"SELECT * FROM prompts{where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            params + [page_size, offset]
        ).fetchall()
    return total, [dict(r) for r in rows]


def update_prompt(
        prompt_id: int,
        content: Optional[str] = None,
        variables: Optional[str] = None,
        variable_bindings: Optional[str] = None,
        description: Optional[str] = None,
        tags: Optional[str] = None,
        change_log: str = "",
        force_version: bool = False
    ) -> Optional[dict]:
    prompt = get_prompt_by_id(prompt_id)
    if not prompt:
        return None

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_version = prompt["version"]
    content_changed = False

    fields, params = [], []
    if content is not None and (content != prompt["content"] or force_version):
        fields.append("content = ?")
        params.append(content)
        new_version = prompt["version"] + 1
        content_changed = True
    if variables is not None:
        fields.append("variables = ?")
        params.append(variables)
    if variable_bindings is not None:
        fields.append("variable_bindings = ?")
        params.append(variable_bindings)
    if description is not None:
        fields.append("description = ?")
        params.append(description)
    if tags is not None:
        fields.append("tags = ?")
        params.append(tags)

    if not fields:
        return prompt

    fields.append("version = ?")
    params.append(new_version)
    fields.append("updated_at = ?")
    params.append(now)
    params.append(prompt_id)

    with get_connection() as conn:
        conn.execute(f"UPDATE prompts SET {', '.join(fields)} WHERE id = ?", params)
        if content_changed:
            conn.execute(
                "INSERT INTO prompt_versions (prompt_id, version, content, change_log, created_at) VALUES (?, ?, ?, ?, ?)",
                (prompt_id, new_version, content, change_log or f"版本 {new_version}", now)
            )
    _refresh_cache()
    return get_prompt_by_id(prompt_id)


def delete_prompt(prompt_id: int) -> bool:
    prompt = get_prompt_by_id(prompt_id)
    if not prompt:
        return False
    with get_connection() as conn:
        conn.execute("UPDATE prompts SET is_active = 0 WHERE id = ?", (prompt_id,))
    _refresh_cache()
    return True


def get_prompt_versions(prompt_id: int) -> List[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM prompt_versions WHERE prompt_id = ? ORDER BY version DESC", (prompt_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def rollback_prompt(prompt_id: int, target_version: int) -> Optional[dict]:
    with get_connection() as conn:
        v = conn.execute(
            "SELECT * FROM prompt_versions WHERE prompt_id = ? AND version = ?",
            (prompt_id, target_version)
        ).fetchone()
        if not v:
            return None
    return update_prompt(prompt_id, content=v["content"], change_log=f"回滚到版本 {target_version}")


def delete_version(prompt_id: int, version_id: int) -> bool:
    """删除指定版本记录（不允许删除当前版本的记录）"""
    prompt = get_prompt_by_id(prompt_id)
    if not prompt:
        return False
    with get_connection() as conn:
        v = conn.execute(
            "SELECT * FROM prompt_versions WHERE id = ? AND prompt_id = ?",
            (version_id, prompt_id)
        ).fetchone()
        if not v:
            return False
        # 不允许删除当前版本对应的版本记录
        if v["version"] == prompt["version"]:
            return False
        conn.execute("DELETE FROM prompt_versions WHERE id = ?", (version_id,))
    return True


# ==================== 热更新 API ====================
def fetch_prompt(
        department: str, 
        platform: str, 
        scene: str,
        current_version: Optional[int] = None
    ) -> Optional[dict]:
    """
    供模型服务调用的获取接口（内存缓存）
    current_version 一致时返回 None
    """
    key = _cache_key(department, platform, scene)
    cached = _version_cache.get(key)
    if not cached:
        prompt = get_prompt_by_key(department, platform, scene)
        if not prompt:
            return None
        _refresh_cache()
        cached = _version_cache.get(key)
    if not cached:
        return None

    version, content, variables, updated_at = cached
    if current_version is not None and current_version == version:
        return None
    return {
        "department": department, "platform": platform, "scene": scene,
        "content": content, "variables": variables,
        "version": version, "updated_at": updated_at
    }


def fetch_prompt_by_name(name: str, current_version: Optional[int] = None) -> Optional[dict]:
    """按名称获取"""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT department, platform, scene, version, content, variables, updated_at FROM prompts WHERE name=? AND is_active=1",
            (name,)
        ).fetchone()
        if not row:
            return None
        if current_version is not None and current_version == row["version"]:
            return None
        return {
            "name": name, "department": row["department"], "platform": row["platform"],
            "scene": row["scene"], "content": row["content"], "variables": row["variables"],
            "version": row["version"], "updated_at": row["updated_at"]
        }

# def fetch_all_active() -> List[dict]:
#     return [
#         {"department": k.split("::")[0], "platform": k.split("::")[1], "scene": k.split("::")[2],
#          "version": v[0], "content": v[1], "variables": v[2], "updated_at": v[3]}
#         for k, v in _version_cache.items()
#     ]

def fetch_all_active() -> List[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT name, department, platform, scene, version, content, variables, updated_at "
            "FROM prompts WHERE is_active=1"
        ).fetchall()
    return [dict(r) for r in rows]


def get_cache_stats() -> dict:
    return {
        "cached_prompts": len(_version_cache),
        "keys": list(_version_cache.keys()),
        "cache_detail": {k: v for k, v in _version_cache.items()},
    }


# ==================== 变量解析引擎 ====================
# robot_id -> 公司映射
ROBOT_COMPANY_MAP = {
    "7422": "雍禾", "8714": "雍禾", "8686": "雍禾", "8542": "雍禾",
    "8771": "雍禾", "9125": "雍禾", "9378": "雍禾", "10122": "雍禾",
    "10569": "雍禾",
    "9352": "碧莲盛", "9358": "碧莲盛",
    "9682": "唐森",
}


def get_company(robot_id: str) -> str:
    return ROBOT_COMPANY_MAP.get(str(robot_id), "")


def resolve_prompt_variables(
    content: str,
    variables: str = "{}",
    variable_bindings: str = "{}",
    robot_id: str = "",
    department: str = "",
    current_round: int = 0,
    action_desc: str = "",
    knowledge_desc: str = "",
    warmup_desc: str = "",
    connect_desc: str = "",
    extra_variables: Optional[dict] = None,
) -> str:
    """
    解析提示词中的变量占位符

    优先级：variable_bindings 映射 > 内置变量 > 自定义变量
    """
    extra_variables = extra_variables or {}

    # 解析自定义变量
    try:
        custom_vars = json.loads(variables) if isinstance(variables, str) else variables
    except (json.JSONDecodeError, TypeError):
        custom_vars = {}

    # 解析 variable_bindings 映射
    try:
        bindings = json.loads(variable_bindings) if isinstance(variable_bindings, str) else variable_bindings
    except (json.JSONDecodeError, TypeError):
        bindings = {}

    now = datetime.now()
    domain_zh = DEPARTMENT_ZH.get(department, department)

    # 合并所有外部变量（extra_variables + 固定字段）
    all_extra = dict(extra_variables)
    if action_desc:
        all_extra.setdefault("action_desc", action_desc)
    if knowledge_desc:
        all_extra.setdefault("knowledge_desc", knowledge_desc)
    if warmup_desc:
        all_extra.setdefault("warmup_desc", warmup_desc)
    if connect_desc:
        all_extra.setdefault("connect_desc", connect_desc)

    resolved = content

    # 1. 应用 variable_bindings 映射（最高优先级）
    # 格式: {"变量名": "占位符标记", ...}
    for var_name, marker in bindings.items():
        value = all_extra.get(var_name)
        if value is not None and marker in resolved:
            resolved = resolved.replace(marker, str(value))

    # 2. 应用内置变量
    builtin_values = {
        "{公司}": get_company(robot_id),
        "{域中文}": domain_zh,
        "{域英文}": department,
        "{时间}": f"{now.year}年{now.month}月{now.day}日",
        "{轮次}": f"第{current_round}轮",
        "{轮次k}": str(current_round),
        "{套联描述}": connect_desc,
        "{动作描述}": action_desc,
        "{知识描述}": knowledge_desc,
        "{暖场描述}": warmup_desc,
    }
    for key, value in builtin_values.items():
        if key in resolved:
            resolved = resolved.replace(key, str(value))

    # 3. 应用自定义变量（直接替换）
    for key, value in custom_vars.items():
        placeholder = f"{{{key}}}" if not key.startswith("{") else key
        resolved = resolved.replace(placeholder, str(value))

    return resolved


# ==================== 机器人ID管理 ====================
def list_bot_ids() -> List[str]:
    """从数据库获取所有机器人ID列表"""
    with get_connection() as conn:
        rows = conn.execute("SELECT bot_id FROM bot_ids ORDER BY id").fetchall()
    return [r["bot_id"] for r in rows]


def add_bot_id(bot_id: str) -> None:
    """添加机器人ID到数据库（已存在则忽略）"""
    bot_id = str(bot_id).strip()
    if not bot_id:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO bot_ids (bot_id, created_at) VALUES (?, ?)",
            (bot_id, now)
        )


# ==================== 机器人配置（公司/科室/平台） ====================
def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def upsert_robot_config(bot_id: str, department: str, platform: str, company: str = "", enabled: bool = True) -> dict:
    bot_id = str(bot_id).strip()
    if not bot_id:
        raise ValueError("bot_id 不能为空")
    now = _now_str()
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO robot_configs (bot_id, department, platform, company, enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(bot_id) DO UPDATE SET
                 department=excluded.department,
                 platform=excluded.platform,
                 company=excluded.company,
                 enabled=excluded.enabled,
                 updated_at=excluded.updated_at""",
            (bot_id, department, platform, company, 1 if enabled else 0, now, now)
        )
    return get_robot_config(bot_id)  # type: ignore[return-value]


def get_robot_config(bot_id: str) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM robot_configs WHERE bot_id = ?", (bot_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["enabled"] = bool(d.get("enabled", 1))
        return d


def delete_robot_config(bot_id: str) -> bool:
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM robot_configs WHERE bot_id = ?", (bot_id,))
        return cur.rowcount > 0


def list_robot_configs(
    department: Optional[str] = None,
    platform: Optional[str] = None,
    enabled_only: bool = False,
) -> List[dict]:
    conditions, params = [], []
    if department:
        conditions.append("department = ?")
        params.append(department)
    if platform:
        conditions.append("platform = ?")
        params.append(platform)
    if enabled_only:
        conditions.append("enabled = 1")
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM robot_configs{where} ORDER BY bot_id", params
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["enabled"] = bool(d.get("enabled", 1))
        result.append(d)
    return result


def _ensure_bot_ids_seeded():
    """用默认 BOT_IDS 常量初始化 bot_ids 表（仅在表为空时）"""
    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM bot_ids").fetchone()[0]
    if count == 0:
        for bid in BOT_IDS:
            add_bot_id(bid)


def _sync_bot_ids_from_content(content: str):
    """从知识库内容JSON中提取所有 bot_id，将新的存入数据库"""
    try:
        items = json.loads(content) if isinstance(content, str) else content
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, dict) and item.get("bot_id"):
                add_bot_id(str(item["bot_id"]))
    except (json.JSONDecodeError, TypeError):
        pass


# ==================== 知识库 CRUD ====================
def create_knowledge_base(department: str, platform: str, content: str = "[]") -> dict:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO knowledge_bases (department, platform, content, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(department, platform) DO UPDATE SET
               content=excluded.content, updated_at=excluded.updated_at""",
            (department, platform, content, now)
        )
    _sync_bot_ids_from_content(content)
    return get_kb_by_key(department, platform)


def get_kb_by_id(kb_id: int) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM knowledge_bases WHERE id = ?", (kb_id,)).fetchone()
        return dict(row) if row else None


def get_kb_by_key(department: str, platform: str) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM knowledge_bases WHERE department = ? AND platform = ?",
            (department, platform)
        ).fetchone()
        return dict(row) if row else None


def list_knowledge_bases(
        department: Optional[str] = None,
        platform: Optional[str] = None,
        keyword: Optional[str] = None,
        departments: Optional[List[str]] = None,
    ) -> Tuple[int, List[dict]]:
    conditions, params = [], []
    if departments is not None:
        if not departments:
            return 0, []
        placeholders = ",".join("?" * len(departments))
        conditions.append(f"department IN ({placeholders})")
        params.extend(departments)
    if department:
        conditions.append("department = ?")
        params.append(department)
    if platform:
        conditions.append("platform = ?")
        params.append(platform)
    if keyword:
        conditions.append("content LIKE ?")
        params.append(f"%{keyword}%")

    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    with get_connection() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM knowledge_bases{where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM knowledge_bases{where} ORDER BY updated_at DESC",
            params
        ).fetchall()
    return total, [dict(r) for r in rows]


def update_knowledge_base(kb_id: int, content: str) -> Optional[dict]:
    kb = get_kb_by_id(kb_id)
    if not kb:
        return None
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        conn.execute(
            "UPDATE knowledge_bases SET content = ?, updated_at = ? WHERE id = ?",
            (content, now, kb_id)
        )
    _sync_bot_ids_from_content(content)
    return get_kb_by_id(kb_id)


def delete_knowledge_base(kb_id: int) -> bool:
    kb = get_kb_by_id(kb_id)
    if not kb:
        return False
    with get_connection() as conn:
        conn.execute("DELETE FROM knowledge_bases WHERE id = ?", (kb_id,))
    return True


# ==================== 流程树 CRUD ====================
def _flow_record_count(conn, flow_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM flow_records WHERE flow_id = ?",
        (flow_id,)
    ).fetchone()
    return row["c"] if row else 0


def create_flow_tree(department: str, platform: str, description: str = "") -> dict:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO flow_trees (department, platform, description, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(department, platform) DO UPDATE SET
               description=excluded.description, updated_at=excluded.updated_at""",
            (department, platform, description, now)
        )
    return get_flow_tree_by_key(department, platform)


def get_flow_tree_by_id(flow_id: int) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM flow_trees WHERE id = ?", (flow_id,)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["record_count"] = _flow_record_count(conn, flow_id)
        return result


def get_flow_tree_by_key(department: str, platform: str) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM flow_trees WHERE department = ? AND platform = ?",
            (department, platform)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["record_count"] = _flow_record_count(conn, row["id"])
        return result


def list_flow_trees(
        department: Optional[str] = None,
        platform: Optional[str] = None,
        keyword: Optional[str] = None,
        bot_id: Optional[str] = None,
        departments: Optional[List[str]] = None,
    ) -> Tuple[int, List[dict]]:
    conditions, params = [], []
    if departments is not None:
        if not departments:
            return 0, []
        placeholders = ",".join("?" * len(departments))
        conditions.append(f"ft.department IN ({placeholders})")
        params.extend(departments)
    if department and not keyword:
        conditions.append("ft.department = ?")
        params.append(department)
    if platform and not keyword:
        conditions.append("ft.platform = ?")
        params.append(platform)
    if keyword:
        conditions.append("EXISTS (SELECT 1 FROM flow_records frs WHERE frs.flow_id = ft.id AND (frs.file_name LIKE ? OR frs.description LIKE ? OR frs.structure LIKE ? OR frs.bot_id LIKE ?))")
        kw = f"%{keyword}%"
        params.extend([kw, kw, kw, kw])
    if bot_id is not None:
        bot_conditions = []
        bot_params = []
        _flow_bot_condition("frb", bot_id, bot_conditions, bot_params)
        if bot_conditions:
            conditions.append(f"EXISTS (SELECT 1 FROM flow_records frb WHERE frb.flow_id = ft.id AND {' AND '.join(bot_conditions)})")
            params.extend(bot_params)

    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    with get_connection() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM flow_trees ft{where}", params
        ).fetchone()[0]
        # JOIN 一次性拿到 record_count，避免 N+1
        rows = conn.execute(
            f"""SELECT ft.*, COALESCE(rc.cnt, 0) AS record_count
                FROM flow_trees ft
                LEFT JOIN (
                    SELECT flow_id, COUNT(*) AS cnt FROM flow_records GROUP BY flow_id
                ) rc ON rc.flow_id = ft.id
                {where}
                ORDER BY ft.updated_at DESC""",
            params
        ).fetchall()
        items = [dict(r) for r in rows]
    return total, items


def update_flow_tree(flow_id: int, description: Optional[str] = None) -> Optional[dict]:
    tree = get_flow_tree_by_id(flow_id)
    if not tree:
        return None
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fields, params = [], []
    if description is not None:
        fields.append("description = ?")
        params.append(description)
    if not fields:
        return tree
    fields.append("updated_at = ?")
    params.append(now)
    params.append(flow_id)
    with get_connection() as conn:
        conn.execute(f"UPDATE flow_trees SET {', '.join(fields)} WHERE id = ?", params)
    return get_flow_tree_by_id(flow_id)


def delete_flow_tree(flow_id: int) -> bool:
    tree = get_flow_tree_by_id(flow_id)
    if not tree:
        return False
    with get_connection() as conn:
        conn.execute("DELETE FROM flow_records WHERE flow_id = ?", (flow_id,))
        conn.execute("DELETE FROM flow_trees WHERE id = ?", (flow_id,))
    return True


# ==================== 流程树记录 CRUD ====================
def create_flow_record(
        flow_id: int,
        file_name: str,
        file_type: str,
        file_path: str,
        description: str = "",
        structure: str = "{}",
        status: str = "pending",
        error: str = "",
        bot_id: str = ""
    ) -> dict:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bot_id = str(bot_id or "").strip()
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO flow_records (flow_id, file_name, file_type, file_path, description, structure, status, error, bot_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (flow_id, file_name, file_type, file_path, description, structure, status, error, bot_id, now)
        )
        rec_id = cursor.lastrowid
        # 同步更新 flow_trees.updated_at
        conn.execute("UPDATE flow_trees SET updated_at = ? WHERE id = ?", (now, flow_id))
    add_bot_id(bot_id)
    return get_flow_record_by_id(rec_id)


def get_flow_record_by_id(record_id: int) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM flow_records WHERE id = ?", (record_id,)
        ).fetchone()
        return dict(row) if row else None


def _flow_bot_condition(alias: str, bot_id: Optional[str], conditions: list, params: list) -> None:
    if bot_id is None:
        return
    bot_id = str(bot_id).strip()
    if bot_id == "__common__":
        conditions.append(f"({alias}.bot_id IS NULL OR {alias}.bot_id = '')")
    elif bot_id:
        conditions.append(f"({alias}.bot_id IS NULL OR {alias}.bot_id = '' OR {alias}.bot_id = ?)")
        params.append(bot_id)


def list_flow_records(flow_id: int, bot_id: Optional[str] = None) -> Tuple[int, List[dict]]:
    conditions, params = ["flow_id = ?"], [flow_id]
    _flow_bot_condition("flow_records", bot_id, conditions, params)
    where = " WHERE " + " AND ".join(conditions)
    with get_connection() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM flow_records{where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM flow_records{where} ORDER BY created_at DESC",
            params
        ).fetchall()
    return total, [dict(r) for r in rows]


def update_flow_record(
        record_id: int,
        description: Optional[str] = None,
        structure: Optional[str] = None,
        status: Optional[str] = None,
        error: Optional[str] = None,
        bot_id: Optional[str] = None
    ) -> Optional[dict]:
    rec = get_flow_record_by_id(record_id)
    if not rec:
        return None
    fields, params = [], []
    if description is not None:
        fields.append("description = ?")
        params.append(description)
    if structure is not None:
        fields.append("structure = ?")
        params.append(structure)
    if status is not None:
        fields.append("status = ?")
        params.append(status)
    if error is not None:
        fields.append("error = ?")
        params.append(error)
    if bot_id is not None:
        bot_id = str(bot_id or "").strip()
        fields.append("bot_id = ?")
        params.append(bot_id)
        add_bot_id(bot_id)
    if not fields:
        return rec
    params.append(record_id)
    with get_connection() as conn:
        conn.execute(f"UPDATE flow_records SET {', '.join(fields)} WHERE id = ?", params)
    return get_flow_record_by_id(record_id)


def delete_flow_record(record_id: int) -> bool:
    rec = get_flow_record_by_id(record_id)
    if not rec:
        return False
    with get_connection() as conn:
        conn.execute("DELETE FROM flow_records WHERE id = ?", (record_id,))
    return True


def search_flow_records(
        department: Optional[str] = None,
        platform: Optional[str] = None,
        keyword: Optional[str] = None,
        bot_id: Optional[str] = None
    ) -> Tuple[int, List[dict]]:
    """跨流程树库搜索所有记录，支持按科室/平台/关键词/机器人ID过滤"""
    conditions, params = [], []
    if department:
        conditions.append("ft.department = ?")
        params.append(department)
    if platform:
        conditions.append("ft.platform = ?")
        params.append(platform)
    if keyword:
        conditions.append(
            "(fr.file_name LIKE ? OR fr.description LIKE ? OR fr.structure LIKE ? OR fr.bot_id LIKE ?)"
        )
        kw = f"%{keyword}%"
        params.extend([kw, kw, kw, kw])
    _flow_bot_condition("fr", bot_id, conditions, params)

    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    with get_connection() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM flow_records fr "
            f"JOIN flow_trees ft ON fr.flow_id = ft.id{where}",
            params
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT fr.*, ft.department, ft.platform, ft.description as ft_description "
            f"FROM flow_records fr "
            f"JOIN flow_trees ft ON fr.flow_id = ft.id{where} "
            f"ORDER BY fr.created_at DESC",
            params
        ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        items.append(d)
    return total, items


# ==================== Settings (LLM API config) ====================

def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=datetime('now', 'localtime')",
            (key, value)
        )


def get_llm_config() -> dict:
    """获取当前激活的 LLM 配置"""
    active_id = get_setting("llm_active_version_id")
    if active_id:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT base_url, api_key, model_name FROM llm_versions WHERE id = ?",
                (int(active_id),)
            ).fetchone()
            if row:
                return {"base_url": row["base_url"], "api_key": row["api_key"], "model_name": row["model_name"]}
    # 回退到旧格式
    return {
        "base_url": get_setting("llm_base_url", "") or "",
        "api_key": get_setting("llm_api_key", "") or "",
        "model_name": get_setting("llm_model_name", "") or "",
    }


def set_llm_config(base_url: str, api_key: str, model_name: str) -> dict:
    """兼容旧接口：直接设置当前配置"""
    set_setting("llm_base_url", base_url or "")
    set_setting("llm_api_key", api_key or "")
    set_setting("llm_model_name", model_name or "")
    return get_llm_config()


# ==================== LLM 多版本管理 ====================

def _ensure_llm_versions_table():
    """确保 llm_versions 表存在（幂等）"""
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS llm_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL DEFAULT '',
                base_url TEXT NOT NULL DEFAULT '',
                api_key TEXT NOT NULL DEFAULT '',
                model_name TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
        """)
        # 迁移：如果旧设置存在但 llm_versions 为空，自动导入
        count = conn.execute("SELECT COUNT(*) FROM llm_versions").fetchone()[0]
        if count == 0:
            old_base = get_setting("llm_base_url", "")
            old_key = get_setting("llm_api_key", "")
            old_model = get_setting("llm_model_name", "")
            if old_base or old_model:
                conn.execute(
                    "INSERT INTO llm_versions (name, base_url, api_key, model_name, is_active) VALUES (?, ?, ?, ?, 1)",
                    ("默认版本", old_base, old_key, old_model)
                )


def list_llm_versions() -> List[dict]:
    _ensure_llm_versions_table()
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM llm_versions ORDER BY is_active DESC, updated_at DESC").fetchall()
        return [dict(r) for r in rows]


def get_llm_version(version_id: int) -> Optional[dict]:
    _ensure_llm_versions_table()
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM llm_versions WHERE id = ?", (version_id,)).fetchone()
        return dict(row) if row else None


def create_llm_version(name: str, base_url: str, api_key: str, model_name: str) -> dict:
    _ensure_llm_versions_table()
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO llm_versions (name, base_url, api_key, model_name) VALUES (?, ?, ?, ?)",
            (name, base_url, api_key, model_name)
        )
        vid = cursor.lastrowid
    return get_llm_version(vid)


def update_llm_version(version_id: int, name: str = None, base_url: str = None,
                        api_key: str = None, model_name: str = None) -> Optional[dict]:
    _ensure_llm_versions_table()
    ver = get_llm_version(version_id)
    if not ver:
        return None
    fields, params = [], []
    if name is not None:
        fields.append("name = ?")
        params.append(name)
    if base_url is not None:
        fields.append("base_url = ?")
        params.append(base_url)
    if api_key is not None:
        fields.append("api_key = ?")
        params.append(api_key)
    if model_name is not None:
        fields.append("model_name = ?")
        params.append(model_name)
    if not fields:
        return ver
    fields.append("updated_at = datetime('now', 'localtime')")
    params.append(version_id)
    with get_connection() as conn:
        conn.execute(f"UPDATE llm_versions SET {', '.join(fields)} WHERE id = ?", params)
    return get_llm_version(version_id)


def activate_llm_version(version_id: int) -> Optional[dict]:
    """激活指定版本，同时将旧设置同步更新"""
    _ensure_llm_versions_table()
    ver = get_llm_version(version_id)
    if not ver:
        return None
    with get_connection() as conn:
        conn.execute("UPDATE llm_versions SET is_active = 0")
        conn.execute("UPDATE llm_versions SET is_active = 1, updated_at = datetime('now', 'localtime') WHERE id = ?", (version_id,))
    # 同步到旧格式（兼容）
    set_setting("llm_base_url", ver["base_url"])
    set_setting("llm_api_key", ver["api_key"])
    set_setting("llm_model_name", ver["model_name"])
    return get_llm_version(version_id)


def delete_llm_version(version_id: int) -> bool:
    _ensure_llm_versions_table()
    ver = get_llm_version(version_id)
    if not ver:
        return False
    if ver["is_active"]:
        return False  # 不允许删除激活版本
    with get_connection() as conn:
        conn.execute("DELETE FROM llm_versions WHERE id = ?", (version_id,))
    return True
