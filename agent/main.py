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
from typing import Optional, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File, Form
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
    html_path = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(html_path):
        return FileResponse(html_path)
    return HTMLResponse("<h1>Agent 服务</h1><p>前端文件未找到，请检查 agent_frontend 目录</p>")


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
    }


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
