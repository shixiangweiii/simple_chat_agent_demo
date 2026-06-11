# Roadmap：智能体感知 & 生成式 UI 演化计划

> 基于调研报告，为 simple_chat_agent_demo 制定的渐进式演化路线
> 修订日期：2026-05-31 | 修订原因：同步 Phase 1-6 实际开发进展与当前接口契约

---

## 当前实现状态

截至 2026-05-31，主线 Phase 1-4 已完成，并额外落地 Future Directions 中的 Checkpoint/Resume 与 Confidence Signal。当前 demo 已从“纯文本 ReAct 聊天”演化为：

- `API_MODE=responses`：保留 Responses API 内置 `web_search` 生命周期，通过 `search_status` 展示联网状态，并支持上下文感知与置信度信号。
- `API_MODE=chat`：使用 Chat Completions native function calling + DashScope WebSearch MCP，支持 Static GenUI、Declarative GenUI、UI action、Plan-and-Execute、runtime checkpoint/resume、confidence draft。
- 前端仍保持单文件原生 JS，不引入构建工具；后端仍保持 HTTP / 业务 / LLM 三层边界。

| 阶段 | 状态 | 当前实际产物 |
|------|------|--------------|
| Phase 1 Static GenUI | 已完成 | `component_loading` / `render_component` / `component_error`，`web_search → search_results` 结构化卡片 |
| Phase 2 Context Awareness | 已完成 | `ChatContext`、`_compute_adaptive_prompt()`、`ui_hint {mode, reason}` |
| Phase 3a Declarative GenUI | 已完成 | `render_ui` native tool、`ui_surface_*` SSE、`DeclarativeRenderer` 6 组件 |
| Phase 3b Action + Data Update | 已完成 | `/api/ui_action`、`update_ui_data`、JSON Pointer 数据更新、button action |
| Phase 4 Plan-and-Execute | 已完成 | `create_plan`、`/api/plan_confirm`、`/api/plan_decision`、`activity_*` |
| Phase 5 Checkpoint/Resume | 已完成 | `data/runtime_state/{session_id}.json`、`GET /api/runtime_state` |
| Phase 6 Confidence Signal | 已完成 | `confidence_signal`、`/api/confidence_decision`、低置信度草稿采纳 |

未完成的进阶方向：Agent Steering、AG-UI 协议对齐、多模态感知。

## 设计原则

1. **每阶段一个核心概念** — 学生完整消化一个新认知后再引入下一个
2. **渐进控制权转移** — Phase 1 后端决定 UI → Phase 2 Agent 感知环境 → Phase 3 模型输出 UI → Phase 4 人机协作规划
3. **保持可读性** — 单文件前端、无构建工具、每层职责清晰
4. **可独立运行** — 每阶段结束时 demo 完整可用，不存在半成品状态

---

## 总体思路

```
原始状态                         当前已实现状态
┌──────────────┐               ┌────────────────────────────────┐
│ 纯文本聊天   │               │ 生成式 UI Agent                │
│ + 思考折叠   │  ──────────→  │ + 感知层 + 自适应交互          │
│ + 基础 HITL  │               │ + 声明式组件 + 协作规划        │
│              │               │ + 断点恢复 + 置信度草稿        │
└──────────────┘               └────────────────────────────────┘
```

主线按 4 个阶段递进；随后已把两个 Future Direction 产品化为 Phase 5 / Phase 6。每个阶段结束时 demo 都保持完整可运行。

---

## Phase 1：Static Generative UI（工具结果卡片化）

**状态**：已完成。当前只对 `API_MODE=chat` + MCP `web_search` 路径做搜索结果卡片化；默认 `API_MODE=responses` 仍使用内置 web_search 的 `search_status` banner，不伪造组件事件。

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

| 工具名 / 事件 | component_type / UI | 当前实现 |
|--------|---------------|----------|
| MCP `web_search` | `search_results` | 搜索结果卡片列表（标题链接 + 摘要 + 来源 badge） |
| `ask_user` | HITL bubble | 通过 `await_user(kind="input")` 渲染输入卡，不走 `render_component` |
| `execute_shell_command` | HITL bubble | 通过 `await_user(kind="approval")` 渲染审批卡，不走 `render_component` |

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

`ask_user` 和 `execute_shell_command` 继续使用 `await_user` 事件。它们不是 Static GenUI 的 `render_component` 组件，而是 HITL 专用交互卡；这样保持“用户输入/审批中断”和“工具结果卡片化”的教学边界清晰。

### 架构约束

- `render_component` 事件与 `tool_result` 事件**并存**，不替代。`tool_result` 仍然发（供 debug/日志），`render_component` 是 UI 增强层。
- 前端收到 `render_component` 时，应**替换**同 `tool_call_id` 对应的 `component_loading` 占位符。
- 模型看不到 `render_component`——它只看到 `role=tool` 中的全文结果。

---

## Phase 2：上下文感知 Agent（环境感知 + 自适应）

**状态**：已完成。Web 请求携带 `context`，HTTP 层用 `ChatContext` 做 Pydantic 校验，业务层只接收普通 dict。

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
def _compute_adaptive_prompt(context: dict | None, memory: Memory) -> tuple[str, str, str]:
    """基于上下文计算：(补充 system prompt 片段, ui_hint mode, reason)。
    
    只做一维判断（对话深度 → 复杂度推断），演示管道即可。
    """
    msg_count = (context or {}).get("session_message_count", 0)
    has_selection = bool((context or {}).get("selected_text"))
    viewport_width = (context or {}).get("viewport_width")
    
    if has_selection:
        return ("用户选中了一段文本，优先围绕选中内容回答。", "focus", "selected_text")
    if viewport_width and viewport_width <= 640:
        return ("当前视口较窄，回复保持紧凑。", "compact", "narrow_viewport")
    if msg_count > 10:
        return ("对话已较长，保持简洁，避免重复已说过的内容。", "compact", "long_session")
    return ("", "chat", "default")
```

### ui_hint 事件

```
event: ui_hint
data: {"mode": "focus", "reason": "selected_text"}
```

前端据此：
- `focus` → 高亮选中文本相关的上下文区域
- `compact` → 折叠历史消息，只展示最近 3 轮
- `chat` → 默认模式，不做改变

实现约定：`ui_hint` 仅在 `mode != "chat"` 时发送；`reason` 可取 `selected_text` / `narrow_viewport` / `long_session`。

当前实际规则：
- `selected_text.strip()` 非空优先进入 `focus`，服务端截断选中文本到 200 字注入 prompt。
- `viewport_width <= 640` 或 `session_message_count > 10` 进入 `compact`。
- context 缺失或类型异常时回退到 Memory 中的用户消息数；默认返回 `("", "chat", "default")`。

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

**状态**：已完成。Phase 3a / 3b 仅在 `API_MODE=chat` 生效，不给 `responses` 模式补伪 action 或伪组件事件。

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
| `button` | 交互 | 按钮（Phase 3a 仅占位提示；Phase 3b 触发 action 回传后端） |

为什么只要 6 种：用这 6 种可以组合出 roadmap 原版的"航班比价"示例——`card` 包 `table` + `row` 包两个 `button`。学生看到的是**组合模式**而非组件数量。

#### `render_ui` 工具定义

```python
# chat_core.py — 加入 chat native tools（但不放入 HITL LOCAL_TOOLS）
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

实现约定：Phase 3a 仅在 `API_MODE=chat` 下生效；Phase 3a 时 `button` 只渲染占位，Phase 3b 已接入 `/api/ui_action`。

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
  button: (comp, surface) => { /* Phase 3a: button 占位提示; Phase 3b: POST /api/ui_action */ },
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
    component_id: str
    event_name: str

@app.post("/api/ui_action")
async def ui_action(req: UiActionRequest) -> StreamingResponse:
    """用户在声明式 UI 上的操作回传，新启 SSE 流继续 ReAct。"""
    ...
```

实现约定：`/api/ui_action` 仅在 `API_MODE=chat` 下可用。后端从内存态 surface registry 校验 `session_id + surface_id + component_id + event_name`，不信任前端传入 action context；校验成功后将点击转换为结构化用户事件输入，开启新的 AI bubble 流。

#### 增量数据更新

Agent 后续轮次可以只更新数据而不重建组件树：
```
event: ui_data_update
data: {"surface_id": "flight_compare", "path": "/flights/0/price", "value": "¥650（已降价）"}
```

首版实现为 JSON Pointer 写入 data model 后重渲染对应 surface；DOM 绑定级局部刷新留到后续优化。

新增 native tool：

```python
UPDATE_UI_DATA_TOOL = {
    "type": "function",
    "function": {
        "name": "update_ui_data",
        "parameters": {
            "type": "object",
            "properties": {
                "surface_id": {"type": "string"},
                "path": {"type": "string"},
                "value": {},
            },
            "required": ["surface_id", "path", "value"],
        },
    },
}
```

UI surface/action 状态不进入 Memory / markdown 归档。Phase 5 之后，它们会同步保存到 `data/runtime_state/{session_id}.json`，用于页面刷新或服务重启后的恢复。

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

**状态**：已完成。计划卡是专用 HITL bubble，不通过 `DeclarativeRenderer` 渲染；执行过程仍可调用 `render_ui`、`update_ui_data`、MCP 搜索和 HITL 工具。

**核心概念**：Agent 输出可编辑的执行计划 → 用户审阅/修改 → 逐步执行 + 实时反馈

**时间预估**：4-5 天

### 教学目标

从"Agent 给我答案"跨越到"我和 Agent 一起规划并执行任务"。这是 Mixed-Initiative 交互的核心模式：**Agent 提议，人类决策，双方协作执行**。

### 交互流程

```
用户发送复杂任务（如"帮我对比三款笔记本"）
    ↓
Agent 判断复杂度 → 调用 create_plan 并输出执行计划（activity_snapshot 事件）
    ↓
前端渲染可编辑的步骤卡片（上移/下移/删除/新增）
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
- 执行前：通过上移/下移按钮重排，支持删除、新增
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

```python
class PlanDecisionRequest(BaseModel):
    session_id: str
    plan_id: str
    step_id: str
    decision: Literal["skip", "retry", "update"]
    steps: list[dict] | None = None

@app.post("/api/plan_decision")
async def plan_decision(req: PlanDecisionRequest) -> StreamingResponse:
    """失败步骤的跳过/重试/修改后继续。"""
    ...
```

### 与现有机制的关系

- `create_plan` 加入 `LOCAL_TOOLS`，`_LOCAL_TOOL_KIND["create_plan"] = "plan"`（新 kind）
- 前端新增 `kind === "plan"` 分支渲染 plan 编辑卡片
- 确认后的执行流程使用单步小 ReAct 循环（`PLAN_STEP_MAX_ROUNDS = 3`），不消耗全局 `MAX_ROUNDS`
- 计划状态保存在 `_PLANS`，不进入 Memory / markdown 归档；Phase 5 之后同步到 runtime sidecar，用于刷新/重启恢复；完成态计划会写入 Memory 摘要并从 runtime sidecar 回收
- 步骤级 HITL：如果某步要调 `execute_shell_command` / `ask_user`，仍走现有 HITL 中断，resume 后继续当前计划步骤
- 失败步骤的 `update` 会保留已完成/已跳过步骤的状态与摘要，只重置当前失败步骤

---

## Phase 5：Checkpoint/Resume（运行态持久化）

**状态**：已完成。

**核心概念**：Memory 归档只保存对话历史；Agent 的运行态需要单独持久化，才能在刷新页面或服务重启后继续中断点。

### 当前实现

- 新增 `data/runtime_state/{session_id}.json` sidecar，使用原子写入保存运行态。
- 保存内容：`_PENDING`、`_UI_SURFACES`、`_PLANS`。
- 不保存内容：Memory markdown、低置信度草稿、前端 DOM 细节。
- 入口懒恢复：`/api/chat`、`/api/resume`、`/api/ui_action`、`/api/plan_confirm`、`/api/plan_decision`、`/api/plan_continue`、`/api/history`、`/api/runtime_state`。
- `reset_session()` / `delete_session()` 同步删除 runtime state 文件。
- 损坏、版本不兼容或结构不符合预期的 JSON 会记录 warning 并忽略，不影响普通聊天。

### HTTP 接口

```http
GET /api/runtime_state?session_id=<uuid>
```

返回给前端的快照只包含可重建 UI 的安全字段：

```json
{
  "session_id": "uuid",
  "pending": {"tool_call_id": "...", "name": "ask_user", "args": {}, "kind": "input"},
  "surfaces": [{"surface_id": "flight_compare", "components": [], "data": {}}],
  "plans": [{"plan_id": "plan_0001", "title": "...", "steps": [], "status": "running", "continuable": true}]
}
```

后端私有恢复字段（如 `messages`、`tools`、`remaining_tool_calls`）不会暴露给前端。

运行中的计划恢复后可调用：

```http
POST /api/plan_continue
```

```json
{"session_id": "uuid", "plan_id": "plan_0001"}
```

### 前端恢复

`loadHistory()` 完成后调用 `loadRuntimeState()`：
- 恢复 HITL bubble（可继续 `/api/resume`）。
- 恢复 Declarative UI surface（按钮继续 `/api/ui_action`）。
- 恢复 Plan 卡片与失败决策卡（可继续 confirm / decision / continue）。

---

## Phase 6：Confidence Signal（置信度可视化）

**状态**：已完成。

**核心概念**：Agent 不只给答案，还要表达不确定性；低置信度答案默认是草稿，用户采纳后才进入 Memory。

### 当前实现

- system prompt 要求最终回答末尾追加：`[confidence: 0.0-1.0 | reason: ...]`，reason 应保持简短。
- 后端仅在尾部疑似 confidence marker 前缀时暂存文本并剥离 marker，避免 marker 出现在 `chunk` 正文里，同时保持短答案正常流式输出。
- `_extract_confidence_signal(text, tool_stats, user_input)` 解析模型自评，并叠加工具启发式：
  - 工具失败会下调到低置信度。
  - 实时类问题未发生搜索会下调到低置信度。
  - 无 marker 时默认 `score=0.7, reason="model_unspecified"`。
- 阈值：`low < 0.55`，`medium < 0.8`，其余 `high`。
- 低置信度回答进入 `_ANSWER_DRAFTS`，不进入 Memory / markdown archive / runtime_state。

### SSE 事件

```json
{
  "event": "confidence_signal",
  "data": {
    "score": 0.48,
    "level": "low",
    "reason": "model_unspecified; realtime_without_search",
    "draft": true,
    "draft_id": "draft_0001"
  }
}
```

### HTTP 接口

```http
POST /api/confidence_decision
```

```json
{
  "session_id": "uuid",
  "draft_id": "draft_0001",
  "decision": "accept"
}
```

- `accept`：将草稿 `{user_input, answer}` 写入 Memory。
- `discard`：删除草稿，不写入 Memory。
- draft 不存在返回 404；非法 session_id 返回 400。

### 前端行为

- 每个 AI bubble 底部显示 high / medium / low 置信度 badge。
- 低置信度时显示“采纳 / 丢弃”按钮。
- reason 使用 `textContent` 渲染，不执行 markdown / HTML。

---

## 当前公共接口总览

### HTTP

| Method | Path | 用途 |
|--------|------|------|
| `POST` | `/api/chat` | 主聊天 SSE，body 含 `{session_id, message, context?}` |
| `POST` | `/api/resume` | HITL resume |
| `POST` | `/api/ui_action` | Declarative UI button action |
| `POST` | `/api/plan_confirm` | 确认并执行计划 |
| `POST` | `/api/plan_decision` | 计划步骤失败后的 skip / retry / update |
| `POST` | `/api/plan_continue` | 恢复 running plan 执行 |
| `GET` | `/api/runtime_state` | 恢复 HITL / UI surface / Plan 运行态 |
| `POST` | `/api/confidence_decision` | 采纳或丢弃低置信度草稿 |

### SSE

| 事件 | 用途 |
|------|------|
| `status` / `thinking` / `chunk` / `done` / `error` | 基础流式回答 |
| `search_status` | responses 模式内置 web_search 生命周期 |
| `tool_call` / `tool_result` | chat native tool calling 可见性 |
| `await_user` | HITL / create_plan / plan_decision 中断 |
| `ui_hint` | Phase 2 上下文感知 UI 建议 |
| `component_loading` / `render_component` / `component_error` | Phase 1 Static GenUI |
| `ui_surface_create` / `ui_surface_update` / `ui_data_update` | Phase 3 Declarative GenUI |
| `activity_snapshot` / `activity_delta` | Phase 4 Plan-and-Execute |
| `confidence_signal` | Phase 6 置信度信号与草稿状态 |

---

## 阶段依赖关系

```
Phase 1 (Static GenUI) ─────────→ Phase 3 (Declarative GenUI)
    │                                    │
    │                                    ▼
    │                              Phase 4 (Plan-and-Execute)
    │
Phase 2 (上下文感知) ── 独立，可与 Phase 1 并行或在任意阶段后插入

Phase 5 (Checkpoint/Resume) ← 持久化 Phase 3/4/HITL 运行态
Phase 6 (Confidence Signal) ← 横切 responses/chat 两条最终回答路径
```

关键依赖：
- Phase 3 依赖 Phase 1 的组件渲染基础设施（`COMPONENT_RENDERERS` + SSE 事件处理分支）
- Phase 4 依赖 Phase 1 的 HITL 机制和 chat native tool calling；Plan 卡片是专用 UI，不复用 `DeclarativeRenderer`
- Phase 2 完全独立，只改 request model + system prompt
- Phase 5 依赖 Phase 3/4 的 `_UI_SURFACES` / `_PLANS` / `_PENDING` 运行态集中在 `chat_core`
- Phase 6 依赖最终回答收尾处能拿到累计文本，并通过 marker 前缀暂存剥离 confidence marker

---

## 时间总览与当前状态

| Phase | 核心概念 | 当前状态 |
|-------|----------|----------|
| 1 | tool → structured data → component | 已完成 |
| 2 | context upload → prompt injection → behavior change | 已完成 |
| 3a | model outputs UI JSON → recursive rendering | 已完成 |
| 3b | data binding + action system | 已完成 |
| 4 | collaborative planning + step execution | 已完成 |
| 5 | runtime checkpoint/resume | 已完成 |
| 6 | confidence signal + low-confidence draft | 已完成 |

---

## 技术选型

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 前端框架 | 保持原生 JS | 教学清晰，无构建依赖 |
| 组件系统 | 纯 DOM 操作 + class-based renderer | 学生能看到每个节点怎么创建的 |
| 声明式协议 | 自定义轻量 JSON DSL（A2UI 子集） | A2UI 完整版过重，只取邻接表 + data binding |
| Agent-User 协议 | 保持现有 SSE 事件名 | 教学阶段不增加协议抽象层 |
| 状态同步 | 前端 Map + JSON Pointer + runtime sidecar | UI 层够用，无需引入 jsonpatch 库或数据库 |

---

## Future Directions（不列入主线 Phase）

以下特性各自有独立价值，但超出了"教学 demo"的核心演化线。可作为进阶选做：

### Confidence Signal（置信度可视化）
- 已实现:模型自评 `[confidence: 0.8 | reason: ...]` + 工具调用启发式修正
- 已实现:`confidence_signal` SSE 展示 high/medium/low badge
- 已实现:低置信度回答变为草稿状态,用户通过 `/api/confidence_decision` 采纳后才写入 Memory
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
- 已实现: 使用 `data/runtime_state/{session_id}.json` 持久化 pending HITL、Plan 状态、active UI surfaces/actions
- 页面刷新或服务重启后,前端通过 `GET /api/runtime_state?session_id=...` 恢复待处理卡片并继续操作
- 不改变 Memory / markdown archive 格式,运行态只作为 session sidecar 保存
- 教学点：有状态 Agent 的持久化设计

> Checkpoint/Resume 与 Confidence Signal 已从 Future Directions 提升为 Phase 5 / Phase 6；此处保留它们是为了记录原始 roadmap 的来源。

---

## 每阶段产出物

| Phase | 代码产出 | 学生应能回答 |
|-------|----------|-------------|
| 1 | search_results 卡片 + parser + component SSE 事件 + HITL 卡片视觉 | "Static GenUI 和直接渲染 markdown 有什么区别？" |
| 2 | context 采集 + 自适应 prompt + ui_hint | "Agent 怎么'看到'用户所处的环境？" |
| 3 | DeclarativeRenderer + 6 组件 + render_ui 工具 | "模型输出 UI JSON 和输出 HTML 有什么区别？为什么前者更安全？" |
| 4 | Plan 卡片 + create_plan 工具 + 逐步执行 | "人机协作规划比 Agent 独自执行好在哪？" |
| 5 | runtime_state sidecar + Checkpoint/Resume | "为什么有状态 Agent 需要把中断点和 UI 状态独立持久化？" |
| 6 | confidence_signal + 低置信度草稿确认 | "为什么 AI 需要表达不确定性,以及何时该让用户采纳？" |

---

## 下一步

主线 Phase 1-4 与 Future Direction 中的 Checkpoint/Resume、Confidence Signal 已完成。后续可继续从 Future Directions 中选择 AG-UI 协议对齐、Agent Steering 或多模态感知作为进阶扩展。

> **更新（2026-06-09）**：Phase 7（质量修复 + AG-UI/A2UI 协议对齐）、Phase 8（流式 UI + 表单组件）、Phase 9（Agent Steering + state snapshot/delta）、Phase 10（多模态：图片 / 文本附件 / 语音输入）已全部落地，详见 `docs/roadmap/ROADMAP-智能体感知与生成式UI演化-Phase7-10.md`。

> **更新（2026-06-11）**：Phase 11（跨会话长期记忆）已落地 —— mem0 式原子事实抽取 + LLM 协调（ADD/UPDATE/DELETE）+ 全局 JSON 存储 + query-aware 注入（text-embedding-v4 召回 + qwen3-rerank 精排，本地 JSON 模拟向量库，带优雅降级阶梯）。在 session 隔离 Memory 之上叠加全局长期记忆，归档时后台批量摄入，注入到后续任意会话的 system prompt；`responses` / `chat` 两模式都生效。新模块 `demo/longterm_memory.py`，详见 `docs/changelog/2026-06-11-phase11-cross-session-memory.md`。
