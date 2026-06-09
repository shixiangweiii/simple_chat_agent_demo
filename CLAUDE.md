# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

A teaching demo of a ReAct (Thought-Action-Observation) chat agent in Python. It deliberately keeps the agent loop, prompt template, and memory store explicit and minimal so each piece can be read top-to-bottom. The tool list is intentionally empty — `TOOLS = []` and `execute_tool` are scaffolding to be extended.

The demo is split into three layers under `demo/` (HTTP / 业务 / LLM 底层) — see "Module layering" below. Two thin entry points share the same business core:
- `demo/common_chat_agent.py` — CLI (stdin/stdout)
- `demo/web_chat_agent.py` + `demo/static/index.html` — FastAPI + SSE web UI

## Environment & commands

- Dependencies in `requirements.txt`. A `.venv` is checked into the working tree but git-ignored.
- Install: `pip install -r requirements.txt`
- API key: both entry points read `DASHSCOPE_API_KEY` from env. Both refuse to start if it's missing.
- Optional override: `DASHSCOPE_API_KEY_MCP` env var for MCP tool calls (only used in `API_MODE=chat`). Falls back to `DASHSCOPE_API_KEY` if not set. Setting a separate key allows LLM and MCP to use different API keys with different permissions/quotas.
- Optional override: `QWEN_MODEL` env var picks the model (defaults to `qwen3.7-max`, required by the Responses API + built-in `web_search` tool — `qwen-plus` will not work).
- Optional override: `API_MODE` env var picks the underlying LLM call protocol — `responses` (default, `client.responses.create`, supports built-in `web_search` lifecycle) or `chat` (`client.chat.completions.create` + native function calling against the DashScope **WebSearch MCP server**, lifecycle visible via `tool_call` / `tool_result` SSE events). Case-insensitive; invalid values raise at module load. See "LLM API mode switch" below.
- The `mcp>=1.10` Python SDK is required for `API_MODE=chat` (it talks streamableHttp to the WebSearch MCP server at `https://dashscope.aliyuncs.com/api/v1/mcps/WebSearch/mcp`). `responses` mode does not need MCP — its web_search is built into the Responses API.
- Run CLI: `export DASHSCOPE_API_KEY=sk-xxx && python demo/common_chat_agent.py`
- Run Web: `export DASHSCOPE_API_KEY=sk-xxx && python demo/web_chat_agent.py`, then open `http://127.0.0.1:8000`.

The LLM endpoint is hardcoded to Alibaba DashScope's OpenAI-compatible gateway (`https://dashscope.aliyuncs.com/compatible-mode/v1`). Changing provider means editing `_build_client()` in `demo/llm_client.py` (the only module that imports the OpenAI SDK).

There is no test suite, linter, or build step configured.

## Architecture

### Module layering

`demo/` is split into three layers, with strict downward-only imports:

```
common_chat_agent.py  (CLI 入口) ─┐                  ┌─→ llm_client.py
                                  ├─→ chat_core.py ──┤
web_chat_agent.py     (HTTP 入口)─┘   (业务逻辑层)    └─→ mcp_web_search.py  (chat 模式才用)
```

| Layer | File | Responsibilities | Forbidden |
|---|---|---|---|
| Entry / HTTP | `common_chat_agent.py`, `web_chat_agent.py` | CLI loop, FastAPI routes, Pydantic models, SSE serialization (`sse()` + `_sse_stream`), domain-exception → HTTPException translation | Direct OpenAI SDK use, ReAct logic, Memory serialization, session disk I/O |
| 业务逻辑 | `chat_core.py` | `Memory` + serialization, `USER_PROMPT` / `TOOLS` / `MAX_ROUNDS` / `TOOL_RESULT_PREVIEW_CHARS`, ReAct primitives (`match_tool_action` / `parse_action_input` / `execute_tool` / `build_prompt`), `react()` (CLI), `stream_agent_response()` (Web, yields abstract `(event_name, payload)` tuples), 上下文感知 (`_compute_adaptive_prompt`), chat-mode native function-calling ReAct (`_react_chat_native` / `_stream_chat_native` / `_stream_react_rounds` / `_memory_to_messages` / `_build_native_tools[_async]`), HITL 基础设施 (`LOCAL_TOOLS` / `_LOCAL_TOOL_KIND` / `_PENDING` / `resume_chat_response` + `_resume_inner`), session storage (`sessions` / `get_or_load` / `archive_session` / `reset_session` / `delete_session` / `list_sessions` / `read_history` / `get_archive_path_if_exists` / `session_count`), domain exceptions (`InvalidSessionId` / `HistoryNotFound` / `PendingNotFound` / `PendingMismatch`); re-exports `MODEL` / `API_MODE` so entry points need not import `llm_client` directly | Importing fastapi / starlette, formatting SSE strings, raising HTTPException |
| LLM 底层 | `llm_client.py` | `MODEL` / `API_MODE` env parsing, `_get_client()` cached singleton, `llm()` (sync, CLI) + `_llm_responses` / `_llm_chat`, `llm_stream()` (async, Web) + `_llm_stream_responses` / `_llm_stream_chat`, `llm_chat_with_tools` / `llm_stream_chat_with_tools` (chat 模式 native function calling 入口) + `_llm_*_chat_with_tools` impls + `_accumulate_tool_call_chunk` / `_finalize_tool_calls` 拼装器, `_extract_output_text` fallback, embedded-error chunk detection, all chunk-type logging | Importing chat_core / fastapi, knowing about Memory / Sessions / SSE / MCP |
| MCP 客户端 | `mcp_web_search.py` | streamableHttp 封装 DashScope WebSearch MCP server: `discover_tool_spec[_async]` (一次性读 schema 并缓存为 OpenAI tools 格式) + `call_tool_async` / `call_tool_sync` (短连接发起一次工具调用,失败返回错误字符串而不抛) | Importing chat_core / llm_client / fastapi, knowing about Memory / SSE / ReAct / OpenAI tool_calls 拼装 |

The abstraction boundaries are:
- `(kind, text)` tuple emission from `llm_client.llm_stream()` — used by `chat_core.stream_agent_response` (responses 模式 / 自定义 TOOLS 文本协议路径).
- `(kind, payload)` tuple emission from `llm_client.llm_stream_chat_with_tools()` — used by `chat_core._stream_chat_native` (chat 模式 native function calling 路径). 与上面同形式但多一个 kind: `("tool_calls", list[dict])`,payload 不是字符串而是结构化对象。
- `(event_name, payload_dict)` tuple emission from `chat_core.stream_agent_response()` — used by the HTTP layer's `_sse_stream` wrapper. **两条 ReAct 路径汇聚到同一组事件类型,前端零分支。**

When adding a feature, decide its layer first. UI behavior → entry layer. ReAct/Memory/Sessions/Tools → `chat_core`. Model invocation / chunk parsing / new SSE-event-bearing data extracted from chunks → `llm_client`. New MCP-backed工具 → `mcp_web_search`(或其同层兄弟新模块). **Never** add a sibling import that skips a layer (e.g., `web_chat_agent` importing `llm_client` directly).

### CLI flow per turn

1. `common_chat_agent.main()` reads a line from stdin and calls `chat_core.react(memory, user_input)`.
2. `chat_core.react()` runs a bounded loop (`for _ in range(MAX_ROUNDS)`, currently 5):
   - `chat_core.build_prompt()` assembles `USER_PROMPT` + tool list + full conversation history (`Memory.get_all()`) + the current `latest_input` into one string by joining sections with `\n`. **The entire history is re-sent on every LLM call** — there is no message-array-style history.
   - `llm_client.llm()` calls DashScope's OpenAI-compat Responses API (`client.responses.create`) with `tools=[{"type": "web_search"}]`, `stream=True`, `extra_body={"enable_thinking": True}`, `store=False`. It only collects `response.output_text.delta` events; `response.completed` is checked for `status` (failed/incomplete → `RuntimeError`) and usage logging. Reasoning summary, web-search status, and other event types are discarded by the CLI.
   - `chat_core.match_tool_action()` matches `^Action:\s*<name>$` on its own line; the name must exactly equal a `TOOLS` entry. `chat_core.parse_action_input()` uses `json.JSONDecoder().raw_decode()` from the first `{` after `Action Input:` — handles multi-line JSON and trailing text.
   - If a tool is matched, `chat_core.execute_tool()` runs it, the LLM output and `Observation: <result>` are appended to `latest_input`, and the loop iterates. Otherwise the response is returned to the user.
3. After return, `main()` appends both the user input and the AI reply to `Memory`.

### Web flow

1. Browser opens `/` → `web_chat_agent.py` serves static `index.html`. The page boots via `init()` which decides `SESSION_ID`: localStorage value if any, else first item from `GET /api/sessions`, else a fresh UUID. session_id is displayed in the header (first 8 chars + hover-for-full + clipboard copy).
2. `POST /api/chat` returns `text/event-stream`. The HTTP layer calls `chat_core.stream_agent_response()` (which mirrors `react()` but yields abstract `(event_name, payload_dict)` tuples) and wraps the stream in `_sse_stream` to format each tuple as an SSE frame via `sse(event, data)`. The handler uses `chat_core.get_or_load(sid)` so even a brand-new server can lazy-load a Memory from disk archive when an existing session_id sends in a chat request.

   **HITL 两段式**(仅 `API_MODE=chat` + HITL 工具触发):当 ReAct 循环在 tool 派发时遇到 `LOCAL_TOOLS` 中的工具(`ask_user` / `execute_shell_command`),业务层不会执行该工具,而是把"未消费的剩余 tool_calls + 当轮 messages + tools + round_num + awaiting 元信息"写入模块级 `_PENDING[session_id]`,yield 一条 `await_user` 事件后立刻 `done` 关流。前端展示 HITL bubble 等用户操作,然后 `POST /api/resume` 启**新流**继续:`resume_chat_response` 弹出 `_PENDING`、按 `decision` 构造 tool result、append `role=tool` 消息,再进入同一个 `_stream_react_rounds`(`pending_remaining` 非空时跳过 LLM 调用直接消费队列),后续轮次行为与 `/api/chat` 完全一致(包括可能再次 await_user 进入第二次中断)。
3. `llm_client.llm_stream()` runs the blocking OpenAI iterator in `loop.run_in_executor` against the Responses API (`client.responses.create`) with `tools=[{"type": "web_search"}]`, `extra_body={"enable_thinking": True}`, `store=False`, `stream=True`. It dispatches by `chunk.type`: `response.output_text.delta` → `("content", text)`, `response.reasoning_summary_text.delta` → `("thinking", text)`, `response.web_search_call.{in_progress|searching|completed}` → `("search_status", phase)`, `response.completed` with `status in ("failed","incomplete")` → `("error", msg)` (otherwise usage is logged at INFO; usage field names are `input_tokens`/`output_tokens`/`total_tokens` + `output_tokens_details.reasoning_tokens`, **not** `prompt_tokens`/`completion_tokens`). Note: Responses API's reasoning is a **summary**, not the full chain-of-thought, so the thinking panel renders shorter content than the previous Chat Completions integration.
4. **UI layout**: three columns + one overlay.
   - **Left 240px sidebar**: archived sessions list (id-prefix + first user message preview), with new/switch/delete actions. The current session shows in the header, not the sidebar — an un-archived current session won't appear there until archived.
   - **Center main chat**: header (sid + 📋 copy + 📄 preview-archive + 归档/重置), thinking/answer streams, input bar.
   - **Right 200px anchor panel**: TOC of every user message in the current chat. Click → smooth-scroll + 1.2s flash. An `IntersectionObserver` highlights the topmost in-view user message (only the top 40% of the chat viewport counts as "in view" via `rootMargin: '0px 0px -60% 0px'`, to keep the active state stable as the user reads downward).
   - **Archive-preview modal**: fullscreen overlay opened by the header 📄 button. Fetches `GET /api/sessions/{sid}/raw` and renders two tabs (Rendered / Source). The Rendered tab parses the markdown's HTML-comment metadata + `<!-- turn: 用户|AI -->` blocks, runs **user content through `textContent`** and **AI content through `renderMarkdown`** (DOMPurify+marked) — same XSS boundary as the chat view. Empty state shows an "立即归档" CTA that calls `archiveCurrent()` then re-opens the modal.

### 上下文感知（Phase 2）

前端每次 `/api/chat` 请求附带 `context` 字段（`viewport_width` / `selected_text` / `session_message_count`）。`stream_agent_response` 调 `_compute_adaptive_prompt(context, memory)` 得到 `(adaptive_fragment, ui_mode)`:
- `adaptive_fragment` 注入 system prompt（`build_prompt` / `_memory_to_messages` 均支持），影响模型行为。
- `ui_mode` 若非 `"chat"` 则在流开头 yield `("ui_hint", {"mode": ...})`，前端据此切换布局（`compact` 折叠历史 / `focus` 提示选中文本）。

管道设计刻意精简（单函数、3 信号、1 维判断），教学目标是演示**数据链路**而非分类器复杂度。

### LLM API mode switch

`API_MODE` env var (default `responses`) toggles the underlying call protocol **and** the联网搜索实现路径。两套路径都汇聚到 `chat_core.stream_agent_response()` 同一个 SSE 事件契约上,前端零分支。

| API_MODE | LLM 调用 | 联网搜索 | UI 反馈 | 层入口 |
|---|---|---|---|---|
| `responses`(默认) | `client.responses.create` 流式,prompt 是一段字符串 | DashScope Responses API **内置** `web_search` 工具,模型自动调,后端透传 lifecycle | `search_status` SSE banner | `chat_core.stream_agent_response` → `llm_client.llm_stream` |
| `chat` | `client.chat.completions.create` 流式,messages 是结构化数组(支持 tool_calls / role=tool) | OpenAI **native function calling** + DashScope **WebSearch MCP server**(`mcp_web_search.py`),模型显式 `tool_calls`,业务层执行后回喂 | `tool_call` / `tool_result` / `await_user` SSE 事件(`await_user` 仅 HITL 工具触发) | `chat_core.stream_agent_response` → `_stream_chat_native` → `_stream_react_rounds` → `llm_client.llm_stream_chat_with_tools` + `mcp_web_search.call_tool_async`(或 HITL 中断写 `_PENDING`) |

`(kind, payload)` tuple emission from `llm_client.llm_stream*()` 是 `llm_client` ↔ `chat_core` 间的唯一抽象边界:

`demo/llm_client.py` 的 LLM 入口现在分两组(共 6 个内部实现 + 4 个对外薄分发):

- **responses 路径**:`_llm_responses(prompt, client)` / `_llm_stream_responses(prompt)` — `client.responses.create(input=prompt, tools=[{"type":"web_search"}], extra_body={"enable_thinking":True}, store=False, stream=True)`. Reads from event-based chunk types (`response.output_text.delta`, `response.reasoning_summary_text.delta`, `response.web_search_call.*`, `response.completed`). Has a fallback path (`_extract_output_text`) for the rare DashScope quirk where the full response sits in `response.completed` instead of streaming as deltas. 上层用 `llm()` / `llm_stream()` 调用。
- **chat 无工具路径(老,目前在 chat 模式下不再被业务层走)**:`_llm_chat(prompt, client)` / `_llm_stream_chat(prompt)` — `client.chat.completions.create(messages=[{"role":"user","content":prompt}], extra_body={"enable_thinking":True, "enable_search":True}, ...)`. Reads from `chunk.choices[0].delta.{content,reasoning_content}`. **保留为参考实现**,展示 `enable_search` 后台搜索与 native function calling 的对比;`react()` / `stream_agent_response()` 在 chat 模式下不再走它(去掉了 `enable_search` 后台搜索,改走 MCP)。
- **chat + native function calling 路径**:`_llm_chat_with_tools(messages, tools, client)` / `_llm_stream_chat_with_tools(messages, tools)` — `client.chat.completions.create(messages=..., tools=..., extra_body={"enable_thinking":True}, stream=True, stream_options={"include_usage":True})`. **不传 `enable_search`** 以避免与 MCP 工具调用双搜。tool_calls 流式增量按 index 用 `_accumulate_tool_call_chunk` 拼接(`function.name` 仅首片携带,`function.arguments` 每片增量),流尾用 `_finalize_tool_calls` 排序成 list 一次性 yield(`("tool_calls", list[dict])`),不逐 chunk yield —— 单 chunk 没有完整的 args 字符串,语义不完整。上层用 `llm_chat_with_tools()` / `llm_stream_chat_with_tools()` 调用,**不**经过 `llm()` / `llm_stream()` 老分发器。

Key behavioral differences:
- **`search_status` SSE events fire only in `responses` mode**(unchanged). chat 模式不发 `search_status`,搜索的可见性改由 `tool_call` / `tool_result` 事件承载 —— 模型每次 `web_search` 都会被前端 tool-strip 显式渲染。
- **Embedded HTTP-200 error chunks** (DashScope's "API key not bound to workspace" quirk) defensive checks 在 responses / chat / chat-with-tools 三组实现里同形保留。
- **Top-level `llm()` / `llm_stream()` 仅做老两路分发**(if API_MODE == "chat" else ...)。新增的 chat-with-tools 入口**不**接入这两个分发器 —— 接口签名(`messages: list[dict]` vs `prompt: str`)不兼容,强行合并会破坏现有 responses 模式的 `prompt: str` 契约。`chat_core.react()` / `stream_agent_response()` 在 `API_MODE=="chat"` 时分发到 `_react_chat_native` / `_stream_chat_native`,这两个再去调 `llm_chat_with_tools` / `llm_stream_chat_with_tools`。

### Chat 模式 ReAct 循环细节

`API_MODE=chat` 下,`chat_core` 增加一组并行的 ReAct 入口:

- `_memory_to_messages(memory, system_prompt, user_input)` — 把 flat-string Memory 转成 OpenAI messages 数组(`Memory.USER → "user"`, `Memory.AI → "assistant"`). **只**在每个 user turn 入口调一次,Memory 存储格式不变。`tool_call_id` 链接只在单个 turn 内活,循环结束只把 final assistant content 写回 Memory。
- `_build_native_tools[_async]()` — 模块级懒加载,首次调用走 `mcp_web_search.discover_tool_spec[_async]()`(列出 MCP server 暴露的所有工具的 OpenAI tools 格式 spec),并加入 `RENDER_UI_TOOL`(Phase 3a 本地立即工具) + `LOCAL_TOOLS.values()`(HITL 伪工具),合并结果缓存到 `_NATIVE_TOOLS_CACHE`。
- `_react_chat_native(memory, user_input)` — 同步循环,CLI 用。受 `MAX_ROUNDS` 保护,模型不再返回 tool_calls 即退出并返回 final content。CLI 物理上做不了 HITL,所以遇到 `LOCAL_TOOLS` 中的工具时**短路**:直接喂回固定错误字符串 `"[HITL 工具 X 在 CLI 模式下不可用,请直接以文本方式向用户说明或寻求其他途径]"`,让模型在循环里恢复(改回纯文本提问 / 放弃 shell)。
- `_stream_chat_native(memory, user_input, is_disconnected, session_id, adaptive_fragment="")` — 异步流式循环的**薄壳**,Web 用。仅做三件事:`_PENDING.pop(session_id, None)` 清旧 pending、`await _build_native_tools_async()` 拿工具集、把所有参数转交给 `_stream_react_rounds(start_round=0, pending_remaining=[])`。`session_id` 参数自 HITL 引入,沿调用链 `stream_agent_response(..., session_id=req.session_id)` 透传下来,responses 模式忽略它。`adaptive_fragment` 由 `_compute_adaptive_prompt` 计算后透传,注入 system prompt。
- `_stream_react_rounds(session_id, memory, user_input, messages, tools, start_round, pending_remaining, is_disconnected)` — 真正的 ReAct 循环主体,**双入口**:fresh start(`start_round=0, pending_remaining=[]`)与 resume(`start_round=断点轮次, pending_remaining=断点未消费的 tool_calls`)。每轮先决:若 `pending_remaining` 非空(只可能是 resume 的第一轮),**跳过 LLM 调用**,直接把它当 `accumulated_tool_calls` 进入派发;否则正常调 LLM,无 tool_calls 即写 Memory + `done` 退出。Tool 派发循环按 name 三分支:若 `tc["name"] in LOCAL_TOOLS`,写 `_PENDING` 并 yield `await_user + done`;若 `tc["name"] in IMMEDIATE_LOCAL_TOOLS`(目前 `render_ui`),本地执行并 yield `ui_surface_*` + `tool_result`;否则调 `mcp_web_search.call_tool_async`,把结果以 role=tool 追加进 messages,继续。MCP 调用失败返回 `"工具调用失败: ..."` 字符串,**不**抛异常。`is_disconnected` 在每个 chunk 之间和每个 tool_call 之间都被 `await`。

**HITL 中断与 resume**:
- `_PENDING: dict[str, dict]` 模块级 pending 表,key 是 session_id,value 含 `user_input` / `messages` / `tools` / `round_num` / `remaining_tool_calls` / `awaiting` 六字段。新 chat 进来时被 `_stream_chat_native` 清掉,resume 时被 `resume_chat_response` 弹出,前端切换会话/重置时只灰化 bubble 不动后端(下次 `/api/chat` 会兜底清理)。
- `LOCAL_TOOLS: dict[str, dict]` 两条伪工具的 OpenAI tools 格式 spec:`ask_user`(参数 `question` + 可选 `options`)和 `execute_shell_command`(参数 `command` + `reason`)。它们通过 `_build_native_tools[_async]` 与 MCP 工具合并后一起发给 LLM,看上去就是 native function calling,**业务层物理上不执行**,而是触发 HITL 中断。
- `_LOCAL_TOOL_KIND: dict[str, str]` 标记每个 LOCAL_TOOL 的前端交互类型:`ask_user → "input"`(等用户文本回答),`execute_shell_command → "approval"`(等同意/拒绝)。`await_user` 事件 payload 会带上 `kind` 字段供前端分支渲染。
- `resume_chat_response(session_id, tool_call_id, decision, answer, is_disconnected)` — sync 外壳,做参数校验和 `_PENDING.pop`,可抛 `InvalidSessionId` / `PendingNotFound`(404)/ `PendingMismatch`(409,且会把 pending 还回去允许重试);返回内层 async generator `_resume_inner`。这是必要的 sync+async split —— async generator 在第一次 `__anext__` 前不会执行 body,无法以"路由级异常"的方式翻译到 HTTPException。
- `_resume_inner(session_id, state, decision, answer, is_disconnected)` — async generator,按 awaiting tool 的 name + decision 构造 tool result(`ask_user → answer` 文本;`execute_shell_command + approve →` **默认** `"[demo stub] 已模拟执行命令: ..."` 字符串,demo 不真执行 shell,教学焦点是 HITL 流程不引入真实 RCE 风险;**`export ALLOW_REAL_SHELL=1` 后**改走 `_execute_shell_real(cmd)`:`asyncio.create_subprocess_shell` 真执行,stderr 合并 stdout,30s 硬超时(`SHELL_EXEC_TIMEOUT_SEC`),输出超 8KB 截断(`SHELL_EXEC_OUTPUT_MAX_CHARS`),命令含 `_SHELL_DENY_PATTERNS`(如 `rm -rf` / `sudo ` / `curl ` / `| sh` / `/etc/passwd` 等)中任一关键词时直接拒绝返回"[拒绝执行] ..."字符串而不发起子进程;超时/spawn 失败/黑名单都不抛,统一转成 tool_result 字符串让模型在循环里恢复;`reject → "用户拒绝执行。理由: ..."`),append role=tool 到 messages,yield 一条 `tool_result`,然后委派给 `_stream_react_rounds`(传入 `remaining_tool_calls` 作为 `pending_remaining`)继续。
- 同轮多 tool_calls:非 HITL 工具正常执行并 yield,直到第一个 HITL 工具触发中断;resume 时先消费"同轮未消费的剩余 tool_calls"(`pending_remaining`),消费完才进入 `round_num+1` 真正调 LLM。**HITL 中断不额外消耗 ReAct 轮次**(`MAX_ROUNDS = 5` 含义不变)。

**thinking 模式跨轮保留 `reasoning_content`** —— 两路 ReAct 都会在 append assistant 消息时,如果累计到了 `reasoning_content` / `accumulated_reasoning`,把它作为 `assistant.reasoning_content` 字段一并回传给上游。`docs/OpenAI兼容-Chat接口-Function Calling.md:142` 显式要求 kimi-k2.5/k2.6 在 thinking + 多轮 tool_calls 场景下必须保留该字段,否则 400;qwen 系列虽未硬性要求,但同 doc 3700+ 行 DashScope 示例也在采集 —— 透传是兼容性最稳的路线。`reasoning_content` 是 DashScope 扩展字段,不需要时模型会忽略,**不**进 Memory(Memory 仍只存最终 content)。HITL 场景下 assistant_msg(含 reasoning_content)在中断前已 append 到 messages,resume 自然通过 `state["messages"]` 复用,无额外处理。

### Session persistence

Each session is persisted to one markdown file at `data/chat_archive/{session_id}.md`. The directory is created on server startup and is git-ignored.

Runtime interaction state is persisted separately to `data/runtime_state/{session_id}.json` (also git-ignored). This sidecar stores resumable HITL pending state, active Declarative UI surfaces/actions, and Plan-and-Execute state. It does **not** change the Memory markdown archive format.

Low-confidence answer drafts from Phase 6 are intentionally **not** persisted to runtime_state. They live in `_ANSWER_DRAFTS` only until the user accepts or discards them.

File layout (HTML-comment metadata header so `GET /api/sessions` only reads the head, no PyYAML dependency):

```markdown
<!-- session_id: 5f8d0c2a-... -->
<!-- updated_at: 2026-05-23T16:30:00+08:00 -->
<!-- turns: 4 -->
<!-- preview: 帮我看看这段代码 -->

<!-- turn: 用户 -->
帮我看看这段代码

<!-- turn: AI -->
当然...
```

Serialization lives on `Memory.to_markdown(session_id)` / `Memory.from_markdown(text)` in `chat_core.py`. Parsing splits on lines that exactly match `^<!-- turn: (用户|AI) -->\s*$` — if a message body itself contains such a line literal it would be mis-split (low-probability acceptable limit).

**Persistence touch points**:

| When | What happens |
|---|---|
| User clicks "归档" | `POST /api/archive` calls `chat_core.archive_session()` which covers-writes the file (atomic via `.tmp` + `os.replace`). Empty Memory is silently skipped. |
| User clicks "新建会话" | Frontend calls `archiveCurrent(silent=true)` first, then generates new UUID. Empty session is skipped server-side, no empty file. |
| User clicks "重置" | `POST /api/reset` calls `chat_core.reset_session()` which drops the session from `sessions` AND deletes the archive file. session_id is preserved. |
| User deletes session in sidebar | `DELETE /api/sessions/{id}` calls `chat_core.delete_session()` which removes the file + drops in-memory session. |
| Page load with stored session_id | `GET /api/history?session_id=...` calls `chat_core.read_history()`; raises `HistoryNotFound` (HTTP 404) if no archive (treated as empty), else returns messages and lazy-loads into `sessions` dict. |
| `/api/chat` for an unknown session_id | `chat_core.get_or_load()` reads from disk if file exists, else creates empty Memory; same lazy-load path. |
| Page load / session switch after Phase 5 | Frontend calls `GET /api/runtime_state?session_id=...` after history load, then rehydrates pending HITL bubbles, UI surfaces, and plan cards from the runtime sidecar. |
| Runtime state mutation | `chat_core` saves `_PENDING` / `_UI_SURFACES` / `_PLANS` to JSON via atomic write; corrupt or incompatible sidecars are logged and ignored. |

**Security**: `session_id` is used as a filename. All disk operations validate it against `^[0-9a-f-]{36}$` (`chat_core.SESSION_ID_RE`). Invalid IDs raise `chat_core.InvalidSessionId`, which the HTTP layer translates to HTTP 400. Path-traversal payloads (`..%2F..%2Fetc%2Fpasswd`) are rejected by the regex or by FastAPI route mismatch.

### HTTP endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Static `index.html` |
| `GET` | `/api/health` | `{ok, model, sessions}` count |
| `GET` | `/api/runtime_state?session_id=...` | Safe runtime UI snapshot for Checkpoint/Resume: pending awaiting info, active surfaces `{surface_id, components, data}`, and plan snapshots. Does not expose backend `messages/tools` recovery internals. |
| `POST` | `/api/chat` | SSE stream (see contract below). Body: `{session_id, message, context?, images?, attachments?}`, context 含 `viewport_width` / `selected_text` / `session_message_count`;**Phase 10a** `images: list[str] | null` 为可选 base64 data URL 列表(≤3 张 / 每张 ≤5MB / 必须 `data:image/*`),仅 `API_MODE=chat` 支持;校验失败 400 `ImagePayloadInvalid`。**Phase 10b** `attachments: list[dict] | null` 为可选文本附件列表(≤5 个 / 单文件硬上限 200KB / 总量硬上限 100KB / mime 严格白名单),仅 `API_MODE=chat` 支持;校验失败 400 `AttachmentPayloadInvalid`。 |
| `POST` | `/api/resume` | HITL resume:body `{session_id, tool_call_id, decision: "answer"\|"approve"\|"reject", answer?}`,启**新**SSE 流继续 ReAct 循环。404 = 该 session 无 pending HITL;409 = `tool_call_id` 与 pending awaiting 不匹配(pending 会被还回去允许重试)。仅 `API_MODE=chat` + HITL 工具触发时才会有 pending 可恢复。 |
| `POST` | `/api/ui_action` | Declarative UI button action:body `{session_id, surface_id, component_id, event_name}`,校验内存态 surface/action 后启**新**SSE 流继续 ReAct。仅 `API_MODE=chat` 可用;404 = surface/action 不存在;409 = event_name mismatch。 |
| `POST` | `/api/plan_confirm` | Plan-and-Execute confirm:body `{session_id, plan_id, steps}`,用户确认/编辑计划后启**新**SSE 流逐步执行。仅 `API_MODE=chat` 可用;404 = plan 不存在;409 = 状态不匹配。 |
| `POST` | `/api/plan_decision` | Plan step failure decision:body `{session_id, plan_id, step_id, decision, steps?}`,支持 `skip` / `retry` / `update` 后继续执行。 |
| `POST` | `/api/confidence_decision` | Confidence draft decision:body `{session_id, draft_id, decision:"accept"\|"discard"}`。`accept` 才把低置信度草稿写入 Memory;`discard` 只删除草稿。 |
| `POST` | `/api/steer` | **Phase 9a** Agent steering:body `{session_id, message}`,把 message 入队到 `_STEER_QUEUES[session_id]`,**立即返回** dict(非 SSE 流);下一轮 ReAct LLM 调用前由 `_stream_react_rounds` 顶部 drain 消费。**不获取** session lock(否则与活跃 SSE 流死锁)。仅 `API_MODE=chat` 可用;400 = 非 chat 模式或 message 为空。 |
| `POST` | `/api/reset` | Clear current session memory + delete archive; session_id preserved |
| `POST` | `/api/archive` | Cover-write current Memory to disk; empty → `{ok, skipped:true}` |
| `GET` | `/api/history?session_id=...` | `{session_id, messages}` or 404 |
| `GET` | `/api/sessions` | List of `{session_id, preview, updated_at, turns}` sorted by `updated_at` desc |
| `DELETE` | `/api/sessions/{session_id}` | Delete archive + drop in-memory session |
| `GET` | `/api/sessions/{session_id}/raw` | Raw archive markdown (text/markdown). 404 if not archived. Backs the frontend archive-preview modal. |

### SSE event contract

All payloads are JSON.

| event | payload | meaning |
|---|---|---|
| `status` | `{phase: "thinking" \| "answering", round}` | Round boundary — `answering` fires on first content delta, useful for collapsing the thinking panel. |
| `thinking` | `{text}` | Incremental reasoning summary token. |
| `chunk` | `{text}` | Incremental answer token. |
| `search_status` | `{phase: "in_progress" \| "searching" \| "completed"}` | Built-in `web_search` lifecycle event(**仅 `responses` 模式发**). May fire multiple times within one round if the model searches more than once. UI shows a transient banner that fades out 1.5s after `completed`. |
| `tool_call` | `{name, args}` | About to invoke a tool. `responses` 模式下当前为空(自定义 `TOOLS` 为空);`chat` 模式 native function calling 路径上,每次模型 `tool_calls` 触发都发(典型即 MCP `web_search`)。 |
| `tool_result` | `{name, result}` | Tool returned. `result` is truncated to `TOOL_RESULT_PREVIEW_CHARS` (500) for UI; full text in server log. `chat` 模式下与 `tool_call` 成对出现,前端 tool-strip 渲染。 |
| `await_user` | `{tool_call_id, name, args, kind: "input"\|"approval"}` | HITL 中断 —— 模型调了 `LOCAL_TOOLS` 中的工具,业务层把状态写入 `_PENDING`,前端按 `kind` 渲染 bubble(input = textarea+提交;approval = 同意/拒绝按钮)。**仅 `chat` 模式 + HITL 工具(`ask_user` / `execute_shell_command`)触发**;紧随其后一定是 `done` 关流,等用户操作触发 `POST /api/resume` 启新流。 |
| `ui_hint` | `{mode: "focus"\|"compact", reason}` | 上下文感知推荐的 UI 模式(Phase 2)。在流开头发(第一帧),前端据此切换布局。`focus` = 选中文本提示;`compact` = 折叠历史。仅 `mode != "chat"` 时发。 |
| `done` | `{}` | Normal end of stream. HITL 中断也会发(关流让前端不再 read)。 |
| `error` | `{message}` | LLM / tool / parsing error — terminal. |
| `component_loading` | `{component_type, tool_call_id, placeholder_text}` | 工具开始执行,前端占位渲染 loading 态。**仅 `chat` 模式 + `TOOL_COMPONENT_MAP` 注册的工具触发。** |
| `render_component` | `{component_type, tool_call_id, props}` | 工具成功,前端按 `component_type` 查 `COMPONENT_RENDERERS` 渲染卡片,替换同 `tool_call_id` 的 loading 占位。`tool_result` 仍并行发,供 debug。 |
| `component_error` | `{component_type, tool_call_id, error_message}` | 工具失败或 props 构建失败,卡片渲染错误态替换 loading 占位。 |
| `ui_surface_create` | `{surface_id}` | Phase 3a 声明式 UI surface 创建。**仅 `chat` 模式 + render_ui 本地立即工具触发**。 |
| `ui_surface_update` | `{surface_id, components}` | 更新 surface 的扁平组件树,前端 `DeclarativeRenderer` 递归渲染。 |
| `ui_data_update` | `{surface_id, path, value}` | 更新 surface 数据对象;Phase 3b 支持 `/` 根替换与 JSON Pointer 深路径写入,前端重渲染对应 surface。 |
| `activity_snapshot` | `{plan_id, title, steps, editable, status}` | Phase 4 计划快照。`editable=true` 时前端渲染可编辑计划卡。 |
| `activity_delta` | `{plan_id, patch}` | Phase 4 计划状态增量,当前使用 `replace` patch 更新步骤状态/摘要/错误。 |
| `confidence_signal` | `{score, level, reason, draft, draft_id?}` | Phase 6 置信度信号。低置信度(`level="low"`)会带 `draft=true` 和 `draft_id`,前端显示采纳/丢弃按钮。 |

### Non-goals (don't "fix" these)

- **The string-prompt + manual `Action:`/`Observation:` parsing 在 `responses` 模式与自定义 `TOOLS` 文本协议路径上是 intentional**, not legacy。仍然展示从字符串解析工具调用的 ReAct 教学点。`API_MODE=chat` 下联网搜索改走 OpenAI native `tools=[...]` / `tool_calls` 协议是教学目标的**扩展**,与老路径并存,展示两种工具调用范式的对照(prompt-engineered 文本协议 vs. API-native 结构化协议)。**两条路径共存,不要试图统一,也不要把 chat 模式的 native function calling 回退到字符串协议**。
- **Memory 存储格式仍是一组 flat string** —— `Memory.memories` 永远是 `[{role, msg}, ...]`,序列化到 markdown 也是按 turn 拼出来的字符串。`responses` 模式下整段 Memory 拼成一个 prompt 字符串发给 LLM(`build_prompt` + `Memory.get_all`);`chat` 模式下 `_memory_to_messages` 把同一份 Memory **临时**转成 OpenAI messages 数组,**只为当次 LLM 调用使用**,turn 完成后只把 final assistant content 写回 Memory(`memory.add(USER, user_input)` + `memory.add(AI, content)`),tool_calls / tool_call_id 不进 Memory。这条 non-goal 维持不变 —— 持久化层的"flat string"教学点没改。
- **`index.html` is one file with inline CSS + JS, ~1790 lines** (sidebar + persistence + anchor TOC + archive-preview modal + HITL bubble 渲染 / consumeStream 抽离 / consumeResumeStream 复用 流读取 + currentHitlBubble 状态管理 + markHitlResolved 副作用)。Splitting into separate files makes it harder to read end-to-end; keep it one file.

### Implications when modifying

- **Layer discipline**: imports flow downward only (entry → `chat_core` → `llm_client` / `mcp_web_search`). Never let `web_chat_agent` or `common_chat_agent` import `llm_client` or `mcp_web_search` directly (use the `MODEL` / `API_MODE` re-exports on `chat_core`); never let `chat_core` import `fastapi` / `starlette` or raise `HTTPException` (raise `InvalidSessionId` / `HistoryNotFound` / `PendingNotFound` / `PendingMismatch` and let the HTTP layer translate to 400/404/404/409); never let `llm_client` or `mcp_web_search` import each other or `chat_core`. Business changes (Memory / ReAct / Sessions / Tools / HITL pending) live in `chat_core`; model invocation / chunk parsing / new SSE-event-bearing data extracted from chunks live in `llm_client`; MCP wire protocol / schema 转换 lives in `mcp_web_search`; HTTP routing / SSE serialization (`sse()` + `_sse_stream`) / Pydantic models / domain-exception translation live in `web_chat_agent`.
- **Adding a ReAct (text-protocol) tool**(老路径,`responses` 模式或自定义文本协议工具): append a `{"name": ..., "description": ..., "parameters": {...}}` dict to `TOOLS` in `chat_core.py`, and add a name→implementation branch in `chat_core.execute_tool()`. The prompt template auto-formats `TOOLS` via `json.dumps` and builds the `Action:` line from tool names. Tool names should be distinctive (used in exact line match) but no longer need to be globally unique substrings.
- **Adding a chat-mode native function-calling tool**(新路径,chat 模式):
  - 如果工具来自一个 MCP server,只需扩 `mcp_web_search.py`(把 endpoint 抽参或新建一个 sibling MCP 客户端模块),`_build_native_tools[_async]` 会自动从 schema 发现拿到。`chat_core` / `llm_client` 不动。
  - 如果是非 MCP 的 native 工具(本地 Python 函数 / 第三方 HTTP API),把 OpenAI tools 格式 spec 加进 `_build_native_tools[_async]` 返回值(目前 MCP 工具 + `LOCAL_TOOLS.values()` 合并),并在 `_react_chat_native` / `_stream_react_rounds` 的 tool 派发表上加一条 `name → executor` 分支(目前非 HITL 的 tool_calls 都路由到 `mcp_web_search.call_tool_*`,引入第二种 executor 时这里需要改成按 name 派发)。
  - **HITL 工具(本地伪工具,前端交互)**:在 `LOCAL_TOOLS` 加 OpenAI tools 格式 spec,在 `_LOCAL_TOOL_KIND` 标 `"input"` 或 `"approval"`,在 `_resume_inner` 加一条按 name 构造 tool result 的分支(如何把用户的 `decision` + `answer` 翻译成给模型看的文本)。如果是 approval 类工具且需要真执行(不像 demo 的 shell stub),在 `approve` 分支里加执行 + 把结果文本作为 tool result;reject 分支保持把拒绝理由原样喂回。前端 `addHitlBubble` 已按 `kind` 分支(input → 输入框,approval → 同意/拒绝按钮)统一渲染,新加 HITL 工具无需改前端 —— 只要复用现有两种 kind。如果需要第三种交互形态(如多选 / 拖拽),才需要前端配合。CLI 短路逻辑(`_react_chat_native` 中的 `if tc["name"] in LOCAL_TOOLS`)自动覆盖新加的 HITL 工具,无需重复。**`execute_shell_command` 例外**:demo 默认 stub 不真执行,`export ALLOW_REAL_SHELL=1` 后 `_resume_inner` 的 approve 分支会改调 `_execute_shell_real` 真执行(30s 超时、stderr 合并 stdout、8KB 截断、`_SHELL_DENY_PATTERNS` 黑名单拒绝)。开关默认关闭以保留教学态、避免 clone 即 RCE;若新增其他需要真执行的 approval 工具,建议沿用同一 env-once-at-import 开关模式而不是另起一个运行时 API。
  - **不**把 chat 模式 native tools 接入 `llm()` / `llm_stream()` 老分发器 —— 接口签名(`messages: list[dict]` vs `prompt: str`)不兼容,会破坏 responses 模式。
- **让新工具结果卡片化(Static GenUI,仅 `chat` 模式生效)**:
  1. 在 `chat_core.TOOL_COMPONENT_MAP` 加 `"tool_name": "component_type"` 映射
  2. 在 `chat_core._build_component_props` 加 `if tool_name == "xxx":` 分支,从 args + result_text 构建 props dict
  3. 在 `index.html` 的 `COMPONENT_RENDERERS` 加同 component_type 的渲染函数
  4. 测试:发请求触发该工具,前端应显示 loading → 卡片(或 error)
  5. 注意:`responses` 模式的内置 web_search 是不透明的(无 `tool_call_id`),卡片事件不会触发——该模式仅有 `search_status` banner
- **Phase 3 声明式 UI (`render_ui` / `update_ui_data`,仅 `chat` 模式生效)**:`RENDER_UI_TOOL` 与 `UPDATE_UI_DATA_TOOL` 不放入 HITL `LOCAL_TOOLS`,而是通过 `IMMEDIATE_LOCAL_TOOLS` 本地立即执行。`render_ui` 参数为 `{surface_id, components, data?}`,其中 `components` 是扁平数组且必须包含 `id="root"`;`button.action` 约定为 `{event_name, context?}`。后端把 surface/action 存进 `_UI_SURFACES`,不进入 Memory / markdown 归档;Phase 5 会把它同步到 runtime sidecar 以支持刷新/重启恢复。前端 `DeclarativeRenderer` 支持 `text/card/row/column/table/button`;所有文本用 `textContent`,不执行 HTML/JS。带 action 的 button 点击 `POST /api/ui_action`,新启 SSE 流继续 ReAct;后端只信任 registry 中保存的 action context。`update_ui_data(surface_id, path, value)` 按 JSON Pointer 更新 data 并发送 `ui_data_update`。CLI 遇到本地立即 UI 工具只喂回 Web-only 文本结果,不渲染 UI。
- **Phase 4 Plan-and-Execute (`create_plan`,仅 `chat` 模式生效)**:`create_plan` 是 HITL `LOCAL_TOOLS` 工具,`_LOCAL_TOOL_KIND["create_plan"]="plan"`。模型调用后后端注册 `_PLANS[session_id][plan_id]`,yield `activity_snapshot` + `await_user(kind="plan")` + `done`;前端展示可编辑计划卡(上移/下移/删除/新增),确认后 `POST /api/plan_confirm`。计划执行使用每步小 ReAct 预算(`PLAN_STEP_MAX_ROUNDS = 3`),通过 `activity_delta` 更新步骤状态;失败时 `await_user(kind="plan_decision")`,用户可 `skip` / `retry` / `update`。计划状态不进入 Memory / markdown 归档,但 Phase 5 会同步到 runtime sidecar;全部完成后只写入一条计划完成摘要。
- **Phase 5 Checkpoint/Resume**:`chat_core._restore_runtime_state(session_id)` 在 chat/resume/ui_action/plan/history/runtime snapshot 入口懒加载 JSON sidecar;`_save_runtime_state(session_id)` 在 pending/surface/plan mutation 后原子写入。前端在 `loadHistory()` 后调用 `loadRuntimeState()` 追加恢复出来的 HITL bubble、Declarative UI surface 和 Plan 卡片。不要把后端私有恢复字段(`messages/tools/remaining_tool_calls`)暴露到 `/api/runtime_state`。
- **Phase 6 Confidence Signal**:system prompt 要求最终回答末尾输出 `[confidence: 0.0-1.0 | reason: ...]`;`chat_core` 用尾部缓冲剥离该 marker,再发送 `confidence_signal`。低置信度阈值是 `<0.55`,中置信度是 `<0.8`。低置信度回答不立即写入 Memory,而是注册 `_ANSWER_DRAFTS`;`POST /api/confidence_decision` 的 `accept` 才写入 Memory,`discard` 不写入。草稿不进入 runtime_state 或 archive。
- **Phase 9 Agent Steering & 状态同步(仅 `chat` 模式生效)**:`_STEER_QUEUES: dict[str, asyncio.Queue]` 与 `_STEER_HISTORY: dict[str, list[dict]]`(上限 `_STEER_HISTORY_MAX=10`)是模块级**进程内** state,**不**持久化到 runtime sidecar(进程退出后未消费的 steer 无意义)。`get_steer_queue(session_id)` 与 `get_session_lock(session_id)` 同纲领(懒创建 accessor),消费侧与生产侧共用以保证 Queue 绑定到唯一事件循环。`POST /api/steer` 路由**不获取** `_SESSION_LOCKS`(否则与活跃 SSE 流死锁);它仅 `put_nowait` 后立即返回 dict(非 SSE 流),下一轮 ReAct LLM 调用前由 `_stream_react_rounds` / `_stream_plan_step_rounds` 顶部 `_drain_steers` 消费,append 为 `{"role":"user","content":"[Steering] ..."}` 到 messages。`_can_append_user_message(messages)` 在 drain 前检查 OpenAI 顺序约束(`assistant.tool_calls` 后必须紧跟 `role=tool`),不满足时 steer 留队列等下轮。steer **不消耗 ReAct 轮次**。新增 SSE 事件 `steer_applied` / `agent_state_snapshot` / `agent_state_delta`:`agent_state_*` 与 `activity_*` 的作用域差异 —— 前者是 **Agent 全局态**纲要(round / tool_stats / surfaces 列表 / plans 列表 / pending / steer_history),后者是 **plan 作用域**详细步骤态;两者各有独立 AG-UI 标签,前端有独立 dispatcher,**不要互相替代**。`agent_state_delta` 是 RFC 6902 JSON Patch,但仅用 `replace` 和 `add(/array/-)` 两种 op;前端 reducer (`agentStateStore.applyPatch`) 也仅支持这两种。`/api/health` 返回 `api_mode` 字段供前端决定是否启用 steer 模式按钮(responses 模式下退化为旧"生成中..."禁用行为)。
- **Phase 10a 多模态感知 — 图片上传(仅 `API_MODE=chat` 生效)**:`ChatRequest.images` 字段承载 base64 data URL 列表,沿调用链 `stream_agent_response → _stream_chat_native → _memory_to_messages` 透传。`_memory_to_messages` 在最后一条 user 节点若 `images` 非空,content 从 str 改为 vision list(`[{type:text}, {type:image_url}*N]`),其余链路零改动:历史 Memory 保持 str(D2)、`Memory.add` 签名不改(D3)、`llm_client._llm_stream_chat_with_tools` 零改动(因为 OpenAI SDK 对 str/list content 都原生支持)。`_validate_images`(总数 ≤ 3 / 每张 ≤ 5MB / 必须 `data:image/*`) 做 `web_chat_agent.py` 的 HTTP 边界校验,对应异常 `ImagePayloadInvalid → 400`。前端 `attachImages(files)` + `renderAttachedImages()` + 三路汇入(📎点击/拖拽/粘贴) + `send()` 携带 + `setStreaming()` 禁用联动。`responses` 模式不支持,`images` 非空时 yield `error` SSE 帧显式提示,不静默丢。CLI 路径零改动。**部署须知**:`export QWEN_MODEL=qwen-vl-max`(或 `qwen3-vl-plus`),否则 LLM 无法处理图片 vision content,会报错回 error SSE 帧。non-goals:不做服务端图片处理/responses 模式适配/图片进 Memory 归档/HITL resume 携带图。
- **Phase 10b 多模态感知 — 文本文件附件(仅 `API_MODE=chat` 生效)**:`ChatRequest.attachments: list[dict] | None` 承载 `{filename, content, mime_type}` 列表,沿调用链 `stream_agent_response → _stream_chat_native → _memory_to_messages` 与 `images` 并行透传。`_memory_to_messages` 在最后一条 user 节点,把 `_build_attachment_block(attachments)` 拼出的 markdown 代码块**追加到 user_input 之后**(刻意不放前面 —— 长附件在前会把短问题挤出模型注意力窗口)。与 `images` 正交可共存。`_validate_attachments`(总数 ≤ 5 / 单文件硬上限 200KB / 总量硬上限 100KB / filename 拒绝路径分隔符+控制字符+Unicode 双向控制 / mime_type 严格白名单 `ALLOWED_ATTACHMENT_MIMES`) 在 `web_chat_agent.py` 做 HTTP 边界校验,对应异常 `AttachmentPayloadInvalid → 400`;**软上限 20KB / 文件**由业务层 `_build_attachment_block` 截断并加 `...(已截断, 原 N 字符)` 尾标,prompt 教模型遇截断要主动提醒用户。语言标识 `_MIME_TO_LANG` 优先 → `_EXT_TO_LANG` fallback。前端 `attachFiles(files)` + `renderAttachedFiles()` + `attachedFiles[]` 数组**与 `attachImages` 并行(不重命名)**,`dispatchSelectedFiles(fileList)` 按 `file.type` 分流到两个独立数组;📎 按钮 / `$fileInput.change` / `drop` 共用 `dispatchSelectedFiles`,`paste` 保持仅图片(文本粘贴走 textarea 即可)。`send()` body 同时携带 `images` 与 `attachments`,user 气泡同时展示图片缩略图条 + 文件 chip 条。`responses` 模式不支持,`attachments` 非空时 yield `error` SSE 帧。HITL pending **不单独存** attachments(附件已烘焙进 `messages`,随 `_PENDING["messages"]` 间接持久化到 runtime_state sidecar,与 images 对称;resume 后模型需看原始附件内容才能继续推理);Memory / markdown 归档**不存** attachments(D2/D4 安全防线,与 images 对称)。USER_PROMPT 增加附件处理指引(第 8 条)。non-goals:不做 PDF/二进制解析、不做附件持久化、不做 HITL resume 携带新附件。
- **Phase 10c 多模态感知 — 语音输入(纯前端,任意 `API_MODE` 都可用)**:`index.html` 输入栏新增 🎤 按钮,基于 `window.SpeechRecognition || window.webkitSpeechRecognition` 检测,无 API 时按钮 `display:none`。`continuous=false / interimResults=true`,interim 文本写入独立 `.voice-interim` DOM 节点(灰色 italic, 不污染 textarea / 不覆盖用户已编辑内容),final 文本**追加**到 textarea 尾部。Safari/Firefox 部分支持:首次 `recognition.start()` 失败时按钮惰性 `display:none` + toast。`isAttachDisabled()` 加入 `recognizing` 互锁,防文件选择对话框打断 onend。后端零改动。non-goals:不做后端 ASR、不做语音指令到工具调用、不做录音文件上传。
- **ReAct max iterations**: `MAX_ROUNDS = 5` in `chat_core.py` applies to both CLI and Web,**两条路径(text-protocol 与 native function-calling)共用同一上限**。Bump if you need longer tool chains.
- **Adding a tool that returns large text**: respect the `TOOL_RESULT_PREVIEW_CHARS` truncation (defined in `chat_core.py`) in the SSE `tool_result` event(`_truncate_tool_result` 在 chat-native 路径用,文本协议路径里 `stream_agent_response` 直接 inline 截断),或 refactor the event contract to carry metadata-only with a separate streaming channel。注意:**喂回模型的 messages 中 `role=tool` 的 content 是 full text**,前端 SSE 才截断 —— 模型需要看完整结果,UI 只需要预览。
- **Frontend XSS surface**: LLM-produced markdown is rendered via `DOMPurify.sanitize(marked.parse(...))` (the `renderMarkdown` helper); user-authored content is rendered via `textContent` only (no markdown parse). The chat view, anchor panel, and archive-preview modal all follow this split. Don't bypass DOMPurify when adding new render paths, and don't promote user input to markdown.
- **`build_prompt` branching on `TOOLS`**: the Responses API `tools=[{"type":"web_search"}]` is sent unconditionally, but Qwen reads the natural-language prompt with higher priority — earlier wording like "（当前无可用工具）" caused the model to refuse built-in web_search even though it was enabled at the API layer. `build_prompt` therefore drops the entire ReAct framing block (`# 工具列表` / `使用如下格式：` / `Action:` / `注意：`) when `TOOLS == []`, leaving only role + conversation + latest input — the model sees no "no tools" wording at all. `USER_PROMPT` separately declares built-in web_search as a positive capability. **`build_prompt` 仅在 `responses` 模式 / 自定义文本协议路径上被调用,chat 模式 native function calling 路径走 `_memory_to_messages` 直接构建结构化 messages,绕开 `build_prompt`。**
- **`enable_thinking` is going away**: docs flag this `extra_body` parameter as deprecated in favor of `reasoning.effort`. Both are still accepted today; we keep `enable_thinking` because the docs don't yet enumerate `reasoning.effort`'s legal values. Migrate when that's clarified —— 三组 LLM 实现(`_llm_*_responses` / `_llm_*_chat` / `_llm_*_chat_with_tools`)需要同步迁移。
- **三组 LLM impls must stay in sync**: any change to the LLM streaming logic (chunk handling, error detection, logging fields, new `(kind, payload)` event types) must land in **all three pairs** of impls:`_llm_responses` / `_llm_stream_responses`、`_llm_chat` / `_llm_stream_chat`、`_llm_chat_with_tools` / `_llm_stream_chat_with_tools`。Otherwise the three `API_MODE` × tool 路径分支 diverge and bug repro depends on which mode the user happened to be in. The dispatchers `llm()` / `llm_stream()` 仍然只是一行 `if API_MODE == "chat"`,新增的 `llm_*_chat_with_tools` 入口**绕开**这两个分发器(签名不兼容)。注意 `_llm_chat_with_tools` 同步实现现在返回 **3-tuple `(content, tool_calls, reasoning_content)`** —— `reasoning_content` 用于 thinking 模式跨轮回传到下一轮 assistant 消息(参见下面"Chat 模式 ReAct 循环细节");异步实现 `_llm_stream_chat_with_tools` 的 `("thinking", text)` 增量事件由上层累计,无需新增协议。
- Comments and prompts are in Chinese; preserve language when editing user-facing strings.

## Skills present in the repo

`.claude/skills/` and `.agents/skills/` contain installed skill bundles (`ata-all`, `ale-file-parser`). These are tooling for Claude Code itself, not part of the demo's runtime.
