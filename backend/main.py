#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
提示词管理系统 - FastAPI 主应用

增强：
1. 科室(口腔/植发/皮肤等) + 平台(百度/小红书/抖音等) + 场景 三维管理
2. 变量占位符引擎（{公司}、{域中文}、{时间}等自动解析）
3. 解析API：直接返回填充好变量的提示词
4. 版本管理与回滚
5. WebSocket 实时通知
6. 知识库管理
"""

import os
import sys
import json
from typing import Optional
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, UploadFile, File, Form, Header, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, Response, JSONResponse
from contextlib import asynccontextmanager

# 将 agent 目录加入 sys.path，以复用统一的流程树解析实现
_AGENT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "agent"))
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)
# 让解析服务把上传文件放在 backend/flow_data，保持原有目录习惯
os.environ.setdefault(
    "FLOW_DATA_DIR",
    os.path.join(os.path.dirname(__file__), "flow_data"),
)

from models import (
    LoginRequest, LoginResponse, UserCreate, UserUpdate, UserResponse,
    PromptCreate, PromptUpdate, PromptResponse, PromptVersionResponse,
    PromptListResponse, RollbackRequest, FetchResponse, FetchByNameResponse,
    ResolveRequest, ResolveResponse,
    KnowledgeBaseCreate, KnowledgeBaseUpdate, KnowledgeBaseResponse, KnowledgeBaseListResponse,
    FlowTreeCreate, FlowTreeUpdate, FlowTreeResponse, FlowTreeListResponse,
    FlowRecordResponse, FlowRecordListResponse, FlowRecordUpdate,
    LLMConfig, LLMVersionCreate, LLMVersionUpdate, LLMVersionResponse
)
from database import (
    init_db, create_prompt, get_prompt_by_id, get_prompt_by_name,
    list_prompts, update_prompt, delete_prompt,
    get_prompt_versions, rollback_prompt, delete_version,
    fetch_prompt, fetch_prompt_by_name,
    fetch_all_active, get_cache_stats, resolve_prompt_variables,
    _refresh_cache, DB_PATH,
    DEPARTMENTS, PLATFORMS, SCENES, DEPARTMENT_ZH, PLATFORM_ZH, SCENE_ZH,
    BUILTIN_VARIABLES, KB_TYPES, BOT_IDS,
    list_bot_ids, add_bot_id,
    create_knowledge_base, get_kb_by_id, get_kb_by_key,
    list_knowledge_bases, update_knowledge_base, delete_knowledge_base,
    create_flow_tree, get_flow_tree_by_id, get_flow_tree_by_key,
    list_flow_trees, update_flow_tree, delete_flow_tree,
    create_flow_record, get_flow_record_by_id, list_flow_records,
    update_flow_record, delete_flow_record, search_flow_records,
    authenticate_user, create_session, get_user_by_token, delete_session,
    list_users, create_user, update_user, delete_user, user_has_permission,
    get_llm_config, set_llm_config,
    list_llm_versions, get_llm_version, create_llm_version,
    update_llm_version, activate_llm_version, delete_llm_version
)
import flow_parser_service


class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        disconnected = []
        for conn in self.active_connections:
            try:
                await conn.send_json(message)
            except Exception:
                disconnected.append(conn)
        for c in disconnected:
            self.disconnect(c)


ws_manager = ConnectionManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _init_sample_data()
    yield


app = FastAPI(title="提示词管理系统", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

PUBLIC_API_PREFIXES = (
    "/api/auth/login", "/api/auth/heartbeat", "/api/v1/", "/api/meta/", "/api/settings/",
    "/docs", "/openapi.json", "/redoc", "/favicon.ico"
)


@app.middleware("http")
async def auth_middleware(request, call_next):
    path = request.url.path
    if path == "/" or path.startswith(PUBLIC_API_PREFIXES) or path.startswith("/ws/"):
        return await call_next(request)
    if path.startswith("/api/"):
        auth = request.headers.get("Authorization", "")
        token = auth.split(" ", 1)[1].strip() if auth.startswith("Bearer ") else request.cookies.get("pm_token", "")
        if not token or not get_user_by_token(token):
            return JSONResponse({"detail": "未登录或登录已过期"}, status_code=401)
    return await call_next(request)


FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")


def _schedule_parse(record_id: int, file_path: str, file_type: str, file_name: str):
    """在后台线程中执行流程树解析，不阻塞当前请求。"""
    import asyncio

    def _do():
        try:
            description, structure, status, error = flow_parser_service.parse_file(
                file_path, file_type, file_name
            )
            update_flow_record(
                record_id, description=description, structure=structure,
                status=status, error=error
            )
        except Exception as e:
            update_flow_record(record_id, status="failed", error=str(e))

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _do)


def get_current_user(request: Request, authorization: Optional[str] = Header(None)) -> dict:
    token = ""
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1].strip()
    else:
        token = request.cookies.get("pm_token", "")
    if not token:
        raise HTTPException(401, "未登录或登录已过期")
    user = get_user_by_token(token)
    if not user:
        raise HTTPException(401, "未登录或登录已过期")
    return user


def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    if current_user.get("role") != "admin":
        raise HTTPException(403, "需要高级权限")
    return current_user


def require_permission(permission: str, message: str, current_user: dict = Depends(get_current_user)) -> dict:
    if not user_has_permission(current_user["id"], permission):
        raise HTTPException(403, message)
    return current_user


def require_prompt_permission(permission: str, current_user: dict = Depends(get_current_user)) -> dict:
    return require_permission(permission, "没有提示词管理权限", current_user)


def require_prompt_view(current_user: dict = Depends(get_current_user)) -> dict:
    return require_prompt_permission("can_prompt_view", current_user)


def require_prompt_edit(current_user: dict = Depends(get_current_user)) -> dict:
    return require_prompt_permission("can_prompt_edit", current_user)


def require_prompt_delete(current_user: dict = Depends(get_current_user)) -> dict:
    return require_prompt_permission("can_prompt_delete", current_user)


def require_knowledge_view(current_user: dict = Depends(get_current_user)) -> dict:
    return require_permission("can_knowledge_view", "没有知识库管理权限", current_user)


def require_knowledge_edit(current_user: dict = Depends(get_current_user)) -> dict:
    return require_permission("can_knowledge_edit", "没有知识库编辑权限", current_user)


def require_knowledge_delete(current_user: dict = Depends(get_current_user)) -> dict:
    return require_permission("can_knowledge_delete", "没有知识库删除权限", current_user)


def require_flow_view(current_user: dict = Depends(get_current_user)) -> dict:
    return require_permission("can_flow_view", "没有流程树管理权限", current_user)


def require_flow_edit(current_user: dict = Depends(get_current_user)) -> dict:
    return require_permission("can_flow_edit", "没有流程树编辑权限", current_user)


def require_flow_delete(current_user: dict = Depends(get_current_user)) -> dict:
    return require_permission("can_flow_delete", "没有流程树删除权限", current_user)


def _validate_role(role: str) -> str:
    if role not in ("admin", "normal"):
        raise HTTPException(400, "权限等级只能是 admin 或 normal")
    return role


def _to_response(p: dict) -> PromptResponse:
    return PromptResponse(
        id=p["id"],
        name=p["name"],
        department=p["department"],
        platform=p["platform"],
        scene=p["scene"],
        content=p["content"],
        variables=p["variables"],
        variable_bindings=p.get("variable_bindings", "{}"),
        description=p["description"],
        tags=p["tags"],
        is_active=bool(p["is_active"]),
        version=p["version"],
        created_at=p["created_at"],
        updated_at=p["updated_at"]
    )


def _init_sample_data():
    """初始化示例提示词数据"""
    samples = [
        {
            "name": "hair_xhs_system",
            "department": "hair", "platform": "xhs", "scene": "system_prompt",
            "content": """# 角色与目标
角色：{公司}{域中文}负责在线咨询的植发专家
核心任务：结合对话历史充分理解访客诉求（表达的意思） → 逻辑问诊和答疑 → 促成留联（联系方式/微信）
当前时间：{时间}
对话轮次计算：访客与客服交替发言1次为1轮，当前轮次为{轮次}

# 核心原则
风格：生硬、冷漠、简短、专业、直接，避免任何客套、营销和机器感
目标：推进留联，从对话第2轮开始，策略性地穿插询问联系方式
不接诊范围：脱毛、腋毛、拉直头发、访客14岁以下等方面 {动作描述}
禁止：
    ❌客套词：严禁使用"请问"、"请"、"方便吗"、"谢谢"、"您好"、"方便的话"等
    ❌确认词：严禁使用"了解了"、"明白啦"、"明白了"、"好的"等
    ❌自我说明：严禁说"我是AI"、"我是机器人"
    ❌解释表述：严禁解释问诊原因以及答疑和套联的原因

{套联描述}

# 回复结构
1.对话第1轮：仅能使用【答疑、问诊】或【答疑】
2.对话第2轮及以后：从第2轮起，每间隔1轮仅能使用【套联】或【答疑、套联】，其他轮次可在【问诊】，【答疑】，【答疑、问诊】中选择
3.套联轮次不得问诊。访客未说话时，默认使用【问诊】
4.不同类型表述之间用"<sep>"连接 {知识描述} {暖场描述}""",
            "variables": "{}",
            "variable_bindings": "{}",
            "description": "植发科小红书平台系统提示词",
            "tags": "植发,小红书,系统提示词"
        },
        {
            "name": "hair_bd_system",
            "department": "hair", "platform": "bd", "scene": "system_prompt",
            "content": """# 角色与目标
角色：{公司}{域中文}负责在线咨询的植发专家
核心任务：结合对话历史充分理解访客诉求（表达的意思） → 逻辑问诊和答疑 → 促成留联（联系方式/微信）
当前时间：{时间}
对话轮次计算：访客与客服交替发言1次为1轮，当前轮次为{轮次}

# 核心原则
风格：生硬、冷漠、简短、专业、直接
目标：推进留联，从对话第3轮开始，策略性地穿插询问联系方式
不接诊范围：脱毛、腋毛、拉直头发、访客14岁以下等方面 {动作描述}
禁止：
    ❌客套词、确认词、自我说明、解释表述
    ❌违禁词：严禁提及具体价格、药品名、医生名和医疗化词语

{套联描述}

# 回复结构
1.对话第1-3轮：仅能使用【答疑、问诊】或【答疑】
2.对话第3轮及以后：从第3轮起，每间隔1轮仅能使用【套联】或【答疑、套联】
3.套联轮次不得问诊
4.不同类型表述之间用"<sep>"连接 {知识描述} {暖场描述}""",
            "variables": "{}",
            "variable_bindings": "{}",
            "description": "植发科百度平台系统提示词",
            "tags": "植发,百度,系统提示词"
        },
        {
            "name": "dentistry_bd_system",
            "department": "dentistry", "platform": "bd", "scene": "system_prompt",
            "content": """你是一名{公司}{域中文}负责在线医疗咨询的医生，当前时间为{时间}。任务是根据已有对话历史（访客和医生相互交替发言1次算1轮），遵循回复策略给出接下来{轮次}医生的简短回复。
## 核心目标
你在对话过程中充分理解对话历史中的"病症"、"诉求"、"部位"的信息，通过简短的问诊和答疑推进对话，问答逻辑严谨，从对话第3轮起开始套联（询问访客联系方式），最终促成访客留联。
## 回复规范
**通用规范**：
    1.访客质疑项目风险，可以转向优势说明，并且不要主动解释"术后风险"。
    2.禁止直接回答用药相关，禁止提及药品名。 {知识描述}
**语气风格规范**
    1.使用生硬、简短、简洁、冷漠的口语化表达。
    2.禁止使用"您"、"请"、"谢谢"、"请问"等客气和书面化营销感的措辞。
**问诊规范**：禁止开放式、反问式、引导式的问诊，禁止重复问已问或已答的内容。
**答疑规范**：禁止提及价格、费用、药品名等违禁表述。
**套联规范**：从第3轮起，每间隔1轮套联一次。{套联描述}
**回复结构**：仅限【答疑、问诊】；【答疑、套联】；【问诊】；【套联】四种，用"<sep>"连接。 {动作描述}""",
            "variables": "{}",
            "variable_bindings": "{}",
            "description": "口腔科百度平台系统提示词",
            "tags": "口腔,百度,系统提示词"
        },
        {
            "name": "dermatology_bd_system",
            "department": "dermatology", "platform": "bd", "scene": "system_prompt",
            "content": """你是一名{公司}{域中文}负责在线医疗咨询的医生客服，当前时间为{时间}。任务是根据已有对话历史（访客和医生客服交替发言1次算1轮），遵循回复策略给出接下来{轮次}医生客服的回复。
## 核心目标
你在对话过程中充分理解对话历史中访客的"病症"、"诉求"、"部位"等信息，通过逻辑严谨的问诊和答疑推进对话，从第3轮起开始套联（询问访客联系方式），最终促成访客留联。
## 回复规范
**语气风格规范**：使用生硬、简短、冷漠、直接的口语化表达。禁止客套和营销感表述。
**问诊规范**：禁止开放式、反问式、引导式的问诊，禁止重复问已问或已答的内容。
**答疑规范**：禁止提及具体价格费用、药品名等违禁内容。
**套联规范**：从第3轮起，每间隔1轮套联一次。{套联描述}
**回复结构**：仅限【答疑、问诊】；【答疑、套联】；【问诊】；【套联】四种，用"<sep>"连接。 {知识描述} {动作描述}""",
            "variables": "{}",
            "variable_bindings": "{}",
            "description": "皮肤科百度平台系统提示词",
            "tags": "皮肤,百度,系统提示词"
        },
        {
            "name": "default_system",
            "department": "general", "platform": "general", "scene": "system_prompt",
            "content": """你是一个具备专业的医疗知识的{域中文}医生客服，场景是{域中文}在线咨询，当前时间为{时间}。
客服通过问诊（询问症状/病史）和答疑（解释病情/治疗）推进对话，最终目标是套联（获取访客联系方式）。对话历史包括访客和医生的交替发言，相互交替发言一次算1轮。
您的任务是根据对当前对话历史的信息识别和访客诉求的理解，给出接下来客服{轮次}的回复，尽量简短一些，保持生硬、简洁干脆、冷漠的口语化表达，并且回复的分句用"<sep>"连接。 {知识描述} {动作描述}""",
            "variables": "{}",
            "variable_bindings": "{}",
            "description": "兜底默认系统提示词",
            "tags": "通用,默认,系统提示词"
        },
    ]
    for s in samples:
        existing = get_prompt_by_name(s["name"])
        if not existing:
            create_prompt(**s)
        # 已存在的提示词保持当前最新版本，不做任何更新


# ==================== 页面 ====================

@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(html_path):
        return FileResponse(html_path)
    return HTMLResponse("<h1>提示词管理系统</h1>")


# ==================== 登录与账户权限 ====================

@app.get("/api/auth/heartbeat")
async def api_auth_heartbeat():
    return {"status": "ok"}


@app.post("/api/auth/login", response_model=LoginResponse)
async def api_login(data: LoginRequest):
    user = authenticate_user(data.username, data.password)
    if not user:
        raise HTTPException(401, "用户名或密码错误")
    session = create_session(user["id"])
    return LoginResponse(token=session["token"], expires_at=session["expires_at"], user=UserResponse(**user))


@app.get("/api/auth/me", response_model=UserResponse)
async def api_me(current_user: dict = Depends(get_current_user)):
    return UserResponse(**current_user)


@app.post("/api/auth/logout")
async def api_logout(authorization: Optional[str] = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        delete_session(authorization.split(" ", 1)[1].strip())
    return {"message": "已退出登录"}


@app.get("/api/users", response_model=list[UserResponse])
async def api_users(_: dict = Depends(require_admin)):
    return [UserResponse(**u) for u in list_users()]


@app.post("/api/users", response_model=UserResponse)
async def api_user_create(data: UserCreate, _: dict = Depends(require_admin)):
    role = _validate_role(data.role)
    try:
        user = create_user(
            data.username, data.password, role,
            data.can_prompt_view, data.can_prompt_edit, data.can_prompt_delete,
            data.can_knowledge_view, data.can_knowledge_edit, data.can_knowledge_delete,
            data.can_flow_view, data.can_flow_edit, data.can_flow_delete
        )
    except Exception as e:
        if "UNIQUE" in str(e).upper():
            raise HTTPException(400, "用户名已存在")
        raise
    return UserResponse(**user)


@app.put("/api/users/{user_id}", response_model=UserResponse)
async def api_user_update(user_id: int, data: UserUpdate, current_user: dict = Depends(require_admin)):
    role = _validate_role(data.role) if data.role is not None else None
    if user_id == current_user["id"] and data.is_active is False:
        raise HTTPException(400, "不能禁用当前登录账号")
    user = update_user(
        user_id,
        password=data.password,
        role=role,
        can_prompt_view=data.can_prompt_view,
        can_prompt_edit=data.can_prompt_edit,
        can_prompt_delete=data.can_prompt_delete,
        can_knowledge_view=data.can_knowledge_view,
        can_knowledge_edit=data.can_knowledge_edit,
        can_knowledge_delete=data.can_knowledge_delete,
        can_flow_view=data.can_flow_view,
        can_flow_edit=data.can_flow_edit,
        can_flow_delete=data.can_flow_delete,
        is_active=data.is_active
    )
    if not user:
        raise HTTPException(404, "账号不存在")
    return UserResponse(**user)


@app.delete("/api/users/{user_id}")
async def api_user_delete(user_id: int, current_user: dict = Depends(require_admin)):
    if user_id == current_user["id"]:
        raise HTTPException(400, "不能删除当前登录账号")
    if not delete_user(user_id):
        raise HTTPException(400, "无法删除该账号")
    return {"message": "删除成功"}


# ==================== 元数据接口 ====================

@app.get("/api/meta/departments")
async def api_departments():
    return {"departments": [{"key": k, "label": DEPARTMENT_ZH[k]} for k in DEPARTMENTS]}


@app.get("/api/meta/platforms")
async def api_platforms():
    return {"platforms": [{"key": k, "label": PLATFORM_ZH[k]} for k in PLATFORMS]}


@app.get("/api/meta/scenes")
async def api_scenes():
    return {"scenes": [{"key": k, "label": SCENE_ZH[k]} for k in SCENES]}


@app.get("/api/meta/variables")
async def api_variables():
    return {"variables": BUILTIN_VARIABLES}


@app.get("/api/meta/kb_types")
async def api_kb_types():
    return {"kb_types": KB_TYPES}


@app.get("/api/meta/bot_ids")
async def api_bot_ids():
    return {"bot_ids": list_bot_ids()}


@app.post("/api/meta/bot_ids")
async def api_add_bot_id(data: dict, _: dict = Depends(require_knowledge_edit)):
    """显式添加机器人ID到数据库"""
    bot_id = str(data.get("bot_id", "")).strip()
    if not bot_id:
        raise HTTPException(400, "bot_id 不能为空")
    add_bot_id(bot_id)
    return {"bot_ids": list_bot_ids()}


# ==================== 管理接口 ====================

@app.post("/api/prompts", response_model=PromptResponse)
async def api_create(data: PromptCreate, _: dict = Depends(require_prompt_edit)):
    existing = get_prompt_by_name(data.name)
    if existing:
        raise HTTPException(400, f"名称 '{data.name}' 已存在")
    p = create_prompt(
        name=data.name,
        department=data.department,
        platform=data.platform,
        scene=data.scene,
        content=data.content,
        variables=data.variables,
        variable_bindings=data.variable_bindings,
        description=data.description,
        tags=data.tags
    )
    await ws_manager.broadcast({"event": "created", "name": p["name"], "version": p["version"]})
    return _to_response(p)


@app.get("/api/prompts", response_model=PromptListResponse)
async def api_list(
    department: Optional[str] = None, platform: Optional[str] = None,
    scene: Optional[str] = None, keyword: Optional[str] = None,
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    _: dict = Depends(require_prompt_view)
):
    total, items = list_prompts(
        department=department, 
        platform=platform,
        scene=scene, 
        keyword=keyword, 
        page=page, 
        page_size=page_size
    )
    return PromptListResponse(total=total, items=[_to_response(i) for i in items])


@app.get("/api/prompts/{prompt_id}", response_model=PromptResponse)
async def api_get(prompt_id: int, _: dict = Depends(require_prompt_view)):
    p = get_prompt_by_id(prompt_id)
    if not p:
        raise HTTPException(404, "不存在")
    return _to_response(p)


@app.put("/api/prompts/{prompt_id}", response_model=PromptResponse)
async def api_update(prompt_id: int, data: PromptUpdate, _: dict = Depends(require_prompt_edit)):
    p = update_prompt(prompt_id, content=data.content, variables=data.variables,
                      variable_bindings=data.variable_bindings,
                      description=data.description, tags=data.tags, change_log=data.change_log)
    if not p:
        raise HTTPException(404, "不存在")
    await ws_manager.broadcast({"event": "updated", "name": p["name"], "version": p["version"]})
    return _to_response(p)


@app.delete("/api/prompts/{prompt_id}")
async def api_delete(prompt_id: int, _: dict = Depends(require_prompt_delete)):
    if not delete_prompt(prompt_id):
        raise HTTPException(404, "不存在")
    await ws_manager.broadcast({"event": "deleted", "prompt_id": prompt_id})
    return {"message": "删除成功"}


@app.get("/api/prompts/{prompt_id}/versions", response_model=list[PromptVersionResponse])
async def api_versions(prompt_id: int, _: dict = Depends(require_prompt_view)):
    vs = get_prompt_versions(prompt_id)
    return [PromptVersionResponse(id=v["id"], prompt_id=v["prompt_id"], version=v["version"],
                                   content=v["content"], change_log=v["change_log"],
                                   created_at=v["created_at"]) for v in vs]


@app.post("/api/prompts/{prompt_id}/rollback", response_model=PromptResponse)
async def api_rollback(prompt_id: int, data: RollbackRequest, _: dict = Depends(require_prompt_edit)):
    p = rollback_prompt(prompt_id, data.target_version)
    if not p:
        raise HTTPException(404, "目标版本不存在")
    await ws_manager.broadcast({"event": "rollback", "name": p["name"], "version": p["version"]})
    return _to_response(p)


@app.delete("/api/prompts/{prompt_id}/versions/{version_id}")
async def api_delete_version(prompt_id: int, version_id: int, _: dict = Depends(require_prompt_delete)):
    """删除指定版本记录（不允许删除当前版本）"""
    if not delete_version(prompt_id, version_id):
        raise HTTPException(400, "无法删除：版本不存在或为当前版本")
    return {"message": "版本删除成功"}


# ==================== 服务接口（模型服务调用） ====================
@app.get("/api/v1/fetch", response_model=Optional[FetchResponse])
async def api_fetch(department: str, platform: str, scene: str, current_version: Optional[int] = None):
    """
    按科室+平台+场景获取提示词（热更新，内存缓存）
    current_version 与最新一致时返回空
    """
    result = fetch_prompt(department, platform, scene, current_version)
    if result is None:
        if current_version is not None:
            return None
        raise HTTPException(404, f"未找到 {department}/{platform}/{scene} 的提示词")
    return FetchResponse(**result)


@app.get("/api/v1/fetch/{name}", response_model=Optional[FetchByNameResponse])
async def api_fetch_by_name(name: str, current_version: Optional[int] = None):
    """按名称获取提示词"""
    result = fetch_prompt_by_name(name, current_version)
    if result is None:
        if current_version is not None:
            return None
        raise HTTPException(404, f"提示词 '{name}' 不存在")
    return FetchByNameResponse(**result)


@app.post("/api/v1/resolve", response_model=ResolveResponse)
async def api_resolve(data: ResolveRequest):
    """
    解析提示词变量，返回填充好的内容
    这是模型服务最常用的接口：传入名称和参数，直接返回可用提示词
    """
    prompt = get_prompt_by_name(data.name)
    if not prompt:
        raise HTTPException(404, f"提示词 '{data.name}' 不存在")

    department = data.department or prompt["department"]
    resolved = resolve_prompt_variables(
        content=prompt["content"],
        variables=prompt["variables"],
        variable_bindings=prompt.get("variable_bindings", "{}"),
        robot_id=data.robot_id,
        department=department,
        current_round=data.current_round,
        action_desc=data.action_desc,
        knowledge_desc=data.knowledge_desc,
        warmup_desc=data.warmup_desc,
        connect_desc=data.connect_desc,
        extra_variables=data.extra_variables,
    )

    return ResolveResponse(
        name=data.name, resolved_content=resolved, version=prompt["version"],
        department=department, platform=prompt["platform"], scene=prompt["scene"]
    )


@app.post("/api/v1/batch_fetch")
async def api_batch_fetch(body: dict):
    results = []
    for item in body.get("prompts", []):
        name = item.get("name")
        cv = item.get("current_version")
        result = fetch_prompt_by_name(name, cv)
        if result:
            results.append(result)
    return {"updated": results, "count": len(results)}


@app.get("/api/v1/sync")
async def api_sync():
    return {"prompts": fetch_all_active()}


@app.get("/api/v1/knowledge", response_model=KnowledgeBaseListResponse)
async def api_v1_knowledge(
    department: Optional[str] = None,
    platform: Optional[str] = None,
    keyword: Optional[str] = None
):
    total, items = list_knowledge_bases(department=department, platform=platform, keyword=keyword)
    return KnowledgeBaseListResponse(total=total, items=[KnowledgeBaseResponse(**i) for i in items])


@app.get("/api/v1/flow_trees", response_model=FlowTreeListResponse)
async def api_v1_flow_trees(
    department: Optional[str] = None,
    platform: Optional[str] = None,
    keyword: Optional[str] = None,
    bot_id: Optional[str] = None
):
    total, items = list_flow_trees(department=department, platform=platform, keyword=keyword, bot_id=bot_id)
    return FlowTreeListResponse(total=total, items=[FlowTreeResponse(**i) for i in items])


@app.get("/api/v1/flow_trees/{flow_id}/records", response_model=FlowRecordListResponse)
async def api_v1_flow_tree_records(flow_id: int, bot_id: Optional[str] = None):
    ft = get_flow_tree_by_id(flow_id)
    if not ft:
        raise HTTPException(404, "流程树库不存在")
    total, items = list_flow_records(flow_id, bot_id=bot_id)
    return FlowRecordListResponse(total=total, items=[FlowRecordResponse(**i) for i in items])


@app.get("/api/v1/flow_records/search")
async def api_v1_flow_records_search(
    department: Optional[str] = None,
    platform: Optional[str] = None,
    keyword: Optional[str] = None,
    bot_id: Optional[str] = None
):
    total, items = search_flow_records(department=department, platform=platform, keyword=keyword, bot_id=bot_id)
    return {"total": total, "items": items}


@app.get("/api/stats")
async def api_stats():
    stats = get_cache_stats()
    total, _ = list_prompts(page_size=1)
    return {"total_prompts": total, "cache_size": stats["cached_prompts"]}


@app.get("/api/debug/cache")
async def api_debug_cache():
    """查看内存缓存详情"""
    stats = get_cache_stats()
    return {
        "cache_count": stats["cached_prompts"],
        "cache_keys": stats["keys"],
        "detail": {k: {"version": v[0], "content_length": len(v[1]), "variables_length": len(v[2]), "updated_at": v[3]}
                   for k, v in stats.get("cache_detail", {}).items()} if "cache_detail" in stats else stats["keys"]
    }


@app.get("/api/debug/db")
async def api_debug_db():
    """查看数据库中所有提示词概要"""
    total, items = list_prompts(page_size=200)
    return {
        "total": total,
        "db_file": str(DB_PATH),
        "prompts": [{
            "id": p["id"], 
            "name": p["name"], 
            "department": p["department"],
            "platform": p["platform"], 
            "scene": p["scene"],
            "version": p["version"], 
            "is_active": bool(p["is_active"]),
            "content_length": len(p["content"]),
            "updated_at": p["updated_at"]
        } for p in items]
    }


@app.post("/api/debug/reload_cache")
async def api_reload_cache():
    """手动刷新内存缓存"""
    _refresh_cache()
    stats = get_cache_stats()
    return {"message": "缓存已刷新", "cache_size": stats["cached_prompts"]}



# ==================== 知识库管理接口 ====================

@app.post("/api/knowledge", response_model=KnowledgeBaseResponse)
async def api_kb_create(data: KnowledgeBaseCreate, _: dict = Depends(require_knowledge_edit)):
    existing = get_kb_by_key(data.department, data.platform)
    if existing:
        raise HTTPException(400, f"该科室+平台的知识库已存在")
    kb = create_knowledge_base(data.department, data.platform, data.content)
    return KnowledgeBaseResponse(**kb)


@app.get("/api/knowledge", response_model=KnowledgeBaseListResponse)
async def api_kb_list(
    department: Optional[str] = None,
    platform: Optional[str] = None,
    keyword: Optional[str] = None,
    _: dict = Depends(require_knowledge_view)
):
    total, items = list_knowledge_bases(department=department, platform=platform, keyword=keyword)
    return KnowledgeBaseListResponse(total=total, items=[KnowledgeBaseResponse(**i) for i in items])


@app.get("/api/knowledge/{kb_id}", response_model=KnowledgeBaseResponse)
async def api_kb_get(kb_id: int, _: dict = Depends(require_knowledge_view)):
    kb = get_kb_by_id(kb_id)
    if not kb:
        raise HTTPException(404, "知识库不存在")
    return KnowledgeBaseResponse(**kb)


@app.put("/api/knowledge/{kb_id}", response_model=KnowledgeBaseResponse)
async def api_kb_update(kb_id: int, data: KnowledgeBaseUpdate, _: dict = Depends(require_knowledge_edit)):
    kb = update_knowledge_base(kb_id, data.content)
    if not kb:
        raise HTTPException(404, "知识库不存在")
    return KnowledgeBaseResponse(**kb)


@app.delete("/api/knowledge/{kb_id}")
async def api_kb_delete(kb_id: int, _: dict = Depends(require_knowledge_delete)):
    if not delete_knowledge_base(kb_id):
        raise HTTPException(404, "知识库不存在")
    return {"message": "删除成功"}


# ==================== 流程树管理接口 ====================

@app.post("/api/flow_trees", response_model=FlowTreeResponse)
async def api_flow_tree_create(data: FlowTreeCreate, _: dict = Depends(require_flow_edit)):
    existing = get_flow_tree_by_key(data.department, data.platform)
    if existing:
        raise HTTPException(400, "该科室+平台的流程树库已存在")
    ft = create_flow_tree(data.department, data.platform, data.description)
    return FlowTreeResponse(**ft)


@app.get("/api/flow_trees", response_model=FlowTreeListResponse)
async def api_flow_tree_list(
    department: Optional[str] = None,
    platform: Optional[str] = None,
    keyword: Optional[str] = None,
    bot_id: Optional[str] = None,
    _: dict = Depends(require_flow_view)
):
    total, items = list_flow_trees(department=department, platform=platform, keyword=keyword, bot_id=bot_id)
    return FlowTreeListResponse(total=total, items=[FlowTreeResponse(**i) for i in items])


@app.get("/api/flow_trees/{flow_id}", response_model=FlowTreeResponse)
async def api_flow_tree_get(flow_id: int, _: dict = Depends(require_flow_view)):
    ft = get_flow_tree_by_id(flow_id)
    if not ft:
        raise HTTPException(404, "流程树库不存在")
    return FlowTreeResponse(**ft)


@app.put("/api/flow_trees/{flow_id}", response_model=FlowTreeResponse)
async def api_flow_tree_update(flow_id: int, data: FlowTreeUpdate, _: dict = Depends(require_flow_edit)):
    ft = update_flow_tree(flow_id, description=data.description)
    if not ft:
        raise HTTPException(404, "流程树库不存在")
    return FlowTreeResponse(**ft)


@app.delete("/api/flow_trees/{flow_id}")
async def api_flow_tree_delete(flow_id: int, _: dict = Depends(require_flow_delete)):
    if not delete_flow_tree(flow_id):
        raise HTTPException(404, "流程树库不存在")
    return {"message": "删除成功"}


# ==================== 流程树记录接口 ====================

@app.get("/api/flow_trees/{flow_id}/records", response_model=FlowRecordListResponse)
async def api_flow_records_list(flow_id: int, bot_id: Optional[str] = None, _: dict = Depends(require_flow_view)):
    ft = get_flow_tree_by_id(flow_id)
    if not ft:
        raise HTTPException(404, "流程树库不存在")
    total, items = list_flow_records(flow_id, bot_id=bot_id)
    return FlowRecordListResponse(total=total, items=[FlowRecordResponse(**i) for i in items])


@app.post("/api/flow_trees/{flow_id}/records/upload", response_model=FlowRecordResponse)
async def api_flow_record_upload(
    flow_id: int,
    file: UploadFile = File(...),
    auto_parse: str = Form("true"),
    bot_id: str = Form(""),
    _: dict = Depends(require_flow_edit)
):
    """上传文件到流程树库，可选自动解析（异步后台执行，不阻塞其他请求）"""
    ft = get_flow_tree_by_id(flow_id)
    if not ft:
        raise HTTPException(404, "流程树库不存在")

    file_bytes = await file.read()
    file_name = file.filename or "unknown"
    file_path, file_type = flow_parser_service.save_uploaded_file(flow_id, file_name, file_bytes)

    # 创建记录（先存为 pending / parsing）
    init_status = "parsing" if (auto_parse.lower() == "true" and file_type in ("image", "pdf")) else "pending"
    rec = create_flow_record(
        flow_id=flow_id,
        file_name=file_name,
        file_type=file_type,
        file_path=file_path,
        status=init_status,
        bot_id=bot_id,
    )

    # 自动解析 —— 在后台线程中执行，不阻塞当前请求
    if init_status == "parsing":
        _schedule_parse(rec["id"], file_path, file_type, file_name)

    return FlowRecordResponse(**rec)


@app.get("/api/flow_records/{record_id}", response_model=FlowRecordResponse)
async def api_flow_record_get(record_id: int, _: dict = Depends(require_flow_view)):
    rec = get_flow_record_by_id(record_id)
    if not rec:
        raise HTTPException(404, "记录不存在")
    return FlowRecordResponse(**rec)


@app.put("/api/flow_records/{record_id}", response_model=FlowRecordResponse)
async def api_flow_record_update(record_id: int, data: FlowRecordUpdate, _: dict = Depends(require_flow_edit)):
    rec = update_flow_record(record_id, description=data.description, structure=data.structure, bot_id=data.bot_id)
    if not rec:
        raise HTTPException(404, "记录不存在")
    return FlowRecordResponse(**rec)


@app.post("/api/flow_records/{record_id}/reparse", response_model=FlowRecordResponse)
async def api_flow_record_reparse(record_id: int, _: dict = Depends(require_flow_edit)):
    """重新解析记录（异步后台执行，立即返回 parsing 状态）"""
    rec = get_flow_record_by_id(record_id)
    if not rec:
        raise HTTPException(404, "记录不存在")
    if rec["file_type"] not in ("image", "pdf"):
        raise HTTPException(400, "仅支持重新解析图片或PDF文件")

    # 先标记为 parsing
    rec = update_flow_record(record_id, status="parsing", error="")

    _schedule_parse(record_id, rec["file_path"], rec["file_type"], rec["file_name"])

    return FlowRecordResponse(**rec)


@app.delete("/api/flow_records/{record_id}")
async def api_flow_record_delete(record_id: int, _: dict = Depends(require_flow_delete)):
    rec = get_flow_record_by_id(record_id)
    if not rec:
        raise HTTPException(404, "记录不存在")
    # 删除物理文件
    flow_parser_service.delete_record_file(rec.get("file_path", ""))
    delete_flow_record(record_id)
    return {"message": "删除成功"}


# ==================== 系统设置（LLM API 配置） ====================

@app.get("/api/settings/llm", response_model=LLMConfig)
async def api_get_llm_config():
    cfg = get_llm_config()
    # 出于安全考虑，掩码 api_key
    masked_key = cfg["api_key"]
    if masked_key and len(masked_key) > 8:
        masked_key = masked_key[:4] + "****" + masked_key[-4:]
    return LLMConfig(
        base_url=cfg["base_url"],
        api_key=masked_key,
        model_name=cfg["model_name"]
    )


@app.get("/api/settings/llm/raw", response_model=LLMConfig)
async def api_get_llm_config_raw():
    """返回原始（未掩码）配置，仅供编辑时使用"""
    cfg = get_llm_config()
    return LLMConfig(**cfg)


@app.put("/api/settings/llm", response_model=LLMConfig)
async def api_set_llm_config(data: LLMConfig):
    # 如果传入的 api_key 包含掩码字符，则不更新 api_key
    current = get_llm_config()
    api_key = data.api_key
    if api_key and "****" in api_key:
        api_key = current["api_key"]
    cfg = set_llm_config(data.base_url, api_key, data.model_name)
    return LLMConfig(**cfg)


# ==================== LLM 版本管理接口 ====================

@app.get("/api/settings/llm/versions", response_model=list[LLMVersionResponse])
async def api_list_llm_versions():
    versions = list_llm_versions()
    # 掩码 api_key
    for v in versions:
        key = v.get("api_key", "")
        if key and len(key) > 8:
            v["api_key"] = key[:4] + "****" + key[-4:]
    return [LLMVersionResponse(**v) for v in versions]


@app.post("/api/settings/llm/versions", response_model=LLMVersionResponse)
async def api_create_llm_version(data: LLMVersionCreate):
    ver = create_llm_version(data.name, data.base_url, data.api_key, data.model_name)
    return LLMVersionResponse(**ver)


@app.put("/api/settings/llm/versions/{version_id}", response_model=LLMVersionResponse)
async def api_update_llm_version(version_id: int, data: LLMVersionUpdate):
    # 如果 api_key 包含掩码，不更新
    update_data = {}
    if data.name is not None:
        update_data["name"] = data.name
    if data.base_url is not None:
        update_data["base_url"] = data.base_url
    if data.api_key is not None:
        if "****" in data.api_key:
            old = get_llm_version(version_id)
            update_data["api_key"] = old["api_key"] if old else data.api_key
        else:
            update_data["api_key"] = data.api_key
    if data.model_name is not None:
        update_data["model_name"] = data.model_name
    ver = update_llm_version(version_id, **update_data)
    if not ver:
        raise HTTPException(404, "版本不存在")
    return LLMVersionResponse(**ver)


@app.post("/api/settings/llm/versions/{version_id}/activate", response_model=LLMVersionResponse)
async def api_activate_llm_version(version_id: int):
    ver = activate_llm_version(version_id)
    if not ver:
        raise HTTPException(404, "版本不存在")
    return LLMVersionResponse(**ver)


@app.delete("/api/settings/llm/versions/{version_id}")
async def api_delete_llm_version(version_id: int):
    if not delete_llm_version(version_id):
        raise HTTPException(400, "无法删除：版本不存在或为当前激活版本")
    return {"message": "版本已删除"}


@app.websocket("/ws/updates")
async def ws_updates(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8900)
