#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
提示词管理系统 - Pydantic 数据模型
"""
from pydantic import BaseModel, Field
from typing import Optional, List


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=1)


class UserResponse(BaseModel):
    id: int
    username: str
    role: str
    can_prompt_view: bool
    can_prompt_edit: bool
    can_prompt_delete: bool
    can_knowledge_view: bool
    can_knowledge_edit: bool
    can_knowledge_delete: bool
    can_flow_view: bool
    can_flow_edit: bool
    can_flow_delete: bool
    is_active: bool
    managed_departments: List[str] = []
    created_at: str
    updated_at: str


class LoginResponse(BaseModel):
    token: str
    expires_at: str
    user: UserResponse


class UserCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=6)
    role: str = Field("normal", description="admin/normal")
    can_prompt_view: bool = True
    can_prompt_edit: bool = False
    can_prompt_delete: bool = False
    can_knowledge_view: bool = True
    can_knowledge_edit: bool = False
    can_knowledge_delete: bool = False
    can_flow_view: bool = True
    can_flow_edit: bool = False
    can_flow_delete: bool = False
    managed_departments: List[str] = Field(default_factory=list, description="该用户管理的科室列表；admin 忽略")


class UserUpdate(BaseModel):
    password: Optional[str] = Field(None, min_length=6)
    role: Optional[str] = None
    can_prompt_view: Optional[bool] = None
    can_prompt_edit: Optional[bool] = None
    can_prompt_delete: Optional[bool] = None
    can_knowledge_view: Optional[bool] = None
    can_knowledge_edit: Optional[bool] = None
    can_knowledge_delete: Optional[bool] = None
    can_flow_view: Optional[bool] = None
    can_flow_edit: Optional[bool] = None
    can_flow_delete: Optional[bool] = None
    is_active: Optional[bool] = None
    managed_departments: Optional[List[str]] = None


class PromptCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, description="模板名称")
    department: str = Field("general", description="科室: hair/dentistry/dermatology/ophthalmology/pediatrics/beauty/general")
    platform: str = Field("general", description="平台: xhs/bd/dy/kuaishou/wechat/general")
    scene: str = Field("system_prompt", description="场景: system_prompt/warmup/knowledge/action_desc/general")
    content: str = Field(..., min_length=1, description="提示词内容，支持变量占位符")
    variables: str = Field("{}", description="自定义变量JSON")
    variable_bindings: str = Field("{}", description="变量映射JSON，格式: {变量名: 占位符标记}")
    description: str = Field("", description="描述")
    tags: str = Field("", description="标签，逗号分隔")


class PromptUpdate(BaseModel):
    content: Optional[str] = None
    variables: Optional[str] = None
    variable_bindings: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[str] = None
    change_log: str = Field("", description="变更说明")


class PromptResponse(BaseModel):
    id: int
    name: str
    department: str
    platform: str
    scene: str
    content: str
    variables: str
    variable_bindings: str
    description: str
    tags: str
    is_active: bool
    version: int
    created_at: str
    updated_at: str


class PromptVersionResponse(BaseModel):
    id: int
    prompt_id: int
    version: int
    content: str
    change_log: str
    created_at: str


class PromptListResponse(BaseModel):
    total: int
    items: List[PromptResponse]


class RollbackRequest(BaseModel):
    target_version: int


class FetchResponse(BaseModel):
    department: str
    platform: str
    scene: str
    content: str
    variables: str
    version: int
    updated_at: str


class FetchByNameResponse(BaseModel):
    name: str
    department: str
    platform: str
    scene: str
    content: str
    variables: str
    version: int
    updated_at: str


class ResolveRequest(BaseModel):
    """变量解析请求"""
    name: str = Field(..., description="提示词名称")
    robot_id: str = Field("", description="机器人ID")
    department: str = Field("", description="科室，为空则使用提示词自带")
    current_round: int = Field(0, description="当前对话轮次")
    action_desc: str = Field("", description="动作描述")
    knowledge_desc: str = Field("", description="知识描述")
    warmup_desc: str = Field("", description="暖场描述")
    connect_desc: str = Field("", description="套联描述")
    extra_variables: dict = Field({}, description="额外自定义变量")


class ResolveResponse(BaseModel):
    """变量解析响应"""
    name: str
    resolved_content: str
    version: int
    department: str
    platform: str
    scene: str


class KnowledgeBaseCreate(BaseModel):
    department: str = Field("general", description="科室")
    platform: str = Field("general", description="平台")
    content: str = Field("[]", description="知识库内容JSON数组")


class KnowledgeBaseUpdate(BaseModel):
    content: str = Field(..., description="知识库内容JSON数组")


class KnowledgeBaseResponse(BaseModel):
    id: int
    department: str
    platform: str
    content: str
    updated_at: str


class KnowledgeBaseListResponse(BaseModel):
    total: int
    items: List[KnowledgeBaseResponse]


# ==================== 流程树 ====================
class FlowTreeCreate(BaseModel):
    """创建流程树库（按 科室+平台 分组）"""
    department: str = Field("general", description="科室")
    platform: str = Field("general", description="平台")
    description: str = Field("", description="描述")


class FlowTreeUpdate(BaseModel):
    description: Optional[str] = None


class FlowTreeResponse(BaseModel):
    id: int
    department: str
    platform: str
    description: str
    updated_at: str
    record_count: int = 0


class FlowTreeListResponse(BaseModel):
    total: int
    items: List[FlowTreeResponse]


class FlowRecordResponse(BaseModel):
    """单条流程树解析记录"""
    id: int
    flow_id: int
    file_name: str
    file_type: str  # image/pdf
    file_path: str
    description: str  # 自然语言描述
    structure: str   # 结构化 JSON
    status: str      # success / failed / pending
    error: str
    bot_id: str = ""
    created_at: str


class FlowRecordListResponse(BaseModel):
    total: int
    items: List[FlowRecordResponse]


class FlowRecordUpdate(BaseModel):
    """手动更新流程树记录的自然语言描述"""
    description: Optional[str] = None
    structure: Optional[str] = None
    bot_id: Optional[str] = None


class RobotConfigCreate(BaseModel):
    bot_id: str = Field(..., min_length=1, max_length=64, description="机器人 ID")
    department: str = Field(..., description="科室")
    platform: str = Field(..., description="平台")
    company: str = Field("", description="公司/品牌名称，如 雍禾、牙博士、邦泰")
    enabled: bool = Field(True, description="是否启用")
    prompt_version: int = Field(-1, description="提示词版本号；-1 = 跟随最新，>=0 = 固定版本")


class RobotConfigUpdate(BaseModel):
    department: Optional[str] = None
    platform: Optional[str] = None
    company: Optional[str] = None
    enabled: Optional[bool] = None
    prompt_version: Optional[int] = None


class RobotConfigResponse(BaseModel):
    bot_id: str
    department: str
    platform: str
    company: str
    enabled: bool
    prompt_version: int = -1
    created_at: str
    updated_at: str


class RobotConfigListResponse(BaseModel):
    total: int
    items: List[RobotConfigResponse]


class LLMConfig(BaseModel):
    """LLM API 配置"""
    base_url: str = ""
    api_key: str = ""
    model_name: str = ""


class LLMVersionCreate(BaseModel):
    """创建 LLM 版本"""
    name: str = Field("", description="版本名称")
    base_url: str = Field("", description="API Base URL")
    api_key: str = Field("", description="API Key")
    model_name: str = Field("", description="模型名称")


class LLMVersionUpdate(BaseModel):
    """更新 LLM 版本"""
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model_name: Optional[str] = None


class LLMVersionResponse(BaseModel):
    """LLM 版本响应"""
    id: int
    name: str
    base_url: str
    api_key: str
    model_name: str
    is_active: bool
    created_at: str
    updated_at: str


# ==================== KICP 机器人视角页面 ====================
class KicpSessionRequest(BaseModel):
    """KICP 页面会话初始化：按机器人ID换取访问令牌

    - 不传 username/password 时使用服务端配置的默认账号（admin）免登录
    - 传入账号密码时按该账号的权限访问
    """
    bot_id: str = Field(..., min_length=1, max_length=64, description="机器人ID")
    username: Optional[str] = Field(None, description="账号；留空表示使用默认 admin 免登录")
    password: Optional[str] = Field(None, description="密码；留空表示使用默认 admin 免登录")


class KicpRobotInfo(BaseModel):
    """机器人上下文（科室 / 平台 / 公司）"""
    bot_id: str
    department: str = ""
    department_label: str = ""
    platform: str = ""
    platform_label: str = ""
    company: str = ""
    enabled: bool = True
    prompt_version: int = -1
    configured: bool = False
    updated_at: str = ""


class KicpScope(BaseModel):
    """当前账号在该机器人科室下的可操作范围"""
    is_admin: bool = False
    dept_allowed: bool = False
    default_login: bool = False
    can_prompt_view: bool = False
    can_prompt_edit: bool = False
    can_prompt_delete: bool = False
    can_knowledge_view: bool = False
    can_knowledge_edit: bool = False
    can_knowledge_delete: bool = False
    can_flow_view: bool = False
    can_flow_edit: bool = False
    can_flow_delete: bool = False


class KicpSessionResponse(BaseModel):
    token: str
    expires_at: str
    user: UserResponse
    robot: KicpRobotInfo
    scope: KicpScope
