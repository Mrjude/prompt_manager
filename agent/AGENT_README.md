# Agent 服务实现说明

`prompt_manager/agent` 是智能客服 Agent 服务，整合了 `agent_proj/shining_dialog_agents`
的 ReAct 编排与病种话术能力，并将其从「静态配置驱动」改造为「数据库配置驱动」。

核心特征：同一套代码通过 **科室 + 平台 + 机器人id** 三级配置，
动态装配出不同人格、不同知识、不同工具集的客服 Agent。

---

## 一、整体架构

```
┌─────────────────────── agent_frontend (Vue3 CDN 单页) ───────────────────────┐
│  工具栏：科室 ▾  平台 ▾  机器人id ▾ │ 检索框(按页签切换语义)               │
│  左栏：对话列表 / 流程树图库        │ 右栏：Agent 对话 / 流程树解析         │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │ HTTP
┌──────────────────────────────────▼──────────────────────────────────────────┐
│                          main.py (FastAPI 路由层)                            │
└───┬─────────────┬──────────────┬──────────────┬───────────────┬─────────────┘
    │             │              │              │               │
┌───▼──────┐ ┌────▼─────────┐ ┌──▼──────────┐ ┌─▼───────────┐ ┌─▼───────────┐
│robot_    │ │agent_prompt_ │ │dialog_agent │ │agent_tools  │ │conversation_│
│config_   │ │builder       │ │(ReAct 循环) │ │(+ skills/)  │ │store        │
│service   │ │(提示词组装)  │ │             │ │             │ │(JSONL 持久化)│
└───┬──────┘ └────┬─────────┘ └──┬──────────┘ └─┬───────────┘ └─────────────┘
    │             │              │              │
    └─────────────┴──────────────┴──────────────┘
                  │                         │
        ┌─────────▼──────────┐   ┌──────────▼──────────┐
        │ backend/database.py│   │ llm_client (OpenAI  │
        │ (SQLite 共享)      │   │  兼容协议 + 思考开关)│
        └────────────────────┘   └─────────────────────┘
```

### 一次对话的完整链路

```
用户发送 message + {bot_id, department, platform, use_tools, enable_thinking}
  │
  ├─1. robot_config_service.resolve_runtime_config()
  │     robot_configs 表 → 补全科室/平台/公司/提示词版本 → runtime
  │
  ├─2. agent_prompt_builder.build_system_prompt(runtime)
  │     骨架 + prompts表业务提示词 + flow_trees流程知识 + 变量替换
  │
  ├─3. agent_tools.get_tool_schemas(department)
  │     按科室裁剪工具集（无 Skill 的科室隐藏 load_disease_skill）
  │
  ├─4. dialog_agent ReAct 循环（最多 5 轮）
  │     LLM → tool_calls? → execute_tool → 回灌 → LLM → ... → 最终回复
  │
  ├─5. conversation_store.record_turn()  持久化到 JSONL
  │
  └─→ {reply, segments, reasoning, tool_calls, meta, runtime}
```

---

## 二、文件结构

```
prompt_manager/agent/
├── main.py                     FastAPI 路由层（548 行）
├── dialog_agent.py             ReAct 引擎 + 会话记忆（307 行）
├── agent_prompt_builder.py     system prompt 动态组装（190 行）
├── agent_tools.py              工具集，function calling（320 行）
├── robot_config_service.py     三级级联 + 配置解析（185 行）
├── conversation_store.py       对话持久化与检索（269 行）
├── llm_client.py               LLM 客户端 + 思考开关（342 行）
├── flow_parser_service.py      流程树图片/PDF 解析（693 行，原有）
├── skills/                     病种话术 Skill 层
│   ├── registry.py             自动发现与注册（154 行）
│   ├── acne_skill.py           痤疮
│   ├── dermatitis_skill.py     皮炎
│   ├── eczema_skill.py         湿疹
│   ├── psoriasis_skill.py      银屑病
│   └── urticaria_skill.py      荨麻疹
├── data/conversations/         对话持久化目录（自动创建）
│   ├── sessions.jsonl          会话索引
│   └── messages_YYYY-MM-DD.jsonl  消息明细
└── AGENT_README.md             本文档

prompt_manager/agent_frontend/
├── app.js                      Vue3 单页（模板 + 逻辑）
└── style.css                   样式
```

### 各模块职责

| 模块 | 职责 | 关键设计 |
|---|---|---|
| `robot_config_service` | 三级级联选项、运行时配置合并 | 配置优先级：默认 < robot_configs 表 < 显式入参 |
| `agent_prompt_builder` | 组装 system prompt | 4 层拼装 + 逐级降级 + 6000 字知识截断 |
| `agent_tools` | 5 个工具的实现与 schema | 按科室裁剪工具集；异常转文本回灌不中断 ReAct |
| `skills/registry` | Skill 自动发现 | 扫描 `*_skill.py`，按 department 隔离 |
| `dialog_agent` | ReAct 编排、会话记忆 | 最多 5 轮工具调用，超限强制无工具收口 |
| `conversation_store` | 对话持久化、列表检索 | 索引全量入内存 + 快慢双路径检索 |
| `llm_client` | LLM 调用 | 按模型 family 分派思考开关参数 |

---

## 三、与 shining_dialog_agents 的整合对照

### 3.1 整合清单

| 原项目能力 | 位置 | 本项目 | 状态 |
|---|---|---|---|
| ReAct 编排 | `src/agents/agent.py` | `dialog_agent.py` | 已整合（改自研实现） |
| 多轮记忆 | LangGraph `InMemorySaver` | `SessionStore`（LRU 500） | 已整合 |
| 系统提示词 | `assets/system_prompt.json` | `agent_prompt_builder` 动态组装 | 已改造 |
| 流程话术工具 | `src/tools/` | `agent_tools.query_flow_strategy` | 已整合 |
| 机构信息工具 | `src/tools/company_info_tool.py` | `agent_tools.get_company_info` | 已改造（改查库） |
| 医学知识工具 | `src/tools/` | `agent_tools.search_medical_knowledge` | 已改造（改查库） |
| 病种话术 Skill | `src/skills/` × 5 | `skills/` × 5 | 已整合（+ 科室隔离） |
| Skill 加载工具 | `src/tools/load_disease_skill.py` | `agent_tools.load_disease_skill` | 已整合 |
| 对话日志 | `src/utils/conversation_logger.py` | `conversation_store.py` | 已改造（+ 索引检索） |
| `<sep>` 分句 | system prompt 约束 | prompt 约束 + `_split_segments` 兜底 | 已增强 |
| HTTP 服务 | `src/serve.py` (falcon) | `main.py` (FastAPI) | 已替换 |

### 3.2 关键改造决策

**1. 放弃 langchain / LangGraph，改自研 ReAct**

原项目依赖 `langchain`、`langgraph`、`langchain-openai`。`prompt_manager` 原本零 LLM 框架依赖，
引入全套会显著膨胀环境。改用 OpenAI 原生 function calling 手写循环，
约 60 行代码实现同等能力（工具调用 + 观察回灌 + 轮次上限保护）。

**2. 静态配置 → 数据库配置**

原项目科室/公司写死在 `config/agent_llm_config.json` 与 `assets/system_prompt.json`，
一个部署只能服务一个科室。改造后从 `robot_configs`（39 条）动态解析，
一个服务实例可服务全部机器人。

**3. Skill 增加 department 字段实现科室隔离**

原项目 5 个 Skill 全局可见，口腔科对话可能误命中「痤疮话术」。
新增 `department = "dermatology"` 字段，`registry.match_skill()` 按科室过滤，
且 `get_tool_schemas()` 对无 Skill 的科室直接隐藏 `load_disease_skill` 工具。

验证：`dermatology` → 5 个 Skill + 5 个工具；`hair` → 0 个 Skill + 4 个工具。

**4. 对话日志增加会话索引**

原项目 `conversation_logger` 按天写 JSONL，只能按 `session_id`/日期精确读取。
前端对话列表需要「按机器人筛选 + 按内容检索 + 分页」，逐条扫描全部日志不可行。
新增 `sessions.jsonl` 会话索引（全量载入内存），检索走快慢双路径：
先匹配标题/末条消息，未命中再回落全文扫描。

### 3.3 尚未整合的部分

| 原项目能力 | 未整合原因 |
|---|---|
| 流式输出（SSE） | 前端当前为整段渲染；ReAct 中间轮次流式语义复杂，暂缓 |
| `.env` 配置体系 | 本项目 LLM 配置统一由 prompt_manager 后端下发，无需独立 env |
| `scripts/` 批量评测脚本 | 属离线评测范畴，与在线服务解耦 |

---

## 四、API 清单

### Agent 相关

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/agent/cascade` | 三级级联选项一次性返回 |
| GET | `/api/agent/bots` | 按科室+平台筛选机器人id |
| GET | `/api/agent/config` | 预览当前筛选条件的生效配置 |
| POST | `/api/agent/chat` | Agent 对话（核心） |
| GET | `/api/agent/sessions` | 对话列表（机器人筛选 + 内容检索 + 分页） |
| GET | `/api/agent/sessions/{id}` | 加载历史会话并恢复上下文 |
| DELETE | `/api/agent/sessions/{id}` | 删除会话 |
| GET | `/api/agent/skills` | 列出病种话术 Skill |
| POST | `/api/agent/reset` | 清空会话记忆 |
| GET | `/api/agent/stats` | 会话统计 |

### `/api/agent/chat` 请求与响应

```jsonc
// 请求
{
  "message": "我脸上一直反复长痘怎么办",
  "session_id": null,          // 为空则新建
  "bot_id": "10122",           // 可空，空则用 department/platform 默认配置
  "department": "dermatology",
  "platform": "xhs",
  "temperature": 0.7,
  "use_tools": true,           // 工具开关
  "enable_thinking": false     // 模型思考开关
}

// 响应
{
  "session_id": "a1b2c3...",
  "reply": "痘痘反复长确实挺烦的。<sep>你现在主要是哪种痘痘？",
  "segments": ["痘痘反复长确实挺烦的。", "你现在主要是哪种痘痘？"],
  "reasoning": "用户描述反复长痘...",   // 思考内容，仅 enable_thinking=true 时有值
  "turn": 1,
  "tool_calls": [{"round":1,"name":"load_disease_skill","arguments":"{...}",
                  "result_preview":"...","elapsed_ms":3}],
  "meta": {"prompt_name":"dermatology_xhs_system","prompt_version":2,
           "flow_record_count":0,"department_zh":"皮肤科"},
  "runtime": {"bot_id":"","department":"dermatology","company":"..."},
  "error": null
}
```

---

## 五、核心机制详解

### 5.1 三级级联

```
科室 ──收窄──> 平台 ──收窄──> 机器人id
  ▲                              │
  └──────── 反向回填 ────────────┘
```

- **单向收窄**：选定科室后，平台下拉只显示该科室下有机器人配置的平台
- **反向回填**：选中机器人后自动回填其科室/平台，保证三者一致
- **重置会话**：任一层级变化都清空 `session_id`，避免沿用旧人格上下文

实测收窄效果：全部 39 → `hair` 18 → `hair` + `dy` 8。

### 5.2 system prompt 四层组装

```
AGENT_SKELETON          角色定义 / 说话风格 / ReAct 流程 / 通用约束
   +
prompts 表业务提示词    按 (department, platform, scene='system_prompt') 检索
   +                    版本由 robot_configs.prompt_version 决定
flow_trees 流程知识     该科室+平台的流程树描述（含机器人专属片段）
   +
变量替换                {公司} {域中文} {平台中文} {时间} {轮次} ...
```

**逐级降级**：`(dept, platform)` → `(dept, general)` → `(general, general)` → 内置兜底。

**已知坑**：`database.resolve_robot_prompt_version()` 不过滤 `scene`，
`ORDER BY id DESC` 会命中打分提示词（如 `hair_dy_score`）。
`_load_business_prompt()` 已加 `scene == 'system_prompt'` 校验修正。
**建议后续在 database 层补上 scene 过滤**，否则其他调用方会踩同样的坑。

### 5.3 工具集

| 工具 | 作用 | 数据源 |
|---|---|---|
| `query_flow_strategy` | 查流程话术 | `flow_trees` + `flow_records` |
| `get_company_info` | 查机构信息 | `knowledge_bases` |
| `search_medical_knowledge` | 查医学科普 | `knowledge_bases` |
| `get_prompt_snippet` | 查场景话术 | `prompts` |
| `load_disease_skill` | 加载病种话术 | `skills/` 模块（仅皮肤科） |

知识库条目结构 `{text, type, bot_id}`，`bot_id` 为空表示通用条目，
`_load_kb_items()` 只返回「通用 + 当前机器人专属」，实现机器人级知识隔离。

### 5.4 ReAct 循环保护

- **轮次上限** `MAX_REACT_ROUNDS = 5`：超限强制无工具收口，防死循环
- **工具异常不中断**：`execute_tool()` 把异常转成可读文本回灌给模型
- **协议完整性**：带 `tool_calls` 的 assistant 消息必须入历史，否则下一轮 API 报错
- **历史裁剪** `MAX_HISTORY_MESSAGES = 40`：裁剪时不把 `tool` 消息与其母消息切散

### 5.5 模型思考开关

各厂商协议不统一，`llm_client._thinking_params()` 按模型名分派：

| 模型 family | 参数 |
|---|---|
| Qwen | `{"enable_thinking": bool}` |
| GLM / Doubao | `{"thinking": {"type": "enabled\|disabled"}}` |
| Claude | `{"thinking": {"type": "enabled"}}`（关闭时不下发） |
| 未知 | 同时下发两种写法，多余字段一般被服务端忽略 |

思考内容从响应的 `reasoning_content` 提取，**不进对话历史**
（避免污染上下文与放大 token 消耗），仅回传前端折叠展示。

> 注意：DeepSeek 的推理由模型版本决定（`deepseek-reasoner`），
> 运行时开关对 `deepseek-v4-flash` 可能无效，属服务端行为。

### 5.6 对话持久化

```
data/conversations/
├── sessions.jsonl                会话索引，同 id 后写覆盖前写，启动时全量载入内存
└── messages_2026-08-12.jsonl     消息明细，追加写，永不覆盖
```

- **删除会话**只从索引移除，消息明细保留用于审计
- **检索快慢双路径**：先匹配标题/末条消息，未命中再全文扫描
- **损坏行容错**：JSONL 解析失败的行跳过，不影响整体读取

---

## 六、前端交互

### 6.1 工具栏

```
[科室 ▾] [平台 ▾] [机器人id ▾] [配置徽标] [检索框] ... [+上传流程树] [流程树解析] [LLM 对话]
```

- 机器人配置框位于**平台筛选框与检索框之间**
- 配置徽标显示提示词版本、锁定状态、流程片段数
- 检索框语义随页签切换：流程树页 = 搜流程树；对话页 = **检索对话内容**

### 6.2 LLM 对话页布局

```
┌── 左栏：对话列表 ─────────┐ ┌── 右栏：Agent 对话 ──────────────────┐
│ 对话列表 共 N 条  [+新对话]│ │ 标题 · 机器人  [工具☑][思考⚪][清空] │
│ ┌─ 按机器人 id 筛选 ─┐    │ │ ─ 生效配置条 ─                       │
│ │ 10122          清除│    │ │ ┌ 模型思考 (216字) 展开 ┐            │
│ └───────────────────┘    │ │ ┌ 工具调用轨迹 ┐                     │
│ ┌─────────────────────┐  │ │ ┌ 气泡1 ┐ ┌ 气泡2 ┐  ← <sep> 分句     │
│ │ 标题                ✕│  │ │                                      │
│ │ 末条消息...          │  │ │ ┌ 输入框 ────────────┐ [发送]        │
│ │ [10122][皮肤科][2轮] │  │ └──────────────────────────────────────┘
│ └─────────────────────┘  │
│      上一页 1/3 下一页    │
└──────────────────────────┘
```

- **机器人id 输入框**：模糊匹配，与上方科室/平台筛选叠加生效
- **思考开关**：位于清空按钮左侧，滑动样式
- **点击对话**：恢复历史消息 + 回填机器人配置，可继续对话
- **发送后**自动刷新列表（新会话置顶）

---

## 七、配置与运行

```bash
# 启动（默认 8901 端口）
cd prompt_manager/agent && python main.py

# 对话数据目录（可选，默认 agent/data/conversations）
export AGENT_CONV_DIR=/path/to/conversations
```

LLM 配置不在本服务，统一从 prompt_manager 后端（默认 `http://localhost:8900`）拉取，
在提示词管理系统界面配置 Base URL / API Key / 模型名。

### 关键常量

| 常量 | 值 | 位置 |
|---|---|---|
| `MAX_REACT_ROUNDS` | 5 | `dialog_agent.py` |
| `MAX_HISTORY_MESSAGES` | 40 | `dialog_agent.py` |
| `MAX_SESSIONS` | 500 | `dialog_agent.py` |
| `MAX_FLOW_KNOWLEDGE_CHARS` | 6000 | `agent_prompt_builder.py` |
| `TITLE_MAX_CHARS` | 24 | `conversation_store.py` |

---

## 八、已验证场景

| 场景 | 结果 |
|---|---|
| 三级级联收窄 | 39 → hair 18 → hair+dy 8 |
| 提示词按科室切换 | 植发抖音 `hair_dy_system` v39；口腔小红书 `dentistry_xhs_system` v2 |
| 人格差异 | 口腔科小红书回复带「宝子」，植发抖音为顾问口吻 |
| Skill 科室隔离 | dermatology 5 个 Skill / 5 工具；hair 0 个 / 4 工具 |
| 工具调用 | `load_disease_skill` + `search_medical_knowledge` 正常触发 |
| 多轮记忆 | 第 2 轮基于「25岁 + 父亲脱发」推进套联 |
| 思考开关 | `enable_thinking=true` 捕获 216 字 reasoning |
| 对话持久化 | 会话索引 + 消息明细写入正常 |
| 机器人筛选 | `bot_id=10122` 精确命中 1 条 |
| 内容检索 | 关键词「牙齿」「下巴」命中正确 |
| 会话恢复/删除 | 消息数与轮次一致，删除后列表同步 |
| 价格合规 | 未编造具体报价，引导面诊 |

---

## 九、后续改进建议

1. **database 层补 scene 过滤**：`resolve_robot_prompt_version()` 目前会误命中打分提示词
2. **流式输出**：对话页改 SSE，提升长回复的感知速度
3. **会话索引持久化优化**：会话量到万级时改用 SQLite 表替代 JSONL
4. **Skill 支持库覆盖**：允许运营在 prompts 表中覆盖内置病种话术，无需改代码
5. **补充其他科室 Skill**：目前仅皮肤科 5 个，植发/口腔可按同一接口扩展
6. **流程树覆盖率**：`hair/dy` 等组合尚未上传流程树，上传后知识自动注入无需改码
