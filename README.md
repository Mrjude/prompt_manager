# Prompt Manager · 提示词 / 知识库 / 流程树 一体化管理平台

面向多科室 × 多平台的客服 / 销售对话场景，提供一站式的：

- 提示词管理：科室 × 平台 × 场景三维索引 + 变量引擎 + 版本回滚 + WebSocket 热更新
- 知识库管理：11 类知识标签 + `bot_id` 机器人维度 + 通用记录合并检索
- 流程树管理：上传流程图 / 客服对话截图，多模态或 OCR + LLM 抽象为纯自然语言描述
- LLM 多版本配置：维护多个 OpenAI 兼容 API，一键切换激活
- Agent 服务：独立 `8901` 端口，提供对话、图片直读、流程图谱和跨库搜索
- Python 客户端 SDK：本地缓存 + 轮询 / WebSocket 双通道热更新
- 登录与权限：内置账号体系，按提示词/知识库/流程树三大模块细分查看/编辑/删除权限

主服务 `8900`，Agent 服务 `8901`，两者共享同一 SQLite 数据库。

---

## 1. 项目结构

```text
prompt_manager/
├── backend/                     # 主服务（默认 8900）
│   ├── main.py                  # FastAPI：REST + WebSocket + 登录鉴权中间件
│   ├── database.py              # SQLite CRUD、内存缓存、变量解析、用户/会话、LLM 多版本
│   ├── models.py                # Pydantic 模型
│   ├── prompt_manager.db        # SQLite（首次启动自动建表 + 幂等迁移）
│   ├── flow_data/               # 流程树原始文件 + LLM 调用日志
│   └── start.sh
├── agent/                       # Agent 服务（默认 8901）
│   ├── main.py                  # /api/chat、流程树管理、图片直解析、文件直链
│   ├── llm_client.py            # OpenAI 兼容客户端（chat / chat_with_image(s)）
│   ├── flow_parser_service.py   # 统一流程树解析：视觉能力探测 + OCR 兜底 + 自然语言清洗
│   └── start.sh
├── frontend/index.html          # 主管理界面（Vue 3 CDN 单文件）
├── agent_frontend/              # Agent 前端（index.html + app.js + style.css）
├── client_sdk/prompt_client.py  # PromptClient：提示词 + 知识库 + 流程树描述
├── knowledge_retriever.py       # KnowledgeRetriever：TF-IDF + 可选语义检索
├── pyproject.toml               # SDK 打包（prompt-manager-client）
├── start.sh                     # 一键启动主服务 + Agent
└── README.md
```

---

## 2. 架构与数据流

```text
┌────────────────────────┐          ┌────────────────────────┐
│  主服务 (8900)         │  同库    │  Agent 服务 (8901)     │
│  - 提示词/知识库/流程树 │◀────────▶│  - LLM 对话            │
│  - 用户/权限/登录       │  SQLite  │  - 流程图/对话截图解析 │
│  - LLM 多版本配置       │          │  - 图片直读 parse_image│
│  - WebSocket 广播       │          │  - 跨库搜索/文件直链   │
└─────────▲──────────────┘          └──────────▲─────────────┘
          │ HTTP / WS                          │ HTTP
   ┌──────┴──────┐                      ┌──────┴──────┐
   │ 主前端 SPA   │                      │ Agent 前端   │
   └─────────────┘                      └─────────────┘
          │
          ▼
   ┌─────────────┐
   │ PromptClient│  业务服务通过 SDK 拉取已解析提示词/知识库/流程描述
   └─────────────┘
```

- 主服务与 Agent 共享同一 SQLite 文件（`PROMPT_DB_PATH`）。
- Agent 通过 `PROMPT_MANAGER_URL` 从主服务读取激活的 LLM 配置。
- 主服务通过 `sys.path.insert("../agent")` 直接复用 Agent 的 `flow_parser_service`，避免解析实现分叉。
- LLM 调用日志：`backend/flow_data/api_logs/llm_api_YYYYMMDD.jsonl` + `flow_parser_YYYYMMDD.log`。

---

## 3. 数据库表

| 表                  | 关键字段                                                                                                                      | 说明                                              |
| ------------------- | ----------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| `prompts`         | `name UNIQUE`, `department/platform/scene`, `content`, `variables`, `variable_bindings`, `version`, `is_active` | 当前激活提示词                                    |
| `prompt_versions` | `prompt_id, version, content, change_log`                                                                                   | 全量版本历史                                      |
| `knowledge_bases` | `(department, platform) UNIQUE`, `content`                                                                                | JSON 数组：`[{text, type, bot_id}, ...]`        |
| `flow_trees`      | `(department, platform) UNIQUE`, `description`                                                                            | 流程树库（按科室+平台分组）                       |
| `flow_records`    | `flow_id`, `file_name`, `file_type`, `file_path`, `description`, `structure`, `status`, `error`               | 一次上传 = 一条记录；`description` 为纯自然语言 |
| `settings`        | `key, value`                                                                                                                | 旧版 LLM 配置兼容存储                             |
| `llm_versions`    | `name, base_url, api_key, model_name, is_active`                                                                            | LLM 多版本，激活时同步写入`settings`            |
| `users`           | `username UNIQUE, password_hash, role, can_prompt_*/can_knowledge_*/can_flow_*, is_active`                                  | 登录账号 + 页面权限                               |
| `auth_sessions`   | `token, user_id, expires_at`                                                                                                | 登录会话（默认 30 天）                            |
| `bot_ids`         | `bot_id UNIQUE`                                                                                                             | 机器人 ID 白名单（前端下拉候选）                  |

`flow_records.status ∈ {pending, parsing, success, partial, failed, unparsed}`。

启动 `init_db()` 时会：

- 幂等建表；对旧 `prompts` 补 `variables`、`variable_bindings` 列；对旧 `users` 补 9 个 `can_*` 权限列。
- 把旧「纯字符串数组」知识库内容迁移为 `{text, type: 答疑, bot_id: 9378}` 对象数组。
- 若不存在默认管理员，自动创建 `admin / admin123456` 并赋满权限；已存在则强制刷新为 admin 且 9 项权限全开。

---

## 4. 元数据枚举

| 维度                 | 取值                                                                                                                                                |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| 科室`department`   | `hair` 植发 · `dentistry` 口腔 · `dermatology` 皮肤 · `ophthalmology` 眼科 · `pediatrics` 儿科 · `beauty` 医美 · `general` 通用 |
| 平台`platform`     | `xhs` 小红书 · `bd` 百度 · `dy` 抖音 · `kuaishou` 快手 · `wechat` 微信 · `general` 通用                                            |
| 场景`scene`        | `system_prompt` 系统提示词 · `warmup` 暖场语 · `knowledge` 知识模板 · `action_desc` 动作描述 · `score` 评分 · `general` 通用       |
| 知识标签`KB_TYPES` | 答疑 · 问诊 · 套联 · 流程 · 默认认知 · 额外 · 问诊约束 · 答疑约束 · 套联约束 · 核心约束 · 违禁词                                          |
| 内置机器人 ID        | `7422 / 8714 / 8686 / 8542 / 8771 / 9125 / 9378 / 10122 / 10569 / 9352 / 9358 / 9682`                                                             |
| 内置变量             | `{公司} {域中文} {域英文} {时间} {轮次} {轮次k} {套联描述} {动作描述} {知识描述} {暖场描述}`                                                      |

`{公司}` 通过 `ROBOT_COMPANY_MAP` 由 `robot_id` 映射为 `雍禾 / 碧莲盛 / 唐森`。

---

## 5. API 概览

> 主服务前缀 `http://localhost:8900`，Agent 前缀 `http://localhost:8901`。字段详情见 `http://localhost:8900/docs`。

### 5.1 登录与权限（主服务）

| 方法                | 路径                  | 说明                                                                   |
| ------------------- | --------------------- | ---------------------------------------------------------------------- |
| POST                | `/api/auth/login`   | 用户名密码登录，返回`token` + `user`                               |
| GET                 | `/api/auth/me`      | 当前登录用户（Header`Authorization: Bearer` 或 cookie `pm_token`） |
| POST                | `/api/auth/logout`  | 注销 token                                                             |
| GET/POST/PUT/DELETE | `/api/users[/{id}]` | admin：账号 CRUD（`role ∈ {admin, normal}`）                        |

权限中间件白名单（无需登录）：`/api/auth/login`、`/api/auth/heartbeat`、`/api/v1/*`、`/api/meta/*`、`/api/settings/*`、`/docs`、`/openapi.json`、`/favicon.ico`。其他 `/api/**` 未登录返回 401；不满足对应 `can_*` 位返回 403。

### 5.2 提示词（主服务）

| 方法               | 路径                                                        | 说明                                                      |
| ------------------ | ----------------------------------------------------------- | --------------------------------------------------------- |
| POST               | `/api/prompts`                                            | 创建                                                      |
| GET                | `/api/prompts`                                            | 列表（`department / platform / scene / keyword`，分页） |
| GET / PUT / DELETE | `/api/prompts/{id}`                                       | 详情 / 更新（内容变更版本+1） / 软删除                    |
| GET                | `/api/prompts/{id}/versions`                              | 版本历史                                                  |
| POST               | `/api/prompts/{id}/rollback`                              | 回滚到指定版本                                            |
| DELETE             | `/api/prompts/{id}/versions/{vid}`                        | 删除某条历史版本（禁止删当前版本）                        |
| GET                | `/api/v1/fetch?department&platform&scene&current_version` | 三维查询（命中内存缓存）                                  |
| GET                | `/api/v1/fetch/{name}?current_version`                    | 按名称查询                                                |
| POST               | `/api/v1/resolve`                                         | 变量解析：返回填充好的提示词                              |
| POST               | `/api/v1/batch_fetch`                                     | 批量按名称获取                                            |
| GET                | `/api/v1/sync`                                            | 全量同步（供 SDK 冷启动）                                 |

调试：`/api/stats`、`/api/debug/cache`、`/api/debug/db`、`POST /api/debug/reload_cache`。

### 5.3 知识库（主服务）

| 方法               | 路径                    | 说明                                                         |
| ------------------ | ----------------------- | ------------------------------------------------------------ |
| POST               | `/api/knowledge`      | 创建（`(department, platform)` 唯一）                      |
| GET                | `/api/knowledge`      | 列表（`department / platform / keyword`）                  |
| GET / PUT / DELETE | `/api/knowledge/{id}` | 详情 / 更新`content` / 删除                                |
| GET                | `/api/v1/knowledge`   | 供 SDK 拉取（公开只读）                                      |
| GET                | `/api/meta/kb_types`  | 知识标签枚举（含约束/违禁词类）                              |
| GET / POST         | `/api/meta/bot_ids`   | 机器人 ID 枚举（GET 查询、POST 追加，POST 需知识库编辑权限） |

### 5.4 流程树（主服务 + Agent 双端提供）

| 方法               | 路径                                          | 说明                                                           |
| ------------------ | --------------------------------------------- | -------------------------------------------------------------- |
| POST               | `/api/flow_trees`                           | 创建流程树库（`科室+平台` 唯一）                             |
| GET                | `/api/flow_trees`                           | 列表                                                           |
| GET / PUT / DELETE | `/api/flow_trees/{flow_id}`                 | 详情 / 改描述 / 删除（级联删记录）                             |
| GET                | `/api/flow_trees/{flow_id}/records`         | 库下所有解析记录                                               |
| POST               | `/api/flow_trees/{flow_id}/records/upload`  | 上传图片/PDF + 可选自动解析（主服务异步、Agent 同步）          |
| GET / PUT / DELETE | `/api/flow_records/{record_id}`             | 单条记录                                                       |
| POST               | `/api/flow_records/{record_id}/reparse`     | 重新解析                                                       |
| GET                | `/api/flow_records/search`（Agent）         | 跨库搜索：`department / platform / keyword`，附 `file_url` |
| GET                | `/api/files/{flow_id}/{file_name}`（Agent） | 直链访问上传文件                                               |
| POST               | `/api/parse_image`（Agent）                 | 上传图片直接解析（不入库）                                     |
| GET                | `/api/v1/flow_records/search`（主服务）     | 供 SDK 拉取流程树描述（公开只读）                              |

### 5.5 LLM 配置（主服务）

| 方法                      | 路径                                         | 说明                                                       |
| ------------------------- | -------------------------------------------- | ---------------------------------------------------------- |
| GET                       | `/api/settings/llm`                        | 当前激活配置（api_key 掩码）                               |
| GET                       | `/api/settings/llm/raw`                    | 原始配置（Agent 内部调用）                                 |
| PUT                       | `/api/settings/llm`                        | 兼容旧接口直接保存                                         |
| GET / POST / PUT / DELETE | `/api/settings/llm/versions[/{id}]`        | 多版本 CRUD（`****` 的 `api_key` 保持不变）            |
| POST                      | `/api/settings/llm/versions/{id}/activate` | 激活（同步写入`settings` 与 `llm_versions.is_active`） |

### 5.6 Agent 通用

| 方法 | 路径                                                | 说明                      |
| ---- | --------------------------------------------------- | ------------------------- |
| GET  | `/api/llm/status`                                 | 检查激活配置是否完整      |
| POST | `/api/llm/refresh`                                | 清空 LLM 配置缓存重新拉取 |
| POST | `/api/llm/test`                                   | 一句话探活                |
| POST | `/api/chat`                                       | 通用对话（OpenAI 兼容）   |
| GET  | `/api/meta/departments` / `/api/meta/platforms` | 元数据                    |

### 5.7 WebSocket

- `ws://localhost:8900/ws/updates`
- 提示词 创建 / 更新 / 删除 / 回滚 事件实时广播；SDK 会自动订阅并刷新对应本地缓存。

---

## 6. 提示词使用指南

### 6.1 通过 SDK（推荐）

```python
from client_sdk import PromptClient   # or: from prompt_client import PromptClient

client = PromptClient(base_url="http://localhost:8900")
client.preload()                       # 预加载提示词 + 知识库 + 流程树描述到本地缓存
client.start_auto_update(interval=30)  # 30s 轮询热更新
# client.start_ws_update()             # 或走 WebSocket，需 pip install websocket-client

# 拿原文
content = client.get_content("hair_xhs_system")

# 拿"已解析变量"的提示词（最常用）
prompt = client.get_resolved(
    name="hair_xhs_system",
    robot_id="9125",       # {公司} → 雍禾/碧莲盛/唐森
    department="hair",
    current_round=3,       # {轮次} → 第3轮，{轮次k} → 3
    action_desc="\n回复意图：问诊、套联",
    knowledge_desc="米诺地尔是常用外用药……",
    warmup_desc="",
    connect_desc="留下联系方式可以安排专业医生……",
    extra_variables={"custom_marker": "客服话术片段"},  # 结合 variable_bindings 使用
)
```

### 6.2 直接 HTTP

```bash
# 按名称取
curl http://localhost:8900/api/v1/fetch/hair_xhs_system

# 三维取
curl "http://localhost:8900/api/v1/fetch?department=hair&platform=xhs&scene=system_prompt"

# 变量解析
curl -X POST http://localhost:8900/api/v1/resolve \
  -H "Content-Type: application/json" \
  -d '{"name":"hair_xhs_system","robot_id":"9125","current_round":3,"action_desc":"\n回复意图：问诊"}'
```

### 6.3 变量解析规则

优先级：`variable_bindings 映射 > 内置变量 > variables 自定义变量 > extra_variables 直接替换`。

- **内置变量**：由 `resolve_prompt_variables` 自动填充，见「元数据枚举」。
- **`variable_bindings`**：管理页面在“变量映射”里维护，格式 `{"变量名": "占位符标记"}`，可把外部字段绑定到任意自定义标记：

```json
{
  "warmup_desc": "{暖场占位符}",
  "knowledge_desc": "{知识占位符}"
}
```

- **`extra_variables`**：在 `resolve` 调用里传入，键名与 `variable_bindings` 对应，可动态注入。

---

## 7. 知识库使用指南

### 7.1 内容格式

```json
[
  {"text": "雄激素性脱发表现为头皮渐进性毛发稀疏……", "type": "答疑", "bot_id": "9378"},
  {"text": "请问您脱发持续多长时间了？",              "type": "问诊", "bot_id": "9378"},
  {"text": "留下联系方式可以安排专业医生……",       "type": "套联", "bot_id": "9378"},
  {"text": "禁止提及具体药品名和价格……",            "type": "违禁词", "bot_id": ""}
]
```

- `type ∈ KB_TYPES`。
- `bot_id` 为空字符串表示「通用记录」：SDK 在按 `bot_id` 过滤时，会**同时命中该 bot_id 的记录和 `bot_id == ""` 的通用记录**。
- 旧「纯字符串数组」格式启动时自动迁移。

### 7.2 通过 SDK 使用

```python
# 全部条目文本
items = client.get_knowledge(department="hair", platform="xhs")

# 按知识标签过滤
qa = client.get_knowledge(department="hair", platform="xhs", knowledge_type="答疑")

# 按机器人 ID 过滤（会同时命中 bot_id="" 的通用记录）
kb_9378 = client.get_knowledge(department="hair", platform="xhs", bot_id="9378")

# 详情列表（含 type / bot_id）
details = client.get_knowledge_detail(department="hair", platform="xhs")

# 拼接为单段文本，便于直接插入 prompt
text = client.get_knowledge_text(
    department="hair", platform="xhs",
    knowledge_type="问诊", bot_id="9378", separator="\n"
)
```

### 7.3 通过检索器（KnowledgeRetriever）

```python
from knowledge_retriever import KnowledgeRetriever

retriever = KnowledgeRetriever(base_url="http://localhost:8900")
retriever.preload(department="hair", platform="xhs")

history = [
    {"role": "user", "content": "最近掉头发很严重怎么办"},
    {"role": "assistant", "content": "请问您掉发持续多长时间了？"},
]

# 关键词检索（默认 TF-IDF）
results = retriever.retrieve(history, "hair", "xhs", top_k=5)

# 按类型 / 机器人过滤
kb = retriever.retrieve(history, "hair", "xhs", top_k=5, knowledge_type="答疑", bot_id="9378")

# 直接拿拼接文本，便于喂给 prompt
kb_text = retriever.retrieve_as_text(history, "hair", "xhs", top_k=5, separator="\n")
```

启用语义检索（可选）：

```bash
pip install sentence-transformers numpy
```

```python
retriever = KnowledgeRetriever(
    base_url="http://localhost:8900",
    use_semantic=True,
    semantic_model="shibing624/text2vec-base-chinese",
)
```

---

## 8. 流程树使用指南

流程树把「业务流程图」或「客服对话截图」抽象为**纯自然语言**的流程逻辑，用于直接拼进 prompt。

### 8.1 解析流水线

```text
上传文件 (image / pdf)
   │
   ├── save_uploaded_file: 落盘到 backend/flow_data/{flow_id}/{ts}_{name}
   │
   └── parse_file  (可异步执行)
         │
         ├── (PDF)  pdf_to_images: PyMuPDF 优先，pdf2image 兜底，200 DPI
         │
         ├── run_skill_parser: 调 dialogue-flow-parser skill 只取 OCR 文本
         │        └─ 缺依赖时自动 fallback 到内置 rapidocr（进程内 或 跨解释器）
         │
         ├── _probe_vision_capability: 探测当前激活 LLM 是否真的能读图
         │        (发一张 1×1 探测图 + 单字问答；结果按 base_url+model 缓存)
         │
         ├── 若支持视觉  → llm.chat_with_image(SYSTEM_PROMPT + OCR 锚点)
         │   若不支持    → llm.chat(SYSTEM_PROMPT + OCR 文本)
         │
         └── _clean_to_natural: 剥离 Markdown / JSON / 列表 / 编号 / 树形线
```

- `flow_records.description` 存**纯自然语言段落**；`structure` 只保存最小页面元信息（页数 / 每页状态 / OCR 长度）。
- `status ∈ {pending, parsing, success, partial, failed, unparsed}`。
- 系统提示词能同时识别 A) 流程图 和 B) 客服对话截图，并把对话抽象为「如果访客… 则客服… 否则…」的循环流程逻辑，敏感信息（城市名/门店名/时间戳/姓名/手机号）自动脱敏。

### 8.2 视觉 / OCR 兼容矩阵

| LLM 是否支持图片输入                              | 处理路径                      | 依赖                                             |
| ------------------------------------------------- | ----------------------------- | ------------------------------------------------ |
| 支持（如`qwen-vl-max`、`gpt-4o`、`glm-4v`） | 多模态直读 + OCR 锚点         | LLM 视觉能力                                     |
| 不支持（如`deepseek-v4-flash`）                 | OCR 抽文本 + 纯文本 chat 抽象 | `rapidocr_onnxruntime`（推荐）或 `paddleocr` |

- 视觉能力探测结果按 `(base_url, model)` 缓存，切换 LLM 版本时会自动重探。
- OCR Python 解释器探测顺序：`OCR_PYTHON` 环境变量 → `sys.executable` → `/data/miniconda3/bin/python3` → `/usr/bin/python3` → `which python3`。找到装了 `rapidocr_onnxruntime` 的即用。
- 建议给服务运行环境装一次 `pip install rapidocr_onnxruntime`，兜底才最稳。

### 8.3 上传与查询

```bash
# ① 主服务：上传到指定流程树库（自动异步解析）
curl -X POST http://localhost:8900/api/flow_trees/1/records/upload \
     -F "file=@flow.png" -F "auto_parse=true"

# ② 拉取记录详情（轮询 status 直到 success / failed）
curl http://localhost:8900/api/flow_records/123

# ③ 重新解析
curl -X POST http://localhost:8900/api/flow_records/123/reparse

# ④ 跨库搜索（Agent 端，附 file_url）
curl "http://localhost:8901/api/flow_records/search?department=hair&platform=xhs&keyword=留资"

# ⑤ 一次性解析图片不入库（Agent 端）
curl -X POST http://localhost:8901/api/parse_image -F "file=@flow.png"
```

### 8.4 把流程描述拼给提示词

```python
# 方式一：SDK 一步到位
flow_desc = client.get_flow_descriptions(department="hair", platform="xhs")

prompt = client.get_resolved(
    name="hair_xhs_system",
    robot_id="9125",
    current_round=3,
    action_desc=f"\n业务流程参考：\n{flow_desc}",
)
```

---

## 9. LLM 配置与多版本切换

- 在主前端右上角「齿轮」图标进入 LLM 版本管理。
- 支持维护多个 OpenAI 兼容 API 版本（`base_url` / `api_key` / `model_name`），一键激活。
- `api_key` 在 GET `/api/settings/llm` 返回时会掩码，PUT 更新时若包含 `****` 则不覆盖。
- 激活时同步写入 `llm_versions.is_active` 与旧格式 `settings` 表，保证 SDK / Agent 读取一致。
- 变更配置后调用 `POST http://localhost:8901/api/llm/refresh` 或重启 Agent 让缓存生效。

**多模态模型建议**：

- 若解析流程图，优先选支持视觉输入的模型（`qwen-vl-max` / `qwen-vl-plus` / `gpt-4o` / `glm-4v` / `claude-3-5-sonnet`）。
- 若模型不支持图片（如 `deepseek-v4-flash`），系统会自动降级为 OCR + 文本 chat，仍能得到抽象流程描述。

---

## 10. 快速开始

### 10.1 一键启动（主服务 + Agent）

```bash
cd prompt_manager
bash start.sh
# 主服务:  http://localhost:8900   API 文档: /docs
# Agent :  http://localhost:8901
```

### 10.2 分别启动

```bash
# 主服务
cd backend && pip install -r requirements.txt && python3 main.py       # 8900

# Agent 服务
cd agent   && pip install -r requirements.txt && python3 main.py       # 8901
# 建议再装 OCR：pip install rapidocr_onnxruntime
```

### 10.3 首次使用流程

1. 浏览器打开 `http://localhost:8900`。
2. 使用默认账号 `admin / admin123456` 登录。
3. 右上角「齿轮」进入 LLM 配置，新增一个 OpenAI 兼容版本 → 「保存并激活」。
4. 需要解析流程图时，激活支持视觉的模型；否则任意兼容模型均可，Agent 会自动 OCR 兜底。
5. 打开 `http://localhost:8901` 或 `POST /api/llm/test` 验证连通性。

---

## 11. Python SDK

包名：`prompt-manager-client`（源码在 `client_sdk/`）。

### 11.1 安装

```bash
# 源码开发安装
cd prompt_manager
pip install -e .

# 或打包安装
pip install build
python -m build
pip install dist/prompt_manager_client-*.whl

# 可选依赖：WebSocket 热更新
pip install "prompt-manager-client[ws]"
```

发布到 PyPI：

```bash
pip install twine
python -m build
twine upload dist/*
```

### 11.2 核心接口

`PromptClient`（详见 `client_sdk/prompt_client.py`）：

- 提示词：`get / get_content / get_resolved / get_version / get_all_names`
- 知识库：`get_knowledge / get_knowledge_detail / get_knowledge_text / get_all_knowledge / refresh_knowledge`
- 流程树：`get_flow_records / get_flow_descriptions / refresh_flow_records`
- 生命周期：`preload / on_update / start_auto_update / start_ws_update / stop`

`KnowledgeRetriever`（详见 `knowledge_retriever.py`）：

- 关键词检索（TF-IDF + 中文 unigram/bigram/trigram + 余弦相似度）
- 可选语义检索（`use_semantic=True`，需 `sentence-transformers`）
- 接口：`preload / retrieve / retrieve_as_text / get_available_keys`

---

## 12. 环境变量

| 变量                           | 默认值                                       | 说明                                                                                                   |
| ------------------------------ | -------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `PROMPT_DB_PATH`             | `<backend>/prompt_manager.db`              | SQLite 路径，主服务 / Agent 共享同一文件                                                               |
| `PROMPT_MANAGER_URL`         | `http://localhost:8900`                    | Agent 拉取 LLM 配置的主服务地址                                                                        |
| `FLOW_DATA_DIR`              | `<backend>/flow_data`                      | 上传文件落盘目录                                                                                       |
| `DIALOGUE_FLOW_PARSER_SKILL` | `~/.codebuddy/skills/dialogue-flow-parser` | OCR skill 根路径；缺失时自动降级到内置 rapidocr                                                        |
| `LLM_API_LOG_DIR`            | `<flow_data>/api_logs`                     | LLM API 调用日志目录                                                                                   |
| `OCR_PYTHON`                 | 无                                           | 显式指定跑 OCR 的 Python 解释器（需装`rapidocr_onnxruntime`），例如 `/data/miniconda3/bin/python3` |

---

## 13. 前端界面速览

主前端（Vue 3 CDN 单文件）在 `frontend/index.html`：

- 登录页 → 主管理台。头部：科室 / 平台 / 场景 / 关键词筛选，右上角齿轮进入 LLM 配置。
- 模式切换：**提示词管理 / 知识库管理 / 流程树管理 / 账户管理**，按登录账号权限动态显示。
- 提示词管理：三维过滤、版本历史、回滚、启停、变量映射编辑器。
- 知识库管理：11 类知识标签 + `bot_id` 双维度筛选，内容为 JSON 数组内联编辑。
- 流程树管理：上传解析、缩略图预览、自然语言描述编辑、重新解析、跨库搜索。
- 账户管理：admin 专用；按提示词/知识库/流程树三大模块颗粒到查看/编辑/删除。

Agent 前端 `agent_frontend/`：

- 左侧「流程树图库」：按科室 / 平台 / 关键词跨库搜索，缩略图卡片。
- 右侧：
  - 「流程树解析」：图片预览、描述编辑、重新解析、删除。
  - 「LLM 对话」：直连 `/api/chat` 与激活模型对话。

---

## 14. 常见问题

**Q: 解析流程图报「未提供图片」？**

- 说明当前 LLM 不支持视觉输入。系统会自动降级到 OCR + 文本 chat，只要环境装了 `rapidocr_onnxruntime` 就能拿到自然语言描述。
- 想要更精细的图形理解，请激活视觉模型（`qwen-vl-max` / `gpt-4o` 等）。

**Q: 服务启动了但页面卡在「正在检查登录状态」？**

- 通常是浏览器缓存旧前端。清一次浏览器缓存或强刷。
- 主前端如需完全脱离外网 CDN，可将 Vue 运行时放到 `frontend/vendor/vue.global.prod.js` 并把 `<script src>` 改为本地路径。

**Q: SDK 拿到的提示词版本没更新？**

- 确认 `client.start_auto_update(interval=30)` 或 `start_ws_update()` 已启动。
- 或调用 `client.preload()` 主动强制同步。

**Q: 想加自定义占位符？**

- 提示词内容里写 `{我的占位符}`，然后在管理页「变量映射」里把变量名映射到 `{我的占位符}`；调用 `resolve` 时通过 `extra_variables` 传值即可。

**Q: 部署环境 Python 里没装 rapidocr，OCR 兜底也失败？**

- 方案 1：在 Agent 服务运行环境执行 `pip install rapidocr_onnxruntime`。
- 方案 2：设置环境变量 `OCR_PYTHON=/data/miniconda3/bin/python3` 指定已装 rapidocr 的解释器，`flow_parser_service` 会通过子进程调用。

---

## 15. 技术栈

- **后端**：FastAPI + SQLite（WAL 模式）+ Pydantic v2
- **前端**：Vue 3（CDN，零构建）
- **流程树解析**：`dialogue-flow-parser` skill（OCR）+ `rapidocr_onnxruntime` 兜底 + OpenAI 兼容多模态 LLM
- **PDF 转图**：PyMuPDF（`fitz`）优先，`pdf2image` 兜底，200 DPI
- **客户端 SDK**：仅依赖 Python 标准库，可选 `websocket-client`（热更新）/ `sentence-transformers`（语义检索）
- **认证**：token 存 `auth_sessions` 表，30 天过期；请求头 `Authorization: Bearer <token>` 或 cookie `pm_token` 均可
