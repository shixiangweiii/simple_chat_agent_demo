# ROADMAP 评审 —— 智能体感知与生成式 UI 演化

> 评审对象:`docs/ROADMAP-智能体感知与生成式UI演化.md`(2026-05-26 修订版)
> 评审基线:当前 `demo/chat_core.py` (1016 行) + `demo/static/index.html` (1921 行)
> 评审日期:2026-05-26

---

## 一、总体评价

**结论:这是一份高质量的渐进式演化路线图。** 4 阶段切分清晰、每阶段聚焦一个核心概念、依赖关系明确、与教学定位匹配。Phase 1 后端 + 前端均已落地,与 roadmap 描述高度一致但有 3 处合理偏差(见 §三)。

亮点:

- **"每阶段一个概念"原则贯彻得很彻底**。Phase 2 主动从原本规划的"5 维分类器"砍到"3 信号 + 1 函数",这一压缩判断是对的 —— 教学 demo 不需要工程级感知层,只需要演示数据链路。
- **Phase 3 拆 3a/3b 的颗粒度合理**。声明式 UI 是改动最大的阶段,先做渲染器再做 action + 数据绑定,符合"每阶段可独立运行"原则。
- **架构约束写得详细**。每个 Phase 都明确写了"哪些事件并存 / 不替代""新工具如何路由""为什么是这种实现"。比一般的产品 roadmap 更接近 ADR(架构决策记录)。
- **正确识别了关键风险点**。Phase 1 结尾的"开始前需确认 MCP 返回格式"、Phase 3 中"`render_ui` 引入第三种工具派发类型"、Phase 4 的方案 A vs B 取舍 —— 都是实施前真正会卡住的地方。

---

## 二、Phase 1 现状盘点(已落地)

| Roadmap 要求 | 现状代码位置 | 一致性 |
|---|---|---|
| `TOOL_COMPONENT_MAP` 注册 | `chat_core.py:501` | ✅ 完全一致 |
| `_build_component_props` 数据转换 | `chat_core.py:509-517` | ⚠️ **实现策略不同** |
| `component_loading` SSE 事件 | `chat_core.py:728-732` | ✅ 字段一致 |
| `render_component` SSE 事件 | `chat_core.py:741-745` | ✅ 字段一致 |
| `component_error` SSE 事件 | `chat_core.py:747-751` | ✅ 字段一致 |
| 与 `tool_result` 并存 | `chat_core.py:736 + 741` | ✅ 双发不冲突 |
| 前端 `COMPONENT_RENDERERS` | `index.html:920-934` | ✅ 已注册 |
| 前端事件分支 | `index.html:1431/1443/1457` | ✅ 三事件齐全 |
| 占位符 → 替换机制 | `index.html:1441 (Map) + 1444 (lookup)` | ✅ 按 `tool_call_id` 配对 |
| 未知 `component_type` 降级 | `index.html:1449-1455` | ✅ 兜底渲染 JSON |
| HITL bubble 卡片化 | 未验证 | — 留待 Phase 1 收尾 |

**结论**:Phase 1 后端 + 前端已就绪,可以验收。

---

## 三、Phase 1 与 Roadmap 的 3 处实际偏差(均合理)

### 偏差 1:`_build_component_props` 走 markdown 透传,不走正则解析

Roadmap §Phase 1 推荐用 `_SEARCH_RESULT_RE` 正则把 MCP 文本拆成 `[{title, url, snippet}, ...]` 结构化 JSON,前端按字段渲染卡片;实测代码改成:

```python
# chat_core.py:509-517
def _build_component_props(tool_name, args, result_text):
    if tool_name == "web_search":
        if result_text.startswith("工具调用失败"):
            return None
        query = args.get("query") or args.get("search_query") or ""
        markdown = (
            result_text[:_COMPONENT_MARKDOWN_MAX_CHARS]
            if len(result_text) > _COMPONENT_MARKDOWN_MAX_CHARS
            else result_text
        )
        return {"query": query, "markdown": markdown}
```

前端对应直接 `renderMarkdown(props.markdown)` (`index.html:930`)。

**评审意见:这个偏差是对的,应保留**。理由:

- Roadmap 末尾的注意事项("开始前需确认 MCP 返回格式")明确暗示了这是 fallback 路径
- DashScope WebSearch MCP 返回的文本格式不稳定(标题/摘要的 markdown 结构常变),硬写正则容易频繁 fallback 到原文渲染,反而是 2 套渲染路径维护
- 当前实现已经有 `_COMPONENT_MARKDOWN_MAX_CHARS = 2000` 防 payload 过大,边界处理已覆盖

**建议**:在 roadmap §Phase 1 的"组件类型清单"表后追加一段:

> **关于解析策略的实践更新**:实测 DashScope WebSearch MCP 返回的 markdown 结构不稳定,正则解析失败率高。当前实现选择直接 markdown 透传(`props.markdown` + 前端 `renderMarkdown`),保留卡片样式 + 边框,放弃结构化字段绑定。后续若引入结构稳定的工具(如自建 search API),可在 `_build_component_props` 中按 `tool_name` 分支引入结构化解析。

### 偏差 2:`_PENDING` 是模块级 dict,未持久化

Roadmap §Future "Checkpoint/Resume" 提到了持久化 pending state 的方向,但 Phase 1-4 主线均未要求实现。当前 `chat_core.py:60` 的 `_PENDING: dict[str, dict]` 是进程内字典,重启即丢。

**评审意见:符合教学定位,无需改动**。HITL 中断的演示价值在"流程"而非"持久性",学生看到一次完整的 await → resume 链路即达成目标。如果未来要做 Phase 4 长任务,Plan 状态持久化才会成为硬需求。

### 偏差 3:`_react_chat_native` CLI 短路 HITL

Roadmap 没明写 CLI 模式下 HITL 工具怎么处理,实际代码在 `chat_core.py:572-577` 把 `LOCAL_TOOLS` 中的工具喂回错误字符串让模型换路:

```python
if tc["name"] in LOCAL_TOOLS:
    tool_result = (
        f"[HITL 工具 {tc['name']} 在 CLI 模式下不可用，"
        "请直接以文本方式向用户说明或寻求其他途径]"
    )
```

**评审意见:正确处理,roadmap 应补充说明**。CLI 物理上做不了"等用户点按钮",这个短路是必需的。建议 roadmap §Phase 1 的"HITL 工具的卡片化"段末尾加一行:

> CLI 模式下 LOCAL_TOOLS 走"喂错误字符串"短路(见 `_react_chat_native`),让模型在循环内自行换路。新增 HITL 工具时无需重复实现 —— `if tc["name"] in LOCAL_TOOLS` 已覆盖。

---

## 四、各 Phase 评审

### Phase 2:上下文感知(0.5-1 天)— **强烈推荐先做**

**优点**:
- 砍掉 5 维分类器,只保留 `_compute_adaptive_prompt` 单函数,是非常正确的取舍
- 3 个采集信号(viewport_width / selected_text / session_message_count)都是浏览器侧零成本取得的
- 用 `ui_hint` 事件传指令而非直接改 UI,职责清晰

**风险**:
- `selected_text` 截取 500 字符 — 教学 demo 不涉及隐私合规,但 roadmap 应该提一句"生产环境需考虑此字段是否包含敏感信息"
- `_compute_adaptive_prompt` 返回 `(prompt_addition, ui_hint_mode)` 元组 — 这两件事职责不同(prompt 影响模型,hint 影响前端),建议拆成两个函数或返回 dataclass

**改进建议**:
```python
@dataclass
class ContextAdaptation:
    prompt_addition: str  # 注入 system prompt 的额外指令
    ui_hint: str | None   # 给前端的 UI 模式提示
    reason: str           # 为何如此适应(用于 ui_hint 事件 + 日志)

def _compute_adaptive(context, memory) -> ContextAdaptation:
    ...
```

这样后续扩展第二、第三个适应维度时不会越改越乱。

### Phase 3a:声明式渲染器(3-4 天)— **最大改动,需重点验收**

**优点**:
- 6 个组件覆盖所有基本模式的判断是对的(`text/card/row/column/table/button`)
- "邻接表 + `id: root` + `children` 引用"的设计直接对齐 A2UI(调研报告 §2.3),为未来对齐标准协议留路
- 新增 3 个事件(`ui_surface_create/update`、`ui_data_update`)与现有 `tool_call/result/render_component` 都是"工具产物的不同表征",没有冲突

**风险**:
- **第三种工具派发类型是关键改动**。当前 `_stream_react_rounds:683-757` 是 `if name in LOCAL_TOOLS / else MCP` 二分;引入 `IMMEDIATE_LOCAL_TOOLS` 后,会变成三分支。建议在 roadmap §Phase 3a 加一段强调:
  > 修改 `_stream_react_rounds` 时务必同步更新 `_react_chat_native`(CLI 路径) —— CLI 也会看到 `render_ui` 工具的 schema,需要决定 CLI 模式下是"打印 UI JSON 占位"还是"短路喂回错误"。两条 ReAct 路径必须保持 tool 派发策略一致,否则模型在 chat 模式可调而 CLI 模式不可调,行为分裂。
- **`render_ui` 的 schema 自由度过大**。`components` 数组的 items 只要求 `["id", "type"]`,实际渲染时如果 `type=table` 但没传 `columns`,会渲染出空表格还是报错?建议在 `_execute_render_ui` 里加 schema 校验(用 `jsonschema` 或手写 dict check),失败 yield `("error", ...)` 而非把残缺 JSON 喂给前端
- **`button` 组件的 action 回传**(`POST /api/ui_action`)其实是 Phase 3b 内容,roadmap 3a/3b 边界稍模糊。建议 3a 只实现"button 渲染 + onclick console.log",3b 再加端点

**已发现的小遗漏**:
- Roadmap §Phase 3 没说 `ui_surface_delete` 事件什么时候发。是模型显式调 `delete_ui` 工具?还是 session 结束自动清?需要明确。

### Phase 3b:Action + 数据绑定(2-3 天)

**优点**:
- `/api/ui_action` 复用 `StreamingResponse` 模式与现有 `/api/resume` 对齐,前端流处理可复用
- 增量数据更新(`ui_data_update` + JSON Pointer path)避免组件树全量重渲染,设计正确
- `resolveValue` 的字面量 vs `{path: ...}` 二态判断简洁

**风险**:
- `POST /api/ui_action` 把用户交互回传成 tool result,意味着**用户点击 button = 喂 ReAct 一轮**。如果一个 UI 表面有 3 个按钮、用户依次点了 3 次,会消耗 3 轮 `MAX_ROUNDS = 5`,容易触发"超过最大 ReAct 轮次"。需要决定:button click 是否单独计 ReAct 轮?
- JSON Pointer 解析自己手写还是引入 `python-json-pointer`?Roadmap 写了"无需引入 jsonpatch 库",但 JSON Pointer 与 JSON Patch 不是一个东西,前者就 6 行代码,可以自己写
- 与 HITL 的关系不清:如果一个 UI 表面里有 button 触发 `execute_shell_command`,是走 `/api/ui_action` 还是 `/api/resume`?需要明确边界

### Phase 4:Plan-and-Execute(4-5 天)

**优点**:
- 方案 A(`create_plan` 作 LOCAL_TOOL)优于方案 B 的判断对 —— 显式 tool call 可观测、可日志、可单测
- Plan 编辑卡片是新 HITL `kind: "plan"`,与现有 `input/approval` 体系一致
- 步骤级失败暂停(跳过/重试/修改计划)是 Mixed-Initiative 的核心模式,产品价值高

**风险**:
- **`PlanStep` 和 `ExecutionPlan` 是 dataclass,但 `_PENDING` 是 dict** — 引入两种状态容器后,持久化与序列化代码需要写两遍。建议统一用 dataclass 重写 `_PENDING` 的 state schema(也利好 §三 偏差 2 的未来持久化)
- 方案 A 复用 HITL 中断机制,意味着 plan 的"步骤执行"是 ReAct 后续轮次。如果一个 plan 有 5 步,每步要消耗一轮,直接打满 `MAX_ROUNDS`。需要决定:plan 内的步骤是否单独计轮次?
- "拖拽重排步骤"前端需要引入 sortable.js 或自己写 HTML5 drag/drop,roadmap 没估算这部分前端工时

---

## 五、跨 Phase 系统性建议

### 5.1 `MAX_ROUNDS = 5` 在 Phase 3b/4 后需要重新评估

当前 5 轮足够 web_search + HITL,但:
- Phase 3b:用户点 UI 按钮 = 一轮
- Phase 4:plan 每步 = 一轮

建议 Phase 3b 落地时,把 `MAX_ROUNDS` 从硬编码升级为"按调用语义计数"(LLM 轮 vs 工具交互轮分别计),或简单地把上限提到 15-20。Roadmap 没提这点,需要补充。

### 5.2 SSE 事件契约文档化

CLAUDE.md 里已有"SSE event contract"表格,但每 Phase 都在新增事件。建议:
- 把事件契约从 CLAUDE.md 单独抽出成 `docs/SSE事件契约.md`,每个 Phase 推进时更新
- 每个事件加版本号或"引入于 Phase X"字段,方便回溯

### 5.3 前端单文件 1921 行已经接近阅读上限

CLAUDE.md 明确"~1790 行,不要拆分"(实际现在 1921 行)。Phase 3a 的 `DeclarativeRenderer` 预计 150 行,Phase 3b 的 action handler + JSON Pointer 预计 80 行,Phase 4 的 plan 卡片 + 拖拽 预计 200+ 行。**Phase 4 结束时 index.html 会到 2400+ 行**。

建议:
- 维持 roadmap 的"单文件"原则不变(教学定位不应改)
- 但在 Phase 3a 落地时,在文件开头加一段"目录索引"注释,标注各功能区的行号范围
- 或考虑在 Phase 4 时拆 1 个 `static/declarative_ui.js` 出去,只把声明式渲染器独立(只 ESM import 一次)

### 5.4 调研报告引用未充分

`docs/调研报告-智能体感知与生成式UI.md` 里详细列了 AG-UI 16 种事件 + A2UI 4 种消息。Roadmap §Phase 3 的 `ui_surface_create/update/data_update/delete` 实际是 A2UI v0.9 的子集(`createSurface/updateComponents/updateDataModel/deleteSurface`),但 roadmap 里没明说这个对齐关系。

建议:在 Phase 3a 段落开头加一句:

> 本 Phase 的 SSE 事件设计直接借鉴 A2UI v0.9 协议(参见 [调研报告 §2.3](./调研报告-智能体感知与生成式UI.md))。我们实现的是 A2UI Basic Catalog 的最小子集 —— 6 个组件、扁平 + id 引用、JSON Pointer 数据绑定。这样设计未来可平滑升级为 AG-UI `CUSTOM` 事件承载 A2UI 完整协议。

---

## 六、Roadmap 缺失的关键决策点

下面这些事 Phase 3/4 实施前需要明确,roadmap 当前没覆盖:

1. **`render_ui` / `create_plan` 等本地立即执行工具能否在 CLI 模式工作?**
   - CLI 没有前端,UI 渲染无意义。是短路喂错(同 HITL 工具)还是 ASCII 打印?
   - 建议:统一在 `LOCAL_TOOLS` / `IMMEDIATE_LOCAL_TOOLS` schema 上加 `cli_behavior: "skip" | "print"` 字段

2. **声明式 UI 输出能否进入 Memory?**
   - 当前 Memory 只存 `(role, msg)` 文本 turn。一个 `render_ui` 输出的 UI surface 在归档 markdown 里怎么表示?
   - 选项 A:不存(UI 是临时的,刷新即丢)
   - 选项 B:把 surface JSON 序列化进 markdown 作 turn body
   - 选项 C:存 surface 摘要文本(如"[已展示航班比价表]")
   - 建议:Phase 3a 默认选项 A,Phase 3b 评估

3. **`ui_action` 回传的事件是否同样需要"思考折叠 / 流式 chunk"?**
   - 用户点按钮 → 后端启 SSE 流 → 模型回应。这个流的 UI 反馈应该是新 message 还是更新原 UI surface?
   - 建议:Phase 3b 设计时先画一张交互时序图

4. **Phase 4 plan 与 Phase 1 component 的展示关系**
   - Plan 卡片是 HITL bubble 的 `kind: "plan"` 新分支,还是用 Phase 3 的声明式渲染器渲染?
   - Roadmap §Phase 4 写"plan 卡片用声明式渲染",但又说 `create_plan` 是 LOCAL_TOOL with `kind: "plan"`,这两件事职责重叠
   - 建议:Phase 4 设计前明确 — plan 编辑卡片走 HITL bubble(交互密度高、状态机复杂),plan 执行进度走声明式 UI(纯展示、模板化)

---

## 七、推荐的下一步执行顺序

| 优先级 | 任务 | 工时 | 价值 |
|---|---|---|---|
| P0 | 验收 Phase 1 前端 HITL bubble 卡片化(roadmap 提了但未验证) | 0.5 天 | 收尾 Phase 1,确认主线无遗漏 |
| P0 | 把 roadmap §Phase 1 的 3 处偏差(本文 §三)反写回 roadmap | 0.5 小时 | 文档与代码一致 |
| P1 | 实施 Phase 2(精简版) | 0.5-1 天 | 投入产出比最高,演示数据链路 |
| P1 | 抽 `docs/SSE事件契约.md`,把现有 7 + Phase 1 新增 3 个事件汇总 | 0.5 天 | 给 Phase 3 落地前的设计基线 |
| P2 | 补充 roadmap §六 的 4 个决策点 | 1 天评估 + 文档 | Phase 3 实施前必须明确 |
| P2 | 实施 Phase 3a | 3-4 天 | 主线 |
| P3 | Phase 3b / Phase 4 | 6-8 天 | 主线 |

---

## 八、一句话总结

Roadmap 本身的设计质量很高,**主要工作不是改 roadmap,而是把 Phase 1 已经发生的 3 处偏差反写回文档,以及在 Phase 3 实施前明确 §六 的 4 个决策点。** Phase 2 可以现在就动手,投入产出比最高。
