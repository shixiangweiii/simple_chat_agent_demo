# 09 — 与本项目 SSE 事件契约映射

## 9.1 本项目 SSE 事件契约回顾

本项目定义了一套自定义 SSE 事件协议，用于 Agent 后端与前端之间的通信：

| 事件 | payload | 用途 |
|---|---|---|
| `status` | `{phase, round}` | 轮次边界 |
| `thinking` | `{text}` | 增量推理 token |
| `chunk` | `{text}` | 增量回答 token |
| `search_status` | `{phase}` | 内置 web_search 生命周期 |
| `tool_call` | `{name, args}` | 工具调用开始 |
| `tool_result` | `{name, result}` | 工具返回结果 |
| `await_user` | `{tool_call_id, name, args, kind}` | HITL 中断 |
| `ui_hint` | `{mode, reason}` | 上下文感知 UI 模式推荐 |
| `done` | `{}` | 流结束 |
| `error` | `{message}` | 错误 |
| `component_loading` | `{component_type, tool_call_id}` | 工具组件加载中 |
| `render_component` | `{component_type, tool_call_id, props}` | 工具结果卡片渲染 |
| `component_error` | `{component_type, tool_call_id, error_message}` | 工具组件错误 |
| `ui_surface_create` | `{surface_id}` | 声明式 Surface 创建 |
| `ui_surface_update` | `{surface_id, components}` | 更新 Surface 组件树 |
| `ui_data_update` | `{surface_id, path, value}` | 更新 Surface 数据 |
| `activity_snapshot` | `{plan_id, title, steps, editable, status}` | 计划快照 |
| `activity_delta` | `{plan_id, patch}` | 计划状态增量 |
| `confidence_signal` | `{score, level, reason, draft, draft_id}` | 置信度信号 |
| `steer_applied` | `{message}` | Agent Steering 消息 |
| `agent_state_snapshot` | 全局 Agent 状态 | Agent 状态快照 |
| `agent_state_delta` | RFC 6902 patch | Agent 状态增量 |

## 9.2 映射到 AG-UI 事件

| 本项目事件 | AG-UI 等效 | 说明 |
|---|---|---|
| `status {phase:"thinking"}` | `StepStarted {stepName:"thinking"}` | 近似；AG-UI 步骤粒度更大 |
| `thinking {text}` | `ReasoningMessageContent {delta}` | 近似；AG-UI 有完整开始-内容-结束三阶段 |
| `chunk {text}` | `TextMessageContent {delta}` | 近似；AG-UI 有 Start/Content/End 三阶段 |
| `done {}` | `RunFinished {}` | 近似；AG-UI 区分 outcome（成功/中断） |
| `error {message}` | `RunError {message, code?}` | 近似 |
| `tool_call {name, args}` | `ToolCallStart + ToolCallArgs` | AG-UI 拆分为两步 |
| `tool_result {name, result}` | `ToolCallResult {content}` | AG-UI 单独事件 |
| `await_user` | AG-UI 中断机制 | 概念不同；AG-UI 有结构化中断类型 |
| `search_status {phase}` | 无直接等效 | AG-UI 无 web_search 生命周期事件 |
| `component_loading` | 无直接等效 | AG-UI 无组件加载状态事件 |
| `render_component` | 无直接等效 | AG-UI 无组件渲染事件 |
| `component_error` | 无直接等效 | AG-UI 无组件错误事件 |
| `agent_state_snapshot` | `StateSnapshot {snapshot}` | 近似 |
| `agent_state_delta` | `StateDelta {delta}` | 近似；都用 RFC 6902 |
| `activity_snapshot` | `ActivitySnapshot {content}` | 近似 |
| `activity_delta` | `ActivityDelta {patch}` | 近似；都用 RFC 6902 |
| `confidence_signal` | `Custom {name, payload}` | 无标准事件；需用 Custom |
| `steer_applied` | `Custom {name, payload}` | 无标准事件；需用 Custom |
| `ui_hint` | `Custom {name, payload}` | 无标准事件；需用 Custom |

### 本项目缺少的 AG-UI 概念

| AG-UI 概念 | 说明 | 本项目状态 |
|---|---|---|
| `RunStarted` | 流开始信号 | 无；隐含在第一个事件中 |
| `TextMessageStart/End` | 文本消息边界 | 无；用 `status` 近似 |
| `ToolCallStart/Args/End` | 工具调用三阶段 | 无；用 `tool_call` 一步完成 |
| `MessagesSnapshot` | 完整对话历史 | 无；用 `GET /api/history` |
| `ReasoningEncryptedValue` | 加密推理 | 无 |

## 9.3 映射到 A2UI 消息

| 本项目事件 | A2UI 等效 | 说明 |
|---|---|---|
| `ui_surface_create {surface_id}` | `createSurface {surfaceId, catalogId}` | 同构；A2UI 多 `catalogId` |
| `ui_surface_update {surface_id, components}` | `updateComponents {surfaceId, components}` | 同构；A2UI 组件属性更丰富（数据绑定） |
| `ui_data_update {surface_id, path, value}` | `updateDataModel {surfaceId, updates}` | 同构；A2UI 支持批量更新 |
| `component_loading` | 无 | A2UI 无组件加载状态 |
| `render_component` | 无 | A2UI 通过 updateComponents 渲染 |
| `component_error` | 无 | A2UI 无组件错误事件 |

### 本项目有但 A2UI 不涉及的概念

| 本项目概念 | A2UI 状态 | 原因 |
|---|---|---|
| 聊天流 (thinking/chunk) | A2UI 不涉及 | A2UI 只管 UI 渲染，不管文本流 |
| 工具调用 (tool_call/tool_result) | A2UI 不涉及 | A2UI 只管 UI 渲染，不管工具调用 |
| HITL (await_user) | A2UI 通过组件交互 | A2UI 用 Button.action + 客户端事件 |
| 状态同步 (agent_state_*) | A2UI 不涉及 | A2UI 只管 Surface 内状态 |
| 计划 (activity_*) | A2UI 不涉及 | A2UI 只管 UI 渲染 |
| 置信度 (confidence_signal) | A2UI 不涉及 | A2UI 只管 UI 渲染 |

## 9.4 本项目声明式 UI 与 A2UI 的结构对比

本项目 Phase 3 的 `render_ui` 工具实现了轻量声明式 UI，与 A2UI 高度同构：

### 组件结构

**本项目** (`DeclarativeRenderer`)：
```json
{
  "components": [
    { "id": "root", "type": "column", "children": ["title", "btn"] },
    { "id": "title", "type": "text", "content": "欢迎使用" },
    { "id": "btn", "type": "button", "label": "开始", "action": { "event_name": "start" } }
  ]
}
```

**A2UI**：
```json
{
  "components": [
    { "id": "root", "component": "Column", "children": ["title", "btn"] },
    { "id": "title", "component": "Text", "text": { "literalString": "欢迎使用" } },
    { "id": "btn", "component": "Button", "child": "btn-text", "action": { "name": "start" } }
  ]
}
```

**差异**：
- 字段命名：`type` vs `component`，`content` vs `text.literalString`
- 子元素：`children: string[]` 一致
- 数据绑定：本项目无 JSON Pointer 绑定；A2UI 有三种绑定模式
- Action：`event_name` vs `name`，结构同构

### 数据更新

**本项目**：
```json
{ "surface_id": "main", "path": "/user/name", "value": "Alice" }
// 或根替换
{ "surface_id": "main", "path": "/", "value": { "user": { "name": "Alice" } } }
```

**A2UI**：
```json
{ "surfaceId": "main", "updates": [
  { "path": "/user/name", "value": "Alice" }
]}
```

**差异**：
- 本项目单条更新；A2UI 支持批量更新
- 本项目支持根替换 (`path: "/"`)；A2UI 也支持
- 本项目无数据绑定（UI 不自动响应 DataModel 变化）；A2UI 组件通过 `path` 绑定自动重渲染

## 9.5 迁移分析：如果引入 A2UI/AG-UI

### 渐进式迁移路径

```
Phase 0 (当前): 自定义 SSE 事件契约
    ↓
Phase 1: SSE 事件对齐 AG-UI 事件类型
    - thinking → ReasoningMessageContent (加 Start/End)
    - chunk → TextMessageContent (加 Start/End)
    - tool_call → ToolCallStart + ToolCallArgs
    - tool_result → ToolCallResult
    - agent_state_* → StateSnapshot + StateDelta
    - activity_* → ActivitySnapshot + ActivityDelta
    ↓
Phase 2: 声明式 UI 对齐 A2UI 消息格式
    - ui_surface_create → createSurface (+ catalogId)
    - ui_surface_update → updateComponents (邻接表格式)
    - ui_data_update → updateDataModel (批量更新)
    - 引入 JSON Pointer 数据绑定
    ↓
Phase 3: 引入 Catalog 安全模型
    - 组件白名单
    - 属性 schema 验证
    - Surface 状态机
    ↓
Phase 4: 传输层替换
    - SSE → AG-UI 标准传输
    - 或 AG-UI + A2UI 组合
```

### 迁移成本评估

| 维度 | 成本 | 原因 |
|---|---|---|
| SSE 事件格式 | 中 | 需要给现有事件加 Start/End 边界 |
| 声明式 UI 格式 | 低 | 已高度同构，主要是字段重命名 + 数据绑定 |
| Catalog 安全模型 | 高 | 本项目目前无组件白名单，需重构渲染管线 |
| 传输层 | 高 | 从 SSE 切换到 AG-UI 传输 |
| 前端渲染器 | 中 | DeclarativeRenderer 需要适配 A2UI 组件格式 |
| Agent 工具定义 | 低 | render_ui 工具参数可渐进对齐 A2UI schema |

### 保持自定义协议的合理性

**本项目是教学演示**，自定义 SSE 事件契约的优势：
1. **零依赖**：不引入 AG-UI/A2UI SDK
2. **直观可读**：每条事件语义明确，便于教学理解
3. **全栈可控**：前后端在同一代码库，协议可自由演进
4. **教学价值**：从零构建事件契约，比使用标准协议更能理解原理

**引入标准协议的场景**：
1. 需要连接多种 Agent 框架
2. 需要跨信任边界（多组织 Agent）
3. 需要跨平台渲染（Web + 移动 + 桌面）
4. 需要生态兼容（CopilotKit / LangGraph 等）

## 9.6 结论

本项目的自定义 SSE 事件契约在概念上与 AG-UI 和 A2UI 高度同构：

- **聊天流 + 工具调用 + 状态同步** → AG-UI 覆盖
- **声明式 UI (Surface + Component + DataModel)** → A2UI 覆盖
- **HITL + 置信度 + Steering** → 本项目特有，可用 AG-UI Custom 事件承载

教学演示的定位使得自定义协议更合适，但理解 A2UI/AG-UI 的设计有助于：
1. 评估协议设计的合理性（本项目事件类型覆盖了 AG-UI 大部分概念）
2. 未来需要时的迁移方向（渐进式对齐 AG-UI 事件类型 + A2UI 组件格式）
3. 理解行业标准的设计决策（为什么 AG-UI 用三阶段流、A2UI 用邻接表）
