#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Agent 主应用 - FastAPI 后端

功能：
1. 使用 prompt_manager 配置的 LLM API 进行对话
2. 图片/PDF 解析（集成 dialogue-flow-parser skill + LLM 多模态）
3. 流程树管理
"""

import os
import sys
import json
import logging
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager
from urllib.request import urlopen, Request as URLRequest
from urllib.error import HTTPError

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File, Form, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# 确保能 import 同级模块
sys.path.insert(0, os.path.dirname(__file__))

from llm_client import get_llm_client
from flow_parser_service import (
    save_uploaded_file, parse_file, delete_record_file,
    FLOW_DATA_DIR
)
import robot_config_service
import service_api
from dialog_agent import get_agent
from conversation_store import conversation_store
from skills.registry import skill_registry

# 导入 prompt_manager 的数据库模块（共享同一数据库）
PM_BACKEND = os.path.join(os.path.dirname(__file__), "..", "backend")
sys.path.insert(0, PM_BACKEND)

from database import (
    init_db, get_llm_config,
    create_flow_tree, get_flow_tree_by_id, get_flow_tree_by_key,
    list_flow_trees, update_flow_tree, delete_flow_tree,
    create_flow_record, get_flow_record_by_id, list_flow_records,
    update_flow_record, delete_flow_record, search_flow_records,
    DEPARTMENTS, PLATFORMS, DEPARTMENT_ZH, PLATFORM_ZH,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agent_app")

PM_URL = os.environ.get("PROMPT_MANAGER_URL", "http://localhost:8900")


# ==================== Models ====================

class ChatRequest(BaseModel):
    messages: List[dict] = Field(..., description="对话消息列表")
    temperature: float = Field(0.7, ge=0, le=2)
    max_tokens: int = Field(4096, ge=1, le=32768)
    system_prompt: Optional[str] = None


class ChatResponse(BaseModel):
    content: str
    model: str = ""


class AgentChatRequest(BaseModel):
    """智能客服 Agent 对话请求（配置由 科室+平台+机器人id 三级筛选决定）"""
    message: str = Field(..., description="访客本轮输入")
    session_id: Optional[str] = Field(None, description="会话 id，为空则新建")
    bot_id: Optional[str] = Field(None, description="机器人 id")
    department: Optional[str] = Field(None, description="科室，留空则用机器人配置中的科室")
    platform: Optional[str] = Field(None, description="平台，留空则用机器人配置中的平台")
    temperature: float = Field(0.7, ge=0, le=2)
    use_tools: bool = Field(True, description="是否启用工具调用")
    enable_thinking: bool = Field(False, description="是否开启模型思考（推理模式）")


class AgentResetRequest(BaseModel):
    session_id: str


class FlowTreeCreateReq(BaseModel):
    department: str = Field("general")
    platform: str = Field("general")
    description: str = Field("")


class FlowTreeUpdateReq(BaseModel):
    description: Optional[str] = None


class FlowRecordUpdateReq(BaseModel):
    description: Optional[str] = None
    structure: Optional[str] = None


# ==================== App ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Agent 服务", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agent_frontend")


# ==================== 页面 ====================

@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/", response_class=HTMLResponse)
async def index():
    """返回前端页面

    静态资源版本号由 app.js / style.css 的最后修改时间生成，
    改动前端文件后浏览器会自动拉取新版本，无需手动清缓存。
    """
    html_path = os.path.join(FRONTEND_DIR, "index.html")
    if not os.path.exists(html_path):
        return HTMLResponse("<h1>Agent 服务</h1><p>前端文件未找到，请检查 agent_frontend 目录</p>")

    try:
        mtimes = [
            os.path.getmtime(os.path.join(FRONTEND_DIR, name))
            for name in ("app.js", "style.css")
            if os.path.exists(os.path.join(FRONTEND_DIR, name))
        ]
        asset_ver = str(int(max(mtimes))) if mtimes else "0"
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read().replace("__ASSET_VER__", asset_ver)
        return HTMLResponse(
            html,
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
    except Exception as e:
        logger.error("渲染首页失败: %s", e)
        return FileResponse(html_path)


# ==================== LLM 配置状态 ====================

@app.get("/api/llm/status")
async def llm_status():
    """检查 LLM API 是否已配置"""
    llm = get_llm_client(PM_URL)
    cfg = llm._load_config()
    return {
        "configured": llm.is_configured(),
        "base_url": cfg.get("base_url", ""),
        "model_name": cfg.get("model_name", ""),
        "api_key_set": bool(cfg.get("api_key")),
        "provider": cfg.get("provider", "openai"),
        "version_name": cfg.get("version_name", ""),
    }


# ==================== LLM 版本管理（代理到提示词管理系统） ====================
# agent 服务不直连数据库，统一转发给 prompt_manager，保证两处配置始终一致。

def _pm_request(path: str, method: str = "GET", payload: Optional[dict] = None) -> Any:
    """向 prompt_manager 发起请求并返回解析后的 JSON"""
    url = f"{PM_URL}{path}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = URLRequest(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=15) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except HTTPError as e:
        detail = e.read().decode(errors="ignore")
        logger.error("代理请求失败 %s %s: %s %s", method, path, e.code, detail[:200])
        raise HTTPException(e.code, f"提示词管理系统返回错误: {detail[:200]}")
    except Exception as e:
        logger.error("代理请求异常 %s %s: %s", method, path, e)
        raise HTTPException(502, f"无法连接提示词管理系统({PM_URL}): {e}")


@app.get("/api/llm/versions")
async def llm_versions_list():
    """LLM 版本列表（api_key 已由上游掩码）"""
    return {"versions": _pm_request("/api/settings/llm/versions")}


@app.post("/api/llm/versions")
async def llm_versions_create(data: Dict[str, Any] = Body(...)):
    """新建 LLM 版本"""
    return _pm_request("/api/settings/llm/versions", "POST", {
        "name": data.get("name", ""),
        "base_url": data.get("base_url", ""),
        "api_key": data.get("api_key", ""),
        "model_name": data.get("model_name", ""),
        "provider": data.get("provider", "openai"),
    })


@app.put("/api/llm/versions/{version_id}")
async def llm_versions_update(version_id: int, data: Dict[str, Any] = Body(...)):
    """编辑 LLM 版本"""
    result = _pm_request(f"/api/settings/llm/versions/{version_id}", "PUT", data)
    get_llm_client(PM_URL)._refresh_config()
    return result


@app.post("/api/llm/versions/{version_id}/activate")
async def llm_versions_activate(version_id: int):
    """激活指定版本，并立即刷新 agent 侧配置缓存"""
    result = _pm_request(f"/api/settings/llm/versions/{version_id}/activate", "POST")
    get_llm_client(PM_URL)._refresh_config()
    logger.info("已切换 LLM 版本: %s (provider=%s)",
                result.get("name"), result.get("provider"))
    return result


@app.delete("/api/llm/versions/{version_id}")
async def llm_versions_delete(version_id: int):
    """删除 LLM 版本"""
    return _pm_request(f"/api/settings/llm/versions/{version_id}", "DELETE")


@app.post("/api/llm/refresh")
async def llm_refresh():
    """刷新 LLM 配置缓存"""
    llm = get_llm_client(PM_URL)
    llm._refresh_config()
    return {"message": "配置已刷新"}


@app.post("/api/llm/test")
async def llm_test():
    """测试 LLM API 连通性"""
    llm = get_llm_client(PM_URL)
    if not llm.is_configured():
        raise HTTPException(400, "LLM API 未配置")
    try:
        resp = llm.chat([{"role": "user", "content": "你好，请回复'连接成功'"}], max_tokens=20)
        return {"success": True, "response": resp}
    except Exception as e:
        raise HTTPException(500, f"LLM API 调用失败: {e}")


# ==================== 对话接口 ====================

@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """通用对话接口"""
    llm = get_llm_client(PM_URL)
    if not llm.is_configured():
        raise HTTPException(400, "LLM API 未配置，请先在提示词管理系统中配置")

    messages = []
    if req.system_prompt:
        messages.append({"role": "system", "content": req.system_prompt})
    messages.extend(req.messages)

    try:
        content = llm.chat(
            messages=messages,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
        )
        cfg = llm._load_config()
        return ChatResponse(content=content, model=cfg.get("model_name", ""))
    except Exception as e:
        raise HTTPException(500, f"对话失败: {e}")


# ==================== 智能客服 Agent ====================

@app.get("/api/agent/cascade")
async def api_agent_cascade(
    department: Optional[str] = None,
    platform: Optional[str] = None,
):
    """科室 -> 平台 -> 机器人id 三级级联选项（一次性返回，减少前端请求）"""
    return robot_config_service.get_cascade_options(department=department, platform=platform)


@app.get("/api/agent/bots")
async def api_agent_bots(
    department: Optional[str] = None,
    platform: Optional[str] = None,
    keyword: Optional[str] = None,
    enabled_only: bool = False,
    include_unconfigured: bool = True,
):
    """按科室+平台级联筛选机器人 id（实时直查库，含禁用与未配置机器人）"""
    bots = robot_config_service.list_bots(
        department=department, platform=platform,
        enabled_only=enabled_only, keyword=keyword,
        include_unconfigured=include_unconfigured,
    )
    return {
        "bots": bots,
        "total": len(bots),
        **robot_config_service.get_config_signature(),
    }


@app.get("/api/agent/bots/signature")
async def api_agent_bots_signature():
    """机器人配置指纹，供前端轮询检测提示词管理侧的配置变更"""
    return robot_config_service.get_config_signature()


@app.get("/api/agent/config")
async def api_agent_config(
    bot_id: Optional[str] = None,
    department: Optional[str] = None,
    platform: Optional[str] = None,
):
    """预览当前筛选条件下将生效的 agent 配置（提示词版本、流程树、工具）"""
    try:
        return get_agent(PM_URL).inspect_config(
            bot_id=bot_id, department=department, platform=platform
        )
    except Exception as e:
        logger.exception("解析 agent 配置失败")
        raise HTTPException(500, f"解析配置失败: {e}")


@app.post("/api/agent/chat")
async def api_agent_chat(req: AgentChatRequest):
    """智能客服 Agent 对话：带会话记忆 + 工具调用 + 按机器人配置装配提示词"""
    llm = get_llm_client(PM_URL)
    if not llm.is_configured():
        raise HTTPException(400, "LLM API 未配置，请先在提示词管理系统中配置")

    if not req.message.strip():
        raise HTTPException(400, "message 不能为空")

    try:
        return get_agent(PM_URL).chat(
            message=req.message,
            session_id=req.session_id,
            bot_id=req.bot_id,
            department=req.department,
            platform=req.platform,
            temperature=req.temperature,
            use_tools=req.use_tools,
            enable_thinking=req.enable_thinking,
        )
    except Exception as e:
        logger.exception("Agent 对话失败")
        raise HTTPException(500, f"Agent 对话失败: {e}")


@app.get("/api/agent/sessions")
async def api_agent_sessions(
    bot_id: Optional[str] = None,
    department: Optional[str] = None,
    platform: Optional[str] = None,
    keyword: Optional[str] = None,
    page: int = 1,
    page_size: int = 30,
):
    """对话列表：支持按机器人 id 筛选、按对话内容关键词检索"""
    return conversation_store.list_sessions(
        bot_id=bot_id, department=department, platform=platform,
        keyword=keyword, page=page, page_size=page_size,
    )


@app.get("/api/agent/sessions/{session_id}")
async def api_agent_session_detail(session_id: str):
    """加载历史会话消息，并把上下文恢复到内存以便继续对话"""
    result = get_agent(PM_URL).load_session(session_id)
    if not result.get("found"):
        raise HTTPException(404, "会话不存在")
    return result


@app.delete("/api/agent/sessions/{session_id}")
async def api_agent_session_delete(session_id: str):
    """删除会话（从列表移除，消息明细保留用于审计）"""
    get_agent(PM_URL).reset_session(session_id)
    return {"success": conversation_store.delete_session(session_id)}


@app.get("/api/agent/skills")
async def api_agent_skills(department: Optional[str] = None):
    """列出病种话术 Skill"""
    return {"skills": skill_registry.list_skills(department=department)}


@app.post("/api/agent/reset")
async def api_agent_reset(req: AgentResetRequest):
    """清空指定会话的多轮记忆"""
    return {"success": get_agent(PM_URL).reset_session(req.session_id)}


@app.get("/api/agent/stats")
async def api_agent_stats():
    return get_agent(PM_URL).stats()


# ==================== 对外服务接口 ====================
# 入参/响应格式对齐 qwen-proj/llm_service/service_qwen_controller.py 的 on_post，
# 已接入该控制器的外部系统可零改造切换。详见 service_api.py 顶部说明。

@app.post("/llm/dialog")
async def api_external_dialog(payload: Dict[str, Any] = Body(...)):
    """外部系统调用入口（控制器兼容格式）"""
    params = service_api.parse_request(payload)
    return service_api.handle_dialog(
        params, get_agent(PM_URL), robot_config_service
    )


# 与控制器保持一致的路径别名，方便直接替换 URL
@app.post("/llm/generate")
async def api_external_generate(payload: Dict[str, Any] = Body(...)):
    """/llm/dialog 的别名"""
    params = service_api.parse_request(payload)
    return service_api.handle_dialog(
        params, get_agent(PM_URL), robot_config_service
    )


# ==================== 元数据 ====================

@app.get("/api/meta/departments")
async def api_departments():
    return {"departments": [{"key": k, "label": DEPARTMENT_ZH[k]} for k in DEPARTMENTS]}


@app.get("/api/meta/platforms")
async def api_platforms():
    return {"platforms": [{"key": k, "label": PLATFORM_ZH[k]} for k in PLATFORMS]}


# ==================== 流程树管理 ====================

@app.post("/api/flow_trees")
async def api_flow_tree_create(data: FlowTreeCreateReq):
    existing = get_flow_tree_by_key(data.department, data.platform)
    if existing:
        raise HTTPException(400, "该科室+平台的流程树库已存在")
    ft = create_flow_tree(data.department, data.platform, data.description)
    return ft


@app.get("/api/flow_trees")
async def api_flow_tree_list(
    department: Optional[str] = None,
    platform: Optional[str] = None,
    keyword: Optional[str] = None
):
    total, items = list_flow_trees(department=department, platform=platform, keyword=keyword)
    return {"total": total, "items": items}


@app.get("/api/flow_trees/{flow_id}")
async def api_flow_tree_get(flow_id: int):
    ft = get_flow_tree_by_id(flow_id)
    if not ft:
        raise HTTPException(404, "流程树库不存在")
    return ft


@app.put("/api/flow_trees/{flow_id}")
async def api_flow_tree_update(flow_id: int, data: FlowTreeUpdateReq):
    ft = update_flow_tree(flow_id, description=data.description)
    if not ft:
        raise HTTPException(404, "流程树库不存在")
    return ft


@app.delete("/api/flow_trees/{flow_id}")
async def api_flow_tree_delete(flow_id: int):
    if not delete_flow_tree(flow_id):
        raise HTTPException(404, "流程树库不存在")
    return {"message": "删除成功"}


# ==================== 流程树记录 ====================

@app.get("/api/flow_trees/{flow_id}/records")
async def api_flow_records_list(flow_id: int):
    ft = get_flow_tree_by_id(flow_id)
    if not ft:
        raise HTTPException(404, "流程树库不存在")
    total, items = list_flow_records(flow_id)
    return {"total": total, "items": items}


@app.post("/api/flow_trees/{flow_id}/records/upload")
async def api_flow_record_upload(
    flow_id: int,
    file: UploadFile = File(...),
    auto_parse: str = Form("true"),
):
    """上传文件到流程树库，可选自动解析"""
    ft = get_flow_tree_by_id(flow_id)
    if not ft:
        raise HTTPException(404, "流程树库不存在")

    file_bytes = await file.read()
    file_name = file.filename or "unknown"
    file_path, file_type = save_uploaded_file(flow_id, file_name, file_bytes)

    rec = create_flow_record(
        flow_id=flow_id,
        file_name=file_name,
        file_type=file_type,
        file_path=file_path,
        status="pending"
    )

    if auto_parse.lower() == "true" and file_type in ("image", "pdf"):
        try:
            description, structure, status, error = parse_file(
                file_path, file_type, file_name
            )
            rec = update_flow_record(
                rec["id"],
                description=description,
                structure=structure,
                status=status,
                error=error
            )
        except Exception as e:
            rec = update_flow_record(rec["id"], status="failed", error=str(e))

    return rec


# ==================== 流程树记录搜索（必须在 {record_id} 路由之前） ====================

@app.get("/api/flow_records/search")
async def api_flow_records_search(
    department: Optional[str] = None,
    platform: Optional[str] = None,
    keyword: Optional[str] = None
):
    """跨流程树库搜索记录，返回包含科室/平台信息的记录列表"""
    total, items = search_flow_records(
        department=department, platform=platform, keyword=keyword
    )
    for item in items:
        fp = item.get("file_path", "")
        if fp:
            item["file_url"] = f"/api/files/{item['flow_id']}/{os.path.basename(fp)}"
        else:
            item["file_url"] = ""
    return {"total": total, "items": items}


@app.get("/api/flow_records/{record_id}")
async def api_flow_record_get(record_id: int):
    rec = get_flow_record_by_id(record_id)
    if not rec:
        raise HTTPException(404, "记录不存在")
    return rec


@app.put("/api/flow_records/{record_id}")
async def api_flow_record_update(record_id: int, data: FlowRecordUpdateReq):
    rec = update_flow_record(record_id, description=data.description, structure=data.structure)
    if not rec:
        raise HTTPException(404, "记录不存在")
    return rec


@app.post("/api/flow_records/{record_id}/reparse")
async def api_flow_record_reparse(record_id: int):
    """重新解析记录（使用 LLM）"""
    rec = get_flow_record_by_id(record_id)
    if not rec:
        raise HTTPException(404, "记录不存在")
    if rec["file_type"] not in ("image", "pdf"):
        raise HTTPException(400, "仅支持重新解析图片或PDF文件")

    try:
        description, structure, status, error = parse_file(
            rec["file_path"], rec["file_type"], rec["file_name"]
        )
        rec = update_flow_record(record_id, description=description, structure=structure, status=status, error=error)
    except Exception as e:
        rec = update_flow_record(record_id, status="failed", error=str(e))

    return rec


@app.delete("/api/flow_records/{record_id}")
async def api_flow_record_delete(record_id: int):
    rec = get_flow_record_by_id(record_id)
    if not rec:
        raise HTTPException(404, "记录不存在")
    delete_record_file(rec.get("file_path", ""))
    delete_flow_record(record_id)
    return {"message": "删除成功"}


# ==================== 文件服务 ====================

@app.get("/api/files/{flow_id}/{file_name:path}")
async def api_serve_file(flow_id: int, file_name: str):
    """提供上传文件（图片/PDF）的访问服务"""
    flow_dir = os.path.join(FLOW_DATA_DIR, str(flow_id))
    file_path = os.path.join(flow_dir, file_name)
    if not os.path.isfile(file_path):
        raise HTTPException(404, "文件不存在")

    # 判断 MIME 类型
    ext = os.path.splitext(file_name)[1].lower()
    media_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".pdf": "application/pdf",
    }.get(ext, "application/octet-stream")

    return FileResponse(file_path, media_type=media_type)


# ==================== 图片直接解析（不存储） ====================

@app.post("/api/parse_image")
async def api_parse_image_direct(file: UploadFile = File(...)):
    """直接上传图片解析（不入库，仅返回解析结果）"""
    file_bytes = await file.read()
    file_name = file.filename or "unknown"

    # 保存到临时目录
    import tempfile
    ext = os.path.splitext(file_name)[1].lower()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext, dir=tempfile.gettempdir())
    tmp.write(file_bytes)
    tmp.close()

    try:
        file_type = "pdf" if ext == ".pdf" else "image"
        description, structure, status, error = parse_file(tmp.name, file_type, file_name)
        return {
            "file_name": file_name,
            "description": description,
            "structure": json.loads(structure) if structure and structure != "{}" else None,
            "status": status,
            "error": error,
        }
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


# 挂载前端静态文件（CSS/JS）—— 必须在 uvicorn.run 之前注册
if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8901)
