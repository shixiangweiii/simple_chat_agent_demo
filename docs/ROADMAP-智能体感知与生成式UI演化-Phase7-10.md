# ROADMAP — 智能体感知与生成式 UI 演化（Phase 7–10）

> 基于源码现状 + 2025–2026 年最新论文/协议/产业实践，规划的后续 4 个阶段。
> 前置条件：Phase 1–6 已全部完成。
> 修订日期：2026-06-04 | 修订原因：对标 A2UI v0.9 + AG-UI 协议 + 行业最新实践，规划 Phase 7–10

---

## Context：为什么需要新的阶段

### 已完成

Phase 1–6 实现了从"纯文本 ReAct 聊天"到"生成式 UI Agent"的完整闭环：
- Static GenUI → Declarative GenUI → Action + Data Update → Plan-and-Execute → Checkpoint/Resume → Confidence Signal

### 当前差距（对标行业 2025–2026）

| 维度 | 本项目现状 | 行业最新实践 | 差距 |
|---|---|---|---|
| **协议标准** | 自定义 SSE 事件（`ui_surface_create` 等） | A2UI v0.9（Google, 2026-04）+ AG-UI（CopilotKit, 2025-05） | 事件命名/结构/流控与标准不互通 |
| **流式 UI** | `surfaceUpdate` 全量替换组件树 | A2UI `surfaceUpdate` 增量追加 + `beginRendering` 流控信号 | 无法逐组件流式渲染，用户需等完整 JSON |
| **交互组件** | 6 种（text/card/row/column/table/button） | A2UI v0.9 20+ 种含 TextField/ChoicePicker/DateTimeInput/Slider/Image/Tabs/Modal | 无法生成表单/输入控件，"聊天确认"仍是唯一路径 |
| **状态同步** | JSON Pointer 单点数据写入 | AG-UI STATE_SNAPSHOT/STATE_DELTA (JSON Patch RFC 6902) | 无快照/增量协议，前端无法高效同步 Agent 全局状态 |
| **Agent 控制** | 无中途干预能力 | AG-UI interrupts + a16z "输入框消失" 预测 | 长任务运行时用户无法纠偏 |
| **多模态** | 纯文本输入 | Qwen3-VL 图像 + Web Speech API 语音 | 无法处理图片/文件/语音 |
| **质量缺陷** | 8 项评审发现（1 High + 4 Medium） | — | 置信度缓冲杀死短回答流式、计划更新重置已完成步骤等 |

### 行业关键洞察

1. **声明式 GenUI 是生产级甜点** — Static（数据填充）→ Declarative（组件组装）→ Open-ended（代码生成），行业共识是 Declarative 平衡了灵活性和安全性。本项目已在此轨道上。
2. **"组件越少，LLM 准确率越高"** — Qwen App/易文助手实践表明，从 15 种组件降到 6 种，生成错误率从 ~20% 降到近 0%。扩充组件需谨慎，每次 +2–3 种并验证。
3. **Agent 协议栈四层分离已成定局** — MCP（工具层）+ A2A（Agent 间通信）+ AG-UI（Agent↔前端交互层）+ A2UI（UI 描述层）。本项目应主动对齐，使学生理解行业标准。
4. **`beginRendering` 信号是流式 UI 的关键** — A2UI 的设计让 LLM 先发占位组件，再逐步补充，前端等到 `beginRendering` 才首次渲染，之后增量更新无需再次等待。这解决了"长时间白屏等待完整 JSON"的问题。
5. **表单组件是"聊天→交互"范式跃迁的门槛** — a16z 2026 预测"输入框消失"，核心就是 Agent 不再需要用户在聊天框输入，而是直接生成表单/按钮/选择器。目前项目缺这层能力。

---

## Phase 7：协议对齐 & 质量修复

**核心教学点**：Agent 协议栈（MCP / A2A / AG-UI / A2UI）的分层设计与标准对齐

**目标**：
1. 修复 Phase 1–6 评审发现的质量缺陷
2. 将 SSE 事件契约对齐 AG-UI / A2UI 标准，保持向后兼容

### 7a：质量修复（优先）

| # | 严重度 | 问题 | 修复方案 | 影响文件 |
|---|---|---|---|---|
| H1 | High | 置信度尾部缓冲（240 字符）杀死短回答流式 | 改为"检测到 `[confidence:` 前缀时才缓冲"，否则正常流式 | `chat_core.py` `_buffer_confidence_delta` / `_extract_confidence_signal` |
| M1 | Medium | `plan_decision="update"` 重置已完成步骤为 `pending` | `_normalize_plan_steps` 保留已完成步骤的 status 字段 | `chat_core.py` `_normalize_plan_steps` |
| M2 | Medium | 长 confidence reason 导致 marker 泄漏 | 尾部缓冲仅截取 marker 行，reason 超长时用正则回溯兜底 | `chat_core.py` `_extract_confidence_signal` |
| M3 | Medium | "修改并继续"按钮是空交互 | 前端 `plan_decision` 按钮仅当 `editable=true` 时渲染，或改为"重试"语义 | `index.html` `addPlanDecisionBubble` |
| M4 | Medium | 运行中计划中断后无法恢复 | `_restore_runtime_state` 将 `status=running` 视为可恢复，前端提供"继续执行"入口 | `chat_core.py` `_restore_runtime_state` + `index.html` |
| M5 | Low | 完成的计划永不清理 | 归档时清理 `_PLANS[session_id]` + 删除 sidecar 中的 plan 数据 | `chat_core.py` `archive_session` |
| M6 | Low | 无并发流互斥 | `sessions` 每个会话加 `asyncio.Lock`，`/api/chat|resume|ui_action|plan_*` 入口获取锁 | `chat_core.py` + `web_chat_agent.py` |
| M7 | Low | 损坏 sidecar 恢复崩溃 | `_extract_ui_actions` / `_restore_runtime_state` 全路径 `try/except`，记录日志跳过 | `chat_core.py` |

### 7b：SSE 事件对齐 AG-UI / A2UI

**策略**：渐进式对齐，现有事件名保持不变（前端不中断），在 payload 中嵌套标准字段。

| 现有事件 | AG-UI / A2UI 对标 | payload 扩展 |
|---|---|---|
| `status` | `RUN_STARTED` / `STEP_STARTED` / `STEP_FINISHED` | `+ag_ui_type: "step_started"` |
| `thinking` | — (AG-UI 无对应，A2UI 无) | 不变 |
| `chunk` | `TEXT_MESSAGE_CONTENT` | `+ag_ui_type: "text_message_content"` |
| `search_status` | — (responses 模式独有) | 不变 |
| `tool_call` | `TOOL_CALL_START` + `TOOL_CALL_ARGS` | `+ag_ui_type: "tool_call_start"` |
| `tool_result` | `TOOL_CALL_END` | `+ag_ui_type: "tool_call_end"` |
| `await_user` | AG-UI interrupt (HITL) | `+ag_ui_type: "interrupt"` |
| `ui_surface_create` | A2UI `surfaceUpdate`（首条） | `+a2ui_type: "surfaceUpdate"` |
| `ui_surface_update` | A2UI `surfaceUpdate`（增量） | `+a2ui_type: "surfaceUpdate"` |
| `ui_data_update` | A2UI `dataModelUpdate` | `+a2ui_type: "dataModelUpdate"` |
| `activity_snapshot` | — | `+ag_ui_type: "state_snapshot"` |
| `activity_delta` | AG-UI `STATE_DELTA` (RFC 6902) | `+ag_ui_type: "state_delta"` |
| `confidence_signal` | — (本项目独有) | 不变 |
| `done` | `RUN_FINISHED` | `+ag_ui_type: "run_finished"` |
| `error` | `RUN_ERROR` | `+ag_ui_type: "run_error"` |

**新增 SSE 事件**：
- `begin_rendering` — `{surface_id, root, catalog_id}` 对齐 A2UI `beginRendering` 信号。`render_ui` 执行后不再立即发 `ui_surface_update`，而是先发 `ui_surface_create`（占位），LLM 后续补充组件时发 `ui_surface_update`，最后发 `begin_rendering` 触发前端首次渲染。

**新增文件**：
- `docs/SSE事件契约.md` — 独立的 SSE 事件契约文档，含版本/Phase 归属和 AG-UI/A2UI 对标表

**关键约束**：
- 前端**不**依赖 `ag_ui_type` / `a2ui_type` 字段（向后兼容），仅作为标准对齐的文档标记
- `begin_rendering` 是**新行为**，需要前后端同步改动

---

## Phase 8：流式 GenUI & 交互表单

**核心教学点**：LLM 生成交互表单 — 从"聊天确认"到"结构化输入"的范式跃迁

**目标**：
1. 支持流式 UI 渲染（逐组件生成，`beginRendering` 流控）
2. 新增 3 类表单组件（TextField / Select / Toggle），使 Agent 能生成可交互表单
3. 表单提交闭环：按钮点击 → `userAction` → Agent 读取表单值 → 处理 → 更新 UI

### 8a：流式 UI 渲染

**当前**：`_execute_render_ui` 验证全部组件后一次性发 `ui_surface_create` + `ui_surface_update` + `ui_data_update`。

**改为**（对齐 A2UI 流式模型）：

1. `render_ui` 工具参数不变（`surface_id`, `components`, `data`），但 prompt 引导模型分多次调用：
   - 第 1 次：`render_ui(surface_id="main", components=[{id:"root", type:"column", children:["header","form","submit"]}])` → 发 `ui_surface_create` + `ui_surface_update`（仅 root 骨架），前端显示骨架占位
   - 第 2 次：`render_ui(surface_id="main", components=[{id:"header", ...}, {id:"form", ...}])` → 发 `ui_surface_update`（追加/替换组件），前端增量渲染
   - 第 3 次：`update_ui_data(...)` 填数据
   - 完成：`begin_rendering` 信号（可选，模型也可以一次性给全）

2. **`_execute_render_ui_for_session` 改动**：
   - 区分"首次创建"和"增量更新"：如果 `_UI_SURFACES[session_id][surface_id]` 已存在，`components` 合并（按 id 覆盖），不重建
   - 新增 `begin_rendering` 参数（bool，默认 false）。当 `true` 时，额外 yield `("begin_rendering", {"surface_id", "root", "catalog_id"})`

3. **前端 `DeclarativeRenderer` 改动**：
   - `_createSurface`：显示骨架占位
   - `_updateComponents`：**合并**而非替换组件 map，然后增量 re-render（只更新变化组件的 DOM，而非全量 innerHTML）
   - 新增 `handleEvent("begin_rendering", ...)`：移除骨架占位，标记 surface 为"已确认渲染"

4. **`RENDER_UI_TOOL` 参数扩展**：新增可选 `complete: bool`（默认 true）。`true` = 一次性完整渲染（兼容现有行为），`false` = 流式渲染（发 `begin_rendering` 信号）。

### 8b：新增表单组件

**新增 3 种组件类型**（遵循"每次 +2–3 种并验证"原则）：

| 组件 | A2UI 对标 | 参数 | 渲染 |
|---|---|---|---|
| `text_field` | `TextField` | `{id, label, placeholder?, value_path?, input_type?: "shortText"\|"longText"\|"number"}` | `<input>` / `<textarea>` + `<label>` |
| `select` | `ChoicePicker` | `{id, label, options: [{label, value}], value_path?, multiple?: bool}` | `<select>` / 多选 checkbox 组 |
| `toggle` | `CheckBox` | `{id, label, value_path?}` | `<input type="checkbox">` + `<label>` |

**数据绑定**：所有表单组件通过 `value_path`（JSON Pointer）绑定到 `surface.data`。前端实时同步用户输入到 `surface.data`。

**表单提交流**：
1. 模型生成 `button` 组件，`action.event_name = "submit_form"`
2. 用户点击按钮 → `sendUiAction` → `POST /api/ui_action`
3. 后端 `ui_action_response` 从 `_UI_SURFACES` 读取当前 `surface.data`（前端通过 `userAction.context` 传递表单快照）
4. Agent 收到结构化表单数据，处理，返回结果

**`_SUPPORTED_UI_COMPONENT_TYPES` 扩展**：`{"text", "card", "row", "column", "table", "button", "text_field", "select", "toggle"}`

**`UI_NODE_RENDERERS` 扩展**（index.html）：3 个新渲染器，表单组件 change 事件实时写入 `surface.data`（通过 `jsonPointerSet`）。

**`userAction` payload 扩展**：前端在 button 点击时，自动附带当前 surface 的 `data` 快照到 `context.formData`，Agent 可直接读取。

**CLI 模式**：遇到表单组件返回 `"[text_field/select/toggle 组件仅 Web 模式可见，请直接以文本方式询问用户]"`，与现有 HITL CLI 短路一致。

### 验证场景

用户说"帮我订机票"，Agent 流式生成：
1. `render_ui(surface_id="flight", components=[{id:"root", type:"column", children:["title","from_to","date","passengers","submit"]}])` — 骨架
2. `render_ui(surface_id="flight", components=[{id:"title", ...}, {id:"from_to", ...}, ...])` — 填充组件
3. `update_ui_data(surface_id="flight", path="/", value={from:"北京", to:"上海", date:"2026-07-01", passengers:1})` — 初始数据
4. `begin_rendering` — 确认渲染
5. 用户修改表单 → 点击"搜索航班" → Agent 收到 `formData` → 调 MCP web_search → 渲染结果

---

## Phase 9：Agent Steering & 状态同步

**核心教学点**：长任务中的人机协作 — 实时纠偏与高效状态同步

**目标**：
1. Agent 执行长任务时，用户可中途注入纠偏指令
2. AG-UI 风格 STATE_SNAPSHOT / STATE_DELTA（JSON Patch RFC 6902）高效同步
3. 并发流保护

### 9a：Agent Steering（实时方向纠正）

**新增 API**：`POST /api/steer`

```python
class SteerRequest(BaseModel):
    session_id: str
    message: str        # 用户纠偏指令
```

**机制**：
- `_STEER_QUEUE: dict[str, asyncio.Queue]` — 每个 session 一个队列
- `_stream_react_rounds` 每轮 LLM 调用前 `await _STEER_QUEUE[session_id].get()` 检查是否有纠偏
- 有纠偏时：将纠偏指令作为 `role=user` 追加 messages，再调 LLM
- 前端：当 Agent 正在执行（流活跃）时，输入栏变为"发送纠偏指令"模式，POST 到 `/api/steer`
- SSE 新事件：`steer_applied` — `{message}` 通知前端纠偏已生效

**关键约束**：
- Steering 不消耗 ReAct 轮次，仅注入上下文
- 仅 `API_MODE=chat` 可用（`responses` 模式的 prompt 字符串难以中途注入）
- CLI 模式不适用

### 9b：STATE_SNAPSHOT / STATE_DELTA

**对齐 AG-UI 状态管理**：

| 现有 | 改进 |
|---|---|
| `ui_data_update`（JSON Pointer 单点写入） | 保留，兼容 |
| 无全局状态快照 | 新增 `state_snapshot` SSE 事件 |
| 无增量补丁 | 新增 `state_delta` SSE 事件（JSON Patch RFC 6902） |

**`state_snapshot`**：在流开头发送完整 Agent 状态（`{surfaces, plans, pending, round, ...}`），前端用于初始化。

**`state_delta`**：每次状态变更发送 JSON Patch，前端增量应用。与现有 `ui_data_update` / `activity_delta` 互补——前者是 UI 数据层，后者是 Agent 全局状态层。

**前端**：新增 `AgentStateStore`，接收 `state_snapshot`/`state_delta`，维护 Agent 全局状态的镜像。用于显示状态面板（当前轮次、工具调用历史、计划进度等）。

### 9c：并发流互斥

（已在 Phase 7a M6 中实现，此处验证和加固）

- 每个 session 的 `asyncio.Lock` 确保同时只有一个 SSE 流
- `/api/steer` 不需要获取锁（仅写入队列，由持有锁的流消费）
- 前端在流活跃期间禁用 `/api/chat` 按钮，改显示"发送纠偏"输入框

---

## Phase 10：多模态感知

**核心教学点**：Agent 的多模态输入 — 从纯文本到图像/文件/语音

**目标**：
1. 支持图片上传（Qwen3-VL 多模态模型）
2. 支持文件附件（文本/代码/CSV 等）
3. 可选：Web Speech API 语音输入（纯前端）

### 10a：图片上传

**API 扩展**：
- `POST /api/chat` body 新增 `images: list[str]`（base64 编码图片）
- `chat_core.stream_agent_response` / `_stream_chat_native` 将图片转为 OpenAI vision 格式 `{"type": "image_url", "image_url": {"url": "data:image/...;base64,..."}}`

**前端**：
- 输入栏新增 📎 附件按钮 → 文件选择 → 图片预览缩略图 → base64 编码随请求发送
- 图片大小限制：5MB / 张，最多 3 张

**模型要求**：`QWEN_MODEL` 需设为支持视觉的模型（`qwen-vl-max` 等），非视觉模型忽略 images 字段

**Memory 扩展**：`Memory.add(role, msg, images=None)` — 图片不进 markdown 归档，仅在 runtime_state 中保留引用

### 10b：文件附件

**API 扩展**：
- `POST /api/chat` body 新增 `attachments: list[dict]`（`{filename, content, mime_type}`）
- 后端将文本类文件内容拼入 prompt，非文本类文件返回"不支持的文件类型"

**前端**：
- 📎 按钮同时支持图片和非图片文件
- 文本文件（.txt/.md/.py/.js/.csv/.json 等）读取内容并作为附件发送
- 文件大小限制：20KB / 文件（软上限，超过的业务层截断并提醒），最多 5 个，总量 ≤ 100KB

> **实际实施备注（2026-06-09）**：
> - 单文件软截断上限 **20KB**(`MAX_ATTACHMENT_CHARS`,业务层截断)、单文件硬上限 **100KB**(`MAX_ATTACHMENT_HARD_CHARS`,HTTP 400)，总量硬上限 **100KB**(`MAX_ATTACHMENT_TOTAL_CHARS`)。原因：设计文档原值 5×100KB ≈ 50 万字符 ≈ 15–20 万 token，已超 qwen3-max 默认上下文窗口（32K 输入），下调后可控在 ~30k token 内。超长附件截断后 prompt 教模型主动告知用户哪些部分不可见。（注：单文件硬上限与总量上限同为 100KB，单文件硬限实际由总量限先兜住。）
> - 注入位置改为 user_input **之后**（非之前），避免长附件挤占短问题的注意力。
> - `dispatchSelectedFiles` 按 file.type 分流到 `attachImages` / `attachFiles` 两个独立数组，**不重命名** `attachImages` 减少改动半径。

### 10c：语音输入（可选，纯前端）

- 使用 Web Speech API `SpeechRecognition`
- 语音识别结果填入输入栏，用户确认后发送
- 无后端改动，纯前端增强
- 浏览器兼容性：Chrome/Edge 原生支持，Safari/Firefox 部分支持

---

## 跨 Phase 约束

| 约束 | 说明 |
|---|---|
| **三层架构不变** | Entry → chat_core → llm_client/mcp_web_search，不下跳导入 |
| **双模式共存** | `responses` + `chat` 不统一，Phase 7–10 新功能仅 `chat` 模式可用（与 Phase 3–6 一致） |
| **单文件前端** | `index.html` 保持一个文件，可加 TOC 注释块辅助导航 |
| **Memory 格式不变** | flat string `[{role, msg}]`，图片/附件不进 markdown |
| **向后兼容** | 新 SSE 事件 payload 嵌套标准字段，旧前端忽略新字段不会崩溃 |
| **组件数量克制** | 每次 +2–3 种，验证 LLM 生成准确率后再追加 |
| **CLI 兼容** | 新功能在 CLI 模式下短路为文本提示，不崩溃 |

---

## 实施优先级

```
Phase 7a（质量修复）  ← 最高优先，立即启动
Phase 7b（协议对齐）  ← 紧随 7a
Phase 8a（流式 UI）   ← 依赖 7b 的 begin_rendering
Phase 8b（交互表单）  ← 依赖 8a 的流式渲染
Phase 9（Steering）   ← 依赖 8 的表单提交闭环
Phase 10（多模态）    ← 独立，可与 Phase 8/9 并行
```

---

## Agent 协议栈全景图

```
┌─────────────────────────────────────────────────────────┐
│                      用户 / 前端                         │
└────────┬──────────────────┬──────────────────┬──────────┘
         │ AG-UI            │ A2UI             │
         │ (事件流通道)      │ (UI 描述规范)     │
┌────────▼──────────────────▼──────────────────▼──────────┐
│                    本项目 (Phase 7b 对齐)                  │
│  SSE 事件 → ag_ui_type / a2ui_type 字段                   │
│  begin_rendering 流控信号                                  │
│  userAction 事件回传                                       │
└────────┬──────────────────┬──────────────────┬──────────┘
         │ MCP              │ A2A              │
         │ (工具调用)        │ (Agent 间通信)    │
┌────────▼──────────────────▼──────────────────▼──────────┐
│                 外部工具 / 其他 Agent                       │
│  DashScope WebSearch MCP  │  (Phase 11+ 预留)             │
└─────────────────────────────────────────────────────────┘
```

---

## 关键参考

| 资源 | 说明 |
|---|---|
| A2UI v0.9 Spec | Google 2026-04 发布，声明式 GenUI 协议，flat component + ID 引用 + catalog + beginRendering |
| AG-UI Protocol | CopilotKit 2025-05 发布，Agent↔前端事件流协议，16+ 事件类型，STATE_SNAPSHOT/STATE_DELTA |
| Vercel AI SDK streamUI | React Server Components 驱动的 GenUI，tool→component 映射 |
| CopilotKit + A2UI | useAgent/useCoAgent hooks，A2UI Composer 可视化编辑器 |
| a2ui-vue (社区实现) | Vue 3 A2UI 渲染器，20+ 内置组件，TypeScript |
| 吴恩达 GenUI 课程 | deeplearning.ai 短课程，受控/声明式/开放式三种 GenUI 范式 |
| Qwen App GenUI 分析 | 最大规模 GenUI 部署（3h 100万+订单），SDUI + A2UI 双协议 |
| 易文助手 SDUI+A2UI | Android 端实践，SDUI（展示型）+ A2UI（交互型）双架构 |
| a16z 2026 预测 | "输入框消失"，Agent 主动观察+干预，人机协作最后一步是人类确认 |
