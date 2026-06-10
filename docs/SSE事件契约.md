# SSE 事件契约

> 本文档定义 `simple_chat_agent_demo` 的 SSE (Server-Sent Events) 事件契约。
> 版本: Phase 10a (2026-06-08)

## 传输格式

```
event: {event_name}\ndata: {json_payload}\n\n
```

所有 payload 为 JSON，UTF-8 编码，`ensure_ascii=False`。

## 协议标签（Phase 7b 新增）

每个 SSE 事件的 payload 中可能包含 `ag_ui_type` 或 `a2ui_type` 字段，用于对齐行业标准协议：

- **`ag_ui_type`** — 对标 [AG-UI (Agent-User Interaction Protocol)](https://docs.ag-ui.com/) 事件类型
- **`a2ui_type`** — 对标 [A2UI (Agent-to-User Interface)](https://github.com/nicobailon/a2ui) 消息类型

前端**不依赖**这些字段（向后兼容），它们仅作标准对齐的文档标记。标签由 `chat_core._with_protocol_tags()` 在 `_sse_stream` 中注入。

---

## 事件列表

### `"status"` — 轮次边界

| 字段 | 类型 | 说明 |
|---|---|---|
| `phase` | `"thinking" \| "answering"` | 当前阶段 |
| `round` | `int` | 当前 ReAct 轮次 |
| `ag_ui_type` | `"step_started"` | AG-UI 对标 |

引入: Phase 1

---

### `"thinking"` — 增量推理摘要

| 字段 | 类型 | 说明 |
|---|---|---|
| `text` | `str` | 增量推理摘要 token |

无标准协议对标。Responses API 的 reasoning 是摘要而非完整 CoT。

引入: Phase 1

---

### `"chunk"` — 增量回答

| 字段 | 类型 | 说明 |
|---|---|---|
| `text` | `str` | 增量回答 token |
| `ag_ui_type` | `"text_message_content"` | AG-UI 对标 |

引入: Phase 1

---

### `"search_status"` — 内置联网搜索生命周期

| 字段 | 类型 | 说明 |
|---|---|---|
| `phase` | `"in_progress" \| "searching" \| "completed"` | 搜索状态 |

**仅 `API_MODE=responses` 发送**。chat 模式使用 `tool_call` / `tool_result` 代替。

无标准协议对标。

引入: Phase 1

---

### `"tool_call"` — 即将调用工具

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | `str` | 工具名称 |
| `args` | `dict` | 工具参数 |
| `ag_ui_type` | `"tool_call_start"` | AG-UI 对标 |

引入: Phase 1

---

### `"tool_result"` — 工具返回结果

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | `str` | 工具名称 |
| `result` | `str` | 结果文本（截断至 `TOOL_RESULT_PREVIEW_CHARS=500`） |
| `ag_ui_type` | `"tool_call_end"` | AG-UI 对标 |

喂回模型的 `role=tool` 消息中 content 是**完整文本**，SSE 事件才截断。

引入: Phase 1

---

### `"await_user"` — HITL 中断

三种 payload 变体，由 `kind` 字段区分：

**Shape A — HITL 工具（ask_user / execute_shell_command）**

| 字段 | 类型 | 说明 |
|---|---|---|
| `tool_call_id` | `str` | 工具调用 ID |
| `name` | `str` | 工具名称 |
| `args` | `dict` | 工具参数 |
| `kind` | `"input" \| "approval"` | 交互类型 |
| `ag_ui_type` | `"interrupt"` | AG-UI 对标 |

**Shape B — 计划创建（create_plan）**

| 字段 | 类型 | 说明 |
|---|---|---|
| `tool_call_id` | `str` | 工具调用 ID |
| `name` | `"create_plan"` | 工具名称 |
| `args` | `dict` | 工具参数 |
| `kind` | `"plan"` | 计划类型 |
| `plan_id` | `str` | 计划 ID |
| `title` | `str` | 计划标题 |
| `steps` | `list[dict]` | 计划步骤 |
| `ag_ui_type` | `"interrupt"` | AG-UI 对标 |

**Shape C — 计划步骤失败（plan_decision）**

| 字段 | 类型 | 说明 |
|---|---|---|
| `kind` | `"plan_decision"` | 决策类型 |
| `plan_id` | `str` | 计划 ID |
| `step_id` | `str` | 失败步骤 ID |
| `title` | `str` | 计划标题 |
| `steps` | `list[dict]` | 计划步骤 |
| `error_message` | `str` | 错误信息 |
| `ag_ui_type` | `"interrupt"` | AG-UI 对标 |

引入: Phase 3b (Shape A), Phase 4 (Shape B, C)

---

### `"done"` — 正常流结束

| 字段 | 类型 | 说明 |
|---|---|---|
| `ag_ui_type` | `"run_finished"` | AG-UI 对标 |

HITL 中断也会发 `done`（关流让前端不再 read）。

引入: Phase 1

---

### `"error"` — 终端错误

| 字段 | 类型 | 说明 |
|---|---|---|
| `message` | `str` | 错误信息 |
| `ag_ui_type` | `"run_error"` | AG-UI 对标 |

流在此事件后结束。

引入: Phase 1

---

### `"ui_hint"` — 上下文感知 UI 模式推荐

| 字段 | 类型 | 说明 |
|---|---|---|
| `mode` | `"focus" \| "compact"` | 推荐的 UI 模式 |
| `reason` | `str` | 推荐原因 |

仅在 `mode != "chat"` 时发送。在流开头发（第一帧）。

引入: Phase 2

---

### `"ui_surface_create"` — 声明式 UI surface 创建

| 字段 | 类型 | 说明 |
|---|---|---|
| `surface_id` | `str` | UI surface 唯一 ID |
| `a2ui_type` | `"surfaceUpdate"` | A2UI 对标（首条创建消息） |

引入: Phase 3a

---

### `"ui_surface_update"` — 声明式 UI 组件更新

| 字段 | 类型 | 说明 |
|---|---|---|
| `surface_id` | `str` | UI surface ID |
| `components` | `list[dict]` | 组件列表 |
| `mode` | `"merge" \| "replace"` | **Phase 8a 新增**。`replace`(默认):前端清空 Map 后装入新组件;`merge`:按 id 合并(旧组件保留,同 id 覆盖)。不带此字段(旧客户端 / runtime_state 重放)按 `replace` 处理 |
| `a2ui_type` | `"surfaceUpdate"` | A2UI 对标（增量更新消息） |

引入: Phase 3a

**Phase 8a 双端一致语义**：模型 `render_ui(complete=true)` → 后端 `_register_ui_surface(merge=False)` 覆盖 + SSE `mode="replace"` → 前端 clear Map 再装入。模型 `render_ui(complete=false)` → 后端按 id 合并 + SSE `mode="merge"` → 前端按 id 合并。双端最终 components 集合恒等。

---

### `"ui_data_update"` — 声明式 UI 数据更新

| 字段 | 类型 | 说明 |
|---|---|---|
| `surface_id` | `str` | UI surface ID |
| `path` | `str` | JSON Pointer 路径（`/` = 替换整个 data） |
| `value` | `any` | 新值 |
| `a2ui_type` | `"dataModelUpdate"` | A2UI 对标 |

引入: Phase 3b

---

### `"begin_rendering"` — 流式渲染确认信号

| 字段 | 类型 | 说明 |
|---|---|---|
| `surface_id` | `str` | UI surface ID |
| `root` | `str` | 根组件 ID（通常为 `"root"`） |
| `catalog_id` | `str` | 组件目录标识 |

**Phase 7b 新增**。对齐 A2UI `beginRendering` 语义：模型调用 `render_ui(complete=false)` 时，先发 `ui_surface_create` + `ui_surface_update`（骨架），最后发 `begin_rendering` 通知前端可以开始渲染。前端收到后移除"正在渲染界面…"占位符。

`render_ui(complete=true)`（默认）不发此事件，行为与 Phase 3a 一致（一次性完整渲染）。

引入: Phase 7b

---

### `"component_loading"` — 静态 GenUI 加载占位

| 字段 | 类型 | 说明 |
|---|---|---|
| `component_type` | `str` | 组件类型（如 `"search_results"`） |
| `tool_call_id` | `str` | 工具调用 ID |
| `placeholder_text` | `str` | 占位文本 |

引入: Phase 1

---

### `"render_component"` — 静态 GenUI 渲染完成

| 字段 | 类型 | 说明 |
|---|---|---|
| `component_type` | `str` | 组件类型 |
| `tool_call_id` | `str` | 工具调用 ID |
| `props` | `dict` | 渲染属性 |

`search_results` 的 props: `{"query": str, "results": [{title, url, snippet}], "total_count": int}` 或 `{"query": str, "results": [], "raw_markdown": str}`。

引入: Phase 1

---

### `"component_error"` — 静态 GenUI 渲染失败

| 字段 | 类型 | 说明 |
|---|---|---|
| `component_type` | `str` | 组件类型 |
| `tool_call_id` | `str` | 工具调用 ID |
| `error_message` | `str` | 错误信息 |

引入: Phase 1

---

### `"activity_snapshot"` — 计划快照

| 字段 | 类型 | 说明 |
|---|---|---|
| `plan_id` | `str` | 计划 ID |
| `title` | `str` | 计划标题 |
| `steps` | `list[dict]` | 步骤列表 |
| `editable` | `bool` | 是否可编辑 |
| `status` | `str` | 计划状态 |
| `ag_ui_type` | `"state_snapshot"` | AG-UI 对标 |

每个 step: `{"id", "title", "status", "result_summary", "error_message"}`。

引入: Phase 4

---

### `"activity_delta"` — 计划状态增量

| 字段 | 类型 | 说明 |
|---|---|---|
| `plan_id` | `str` | 计划 ID |
| `patch` | `list[dict]` | JSON Patch 操作 |
| `ag_ui_type` | `"state_delta"` | AG-UI 对标 |

patch 路径: `/steps/{i}/status`, `/steps/{i}/result_summary`, `/steps/{i}/error_message`, `/status`。

引入: Phase 4

---

### `"confidence_signal"` — 置信度信号

| 字段 | 类型 | 说明 |
|---|---|---|
| `score` | `float` | 置信度分数 (0.0-1.0) |
| `level` | `"low" \| "medium" \| "high"` | 置信度等级 |
| `reason` | `str` | 置信度原因 |
| `draft` | `bool` | 是否为低置信度草稿 |
| `draft_id` | `str \| None` | 草稿 ID（draft=True 时有值） |

低置信度（<0.55）时 `draft=true`，回答不写入 Memory，等用户通过 `/api/confidence_decision` 采纳。

引入: Phase 6

---

### `"steer_applied"` — 用户纠偏指令已注入

| 字段 | 类型 | 说明 |
|---|---|---|
| `message` | `str` | 用户原始 steer 文本 |
| `round` | `int` | 注入到 messages 时的 ReAct 轮次 |

仅 `API_MODE=chat` 发。由 `_stream_react_rounds` / `_stream_plan_step_rounds` 在每轮 LLM 调用前 drain steer 队列时触发,每条 steer emit 一次。

引入: Phase 9a

---

### `"agent_state_snapshot"` — Agent 全局状态完整快照

| 字段 | 类型 | 说明 |
|---|---|---|
| `session_id` | `str` | 会话 ID |
| `round` | `int` | 当前 ReAct 轮次 |
| `tool_stats` | `dict` | 工具调用计数(按 key,如 `tool_calls`/`search_calls`/`tool_failures`/`errors`) |
| `surfaces` | `list[{surface_id, component_count}]` | 当前所有 UI surface 元信息(不含 components 详情) |
| `plans` | `list[{plan_id, title, status, step_count}]` | 当前所有 plan 元信息(不含 steps 详情) |
| `pending` | `dict \| null` | 当前 HITL awaiting(如有) |
| `steer_history` | `list[{ts, message}]` | 最近 10 条 steer 历史 |

仅 `API_MODE=chat` 发,**仅在 `/api/chat` 与 `/api/ui_action`** 流开头发一次完整快照(在 `ui_hint` 之后)—— 这两条都经 `stream_agent_response`（`ui_action_response` 复用它）。`/api/resume` 与各 `/api/plan_*` 流**不发**快照(它们不经 `stream_agent_response`），前端 reducer 在这些流上靠 `agent_state_delta` 增量维护。前端首次拿到快照用于初始化 agent 状态 reducer。

对标 AG-UI `STATE_SNAPSHOT`,作用域为 Agent 全局(不同于 `activity_snapshot` 的 plan 作用域)。不在 `_SSE_PROTOCOL_TAGS` 中加 `ag_ui_type` —— 与 `activity_snapshot` 共享同一 ag_ui_type 会引起前端 dispatcher 歧义。

引入: Phase 9b

---

### `"agent_state_delta"` — Agent 全局状态增量

| 字段 | 类型 | 说明 |
|---|---|---|
| `patch` | `list[{op, path, value}]` | RFC 6902 JSON Patch,仅含 `replace` / `add(/array/-)` 两种 op |

触发点(均在 `_stream_react_rounds` 内):
- 顶部 drain steer 后: `replace /steer_history` 整段
- 每轮 LLM 调用前 status emit 后: `replace /round`
- 每次 tool_call emit 后: `replace /tool_stats` 整段

`_stream_plan_step_rounds` 内仅 steer drain 时 emit `/steer_history` delta,不发 `/round`(避免与全局 round 概念混淆)。

对标 AG-UI `STATE_DELTA`(RFC 6902)。前端 reducer 仅需支持 `replace` 和 `add(/array/-)` 两种 op,其余 op 应 console.warn 后跳过。

引入: Phase 9b

---

## 声明式 UI 组件类型（Phase 8b 扩展）

`ui_surface_update.components[]` 每项是一个 `{id, type, ...}` 描述。type 必须在白名单内（后端 `_SUPPORTED_UI_COMPONENT_TYPES` 校验）：

| type | 用途 | 必填字段 | 可选字段 |
|---|---|---|---|
| `text` | 文本/标题/code | `id` | `text`、`path`（JSON Pointer 引用 data）、`variant: "h1"\|"h2"\|"body"\|"caption"\|"code"` |
| `card` | 标题卡片容器 | `id` | `title`、`children: [id...]` |
| `row` | 横向布局容器 | `id` | `children: [id...]` |
| `column` | 纵向布局容器 | `id` | `children: [id...]` |
| `table` | 表格 | `id`、`columns: [{key, label}]` | `rows_path`（JSON Pointer 指向 data 中的 rows 数组） |
| `button` | 按钮 | `id`、`label` | `variant: "primary"\|"secondary"`、`action: {event_name, context?}` |
| `text_field` | 文本输入框（Phase 8b） | `id`、`value_path` | `label`、`placeholder`、`input_type: "shortText"\|"longText"\|"number"` |
| `select` | 下拉选择（Phase 8b） | `id`、`value_path`、`options: [{label, value}]` | `label`、`multiple: bool` |
| `toggle` | 开关（Phase 8b） | `id`、`value_path` | `label` |

**`root` 节点必须存在**（其 type 通常是 `column` 或 `card`）。

**表单组件 `value_path`** 是 JSON Pointer（如 `/user/name`），绑定到 `surface.data`：
- 渲染时从 `surface.data` 读取当前值作为初始 / 显示值
- 用户编辑时，前端 `writeFormValue` 把新值写回 `surface.data`（不触发重渲染，保持焦点）
- 用户点击 button（带 `action.event_name`）时，前端把 `surface.data` 整体作为 `form_data` 字段 POST 到 `/api/ui_action`

---

## `/api/ui_action` 请求体（Phase 8b 扩展）

| 字段 | 类型 | 说明 |
|---|---|---|
| `session_id` | `str` | 会话 ID |
| `surface_id` | `str` | UI surface ID |
| `component_id` | `str` | 被点击的 button 组件 id |
| `event_name` | `str` | button.action.event_name，必须与后端 surface.actions 中的一致 |
| `form_data` | `dict \| null` | **Phase 8b 新增**。前端表单当前 `surface.data` 快照;不传或空 dict 时后端跳过注入。后端会把它作为 `form_data: {...}` 行追加到 `[UI Action]` 文本里供模型读取,并以**浅合并**方式写回 `_UI_SURFACES[..]["data"]`(保留模型私有字段如 token/internal state) |

旧客户端不传 `form_data` 时行为与 Phase 3a/3b 完全一致。

---

## `/api/steer` 请求体（Phase 9a 新增）

| 字段 | 类型 | 说明 |
|---|---|---|
| `session_id` | `str` | 会话 ID |
| `message` | `str` | 用户纠偏指令(非空字符串,strip 后必须 truthy) |

返回 `{"ok": true, "queued": true, "queue_size": N}`。**立即返回(非 SSE 流)**。

**设计约束**:
- 本路由**不获取** `_SESSION_LOCKS` —— 否则与正在持有锁的活跃 SSE 流死锁
- 仅做参数校验 + `put_nowait` 到 `_STEER_QUEUES[session_id]`,下一轮 ReAct LLM 调用前由 `_stream_react_rounds` / `_stream_plan_step_rounds` 顶部 `_drain_steers` 消费
- 允许无活跃流时入队(等待下次 `/api/chat` 消费),简化前端时序
- OpenAI messages 顺序约束: `assistant.tool_calls` 后必须紧跟 `role=tool`,此时 `_can_append_user_message` 返回 false,steer 留队列等下轮
- steer **不消耗 ReAct 轮次**(`MAX_ROUNDS=5` 仍是模型自主步数上限)

**错误响应**:
- HTTP 400 `invalid session_id` — session_id 不是 UUID 格式
- HTTP 400 `steer 仅在 API_MODE=chat 下可用` — responses 模式
- HTTP 400 `steer message 不能为空` — message 为 None / 空字符串 / 全空白

---

## `/api/chat` 请求体 — Phase 10a 多模态扩展

| 字段 | 类型 | 说明 |
|---|---|---|
| `session_id` | `str` | UUID 形式会话 ID |
| `message` | `str` | 用户文本 |
| `context` | `ChatContext \| null` | 可选环境元数据(viewport_width / selected_text / session_message_count) |
| `images` | `list[str] \| null` | **Phase 10a 新增**:可选图片附件,base64 data URL 数组 |

**images 字段约束**(后端 `_validate_images` + 前端 `attachImages` 双层防御):
- 总数 ≤ **3** 张
- 每条必须 `startswith("data:image/")`(白名单 image/*,允许 PNG/JPEG/GIF/WebP/SVG 等)
- 每条字符长度 ≤ ~7.5MB(对应原图 ≤ **5MB**)
- 字段缺省 / null / `[]` 均视为无图,走原文本路径(向后兼容)

**设计约束**:
- 仅 `API_MODE=chat` 支持图片;`responses` 模式收到 `images` 非空时显式 `("error", ...)` SSE 帧不静默丢
- 图片**不进** Memory、不进 markdown 归档、不进 runtime_state sidecar —— 仅当前 turn 的 LLM 调用中送入
- 仅 fresh `/api/chat` 携带;`/api/resume` `/api/ui_action` `/api/plan_*` `/api/steer` 不支持图片
- 后端不做模型能力嗅探;需要用户显式 `export QWEN_MODEL=qwen-vl-max` 等视觉模型
- Phase 10a **不新增 SSE 事件类型**,沿用 `chunk` / `done` / `error`

**错误响应**(HTTP 400 `ImagePayloadInvalid`):
- `最多附带 3 张图片(收到 N 张)` — 超数量
- `第 N 张图片格式非法,必须是 data:image/* 起始的 data URL` — 类型不对
- `第 N 张图片过大,请压缩到 5MB 以内` — 单张超限

**vision content 格式**(`_memory_to_messages` 注入):
```json
{
  "role": "user",
  "content": [
    {"type": "text", "text": "用户文本"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
  ]
}
```
`llm_client._llm_stream_chat_with_tools` 透传给 OpenAI SDK,SDK 对 str / list 两种 content 均原生支持,本项目 LLM 调用层零改动。

---

## AG-UI / A2UI 协议对标总表

| 本项目事件 | AG-UI 对标 | A2UI 对标 |
|---|---|---|
| `status` | `STEP_STARTED` / `STEP_FINISHED` | — |
| `thinking` | — | — |
| `chunk` | `TEXT_MESSAGE_CONTENT` | — |
| `search_status` | — | — |
| `tool_call` | `TOOL_CALL_START` + `TOOL_CALL_ARGS` | — |
| `tool_result` | `TOOL_CALL_END` | — |
| `await_user` | Interrupt | — |
| `ui_surface_create` | — | `surfaceUpdate`（创建） |
| `ui_surface_update` | — | `surfaceUpdate`（增量） |
| `ui_data_update` | — | `dataModelUpdate` |
| `begin_rendering` | — | `beginRendering` |
| `activity_snapshot` | `STATE_SNAPSHOT` (plan 作用域) | — |
| `activity_delta` | `STATE_DELTA` (RFC 6902, plan 作用域) | — |
| `agent_state_snapshot` | 对标 `STATE_SNAPSHOT` (agent 全局作用域) | — |
| `agent_state_delta` | 对标 `STATE_DELTA` (RFC 6902, agent 全局作用域) | — |
| `steer_applied` | — (Phase 9a 项目独有) | — |
| `confidence_signal` | — | — |
| `component_loading` | — | — |
| `render_component` | — | — |
| `component_error` | — | — |
| `ui_hint` | — | — |
| `done` | `RUN_FINISHED` | — |
| `error` | `RUN_ERROR` | — |

## Agent 协议栈四层分离

```
MCP  (工具层)    — Agent ↔ 工具/数据
A2A  (Agent 间)  — Agent ↔ Agent
AG-UI (交互层)  — Agent ↔ 前端应用
A2UI (UI 描述层) — Agent → UI 组件描述
```

本项目 SSE 事件在 AG-UI 和 A2UI 层面对齐，MCP 层面已有 `mcp_web_search.py`，A2A 层面尚未涉及（预留 Phase 11+）。
