# 08 — 与 AG-UI 对比

## 8.1 根本定位差异

| 维度 | A2UI | AG-UI |
|---|---|---|
| **全称** | Agent-to-UI | Agent-User Interaction Protocol |
| **发起者** | Google | CopilotKit |
| **核心问题** | Agent 如何**描述** UI 应该呈现什么？ | Agent 如何**连接**前端应用？ |
| **协议层** | UI 渲染层 | 传输/事件层 |
| **核心隐喻** | 蓝图 (Agent 画蓝图，客户端建房子) | 管道 (Agent 通过管道流式输出事件) |

一句话区分：**AG-UI 是管道，A2UI 是管道里传输的内容。**

## 8.2 架构对比

### A2UI 架构

```
Agent ──A2UI JSON──→ 传输层 (SSE/WS/A2A/AG-UI) ──→ 客户端原生渲染器
  │                                                       │
  │  只关心: 组件结构、数据绑定、安全约束                    │  只关心: 组件映射、数据绑定、事件回传
  │                                                       │
  │  不关心: 传输方式、聊天流、工具调用生命周期               │  不关心: 组件如何渲染、数据如何绑定
```

### AG-UI 架构

```
Agent ──事件流──→ AG-UI Client ──→ 前端应用
  │                                     │
  │  关心: 事件类型、生命周期、状态同步    │  关心: 事件消费、状态管理、UI 更新
  │                                     │
  │  不关心: UI 如何渲染、组件是什么      │  不关心: Agent 内部逻辑
```

## 8.3 消息/事件类型对比

### A2UI (4 种消息)

| 消息 | 用途 |
|---|---|
| `createSurface` | 创建 UI 画布 |
| `updateComponents` | 更新组件结构 |
| `updateDataModel` | 更新数据模型 |
| `deleteSurface` | 销毁画布 |

**特点**：消息类型少，每条消息是完整的结构化数据，面向 UI 渲染。

### AG-UI (~29 种事件)

| 类别 | 事件 | 数量 |
|---|---|---|
| 生命周期 | RunStarted, RunFinished, RunError, StepStarted, StepFinished | 5 |
| 文本消息 | TextMessageStart, TextMessageContent, TextMessageEnd, TextMessageChunk | 4 |
| 工具调用 | ToolCallStart, ToolCallArgs, ToolCallEnd, ToolCallResult, ToolCallChunk | 5 |
| 状态管理 | StateSnapshot, StateDelta, MessagesSnapshot | 3 |
| 活动 | ActivitySnapshot, ActivityDelta | 2 |
| 推理 | ReasoningStart/End, ReasoningMessage*, ReasoningEncryptedValue | 7 |
| 特殊 | Raw, Custom, MetaEvent (Draft) | 3 |

**特点**：事件类型丰富，每条事件是增量/生命周期信号，面向流式通信。

### 关键区别

| 维度 | A2UI | AG-UI |
|---|---|---|
| 消息粒度 | 粗粒度（一条消息描述完整组件树） | 细粒度（一个 token 一个事件） |
| 流模式 | JSONL 流式（逐条消息） | 开始-内容-结束 三阶段流 |
| 状态同步 | DataModel + JSON Pointer | StateSnapshot + StateDelta (RFC 6902) |
| 工具调用 | 无（不在协议范围内） | 完整生命周期（Start → Args → End → Result） |
| 推理过程 | 无（不在协议范围内） | 完整流式推理（支持加密值） |
| HITL | 通过组件交互（Button.action） | 通过中断机制（Interrupt） |

## 8.4 安全模型对比

| 维度 | A2UI | AG-UI |
|---|---|---|
| **信任模型** | 信任 Catalog，不信任 Agent 内容 | 信任 Agent 事件，前端自由解释 |
| **安全边界** | 组件白名单 + 属性 schema | Secure Proxy + 权限控制 |
| **代码执行** | 无（纯声明式数据） | 无（事件驱动，前端解释） |
| **注入防护** | Catalog 从源头杜绝 | 前端负责清洗和验证 |
| **跨信任边界** | 原生支持（A2A 场景） | 通过 Secure Proxy |

A2UI 的安全模型更严格：Agent 只能在白名单组件的属性范围内操作。AG-UI 的安全模型更灵活：Agent 可以发出任意事件，前端负责解释和防护。

## 8.5 适用场景对比

### A2UI 更适合

- **跨信任边界**：多 Agent 平台，Agent 来自不同组织，需要严格控制 UI 输出
- **跨平台渲染**：同一 UI 需要在 Web/移动/桌面原生渲染
- **业务组件库**：已有成熟组件库，需要 Agent 驱动组件展示
- **声明式 UI 优先**：表单、卡片、仪表板等结构化 UI

### AG-UI 更适合

- **实时交互**：聊天流、推理可视化、工具调用生命周期展示
- **React 生态**：已有 React 前端，需要 Agent 驱动状态更新
- **事件驱动 UI**：UI 更新由 Agent 事件流驱动，而非组件描述
- **多框架 Agent**：需要连接多种 Agent 框架（LangGraph, CrewAI, etc.）

### A2UI + AG-UI 组合

最常见的组合模式：

```
Agent ──AG-UI 事件流──→ 前端
  │                        │
  ├── TextMessage* (聊天流) │
  ├── ToolCall* (工具调用)  │
  ├── StateDelta (状态同步) │
  └── Custom(A2UI) ───────→ 前端 A2UI 渲染器
                             │
                             └── createSurface / updateComponents / updateDataModel
```

AG-UI 提供管道（事件流），A2UI 提供内容（组件描述）。

## 8.6 框架集成对比

### AG-UI 集成生态（更广）

| 框架 | 集成级别 |
|---|---|
| LangGraph | 一级支持 |
| CrewAI | 合作伙伴 |
| Microsoft Agent Framework | 一级支持 |
| Google ADK | 一级支持 |
| AWS Strands Agents | 一级支持 |
| Pydantic AI | 一级支持 |
| Mastra | 一级支持 |
| AG2 | 一级支持 |
| LlamaIndex | 一级支持 |
| Claude Agent SDK | 社区支持 |
| OpenAI Agent SDK | 进行中 |
| AWS Bedrock Agents | 进行中 |

SDK：TypeScript, Python, Kotlin, Go, Dart, Java, Rust, Ruby, C++

### A2UI 集成生态（较新）

| 框架 | 集成级别 |
|---|---|
| Google ADK | 一级支持 |
| LangChain | 一级支持 |
| Vercel AI SDK | 一级支持 |
| A2A SDK | 一级支持 |
| LangGraph | 计划中 |
| Genkit | 计划中 |

SDK：TypeScript, Python; 计划中: Go, Kotlin

## 8.7 版本与社区

| 维度 | A2UI | AG-UI |
|---|---|---|
| 首次发布 | 2025-12 | 2025-05 |
| 当前版本 | v0.8 (稳定) / v0.9 (草案) | 活跃开发 |
| GitHub Stars | ~15.2k | ~14.2k |
| 许可证 | Apache 2.0 | MIT |
| 主要采用者 | Google (Opal, Gemini Enterprise, Flutter GenUI) | CopilotKit, Amazon Bedrock, 多 Agent 框架 |
| 行业标准化 | Google 内部标准 | Linux 基金会 / 多公司支持 |

## 8.8 选择建议

| 场景 | 推荐 | 理由 |
|---|---|---|
| 需要 Agent 生成 UI 组件 | A2UI | 核心能力 |
| 需要 Agent 与前端实时通信 | AG-UI | 核心能力 |
| 需要两者 | AG-UI + A2UI | 互补组合 |
| 纯聊天 Agent | AG-UI | 文本流 + 工具调用 |
| 跨平台 Agent UI | A2UI | 声明式渲染跨平台 |
| 多 Agent 编排 UI | A2UI | Catalog 安全模型 |
| React 专有前端 | AG-UI | CopilotKit 一级支持 |
| 企业级安全需求 | A2UI | Catalog 白名单 + 双重验证 |
