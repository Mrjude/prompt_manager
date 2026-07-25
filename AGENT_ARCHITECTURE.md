# Prompt Manager · Agent 架构文档

> 本文基于 `prompt_manager/agent/` 现有实现整理，并给出可扩展的 Agent 架构演进建议。

---

## 一、Agent 在整体项目中的定位

`prompt_manager` 由两个独立 FastAPI 服务组成，共享同一 SQLite：

```text
┌────────────────────────┐        ┌────────────────────────┐
│  主服务 backend (8900)  │  同库  │  Agent 服务 (8901)     │
│  提示词/知识库/流程树/   │◀──────▶│  LLM 对话 + 图片解析   │
│  用户权限/LLM 多版本配置 │ SQLite │  流程树管理/文件直链   │
└─────────▲──────────────┘        └──────────▲─────────────┘
          │                                   │
     主前端 SPA                          Agent 前端 SPA
```

Agent 服务承担两类职责：

1. 通用 LLM 对话代理（`/api/chat`）：不自存 key，运行时向主服务拉取“当前激活的 LLM 配置”，转成 OpenAI 兼容请求。
2. 图片/PDF 解析代理（流程树解析）：上传图片/PDF → OCR/多模态 → 抽象为自然语言流程描述并入库。

---

## 二、目录与文件职责

```text
agent/
├── main.py                 # FastAPI 入口：路由层（对话/流程树CRUD/文件服务/图片直解析）
├── llm_client.py           # LLM 客户端：拉取激活配置 + OpenAI 兼容调用 + 重试 + 日志
├── flow_parser_service.py  # 解析编排：视觉能力探测 → 多模态直读 / OCR+文本 兜底 → 清洗
├── requirements.txt
└── start.sh

agent_frontend/
├── index.html              # 单页应用（流程树图库 + 解析 + LLM 对话）
├── app.js
└── style.css
```

| 文件 | 层次 | 关键职责 |
|---|---|---|
| `main.py` | 接入/路由层 | REST 接口、Pydantic 校验、把请求编排到底层能力 |
| `llm_client.py` | 模型访问层 | `LLMClient` 单例；`_load_config` 拉主服务激活配置；`chat / chat_with_image(s)`；3 次重试；jsonl 日志 |
| `flow_parser_service.py` | 工具/能力层 | 解析流水线：OCR skill、视觉能力探测与缓存、多模态解析、OCR 文本兜底、自然语言清洗 |
| `database.py`（复用主服务） | 数据层 | 流程树库 / 记录 CRUD、LLM 配置读取；与主服务同库 |
| `agent_frontend/` | 展示层 | 图库浏览、上传解析、描述编辑、对话 |

---

## 三、运行时调用链

### 3.1 通用对话 `/api/chat`

```text
调用方 → POST /api/chat {messages, system_prompt, temperature, max_tokens}
  └ get_llm_client(PM_URL)                  # 单例
      └ _load_config(): GET 主服务 /api/settings/llm/raw   # base_url/key/model
  └ llm.chat(messages)
      └ _call_api(): POST {base_url}/chat/completions       # OpenAI 兼容 + 重试 + 日志
  └ ChatResponse{content, model}
```

### 3.2 流程树/对话截图解析

```text
上传文件(image/pdf)
  └ save_uploaded_file → 落盘 flow_data/{flow_id}/{ts}_{name}
  └ create_flow_record(status=pending)
  └ parse_file(file_path, file_type):
      ├ (pdf) pdf_to_images: PyMuPDF 优先 / pdf2image 兜底
      ├ get_ocr_text: dialogue-flow-parser skill → 失败则内置 rapidocr(跨解释器)
      ├ _probe_vision_capability(llm): 1×1 探测图判定能否读图（按 base_url+model 缓存）
      ├ 支持视觉 → llm.chat_with_image(SYSTEM_PROMPT + OCR 锚点)
      │ 不支持   → llm.chat(SYSTEM_PROMPT + OCR 文本)
      └ _clean_to_natural: 去 Markdown/JSON/列表/编号 → 纯自然语言
  └ update_flow_record(description, structure, status)
```

---

## 四、当前 Agent 架构类型判定

按业界常见 Agent 分类，`prompt_manager/agent` 属于：

> 单体 / 工具增强型 LLM 服务（Tool-augmented single LLM service），尚未构成“自主规划的 Agent”。

| 维度 | 现状 |
|---|---|
| 规划 Planning | 无。流程由代码硬编码（探测→解析→清洗） |
| 工具调用 Tool use | 有，但是**代码编排式**固定管道，非模型自选 |
| 记忆 Memory | 短期无；长期＝SQLite 流程树描述（知识沉淀） |
| 多轮自治 Loop | 无 ReAct/反思循环，一次请求一次产出 |
| 多智能体 | 无 |
| 配置外置 | 有，LLM/提示词从主服务动态获取 |

一句话：它是**“配置驱动 + 固定工具管道”的 LLM 网关型服务**，工程健壮性好（重试、降级、能力探测、日志），但缺“自主决策”层。

---

## 五、业界主流 Agent 架构一览

### 5.1 按单体能力

1. **Tool-augmented LLM（工具增强）**：LLM + 固定工具管道（当前本项目）。可控稳定，但不灵活。
2. **ReAct（Reasoning + Acting）**：模型交替“思考→选工具→观察→再思考”，自主决定调用与结束。适合任务不固定。
3. **Plan-and-Execute（先规划后执行）**：Planner 出计划，Executor 逐步执行，必要时 Replan。适合多步复杂任务。
4. **Reflection / Self-critique（反思）**：产出后自评修正再输出（本项目 `score` 评分提示词接近雏形）。

### 5.2 按协作规模

5. **Router / Orchestrator（路由编排）**：调度器按意图把请求分发到不同子 Agent / 工具链。
6. **Multi-Agent（多智能体）**：多角色（规划/执行/审查）消息协作，如 AutoGen、CrewAI、LangGraph 多节点。

### 5.3 按框架落地

7. **Graph / State-machine Agent（图/状态机）**：以有向图描述节点与转移（LangGraph 代表），天然适合“分支+循环+人审”。

---

## 六、面向可扩展的推荐架构

结合本项目“客服对话/流程图解析 + 未来可能接 NER、评分、话术生成、质检等”的诉求，推荐向 **Router + Tool Registry + 可选 ReAct 循环** 的分层架构演进。

```text
              ┌──────────────────────────────┐
              │         API 接入层            │  FastAPI 路由（保持现状）
              └───────────────┬──────────────┘
              ┌───────────────▼──────────────┐
              │       Agent Orchestrator      │  意图路由 / (可选)ReAct 循环
              └───────┬───────────────┬───────┘
          ┌───────────▼───┐   ┌───────▼─────────┐
          │ Tool Registry  │   │ Capability层    │
          └───────┬────────┘   └───────┬─────────┘
      ┌───────────┼───────────┬────────┴───────────┐
      ▼           ▼           ▼                    ▼
 flow_parse   chat        knowledge_search      score/qc ...
      │
      ▼
 ┌───────────┐ ┌───────────┐ ┌───────────────┐
 │LLMProvider│ │OCRProvider│ │ Memory/Store  │
 └───────────┘ └───────────┘ └───────────────┘
```

### 6.1 分层职责

| 层 | 职责 | 对应现有代码 |
|---|---|---|
| API 接入层 | HTTP 路由、鉴权、参数校验 | `main.py`（保留） |
| Orchestrator | 意图路由；可选 ReAct/Plan 循环；统一超时/重试 | 新增 `orchestrator.py` |
| Tool Registry | 注册表登记工具（名称、入参 schema、执行函数） | 新增 `tools/__init__.py` |
| Capability/Tools | 每个能力独立成模块（解析、对话、检索、评分…） | `flow_parser_service.py` 拆分归位 |
| Provider 抽象 | LLM / OCR / 存储各自接口，多实现可插拔 | `llm_client.py` 抽象为 `LLMProvider` |
| Memory/Store | 会话短期记忆 + 长期知识（可加向量库） | `database.py` + 可选向量层 |

### 6.2 关键设计点

1. **Provider 接口化（最优先、收益最大）**
   - 定义抽象基类：`LLMProvider(chat / chat_with_image / supports_vision)`、`OCRProvider(extract_text)`。
   - 现有 `LLMClient`、`rapidocr` 各成为一个实现。
   - 收益：换模型 / 换 OCR / 加多模态只加实现，不改业务代码。

2. **工具注册表（Tool Registry）**
   - 用装饰器登记工具，统一入参/出参 schema，便于 Orchestrator 发现与调用：

   ```python
   @register_tool(name="flow_parse", desc="解析流程图/对话截图为自然语言")
   def flow_parse(file_path: str, file_type: str) -> dict: ...
   ```

   - 后续要加“知识检索工具、评分工具、NER 工具”只需注册，无需改路由。

3. **Orchestrator（编排器）**
   - 初期做**规则路由**：根据请求类型（chat / parse / search）分发到对应工具，等价于把 `main.py` 里 if 逻辑上移收敛。
   - 进阶做**ReAct 循环**：让模型基于工具清单自主选择工具、串联多步（如“先 OCR → 再检索知识库 → 再生成话术”）。

4. **Memory / 上下文**
   - 短期：对话轮次上下文（当前 `/api/chat` 由调用方自带 messages，可在 Orchestrator 内维护 session）。
   - 长期：流程树描述已入库，可再引入向量库（如 chroma/faiss）支撑语义检索工具。

5. **统一横切能力**
   - 把现有的“重试、超时、降级（视觉→OCR）、jsonl 日志、能力探测缓存”下沉为 Provider/Tool 的通用装饰器，所有工具共享。

### 6.3 渐进式演进路线（低风险）

| 阶段 | 目标 | 动作 | 兼容性 |
|---|---|---|---|
| P0（现状） | 工具管道 | 无 | — |
| P1 | Provider 抽象 | 抽 `LLMProvider`/`OCRProvider`，现有类改为实现 | 完全兼容，接口不变 |
| P2 | Tool Registry | 把 `chat`/`flow_parse`/`parse_image` 注册为工具 | 路由改为查注册表 |
| P3 | Orchestrator 规则路由 | 新增 `orchestrator.py`，`main.py` 只做 HTTP 适配 | 行为不变，结构更清晰 |
| P4 | ReAct/Plan 可选 | 对复杂任务启用模型自主多步 | 通过开关灰度 |
| P5 | Multi-Agent（可选） | 规划/执行/审查分离，或引入 LangGraph | 按需 |

### 6.4 选型建议

- **不建议**一步到位上多智能体框架（AutoGen/CrewAI）——当前业务是“确定性管道”，多智能体会引入不可控性和成本。
- **建议**优先做 P1 + P2 + P3：只做“接口抽象 + 工具注册 + 规则编排”，即可获得 80% 的可扩展收益，且风险最低、对现有接口零破坏。
- 若后续任务变得“开放式、需要模型自己决定步骤”，再在 Orchestrator 内引入 **ReAct** 或 **LangGraph 状态图**（保留人审节点，契合客服质检场景）。

---

## 七、现有实现的工程亮点（值得保留）

- **配置与运行解耦**：Agent 不存密钥，运行时拉主服务激活配置，切模型无需改 Agent。
- **视觉能力探测 + 优雅降级**：先探测模型是否支持图片，不支持自动降级 OCR+文本，保证不同模型都能出结果。
- **跨解释器 OCR 兜底**：`OCR_PYTHON` / 多路径探测，解决部署环境缺依赖问题。
- **输出清洗**：`_clean_to_natural` 强制纯自然语言，保证结果可直接拼进 prompt。
- **可观测**：`llm_api_*.jsonl` + `flow_parser_*.log` 全链路日志。

这些能力在演进为分层 Agent 架构后应作为通用横切装饰器保留复用。

---

## 八、一句话总结

- **当前**：配置驱动 + 固定工具管道的“LLM 网关型服务”，稳定但扩展靠改代码。
- **推荐**：按 `Provider 抽象 → Tool Registry → Orchestrator 规则路由 →（可选）ReAct/图` 渐进演进，即可在不破坏现有接口的前提下，把“新增一个能力”从“改代码”降为“注册一个工具”。
