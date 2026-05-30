# Roadmap：智能体感知 & 生成式 UI 演化计划

> 基于调研报告，为 simple_chat_agent_demo 制定的渐进式演化路线
> 修订日期：2026-05-26 | 修订原因：重新评估各阶段粒度与教学 ROI

---

## 设计原则

1. **每阶段一个核心概念** — 学生完整消化一个新认知后再引入下一个
2. **渐进控制权转移** — Phase 1 后端决定 UI → Phase 2 Agent 感知环境 → Phase 3 模型输出 UI → Phase 4 人机协作规划
3. **保持可读性** — 单文件前端、无构建工具、每层职责清晰
4. **可独立运行** — 每阶段结束时 demo 完整可用，不存在半成品状态

---

## 总体思路

```
当前状态                         目标状态
┌──────────────┐               ┌────────────────────────────────┐
│ 纯文本聊天   │               │ 生成式 UI Agent                │
│ + 思考折叠   │  ──────────→  │ + 感知层 + 自适应交互          │
│ + 基础 HITL  │               │ + 声明式组件 + 协作规划        │
└──────────────┘               └────────────────────────────────┘
```

分 4 个阶段，每阶段引入一个核心概念，独立可运行。

---

## Phase 1：Static Generative UI（工具结果卡片化）

**核心概念**：tool_call → structured data → component rendering

**时间预估**：2-3 天

### 教学目标

让学生理解 Static GenUI 的本质：**前端预定义一组组件模板，Agent 只负责"何时展示 + 填什么数据"**。控制权完全在前端——模型无法发明新组件，只能触发已注册的组件并填充数据槽。

### 改造点

| 层 | 改动 |
|----|------|
| **SSE 协议** | 新增 `render_component` / `component_loading` / `component_error` 事件 |
| **chat_core** | 工具执行后 yield render 事件；新增 `_build_component_props` 数据转换层 |
| **前端** | 注册 `COMPONENT_RENDERERS` 映射表，收到 render 事件按 type 渲染对应卡片 |

### SSE 事件设计

```
event: component_loading
data: {"component_type": "search_results", "tool_call_id": "call_abc", "placeholder_text": "正在搜索..."}

event: render_component
data: {"component_type": "search_results", "tool_call_id": "call_abc", "props": {...}}

event: component_error
data: {"component_type": "search_results", "tool_call_id": "call_abc", "error_message": "搜索超时"}
```

### 组件类型清单

| 工具名 | component_type | 组件形态 |
|--------|---------------|----------|
| `web_search` | `search_results` | 搜索结果卡片列表（标题链接 + 摘要 + 来源 badge） |
| `ask_user` | `user_input_form` | 结构化表单（问题 + 快捷选项 pill + 输入框） |
| `execute_shell_command` | `shell_panel` | 命令面板（等宽命令 + 原因 + 审批按钮 + 输出折叠区） |

### 关键实现：MCP 搜索结果解析

DashScope WebSearch MCP 返回的是**文本**（markdown 格式含 `[title](url)` + 摘要段落），不是结构化 JSON。需要一个 parser + fallback：

```python
# chat_core.py — _build_component_props 中 web_search 分支

import re

_SEARCH_RESULT_RE = re.compile(
    r'\[(?P<title>[^\]]+)\]\((?P<url>[^\)]+)\)\s*\n(?P<snippet>[^\n]+)',
    re.MULTILINE,
)

def _parse_search_results(raw_text: str, query: str) -> dict:
    """从 MCP web_search 返回的 markdown 文本中提取结构化搜索结果。
    解析失败时 fallback 为 markdown 原文渲染。
    """
    results = []
    for m in _SEARCH_RESULT_RE.finditer(raw_text):
        results.append({
            "title": m.group("title"),
            "url": m.group("url"),
            "snippet": m.group("snippet").strip(),
        })

    if results:
        return {"query": query, "results": results, "total_count": len(results)}

    # fallback: 解析不出结构化结果,把原文作为 markdown 整体展示
    return {"query": query, "results": [], "raw_markdown": raw_text}
```

前端对应处理 fallback：
```javascript
function renderSearchCards(props) {
  if (props.results.length > 0) {
    // 渲染结构化卡片列表
    return props.results.map(r => `
      <div class="search-card">
        <a href="${r.url}" target="_blank">${escapeHtml(r.title)}</a>
        <p class="snippet">${escapeHtml(r.snippet)}</p>
      </div>
    `).join('');
  }
  // fallback: markdown 渲染
  return `<div class="search-fallback">${renderMarkdown(props.raw_markdown)}</div>`;
}
```

### chat_core 层改动位置

```python
# _stream_react_rounds() 工具执行完成分支（伪代码）
TOOL_COMPONENT_MAP = {"web_search": "search_results"}

# 执行前
component_type = TOOL_COMPONENT_MAP.get(tool_name)
if component_type:
    yield ("component_loading", {
        "component_type": component_type,
        "tool_call_id": call_id,
        "placeholder_text": f"正在执行 {tool_name}...",
    })

# 执行
result_text = await mcp_web_search.call_tool_async(tool_name, args)

# 执行后
if component_type:
    props = _build_component_props(tool_name, args, result_text)
    yield ("render_component", {
        "component_type": component_type,
        "tool_call_id": call_id,
        "props": props,
    })
```

### HITL 工具的卡片化

`ask_user` 和 `execute_shell_command` 已有 `await_user` 事件，Phase 1 只需让前端在渲染 HITL bubble 时复用卡片样式——不改后端事件契约，仅前端视觉升级。

### 架构约束

- `render_component` 事件与 `tool_result` 事件**并存**，不替代。`tool_result` 仍然发（供 debug/日志），`render_component` 是 UI 增强层。
- 前端收到 `render_component` 时，应**替换**同 `tool_call_id` 对应的 `component_loading` 占位符。
- 模型看不到 `render_component`——它只看到 `role=tool` 中的全文结果。

---

## Phase 2：上下文感知 Agent（环境感知 + 自适应）

**核心概念**：前端上报上下文 → Agent 据此调整行为与 UI

**时间预估**：0.5-1 天

### 教学目标

演示**数据链路**：前端环境采集 → 随请求上报 → 后端注入 system prompt → Agent 行为变化 → 前端据 hint 切换布局。重点在管道本身，不在分类器实现细节。

### 为什么精简

感知层的工程价值在于 5+ 维度的分类器（意图/阶段/复杂度/专业度/UI 模式）。但对教学 demo 而言：
- 5 个 regex heuristic 函数 = 200 行 if/else，学生学到的是"正则可以粗糙分类"而非"感知层为什么重要"
- 真正要演示的是**数据怎么流**，不是数据怎么分析
- 保持"每阶段一个概念"原则

### 改造点（极简版）

| 层 | 改动 |
|----|------|
| **前端** | 采集 3 个信号随请求发送 |
| **HTTP 层** | `ChatRequest` 新增 `context` 字段 |
| **chat_core** | 一个 `_compute_adaptive_prompt(context, memory)` 函数 |
| **SSE** | 新增 `ui_hint` 事件 |

### 前端采集（3 行核心代码）

```javascript
function collectContext() {
  return {
    viewport_width: window.innerWidth,
    selected_text: window.getSelection()?.toString()?.slice(0, 500) || null,
    session_message_count: chatMessages.length,
  };
}
// POST /api/chat body 中新增 context 字段
```

### 后端感知（一个函数，不是一个类）

```python
# chat_core.py
def _compute_adaptive_prompt(context: dict | None, memory: Memory) -> tuple[str, str]:
    """基于上下文计算：(补充 system prompt 片段, ui_hint mode)。
    
    只做一维判断（对话深度 → 复杂度推断），演示管道即可。
    """
    msg_count = (context or {}).get("session_message_count", 0)
    has_selection = bool((context or {}).get("selected_text"))
    
    if has_selection:
        return ("用户选中了一段文本，优先围绕选中内容回答。", "focus")
    if msg_count > 10:
        return ("对话已较长，保持简洁，避免重复已说过的内容。", "compact")
    return ("", "chat")
```

### ui_hint 事件

```
event: ui_hint
data: {"mode": "focus", "reason": "user_has_selection"}
```

前端据此：
- `focus` → 高亮选中文本相关的上下文区域
- `compact` → 折叠历史消息，只展示最近 3 轮
- `chat` → 默认模式，不做改变

### 扩展方向（留作注释，不实现）

```python
# 未来可扩展为完整 PerceptionModule:
# - _classify_intent: chitchat | task_request | clarification
# - _detect_phase: opening | exploring | executing | wrapping_up
# - _infer_expertise: beginner | intermediate | expert
# 但对教学 demo 而言，上面的单函数已足够演示"感知→自适应"链路
```

---

## Phase 3：Declarative Generative UI（声明式动态界面）

**核心概念**：模型通过 tool_call 输出 UI 结构 JSON → 前端按 schema 递归渲染组件树

**时间预估**：5-7 天（分两步）

### 教学目标

让学生理解 Declarative GenUI 的本质：**模型获得了"画界面"的能力，但画什么受前端 component catalog 约束**。模型输出的是结构描述（JSON），不是代码（HTML/JS），安全可控。

### Phase 3a：核心渲染器（3-4 天）

#### 最小组件 Catalog（6 种，覆盖所有基本模式）

| type | 模式 | 用途 |
|------|------|------|
| `text` | 叶节点 | 文本展示（支持 variant: h1/h2/body/caption/code） |
| `card` | 容器 | 带标题的卡片容器 |
| `row` | 布局 | 水平排列子组件 |
| `column` | 布局 | 垂直排列子组件 |
| `table` | 数据 | 表格（columns 定义 + rows 数据） |
| `button` | 交互 | 按钮（触发 action 回传后端） |

为什么只要 6 种：用这 6 种可以组合出 roadmap 原版的"航班比价"示例——`card` 包 `table` + `row` 包两个 `button`。学生看到的是**组合模式**而非组件数量。

#### `render_ui` 工具定义

```python
# chat_core.py — 加入 LOCAL_TOOLS（但不走 HITL 中断路径）
RENDER_UI_TOOL = {
    "type": "function",
    "function": {
        "name": "render_ui",
        "description": (
            "当回答需要结构化展示（表格、对比、步骤等）时调用。"
            "输出 JSON 描述 UI 界面，前端据此渲染交互组件。"
            "仅在纯文本不够直观时使用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "surface_id": {"type": "string", "description": "UI 表面唯一 ID"},
                "components": {
                    "type": "array",
                    "description": "组件扁平列表，通过 children 数组引用构建树，必须有 id='root'",
                    "items": {"type": "object", "required": ["id", "type"]},
                },
                "data": {
                    "type": "object",
                    "description": "组件引用的数据，通过 JSON Pointer path 绑定",
                },
            },
            "required": ["surface_id", "components"],
        },
    },
}
```

#### 第三种工具派发类型

当前 `_stream_react_rounds` 的派发是二分的：
- `name in LOCAL_TOOLS` → HITL 中断
- 其他 → MCP 调用

`render_ui` 引入**第三种**：本地立即执行、不中断、不走网络。重构派发表：

```python
# _stream_react_rounds 工具派发逻辑（伪代码）
IMMEDIATE_LOCAL_TOOLS = {"render_ui": _execute_render_ui}

for tc in accumulated_tool_calls:
    name = tc["name"]
    if name in LOCAL_TOOLS:
        # HITL: 中断，写 _PENDING，关流
        ...
    elif name in IMMEDIATE_LOCAL_TOOLS:
        # 本地立即执行，yield 事件，构造 tool result，继续循环
        result = IMMEDIATE_LOCAL_TOOLS[name](tc["args"])
        yield ("ui_surface_create", {"surface_id": args["surface_id"]})
        yield ("ui_surface_update", {"surface_id": ..., "components": ...})
        if args.get("data"):
            yield ("ui_data_update", {"surface_id": ..., "path": "/", "value": args["data"]})
        messages.append({"role": "tool", "tool_call_id": tc["id"], "content": "UI 已渲染"})
    else:
        # MCP 远程调用
        result_text = await mcp_web_search.call_tool_async(name, args)
        ...
```

#### SSE 事件

```
event: ui_surface_create
data: {"surface_id": "flight_compare"}

event: ui_surface_update
data: {"surface_id": "flight_compare", "components": [...]}

event: ui_data_update
data: {"surface_id": "flight_compare", "path": "/", "value": {...}}
```

#### 前端 DeclarativeRenderer（核心 ~150 行）

```javascript
class DeclarativeRenderer {
  constructor(container) {
    this.container = container;
    this.surfaces = new Map(); // surface_id → {components: Map, data: {}, dom: Element}
  }

  handleEvent(eventName, payload) {
    switch (eventName) {
      case 'ui_surface_create': this._createSurface(payload.surface_id); break;
      case 'ui_surface_update': this._updateComponents(payload.surface_id, payload.components); break;
      case 'ui_data_update': this._updateData(payload.surface_id, payload.path, payload.value); break;
      case 'ui_surface_delete': this._deleteSurface(payload.surface_id); break;
    }
  }

  _renderNode(comp, surface, depth = 0) {
    const fn = NODE_RENDERERS[comp.type];
    if (!fn) return document.createTextNode(`[unknown: ${comp.type}]`);
    return fn(comp, surface, (childId) => {
      const child = surface.components.get(childId);
      return child ? this._renderNode(child, surface, depth + 1) : null;
    });
  }
}

const NODE_RENDERERS = {
  text:   (comp, surface) => { /* 创建 span/h1/h2/p/code 按 variant */ },
  card:   (comp, surface, renderChild) => { /* div.card + title + children.map(renderChild) */ },
  row:    (comp, surface, renderChild) => { /* div.flex-row + children.map(renderChild) */ },
  column: (comp, surface, renderChild) => { /* div.flex-col + children.map(renderChild) */ },
  table:  (comp, surface) => { /* 从 surface.data 按 rows_path 取数据渲染 <table> */ },
  button: (comp, surface) => { /* button + onclick → POST /api/ui_action */ },
};
```

#### 完整示例：航班比价

模型调用 `render_ui`：
```json
{
  "surface_id": "flight_compare",
  "components": [
    {"id": "root", "type": "card", "title": "航班比价结果", "children": ["tbl", "actions"]},
    {"id": "tbl", "type": "table", "columns": [
      {"key": "flight", "label": "航班"},
      {"key": "price", "label": "价格"},
      {"key": "duration", "label": "时长"}
    ], "rows_path": "/flights"},
    {"id": "actions", "type": "row", "children": ["btn1", "btn2"], "gap": 12},
    {"id": "btn1", "type": "button", "label": "选最低价", "variant": "primary",
     "action": {"event_name": "select_flight", "context": {"criteria": "cheapest"}}},
    {"id": "btn2", "type": "button", "label": "选最快", "variant": "secondary",
     "action": {"event_name": "select_flight", "context": {"criteria": "fastest"}}}
  ],
  "data": {
    "flights": [
      {"flight": "MU5101", "price": "¥680", "duration": "2h15m"},
      {"flight": "CA1831", "price": "¥720", "duration": "2h05m"},
      {"flight": "CZ3512", "price": "¥650", "duration": "2h30m"}
    ]
  }
}
```

### Phase 3b：Action 系统 + 数据更新（2-3 天）

#### 用户交互回传

```python
# web_chat_agent.py
class UiActionRequest(BaseModel):
    session_id: str
    surface_id: str
    event_name: str
    context: dict

@app.post("/api/ui_action")
async def ui_action(req: UiActionRequest) -> StreamingResponse:
    """用户在声明式 UI 上的操作回传，作为 tool result 注入 ReAct 循环。"""
    ...
```

#### 增量数据更新

Agent 后续轮次可以只更新数据而不重建组件树：
```
event: ui_data_update
data: {"surface_id": "flight_compare", "path": "/flights/0/price", "value": "¥650（已降价）"}
```

前端收到后只更新对应 DOM 节点，不全量重渲染。

#### 数据绑定机制

组件 props 中使用 `{"path": "/flights"}` 格式引用 data model：
```javascript
function resolveValue(dynamicVal, data) {
  if (typeof dynamicVal === 'string') return dynamicVal;  // 字面量
  if (dynamicVal?.path) return jsonPointerGet(data, dynamicVal.path);  // 绑定
  return '';
}
```

---

## Phase 4：Plan-and-Execute（协作式任务规划）

**核心概念**：Agent 输出可编辑的执行计划 → 用户审阅/修改 → 逐步执行 + 实时反馈

**时间预估**：4-5 天

### 教学目标

从"Agent 给我答案"跨越到"我和 Agent 一起规划并执行任务"。这是 Mixed-Initiative 交互的核心模式：**Agent 提议，人类决策，双方协作执行**。

### 交互流程

```
用户发送复杂任务（如"帮我对比三款笔记本"）
    ↓
Agent 判断复杂度 → 输出执行计划（activity_snapshot 事件）
    ↓
前端渲染可编辑的步骤卡片（拖拽重排/删除/新增）
    ↓
用户确认 → POST /api/plan_confirm
    ↓
Agent 逐步执行，每步更新状态（activity_delta 事件）
    ↓
步骤失败 → 自动暂停，等用户决策（跳过/重试/修改计划）
```

### SSE 事件设计（对齐 AG-UI ACTIVITY 类型）

```
event: activity_snapshot
data: {
  "plan_id": "plan_001",
  "title": "帮你比较三款笔记本",
  "steps": [
    {"id": "s1", "title": "搜索 MacBook Pro 规格和价格", "status": "pending"},
    {"id": "s2", "title": "搜索 ThinkPad X1 规格和价格", "status": "pending"},
    {"id": "s3", "title": "生成对比表格", "status": "pending"}
  ],
  "editable": true
}

event: activity_delta
data: {
  "plan_id": "plan_001",
  "patch": [{"op": "replace", "path": "/steps/0/status", "value": "running"}]
}
```

### 后端数据结构

```python
@dataclass
class PlanStep:
    id: str
    title: str
    status: str  # "pending" | "running" | "done" | "error" | "skipped"
    tool_name: str | None = None
    result_summary: str | None = None
    error_message: str | None = None

@dataclass
class ExecutionPlan:
    plan_id: str
    title: str
    steps: list[PlanStep]
    confirmed: bool = False
    current_step_index: int = 0
```

### 触发机制

Plan-and-Execute 不应该每次对话都触发，而是在 Agent 判断任务足够复杂时主动发起。实现方式：

**方案 A**（简单，推荐）：新增 `create_plan` 为 LOCAL_TOOL，模型自主判断何时调用。与现有 HITL 机制复用——`create_plan` 触发中断等用户确认，用户确认后 resume 执行。

**方案 B**（基于 Phase 2 感知）：`_compute_adaptive_prompt` 根据复杂度在 system prompt 中注入"请先列出计划"指引。

推荐方案 A——显式工具调用比 prompt 注入更可靠、更可观测。

### 前端 Plan 卡片

- 步骤列表，每步：状态图标 + 标题 + 操作按钮
- 执行前：可拖拽重排、删除、新增
- 执行中：当前步骤高亮动画，已完成步骤折叠为一行摘要
- 失败步骤：红色标记 + "跳过 / 重试 / 修改计划"按钮组

### API 端点

```python
class PlanConfirmRequest(BaseModel):
    session_id: str
    plan_id: str
    steps: list[dict]  # 用户可能修改过（重排/删除/新增）

@app.post("/api/plan_confirm")
async def plan_confirm(req: PlanConfirmRequest) -> StreamingResponse:
    """确认计划后开始执行，返回 SSE 流追踪进度。"""
    ...
```

### 与现有机制的关系

- `create_plan` 加入 `LOCAL_TOOLS`，`_LOCAL_TOOL_KIND["create_plan"] = "plan"`（新 kind）
- 前端新增 `kind === "plan"` 分支渲染 plan 编辑卡片
- 确认后的执行流程复用 `_stream_react_rounds`（每步是一次 tool 调用）
- 步骤级 HITL：如果某步要调 `execute_shell_command`，仍走现有 HITL 中断

---

## 阶段依赖关系

```
Phase 1 (Static GenUI) ─────────→ Phase 3 (Declarative GenUI)
    │                                    │
    │                                    ▼
    │                              Phase 4 (Plan-and-Execute)
    │
Phase 2 (上下文感知) ── 独立，可与 Phase 1 并行或在任意阶段后插入
```

关键依赖：
- Phase 3 依赖 Phase 1 的组件渲染基础设施（`COMPONENT_RENDERERS` + SSE 事件处理分支）
- Phase 4 依赖 Phase 3 的 `DeclarativeRenderer`（plan 卡片用声明式渲染）+ Phase 1 的 HITL 机制
- Phase 2 完全独立，只改 request model + system prompt

---

## 时间总览

| Phase | 核心概念 | 预估时间 | 累计 |
|-------|----------|----------|------|
| 1 | tool → structured data → component | 2-3 天 | 2-3 天 |
| 2 | context upload → prompt injection → behavior change | 0.5-1 天 | 3-4 天 |
| 3a | model outputs UI JSON → recursive rendering | 3-4 天 | 6-8 天 |
| 3b | data binding + action system | 2-3 天 | 8-11 天 |
| 4 | collaborative planning + step execution | 4-5 天 | 12-16 天 |

---

## 技术选型

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 前端框架 | 保持原生 JS | 教学清晰，无构建依赖 |
| 组件系统 | 纯 DOM 操作 + class-based renderer | 学生能看到每个节点怎么创建的 |
| 声明式协议 | 自定义轻量 JSON DSL（A2UI 子集） | A2UI 完整版过重，只取邻接表 + data binding |
| Agent-User 协议 | 保持现有 SSE 事件名 | 教学阶段不增加协议抽象层 |
| 状态同步 | 前端 Map + JSON Pointer | 够用，无需引入 jsonpatch 库 |

---

## Future Directions（不列入主线 Phase）

以下特性各自有独立价值，但超出了"教学 demo"的核心演化线。可作为进阶选做：

### Confidence Signal（置信度可视化）
- 模型自评 `[confidence: 0.8]` + 工具调用启发式修正
- 低置信度时回答变为草稿状态，用户确认后正式采纳
- 教学点：建立 AI 信任的 UX 模式

### Agent Steering（实时方向修正）
- Agent 执行多步任务时，用户可中途注入修正指令
- `POST /api/steer` + `_STEER_QUEUE` 队列 + 每轮开始前检查
- 教学点：打破"要么等 Agent 完成要么重来"的二选一困境

### 多模态感知
- 图片上传 → Qwen3-VL 理解（DashScope OpenAI 兼容接口）
- 文件解析 → base64 或 VL 文档模式
- 语音输入 → 浏览器 Web Speech API（纯前端，无后端 ASR）
- 教学点：Agent 的输入从纯文本扩展到多模态

### AG-UI 协议对齐
- 现有事件 → AG-UI 标准事件名映射（参见调研报告 §2.2）
- 渐进策略：先在 payload 嵌套标准格式，保持向后兼容
- 教学点：产品协议如何对齐行业标准

### Checkpoint/Resume（断点恢复）
- 持久化 pending plan / HITL 状态 / active surfaces
- 页面刷新后可恢复到中断点继续
- 教学点：有状态 Agent 的持久化设计

---

## 每阶段产出物

| Phase | 代码产出 | 学生应能回答 |
|-------|----------|-------------|
| 1 | 3 种卡片模板 + parser + SSE 事件 | "Static GenUI 和直接渲染 markdown 有什么区别？" |
| 2 | context 采集 + 自适应 prompt + ui_hint | "Agent 怎么'看到'用户所处的环境？" |
| 3 | DeclarativeRenderer + 6 组件 + render_ui 工具 | "模型输出 UI JSON 和输出 HTML 有什么区别？为什么前者更安全？" |
| 4 | Plan 卡片 + create_plan 工具 + 逐步执行 | "人机协作规划比 Agent 独自执行好在哪？" |

---

## 下一步

建议从 Phase 1 开始——改动集中在 3 个文件（`chat_core.py` yield 逻辑 + `web_chat_agent.py` 无改动 + `index.html` 组件模板），效果直观（搜索结果从文本变卡片），且为后续阶段奠定基础。

开始前需确认一件事：实际跑一次 `API_MODE=chat` 下的 web_search，看 MCP 返回的文本具体是什么格式，据此调整 `_parse_search_results` 的正则。
