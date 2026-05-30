# 智能体感知层 & 生成式 UI 调研报告

> 调研时间：2026-05-26 | 调研目标：为 simple_chat_agent_demo 的演化方向提供技术选型依据

---

## 一、行业背景与趋势判断

2026 年 Agent 开发已从"能跑通 ReAct 循环"进入**"如何让用户高效感知和协同 Agent"**的阶段。核心转变：

| 维度 | 2024-2025 | 2026+ |
|------|-----------|-------|
| UI 形态 | 纯聊天框 + Markdown | 生成式 UI（运行时动态渲染组件） |
| 人机关系 | 用户指令 → Agent 执行 | 混合主动权（Mixed-Initiative） |
| 透明度 | 隐藏过程，只给结果 | 思考步骤可视化 + 置信度信号 |
| 协议标准 | 自定义 SSE/WebSocket | AG-UI / A2UI / MCP 三层协议栈 |
| 感知能力 | 纯文本上下文 | 多模态感知（屏幕/文件/语音） |

---

## 二、核心概念解析

### 2.1 生成式 UI（Generative UI）

**定义**：Agent 在运行时不仅生成文本，还能"生成界面"——根据用户意图和上下文动态选择/构建 UI 组件。

**三种模式（控制权光谱）**：

```
高控制 ←————————————————————→ 高自由
Static GenUI    Declarative GenUI    Open-ended GenUI
(AG-UI)         (A2UI/Open-JSON-UI)  (MCP Apps/iframe)
```

| 模式 | 原理 | 适用场景 |
|------|------|----------|
| **Static GenUI** | 前端预定义组件，Agent 只决定"何时展示 + 填什么数据" | 天气卡片、搜索结果列表、确认弹窗 |
| **Declarative GenUI** | Agent 返回结构化 JSON UI 描述，前端按约束渲染 | 动态表单、排序表格、多步向导 |
| **Open-ended GenUI** | Agent 返回完整 UI surface（HTML/iframe），前端仅托管 | 沙箱代码预览、第三方嵌入 |

### 2.2 AG-UI 协议（Agent–User Interaction Protocol）

由 CopilotKit 发起的开放标准，定位为 **Agent ↔ 用户前端** 的通用双向连接层。已获得 LangGraph、CrewAI、Google ADK、AWS Strands、Mastra、Pydantic AI、Microsoft Agent Framework 等主流框架的 1st-party 支持。

**协议架构**：基于 HTTP/WebSocket 的事件流，每条消息是一个带 `type` 字段的 JSON 对象。

**完整事件类型（16 种，分 6 类）**：

| 类别 | 事件名 | 用途 |
|------|--------|------|
| **Lifecycle** | `RUN_STARTED` | 开启一次 Agent 运行（携带 threadId、runId） |
| | `RUN_FINISHED` | 运行结束（支持 outcome: success / interrupt） |
| | `RUN_ERROR` | 运行出错（携带 message、code） |
| | `STEP_STARTED` / `STEP_FINISHED` | 步骤开始/结束（stepName 标识） |
| **Text Message** | `TEXT_MESSAGE_START` | 消息开始（messageId + role） |
| | `TEXT_MESSAGE_CONTENT` | 流式文本增量（delta 字段） |
| | `TEXT_MESSAGE_END` | 消息结束 |
| | `TEXT_MESSAGE_CHUNK` | 便捷事件，自动展开为 Start→Content→End |
| **Tool Call** | `TOOL_CALL_START` | 工具调用开始（toolCallId + toolCallName） |
| | `TOOL_CALL_ARGS` | 工具参数增量流（delta，通常是 JSON 片段） |
| | `TOOL_CALL_END` | 工具调用规范完成 |
| | `TOOL_CALL_RESULT` | 工具执行结果（content 字段） |
| **State** | `STATE_SNAPSHOT` | 完整状态快照（替换式同步） |
| | `STATE_DELTA` | 增量状态更新（RFC 6902 JSON Patch） |
| **Reasoning** | `REASONING_START` / `REASONING_END` | 推理过程的边界 |
| | `REASONING_MESSAGE_START/CONTENT/END` | 推理内容流式展示 |
| | `REASONING_ENCRYPTED_VALUE` | 加密 CoT（跨轮次保持推理状态） |
| **Activity** | `ACTIVITY_SNAPSHOT` | 活动完整快照（如 PLAN、SEARCH） |
| | `ACTIVITY_DELTA` | 活动增量更新（JSON Patch） |
| **Special** | `RAW` | 外部系统事件透传 |
| | `CUSTOM` | 应用自定义事件（name + value） |

**三种核心模式**：
- **Start-Content-End**：用于流式内容（文本/工具调用），保证有序拼装
- **Snapshot-Delta**：用于状态同步，快照全量 + 增量 Patch 高效更新
- **Lifecycle**：用于运行监控，Started/Finished/Error 界定边界

**中断/恢复机制（Interrupts）**：
- `RUN_FINISHED` 可携带 `outcome: { type: "interrupt", interrupts: [...] }`
- 客户端通过新启 run 并附带 `resume` 数组来恢复暂停的流程
- 完美支持 HITL 审批、用户输入等场景

**协议栈定位**：
```
┌─────────────────────────────────┐
│  Agent ↔ User:   AG-UI          │  ← 你的 demo 重点演化方向
├─────────────────────────────────┤
│  Agent ↔ Tools:  MCP            │  ← 已实现（mcp_web_search）
├─────────────────────────────────┤
│  Agent ↔ Agent:  A2A            │  ← 未来多 Agent 场景
└─────────────────────────────────┘
```

### 2.3 A2UI（Agent-to-User Interface）

Google 发起的声明式 UI 规范（v0.9 Draft / v0.8 Stable）。核心哲学：**UI 结构与应用数据严格分离，支持流式渐进渲染**。

**协议消息类型（v0.9，4 种）**：

| 消息 | 用途 | 关键字段 |
|------|------|----------|
| `createSurface` | 创建新 UI 表面 | surfaceId, catalogId, theme, sendDataModel |
| `updateComponents` | 添加/更新组件定义 | surfaceId, components（扁平列表 + ID 引用构建树） |
| `updateDataModel` | 更新数据模型 | surfaceId, path（JSON Pointer）, value |
| `deleteSurface` | 删除 UI 表面 | surfaceId |

**组件模型（邻接表）**：
- 组件以扁平列表发送，通过 `id` 引用构建父子关系
- 必须有一个 `id: "root"` 的组件作为树根
- 容器组件（Row/Column/Card）通过 `children` 数组引用子组件 ID
- 客户端维护 `Map<String, Component>` 并按需重建渲染树

**Basic Catalog 组件列表**：
- **布局**：`Row`、`Column`、`Card`、`Divider`、`List`
- **文本**：`Text`（支持 variant: h1/h2/body/caption）
- **输入**：`TextField`（shortText/longText）、`CheckBox`、`ChoicePicker`（单选/多选）
- **交互**：`Button`（primary/secondary/tertiary）、`Icon`
- **数据绑定**：通过 `DynamicString/Number/Boolean` 类型，支持 `path`（JSON Pointer）或 `literalString` 两种取值方式

**Action 系统**：
- **Server Action**：`action: { event: { name: "submit_form", context: {...} } }` → 发送事件到后端
- **Local Action**：`action: { functionCall: { call: "openUrl", args: {...} } }` → 客户端本地执行

**数据绑定示例**：
```jsonl
{"version":"v0.9","createSurface":{"surfaceId":"profile","catalogId":"https://a2ui.org/..."}}
{"version":"v0.9","updateComponents":{"surfaceId":"profile","components":[{"id":"root","component":"Column","children":["name","email"]},{"id":"name","component":"Text","text":{"path":"/user/name"},"variant":"h2"},{"id":"email","component":"TextField","label":"Email","value":{"path":"/user/email"},"variant":"shortText"}]}}
{"version":"v0.9","updateDataModel":{"surfaceId":"profile","path":"/user","value":{"name":"Alice","email":"alice@example.com"}}}
```

**传输层解耦**：A2UI 协议本身不绑定传输，可承载于 AG-UI、A2A、MCP、SSE+JSON-RPC、WebSocket、REST 等。与 AG-UI 结合时，A2UI 消息作为 AG-UI `CUSTOM` 事件的 payload 传输。

**与 Open-JSON-UI 的区别**：Open-JSON-UI 是 OpenAI 内部声明式 UI schema 的开放标准化版本，结构更扁平；A2UI 则更强调组件树的层次关系和数据绑定的严格性。两者都属于 Declarative GenUI 范畴。

### 2.4 Agent 感知层

Agent 的"感知"不再仅限于用户输入的文本，而是扩展到：

| 感知维度 | 说明 | 技术实现 |
|----------|------|----------|
| **用户意图** | 从对话上下文推断用户真实目的 | 意图分类 + 上下文窗口管理 |
| **环境状态** | 当前页面/文件/光标/选区 | 前端 → Agent 的上下文注入 |
| **交互行为** | 用户的操作模式、偏好 | 行为序列分析 + 自适应 |
| **任务进度** | 多步任务的当前阶段 | 结构化任务状态机 |
| **多模态输入** | 图片/文件/语音/屏幕截图 | VLM 视觉理解 + ASR |

---

## 三、Agent UX 设计模式（来自 1000+ 产品实践）

### 3.1 核心设计模式

| 模式 | 描述 | demo 适用性 |
|------|------|-------------|
| **Plan-and-Execute** | 先展示计划，用户确认后执行 | ⭐⭐⭐ 高 |
| **Confidence Signaling** | 置信度可视化，低置信度时请求确认 | ⭐⭐⭐ 高 |
| **Progressive Disclosure** | 逐步展示，先摘要后详情 | ⭐⭐⭐ 高 |
| **Mixed-Initiative** | 人机交替掌握主动权 | ⭐⭐ 中 |
| **Thinking Visualization** | 思考过程分区、折叠、实时流 | ⭐⭐⭐ 已有基础 |
| **Tool Result Cards** | 工具结果用卡片而非文本 | ⭐⭐⭐ 高 |
| **Streaming Components** | 组件随数据流逐步渲染 | ⭐⭐⭐ 高 |
| **Error Recovery UX** | 优雅降级 + 可操作的恢复建议 | ⭐⭐ 中 |

### 3.2 信任建设三要素

1. **透明度**：展示 Agent 在做什么（而非"请稍候..."）
2. **可控性**：随时可中断、修正、覆盖 Agent 行为
3. **可预测性**：UI 行为一致，不让用户感到"惊吓"

---

## 四、技术实现路径分析（适配本 demo 项目）

### 当前 demo 架构优势
- ✅ 已有 SSE 流式传输基础
- ✅ 已有 HITL（ask_user / execute_shell_command）
- ✅ 已有思考过程 `<details>` 面板
- ✅ 已有 tool_call / tool_result 事件区分
- ✅ Python FastAPI 后端 + 单文件前端，改造成本低

### 演化路径选择

**推荐方案：渐进式引入生成式 UI，从 Static GenUI → Declarative GenUI**

理由：
1. 你的后端是 Python/FastAPI，前端是原生 HTML/JS，无需引入 React 全家桶
2. Static GenUI 模式与现有 SSE 事件机制天然兼容——只需新增事件类型
3. 声明式 UI 可以用 JSON Schema 描述，前端按模板渲染，复杂度可控
4. 保持"教学 demo"定位——每一步都能清晰看到"多了什么"

---

## 五、参考资源

| 资源 | 链接 | 说明 |
|------|------|------|
| AG-UI 官方文档 | https://docs.ag-ui.com | 协议规范 + SDK |
| CopilotKit GenUI 指南 | copilotkit.ai/blog/the-developer-s-guide-to-generative-ui-in-2026 | 三种模式详解 |
| A2UI 规范（Google） | https://a2ui.org / github.com/google/A2UI | 声明式 UI JSON 协议 |
| Vercel AI SDK GenUI | ai-sdk.dev/docs/ai-sdk-ui/generative-user-interfaces | React 流式组件实现参考 |
| Agentic Design Patterns | agentic-design.ai/patterns/ui-ux-patterns | 14 种 Agent UX 模式详解 |
| Berkeley CLTC Agent UX | cltc.berkeley.edu/publication/ux-design-considerations-for-human-ai-agent-interaction | 学术研究 |
| arXiv: Human-AI Interaction Design Standards | arxiv.org/pdf/2503.16472 | 交互设计标准论文 |
| awesome-generative-ui | github.com/narrowin/awesome-generative-ui | 生成式 UI 资源汇总 |
| CopilotKit GenUI Playground | github.com/CopilotKit/generative-ui | 三种模式代码示例 |
