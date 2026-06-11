# A2UI 协议调研

Google A2UI (Agent-to-UI) 协议的深度调研文档集。

## 文档索引

| 序号 | 文档 | 内容 |
|---|---|---|
| 01 | [协议概述](01-协议概述.md) | A2UI 是什么、解决什么问题、谁在用、版本演进 |
| 02 | [架构与核心概念](02-架构与核心概念.md) | 三层架构、Surface/Component/Catalog/DataModel 等核心抽象 |
| 03 | [消息类型与 Schema](03-消息类型与Schema.md) | v0.8 / v0.9 消息类型、字段定义、JSON 示例 |
| 04 | [组件模型与数据绑定](04-组件模型与数据绑定.md) | 邻接表组件模型、JSON Pointer 数据绑定、基础组件目录 |
| 05 | [安全模型](05-安全模型.md) | 声明式安全、目录白名单、双重验证、Surface 状态机 |
| 06 | [传输与流式](06-传输与流式.md) | JSONL 流式、SSE/WebSocket/A2A/AG-UI 传输层、渐进式渲染 |
| 07 | [渲染器生态](07-渲染器生态.md) | 官方渲染器 (Lit/Angular/React/Flutter) + 社区渲染器 (Vue/Compose/HarmonyOS/Blazor) |
| 08 | [与 AG-UI 对比](08-与AG-UI对比.md) | A2UI vs AG-UI 定位/架构/事件类型/安全模型/适用场景 |
| 09 | [与本项目 SSE 事件契约映射](09-与本项目SSE事件契约映射.md) | 本项目自定义 SSE 事件 → A2UI/AG-UI 概念映射与迁移分析 |
| 10 | [实现案例](10-实现案例.md) | B站 Vue 渲染器、Jetpack Compose 渲染器、鸿蒙 AGenUI、阿里千问 |

## 关键结论速览

1. **A2UI 是声明式 UI 渲染协议**，不是传输协议。Agent 输出 JSON 组件描述，客户端用原生组件渲染。
2. **AG-UI 是事件流传输协议**，不是渲染协议。两者互补：AG-UI 传输 A2UI 消息。
3. **A2UI 核心安全模型**：基于目录的组件白名单 + 声明式数据格式（无代码执行）。
4. **邻接表组件模型**：扁平 ID 引用而非嵌套 JSON 树，为 LLM 增量生成优化。
5. **本项目 Phase 3 声明式 UI**（`render_ui` / `ui_surface_create` / `ui_data_update`）与 A2UI 的 `surfaceUpdate` / `dataModelUpdate` 高度同构，但更轻量（无 Catalog 安全模型、无 JSON Pointer 绑定）。

## 调研时间

2026-06-09
