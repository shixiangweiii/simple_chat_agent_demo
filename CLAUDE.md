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
| 业务逻辑 | `chat_core.py` | `Memory` + serialization, `USER_PROMPT` / `TOOLS` / `MAX_ROUNDS` / `TOOL_RESULT_PREVIEW_CHARS`, ReAct primitives (`match_tool_action` / `parse_action_input` / `execute_tool` / `build_prompt`), `react()` (CLI), `stream_agent_response()` (Web, yields abstract `(event_name, payload)` tuples), chat-mode native function-calling ReAct (`_react_chat_native` / `_stream_chat_native` / `_memory_to_messages` / `_get_native_tools[_async]`), session storage (`sessions` / `get_or_load` / `archive_session` / `reset_session` / `delete_session` / `list_sessions` / `read_history` / `get_archive_path_if_exists` / `session_count`), domain exceptions (`InvalidSessionId` / `HistoryNotFound`); re-exports `MODEL` / `API_MODE` so entry points need not import `llm_client` directly | Importing fastapi / starlette, formatting SSE strings, raising HTTPException |
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
3. `llm_client.llm_stream()` runs the blocking OpenAI iterator in `loop.run_in_executor` against the Responses API (`client.responses.create`) with `tools=[{"type": "web_search"}]`, `extra_body={"enable_thinking": True}`, `store=False`, `stream=True`. It dispatches by `chunk.type`: `response.output_text.delta` → `("content", text)`, `response.reasoning_summary_text.delta` → `("thinking", text)`, `response.web_search_call.{in_progress|searching|completed}` → `("search_status", phase)`, `response.completed` with `status in ("failed","incomplete")` → `("error", msg)` (otherwise usage is logged at INFO; usage field names are `input_tokens`/`output_tokens`/`total_tokens` + `output_tokens_details.reasoning_tokens`, **not** `prompt_tokens`/`completion_tokens`). Note: Responses API's reasoning is a **summary**, not the full chain-of-thought, so the thinking panel renders shorter content than the previous Chat Completions integration.
4. **UI layout**: three columns + one overlay.
   - **Left 240px sidebar**: archived sessions list (id-prefix + first user message preview), with new/switch/delete actions. The current session shows in the header, not the sidebar — an un-archived current session won't appear there until archived.
   - **Center main chat**: header (sid + 📋 copy + 📄 preview-archive + 归档/重置), thinking/answer streams, input bar.
   - **Right 200px anchor panel**: TOC of every user message in the current chat. Click → smooth-scroll + 1.2s flash. An `IntersectionObserver` highlights the topmost in-view user message (only the top 40% of the chat viewport counts as "in view" via `rootMargin: '0px 0px -60% 0px'`, to keep the active state stable as the user reads downward).
   - **Archive-preview modal**: fullscreen overlay opened by the header 📄 button. Fetches `GET /api/sessions/{sid}/raw` and renders two tabs (Rendered / Source). The Rendered tab parses the markdown's HTML-comment metadata + `<!-- turn: 用户|AI -->` blocks, runs **user content through `textContent`** and **AI content through `renderMarkdown`** (DOMPurify+marked) — same XSS boundary as the chat view. Empty state shows an "立即归档" CTA that calls `archiveCurrent()` then re-opens the modal.

### LLM API mode switch

`API_MODE` env var (default `responses`) toggles the underlying call protocol **and** the联网搜索实现路径。两套路径都汇聚到 `chat_core.stream_agent_response()` 同一个 SSE 事件契约上,前端零分支。

| API_MODE | LLM 调用 | 联网搜索 | UI 反馈 | 层入口 |
|---|---|---|---|---|
| `responses`(默认) | `client.responses.create` 流式,prompt 是一段字符串 | DashScope Responses API **内置** `web_search` 工具,模型自动调,后端透传 lifecycle | `search_status` SSE banner | `chat_core.stream_agent_response` → `llm_client.llm_stream` |
| `chat` | `client.chat.completions.create` 流式,messages 是结构化数组(支持 tool_calls / role=tool) | OpenAI **native function calling** + DashScope **WebSearch MCP server**(`mcp_web_search.py`),模型显式 `tool_calls`,业务层执行后回喂 | `tool_call` / `tool_result` SSE 事件 | `chat_core.stream_agent_response` → `_stream_chat_native` → `llm_client.llm_stream_chat_with_tools` + `mcp_web_search.call_tool_async` |

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
- `_get_native_tools[_async]()` — 模块级懒加载,首次调用走 `mcp_web_search.discover_tool_spec[_async]()`(列出 MCP server 暴露的所有工具的 OpenAI tools 格式 spec),缓存到 `_NATIVE_TOOLS_CACHE`。
- `_react_chat_native(memory, user_input)` — 同步循环,CLI 用。受 `MAX_ROUNDS` 保护,模型不再返回 tool_calls 即退出并返回 final content。
- `_stream_chat_native(memory, user_input, is_disconnected)` — 异步流式循环,Web 用。逐 chunk yield `("status", ...)` / `("thinking", ...)` / `("chunk", ...)`,流尾若有 tool_calls 则按顺序 yield `("tool_call", ...)` / `("tool_result", ...)` 并把结果以 role=tool 追加进本地 messages,继续下一轮;最终 `("done", {})` 退出。MCP 调用 `mcp_web_search.call_tool_async(...)` 失败返回 `"工具调用失败: ..."` 字符串,**不**抛异常 —— 让模型看到错误并在循环里恢复或转向用户求助。`is_disconnected` 在每个 chunk 之间和每个 tool_call 之间都被 `await`。

**thinking 模式跨轮保留 `reasoning_content`** —— 两路 ReAct 都会在 append assistant 消息时,如果累计到了 `reasoning_content` / `accumulated_reasoning`,把它作为 `assistant.reasoning_content` 字段一并回传给上游。`docs/OpenAI兼容-Chat接口-Function Calling.md:142` 显式要求 kimi-k2.5/k2.6 在 thinking + 多轮 tool_calls 场景下必须保留该字段,否则 400;qwen 系列虽未硬性要求,但同 doc 3700+ 行 DashScope 示例也在采集 —— 透传是兼容性最稳的路线。`reasoning_content` 是 DashScope 扩展字段,不需要时模型会忽略,**不**进 Memory(Memory 仍只存最终 content)。

### Session persistence

Each session is persisted to one markdown file at `data/chat_archive/{session_id}.md`. The directory is created on server startup and is git-ignored.

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

**Security**: `session_id` is used as a filename. All disk operations validate it against `^[0-9a-f-]{36}$` (`chat_core.SESSION_ID_RE`). Invalid IDs raise `chat_core.InvalidSessionId`, which the HTTP layer translates to HTTP 400. Path-traversal payloads (`..%2F..%2Fetc%2Fpasswd`) are rejected by the regex or by FastAPI route mismatch.

### HTTP endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Static `index.html` |
| `GET` | `/api/health` | `{ok, model, sessions}` count |
| `POST` | `/api/chat` | SSE stream (see contract below) |
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
| `done` | `{}` | Normal end of stream. |
| `error` | `{message}` | LLM / tool / parsing error — terminal. |

### Non-goals (don't "fix" these)

- **The string-prompt + manual `Action:`/`Observation:` parsing 在 `responses` 模式与自定义 `TOOLS` 文本协议路径上是 intentional**, not legacy。仍然展示从字符串解析工具调用的 ReAct 教学点。`API_MODE=chat` 下联网搜索改走 OpenAI native `tools=[...]` / `tool_calls` 协议是教学目标的**扩展**,与老路径并存,展示两种工具调用范式的对照(prompt-engineered 文本协议 vs. API-native 结构化协议)。**两条路径共存,不要试图统一,也不要把 chat 模式的 native function calling 回退到字符串协议**。
- **Memory 存储格式仍是一组 flat string** —— `Memory.memories` 永远是 `[{role, msg}, ...]`,序列化到 markdown 也是按 turn 拼出来的字符串。`responses` 模式下整段 Memory 拼成一个 prompt 字符串发给 LLM(`build_prompt` + `Memory.get_all`);`chat` 模式下 `_memory_to_messages` 把同一份 Memory **临时**转成 OpenAI messages 数组,**只为当次 LLM 调用使用**,turn 完成后只把 final assistant content 写回 Memory(`memory.add(USER, user_input)` + `memory.add(AI, content)`),tool_calls / tool_call_id 不进 Memory。这条 non-goal 维持不变 —— 持久化层的"flat string"教学点没改。
- **`index.html` is one file with inline CSS + JS, ~1300 lines** (sidebar + persistence + anchor TOC + archive-preview modal grew it from ~430). Splitting into separate files makes it harder to read end-to-end; keep it one file.

### Implications when modifying

- **Layer discipline**: imports flow downward only (entry → `chat_core` → `llm_client` / `mcp_web_search`). Never let `web_chat_agent` or `common_chat_agent` import `llm_client` or `mcp_web_search` directly (use the `MODEL` / `API_MODE` re-exports on `chat_core`); never let `chat_core` import `fastapi` / `starlette` or raise `HTTPException` (raise `InvalidSessionId` / `HistoryNotFound` and let the HTTP layer translate); never let `llm_client` or `mcp_web_search` import each other or `chat_core`. Business changes (Memory / ReAct / Sessions / Tools) live in `chat_core`; model invocation / chunk parsing / new SSE-event-bearing data extracted from chunks live in `llm_client`; MCP wire protocol / schema 转换 lives in `mcp_web_search`; HTTP routing / SSE serialization (`sse()` + `_sse_stream`) / Pydantic models / domain-exception translation live in `web_chat_agent`.
- **Adding a ReAct (text-protocol) tool**(老路径,`responses` 模式或自定义文本协议工具): append a `{"name": ..., "description": ..., "parameters": {...}}` dict to `TOOLS` in `chat_core.py`, and add a name→implementation branch in `chat_core.execute_tool()`. The prompt template auto-formats `TOOLS` via `json.dumps` and builds the `Action:` line from tool names. Tool names should be distinctive (used in exact line match) but no longer need to be globally unique substrings.
- **Adding a chat-mode native function-calling tool**(新路径,chat 模式):
  - 如果工具来自一个 MCP server,只需扩 `mcp_web_search.py`(把 endpoint 抽参或新建一个 sibling MCP 客户端模块),`_get_native_tools[_async]` 会自动从 schema 发现拿到。`chat_core` / `llm_client` 不动。
  - 如果是非 MCP 的 native 工具(本地 Python 函数 / 第三方 HTTP API),把 OpenAI tools 格式 spec 加进 `_get_native_tools[_async]` 返回值,并在 `_react_chat_native` / `_stream_chat_native` 的 tool 派发表上加一条 `name → executor` 分支(目前所有 tool_calls 都路由到 `mcp_web_search.call_tool_*`,引入第二种 executor 时这里需要改成按 name 派发)。
  - **不**把 chat 模式 native tools 接入 `llm()` / `llm_stream()` 老分发器 —— 接口签名(`messages: list[dict]` vs `prompt: str`)不兼容,会破坏 responses 模式。
- **ReAct max iterations**: `MAX_ROUNDS = 5` in `chat_core.py` applies to both CLI and Web,**两条路径(text-protocol 与 native function-calling)共用同一上限**。Bump if you need longer tool chains.
- **Adding a tool that returns large text**: respect the `TOOL_RESULT_PREVIEW_CHARS` truncation (defined in `chat_core.py`) in the SSE `tool_result` event(`_truncate_tool_result` 在 chat-native 路径用,文本协议路径里 `stream_agent_response` 直接 inline 截断),或 refactor the event contract to carry metadata-only with a separate streaming channel。注意:**喂回模型的 messages 中 `role=tool` 的 content 是 full text**,前端 SSE 才截断 —— 模型需要看完整结果,UI 只需要预览。
- **Frontend XSS surface**: LLM-produced markdown is rendered via `DOMPurify.sanitize(marked.parse(...))` (the `renderMarkdown` helper); user-authored content is rendered via `textContent` only (no markdown parse). The chat view, anchor panel, and archive-preview modal all follow this split. Don't bypass DOMPurify when adding new render paths, and don't promote user input to markdown.
- **`build_prompt` branching on `TOOLS`**: the Responses API `tools=[{"type":"web_search"}]` is sent unconditionally, but Qwen reads the natural-language prompt with higher priority — earlier wording like "（当前无可用工具）" caused the model to refuse built-in web_search even though it was enabled at the API layer. `build_prompt` therefore drops the entire ReAct framing block (`# 工具列表` / `使用如下格式：` / `Action:` / `注意：`) when `TOOLS == []`, leaving only role + conversation + latest input — the model sees no "no tools" wording at all. `USER_PROMPT` separately declares built-in web_search as a positive capability. **`build_prompt` 仅在 `responses` 模式 / 自定义文本协议路径上被调用,chat 模式 native function calling 路径走 `_memory_to_messages` 直接构建结构化 messages,绕开 `build_prompt`。**
- **`enable_thinking` is going away**: docs flag this `extra_body` parameter as deprecated in favor of `reasoning.effort`. Both are still accepted today; we keep `enable_thinking` because the docs don't yet enumerate `reasoning.effort`'s legal values. Migrate when that's clarified —— 三组 LLM 实现(`_llm_*_responses` / `_llm_*_chat` / `_llm_*_chat_with_tools`)需要同步迁移。
- **三组 LLM impls must stay in sync**: any change to the LLM streaming logic (chunk handling, error detection, logging fields, new `(kind, payload)` event types) must land in **all three pairs** of impls:`_llm_responses` / `_llm_stream_responses`、`_llm_chat` / `_llm_stream_chat`、`_llm_chat_with_tools` / `_llm_stream_chat_with_tools`。Otherwise the three `API_MODE` × tool 路径分支 diverge and bug repro depends on which mode the user happened to be in. The dispatchers `llm()` / `llm_stream()` 仍然只是一行 `if API_MODE == "chat"`,新增的 `llm_*_chat_with_tools` 入口**绕开**这两个分发器(签名不兼容)。注意 `_llm_chat_with_tools` 同步实现现在返回 **3-tuple `(content, tool_calls, reasoning_content)`** —— `reasoning_content` 用于 thinking 模式跨轮回传到下一轮 assistant 消息(参见下面"Chat 模式 ReAct 循环细节");异步实现 `_llm_stream_chat_with_tools` 的 `("thinking", text)` 增量事件由上层累计,无需新增协议。
- Comments and prompts are in Chinese; preserve language when editing user-facing strings.

## Skills present in the repo

`.claude/skills/` and `.agents/skills/` contain installed skill bundles (`ata-all`, `ale-file-parser`). These are tooling for Claude Code itself, not part of the demo's runtime.
