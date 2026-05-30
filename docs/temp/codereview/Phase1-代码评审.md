# Phase 1 代码评审 —— Static Generative UI(工具结果卡片化)

> 评审范围:相对 `HEAD` 的未提交 diff
> 评审基线:`demo/chat_core.py` (+48 行) + `demo/static/index.html` (+143 行)
> 评审日期:2026-05-26
> 严重度图例:🔴 Critical / 🟠 Major / 🟡 Minor / 🔵 Nit / ✅ Positive

---

## 总体结论

**质量良好,可合并,但有 2 处需要修改、4 处建议改进、3 处文档需同步。**

- 架构层面正确:层职责未越界,`web_chat_agent.py` 零改动验证了 SSE 抽象边界设计的合理性
- 注册表模式 `TOOL_COMPONENT_MAP` 可扩展性好
- 前端事件处理与 `tool_call_id` 配对清晰
- 主要问题集中在**模式覆盖不完整**(只对 chat-native 路径生效)+ **跨模块的隐式契约**(magic string 错误判断)

---

## 1. 后端 `chat_core.py` 评审

### 1.1 ✅ 优点

**P1. 注册表模式优雅可扩展**(`chat_core.py:501-503`)
```text
TOOL_COMPONENT_MAP: dict[str, str] = {
    "web_search": "search_results",
}
```
- 新增工具卡片化只需加一行映射 + `_build_component_props` 加一个 if 分支
- 派发逻辑无 if/else 蔓延,通过 `.get()` 自然降级到"无组件"路径

**P2. 与 `tool_result` 事件并存而非替代**(chat_core.py:736 + 741)
- 严格遵循 roadmap §Phase 1 "架构约束"段落
- `tool_result` 仍用于 debug/log,`render_component` 是 UI 增强层
- 解耦了"协议层调试可观测性"与"用户层视觉呈现"

**P3. 三态契约清晰**
- 成功:`component_loading` → `render_component`
- 失败:`component_loading` → `component_error`
- 未注册:无事件,降级到原 `tool_result` 行为
- `_build_component_props` 返回 `None` → `component_error`,二态契约干净

**P4. SSE 序列化层零改动**(`web_chat_agent.py` diff = 0)
- 印证了 `_sse_stream` 对 `(event_name, payload)` 元组的通用设计是对的
- 新事件类型自动被 `sse(event_name, payload)` 序列化,无需新增路由代码
- 这是层职责切分得好的客观证据

### 1.2 🟠 Major 问题

**M1. 仅对 `API_MODE=chat` 路径生效,`responses` 模式与 CLI 完全无卡片**

Phase 1 代码只插桩到 `_stream_react_rounds`(`chat_core.py:725-751`),但:
- `stream_agent_response` 的 responses 模式分支(`chat_core.py:949-1014`)**没有**任何组件事件
- `react()` / `_react_chat_native` 的 CLI 路径**没有**任何组件事件

**影响**:
- 默认 `API_MODE=responses` 启动时,用户搜索没有任何卡片渲染
- 现网/教学场景下,新加入的学生可能误以为 Phase 1 没生效
- CLAUDE.md 描述的"两条路径汇聚到同一组事件类型,前端零分支"被打破 —— 前端处理这 3 个事件,但 responses 模式不发,等于多了 3 个"chat 模式独占"事件

**建议**(任选一):
1. **首选**:在 roadmap §Phase 1 顶部明确写"本 Phase 仅覆盖 `API_MODE=chat`,responses 模式的内置 web_search 没有 `tool_call_id` 可挂卡片,需 Phase 1.5 单独处理"
2. responses 模式接入:在 `search_status: completed` 事件上挂"合成 tool_call_id",但需要从 LLM 响应里抓 web_search 结果,改动较大,建议另起一 Phase

**修改位置**:不改代码,改 roadmap 或 CLAUDE.md。

**M2. Magic-string 跨模块耦合**(`chat_core.py:512`)
```text
if result_text.startswith("工具调用失败"):
    return None
```

- 字符串前缀来自 `mcp_web_search.call_tool_async` 的错误返回(参考 CLAUDE.md "MCP 客户端" 段:"失败返回错误字符串而不抛")
- 一旦 `mcp_web_search.py` 改了错误前缀(比如改成 `"MCP 调用失败:"` 或加上工具名),这里的判断**静默失效**,失败的工具仍会进入 `render_component` 路径,前端把"工具调用失败:..."当成 markdown 渲染出来

**建议**(三选一,按推荐度):
1. 在 `mcp_web_search.py` 顶部定义模块常量,`chat_core.py` 导入使用:
   ```python
   # mcp_web_search.py
   ERROR_PREFIX = "工具调用失败"
   # chat_core.py
   from mcp_web_search import ERROR_PREFIX
   if result_text.startswith(ERROR_PREFIX): ...
   ```
2. 让 `mcp_web_search.call_tool_async` 抛专门异常,`_stream_react_rounds` 捕获后直接 yield `component_error`(更符合"失败不混入返回值"的设计原则,但改动跨层,需评估对现有 fallback 路径的影响)
3. 在 `mcp_web_search.py` 暴露一个 `is_error_result(text: str) -> bool` 函数,封装判定逻辑

**推荐选 1**,最小改动同时消除 magic string。

### 1.3 🟡 Minor 问题

**MI1. `_build_component_props` 中两个 query 字段名做 OR 兜底**(`chat_core.py:514`)
```text
query = args.get("query") or args.get("search_query") or ""
```
- 注释里没说明为何要兼容两个名字
- 实际 MCP schema(`mcp_web_search.discover_tool_spec`)只会发一个名字,另一个永远不命中
- 建议:确认实际 schema 字段名后**删掉冗余分支**,或加注释说明"qwen 历史版本两种命名都见过,保留兼容"

**MI2. 截断后无"...(截断)"提示**(`chat_core.py:515`)
```text
markdown = result_text[:_COMPONENT_MARKDOWN_MAX_CHARS] if len(result_text) > _COMPONENT_MARKDOWN_MAX_CHARS else result_text
```
对比 `_truncate_tool_result`(`chat_core.py:489-493`):
```text
return text[:TOOL_RESULT_PREVIEW_CHARS] + f"...（截断，原文 {len(text)} 字符见 server log）"
```
- `_truncate_tool_result` 加了"...(截断)"提示,_build_component_props 没加
- 用户看到的卡片可能在句子中间断开,不知道是否有更多内容
- 建议:统一两个截断函数的行为,或抽出共用 `_truncate_with_notice(text, max_chars)` helper

**MI3. `_COMPONENT_MARKDOWN_MAX_CHARS = 2000` 与 `TOOL_RESULT_PREVIEW_CHARS = 500` 关系未说明**
- 两个常量都是"防止 payload 过大"的截断阈值,但相差 4 倍
- 没注释解释为何卡片用 2000、tool_result preview 用 500
- 真实差异是:`tool_result` 是 debug 日志预览(短即可),`component` 是用户主要消费媒介(需更长)。建议加一行注释说明

**MI4. 静默工具注册不匹配**
- 如果未来加 `TOOL_COMPONENT_MAP["foo_tool"] = "foo_card"` 但忘记在 `_build_component_props` 加 `if tool_name == "foo_tool"` 分支
- 运行时会:yield `component_loading` → `_build_component_props` 返回 None → yield `component_error("工具执行失败，无法渲染卡片")`
- 错误信息**误导**(工具执行其实成功了,只是 props 没构建),没有 logger.warning

**建议**:在 `_build_component_props` 末尾加:
```text
def _build_component_props(tool_name, args, result_text):
    if tool_name == "web_search":
        ...
    # 注册了 component_type 但没有 props 构建器
    if tool_name in TOOL_COMPONENT_MAP:
        logger.warning(
            "TOOL_COMPONENT_MAP 注册了 %s 但 _build_component_props 无对应分支,卡片将渲染为 component_error",
            tool_name,
        )
    return None
```

**MI5. `component_error` 错误消息硬编码,丢失上下文**(`chat_core.py:750`)
```text
yield ("component_error", {
    "component_type": component_type,
    "tool_call_id": tc["id"],
    "error_message": "工具执行失败，无法渲染卡片",
})
```
- 实际错误可能是 MCP 超时、网络断开、schema 不匹配,都被压成同一句话
- `tool_result` 事件里已经发了真实错误(`"工具调用失败:..."`),但 `component_error` 没复用
- 建议:把 `result_text` 摘要传过来:
  ```python
  error_message=f"工具执行失败:{result_text[:80]}",
  ```

### 1.4 🔵 Nit

**N1. `chat_core.py:515` 三元表达式过长**,可读性差。前面 MI2 建议抽 helper 时一并修复。

---

## 2. 前端 `index.html` 评审

### 2.1 ✅ 优点

**P5. `consumeStream` 共用机制让 resume 自动支持组件**
- `consumeStream` 是 `send()` 和 `consumeResumeStream()` 的公共流处理函数(index.html:1400)
- 新加的 3 个事件分支自动在 resume 流里也生效
- 不需要在两处分别加代码 —— 这是之前抽 `consumeStream` 的红利

**P6. XSS 边界守得住**(index.html:920-934)
- `props.query` 走 `escapeHtml`(index.html:926)
- `props.markdown` 走 `renderMarkdown`(DOMPurify+marked,index.html:930)
- 与 CLAUDE.md "Frontend XSS surface" 段落要求的"LLM-produced 走 sanitize,user-authored 走 textContent"一致
- 注意:这里 `props.query` 是用户的搜索词(user-authored),用 `escapeHtml` 是对的

**P7. 未知 `component_type` 优雅降级**(index.html:1449-1455)
- 前端没注册的组件类型不会崩溃,而是把 props JSON 渲染成 `<pre>` 块
- 利好后端单独发布新事件类型时的前后端版本错位场景

### 2.2 🟠 Major 问题

**M3. `component_loading` 后流中断 → 永久转圈**(index.html:1431-1442)

场景:
1. 后端 yield `component_loading`
2. MCP 调用超时 / 服务进程崩溃 / 网络断开
3. 前端永远收不到 `render_component` 或 `component_error`
4. CSS `pulse-bg` + `comp-spin` 动画**永久**运行

**复现路径**:
- 启动 web,发个搜索 query
- 后端 MCP 调用过程中 `Ctrl+C` 杀进程
- 前端的 loading 状态保持转圈,无任何错误提示

**建议**:
- 在 `consumeStream` 的流结束 / 错误分支,清扫所有未 resolve 的 `componentSlots`:
  ```javascript
  // 在 'done' 或 'error' 分支末尾
  for (const [tcId, slot] of ctx.componentSlots) {
    if (slot.querySelector('.component-loading')) {
      slot.innerHTML = '';
      slot.appendChild(renderComponentError('流意外结束,组件未完成渲染'));
    }
  }
  ```
- 或在 `try/catch/finally` 的 finally 块统一清扫
- 给 loading 加 max-age timeout(如 30 秒),超时转 error 状态

**严重度判定为 Major** 而非 Critical,因为只影响**异常场景**且不阻塞主功能;但 UX 上是 dead-end,值得修。

### 2.3 🟡 Minor 问题

**MI6. `componentSlots` 生命周期假设未文档化**(index.html:1346)
```text
componentSlots: new Map(),  // tool_call_id → component-slot DOM 元素
```
- `consumeResumeStream` 每次启新流时 `createAiStreamContext()`,生成**新的** `ctx.componentSlots`(index.html:1667)
- HITL 触发**在工具执行之前**,所以原 ctx 不会有挂起的 component_loading slot
- 但 roadmap Phase 4 引入"先执行 N 个工具再 HITL"模式后,这个假设会被打破
- 建议加注释:
  ```text
  componentSlots: new Map(),  // tool_call_id → component-slot DOM 元素
                              // 注意:resume 流会新建 ctx,旧 ctx 的 slots 不可达。
                              // 当前 HITL 在工具执行前触发,无遗留 slot 问题。
  ```

**MI7. `slot.innerHTML = ''` 销毁子节点未触发 cleanup**(index.html:1447 / 1450 / 1460)
- `render_component` / `component_error` 通过 `innerHTML = ''` 清空 loading 占位符
- 当前 loading 元素是纯静态 DOM,无事件监听器,无副作用 —— 安全
- 但未来如果 loading 加了倒计时 / WebSocket / 其他副作用,`innerHTML = ''` 不会触发清理
- 建议:抽 `clearSlot(slot)` helper 统一管理,未来加副作用时只改一处

**MI8. tool-strip 与 component slot 视觉脱节**(index.html:1439)
```text
ctx.$toolStrip.parentNode.insertBefore(slot, ctx.$answer);
```
- slot 插在 `$answer` 前,$toolStrip 在 slot 前
- 视觉顺序:thinking → tool-strip(`🛠 调用 web_search` + `✓ 工具 web_search 已返回结果` 两行)→ 卡片 → 答复
- tool-strip 里的工具调用线和下方的卡片在视觉上是"两个独立元素",用户可能不知道它们对应同一次 web_search
- 建议(留作 Phase 1 收尾打磨,不阻塞合并):
  - 给 tool-strip 与 component-slot 加视觉连接线 / 共同容器
  - 或在 tool-strip 里写"↓ 见下方搜索卡片"
  - 或干脆把对应的 tool_call/result 行折叠进卡片头部

**MI9. CSS 重复**(index.html:418-425 + index.html:751-758)
- `.search-status::before` 和 `.component-loading::before` 两段 spinner CSS 高度相似(都是 12-14px 圆 + `comp-spin` 动画 + 蓝色)
- 建议抽共用 class `.spinner-circle`,或保留两份但在两段 CSS 顶部加注释指明"另一处也用同款 spinner"

### 2.4 🔵 Nit

**N2. `header.innerHTML = '\u{1F50D} <span>' + escapeHtml(...) + '</span>'`**(index.html:926)
- innerHTML 拼接稍嫌啰嗦,可以:
  ```javascript
  const emoji = document.createTextNode('🔍 ');
  const span = document.createElement('span');
  span.textContent = props.query || '';
  header.appendChild(emoji);
  header.appendChild(span);
  ```
- 不是 bug,纯风格

**N3. `style.cssText = 'font-size:11px;...'` 内联样式**(index.html:1452)
- 降级渲染 JSON 块用内联 style,不一致(其他都用 class)
- 建议挪到 `<style>` 里加 `.component-fallback-json` class

---

## 3. 跨层 / 文档评审

### 3.1 🟠 Major:文档同步缺失

**D1. CLAUDE.md SSE 事件契约表未更新**(CLAUDE.md "SSE event contract" 段落)

当前表只列 7 个事件,Phase 1 新增的 `component_loading` / `render_component` / `component_error` 没收录。这是 CLAUDE.md 自己声明的"权威契约文档",代码与文档背离会让后来者(尤其 LLM coding agent)做错决策。

**建议**:在 CLAUDE.md 表后追加 3 行:
```markdown
| `component_loading` | `{component_type, tool_call_id, placeholder_text}` | 工具开始执行,前端占位渲染 loading 状态。仅 `chat` 模式 + 注册在 `TOOL_COMPONENT_MAP` 的工具触发。 |
| `render_component` | `{component_type, tool_call_id, props}` | 工具成功,前端按 `component_type` 查 `COMPONENT_RENDERERS` 渲染卡片,替换同 `tool_call_id` 的占位符。`tool_result` 仍并行发,供 debug。 |
| `component_error` | `{component_type, tool_call_id, error_message}` | 工具失败,卡片渲染错误态。 |
```

**D2. CLAUDE.md "Implications when modifying" 段没说"加新工具时如何注册组件"**

当前文档说了 "Adding a chat-mode native function-calling tool" 的步骤,但没说"如果要让这个工具的结果以卡片形式展示,需要额外做什么"。

**建议**:追加一节:
```markdown
- **让新工具结果卡片化(Static GenUI)**:
  1. 在 `TOOL_COMPONENT_MAP` 加 `tool_name → component_type` 映射
  2. 在 `_build_component_props` 加 `if tool_name == "xxx":` 分支,返回 props dict
  3. 在 `index.html` 的 `COMPONENT_RENDERERS` 加同 component_type 的渲染函数
  4. 测试:发请求触发该工具,确认前端有卡片(而非只有 tool_result 文本)
```

**D3. Roadmap 与实现不一致**(详见 `docs/temp/ROADMAP-评审.md` §三 偏差 1)
- Roadmap 写的是正则解析 `[title](url)+snippet`
- 实现是 markdown 直接透传
- 已在 ROADMAP-评审.md 提议反写回 roadmap

### 3.2 🟡 Minor

**D4. 无单元测试**
- `_build_component_props` 是纯函数,易于单测:
  - `web_search` + 正常文本 → 返回 `{query, markdown}` dict
  - `web_search` + 错误文本 → 返回 None
  - `web_search` + 超长文本 → markdown 字段被截断到 2000 字
  - `web_search` + 缺 query 字段 → 返回 dict with `query=""`
  - 未知工具名 → 返回 None
- 项目当前没有测试基础设施(CLAUDE.md 明确说 "no test suite"),与项目惯例一致
- 不强求加,但 Phase 3 引入 `_execute_render_ui` 时会有更复杂的纯函数,届时建议同步建立 `pytest` 框架

---

## 4. 总结清单与处置建议

| 问题 | 严重度 | 建议 |
|---|---|---|
| M1 仅 chat 模式生效,responses 无卡片 | 🟠 | **必改文档**(roadmap 或 CLAUDE.md 显式标注限制),代码暂不动 |
| M2 magic-string 跨模块耦合 | 🟠 | **改代码**:`mcp_web_search.ERROR_PREFIX` 常量化 |
| M3 loading 流意外中断 → 永久转圈 | 🟠 | **改代码**:`consumeStream` finally 块清扫未 resolve slot |
| D1 CLAUDE.md SSE 事件表未更新 | 🟠 | **必改文档** |
| MI1 query 字段名 OR 兜底无注释 | 🟡 | 看实际 schema,要么删冗余要么加注释 |
| MI2 截断无"...(截断)"提示 | 🟡 | 抽共用 helper 统一行为 |
| MI3 两个截断常量关系无说明 | 🟡 | 加注释 |
| MI4 静默工具注册不匹配 | 🟡 | 加 logger.warning 兜底 |
| MI5 `component_error` 错误消息丢上下文 | 🟡 | 把 result_text 摘要带出 |
| MI6 `componentSlots` 生命周期假设无注释 | 🟡 | 加注释 |
| MI7 `slot.innerHTML = ''` 未来副作用风险 | 🟡 | 抽 `clearSlot` helper |
| MI8 tool-strip 与卡片视觉脱节 | 🟡 | UX 打磨,可下一轮再做 |
| MI9 spinner CSS 重复 | 🟡 | 抽共用 class |
| D2 CLAUDE.md 缺"加工具卡片化"步骤 | 🟡 | 加 4 步说明 |
| D3 roadmap 与实现不一致 | 🟡 | 已在 ROADMAP-评审.md 提议 |
| D4 无单测 | 🟡 | 暂保持项目无测试惯例 |
| N1/N2/N3 风格 nit | 🔵 | 不强求 |

### 4.1 推荐合并前必改 3 项

1. **M2** —— 消除 magic string,新加常量
2. **M3** —— loading 永久转圈兜底
3. **D1** —— CLAUDE.md SSE 表更新

### 4.2 推荐合并后立即跟进 2 项

1. **M1** —— 明确"chat 模式独占"的文档标注
2. **MI4** —— 静默不匹配的 warning 日志

### 4.3 可在 Phase 2/3 启动前再处理

- MI1/MI2/MI3/MI5/MI6/MI7/MI8/MI9/D2/D4
- 风格 nit 全部

---

## 5. 一句话总结

**Phase 1 是质量良好的功能落地,可以合并。3 个 Major 问题(magic string 耦合 / loading 永久转圈 / 文档背离)建议合并前修;其余 minor 与 nit 可在后续 Phase 滚动迭代。架构层面的"层职责未越界 + 注册表模式 + 共用 consumeStream"是这次改动最值得保留的设计资产。**
