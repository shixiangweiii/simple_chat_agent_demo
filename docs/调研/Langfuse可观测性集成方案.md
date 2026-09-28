# Langfuse 可观测性集成方案（手动埋点）

> 状态：已实施并端到端验证（2026-09-28）。本文是设计记录 + 二次反思的修订结论 + 验证结果；运行时行为以代码与 CLAUDE.md「Langfuse 可观测性」一节为准。

## 1. 决策（已拍板）

| 决策点 | 结论 |
|---|---|
| 接入方式 | **手动埋点**（`start_observation` + 显式 `.end()`），不用 `langfuse.openai` drop-in |
| HITL / 计划执行 | **续接同一条 trace**（`/api/resume`、`/api/plan_*` 挂回原 trace） |
| generation 输入 | **完整 messages**（含 system prompt / 历史 / 工具结果 / 附件） |
| 图片 | **上传为 Langfuse media**（SDK 自动识别 base64 data URI） |
| 范围 | 三期全做：P1 chat 主链路 + 前端 trace 展示；P2 续接 / 评分 / steer；P3 长期记忆 / responses 模式 / CLI |

## 2. 已验证的前提（均实测）

- 本地服务端 Langfuse **v4.46.0**，运行在 **v4 events_only 模式**：旧 `GET /api/public/traces/{id}` 返回 404，验证数据必须走 `GET /api/public/v2/observations?traceId=...&fields=core,basic,io,usage,model,trace_context,metadata` 与 `GET /api/public/v3/scores?traceId=...`。
- UI 路由仍可用：trace `/project/{pid}/traces/{trace_id}`、会话 `/project/{pid}/sessions/{session_id}`。
- SDK `langfuse 4.15.6`（v4 要求 ≥4.7）；装进现有 `.venv` 只新增依赖，不升级任何已有包。
- `.env` 的 `LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL` 通过 `auth_check()`，项目 `demo`。
- 离线 probe（InMemorySpanExporter）结论：
  - `run_in_executor` 线程里创建的 observation 会变成**孤立 trace**（contextvars 不跨线程）→ 所有 observation 必须在事件循环线程创建；
  - 手动 observation + 显式 parent 在 async generator 跨 yield 下嵌套正确；客户端断连（`aclose()` 来自其他 task）不报错，SDK 已安全吞掉跨 context detach；
  - 未 `.end()` 的 observation 直接丢失 → 必须 `finally` 收尾；
  - root 必须在 `propagate_attributes` 作用域**内**创建，才会带 `session_id`。
- 真实服务端 probe 结论：
  - `trace_context={"trace_id", "parent_span_id"}` 续接后，续接 span 正确挂在原 root 下、同 session；**但它会被标为 root observation，且 `traceName` 默认变成自己的 span 名、tags 为空** → 续接流必须重新 propagate 原 `trace_name` 与 `tags`；
  - base64 图片被替换为 `@@@langfuseMedia:...@@@` 引用（本地实例 blob storage 可用）；
  - `usage_details` 自定义 key（如 `output_reasoning_tokens`）原样保留；`score_trace` 可在 v3 scores API 查到。

## 3. 架构

```
common_chat_agent / web_chat_agent ─→ chat_core ─┬─→ llm_client        （新增 ("usage", dict) 事件 / on_usage 回调，不感知 langfuse）
                                                 ├─→ mcp_web_search    （不感知 langfuse）
                                                 ├─→ longterm_memory ─→ tracing（P3：接收 Obs 句柄）
                                                 └─→ tracing.py        （新：唯一 import langfuse 的叶子模块）
```

- `demo/tracing.py`：叶子模块，不 import 任何项目模块；可被 `chat_core` / `longterm_memory` 使用；入口层只经 `chat_core` 的 re-export（`tracing_public_config` / `tracing_init` / `tracing_shutdown`）接触它，守住分层。
- **降级**：未安装 langfuse / 缺 key / `LANGFUSE_TRACING_ENABLED=false` → 所有 API 返回空对象 `NOOP`，不发 `trace_info`，demo 行为与接入前完全一致。
- **容错**：`Obs` 所有方法吞异常（debug 日志），可观测性永不打断聊天。
- **不阻塞事件循环**：`get_trace_url()` 首次会同步拉 project id，所以改为启动时后台线程解析 project id 并缓存、URL 自己拼；score 走 SDK 后台队列。

### 3.1 `tracing.py` API

| API | 说明 |
|---|---|
| `Obs` / `NOOP` | 空安全包装：`child(name, as_type=..., **kw)`、`event(name, **kw)`、`update(**kw)`、`end(**kw)`（幂等）、`score_trace(...)`、`carrier()`、`trace_id` / `id` / `has_output` |
| `scope(session_id, trace_name, tags, metadata)` | `propagate_attributes` 薄封装（metadata 强转 `dict[str,str]`、≤200 字符） |
| `start_root(name, as_type, input, metadata, carrier, ctx)` | 新建 trace root；`carrier` 非空时用 `trace_context` 续接 |
| `trace_url` / `session_url` / `public_config` | 基于缓存的 project id 拼 UI 链接 |
| `score(trace_id, ...)` | 按 trace id 打分（草稿采纳/丢弃） |
| `usage_details(usage)` | OpenAI / Responses 两种 usage 形态 → Langfuse `usage_details` |
| `init()` / `flush()` / `shutdown()` | 生命周期 |

## 4. Trace 模型

一条 trace = 一次用户动作及其全部续接段；续接段（HITL resume / plan 执行）都挂在**原始 root** 下，互为兄弟。

```
trace  name=chat  session=<会话UUID>  tags=[api_mode:chat, route:chat]
└─ agent chat-turn                       input=用户消息+上下文  output=回答 / 等待用户
   ├─ span       mcp-discover-tools      （仅进程首个请求）
   ├─ retriever  ltm-retrieve            input=query  output=注入片段  metadata.path
   │  ├─ embedding ltm-query-embed
   │  └─ span      ltm-rerank
   ├─ generation llm-round-0             model / 完整 messages / {content, reasoning_content, tool_calls} / usage / TTFT
   ├─ tool       web_search              input=args  output=完整结果（SSE 只给 500 字预览）
   ├─ tool       render_ui / update_ui_data / create_plan
   ├─ event      await_user              （HITL 中断）
   └─ event      steer                   （Phase 9 纠偏生效）
   ├─ agent  hitl-resume:ask_user        ← /api/resume 续接段（同 trace）
   │  ├─ tool       ask_user             input=args+decision  output=喂回模型的 tool result
   │  └─ generation llm-round-1 …
   └─ chain  plan-confirm                ← /api/plan_confirm 续接段（同 trace）
      ├─ chain plan-step-1 … └─ generation / tool …
      └─ chain plan-step-2 …
score  confidence (NUMERIC, comment=reason)   ← Phase 6 置信度
score  draft_accepted (BOOLEAN)               ← /api/confidence_decision
```

| 入口 | trace 策略 | root |
|---|---|---|
| `/api/chat` | 新 trace（name=`chat`） | `agent chat-turn` |
| `/api/ui_action` | 新 trace（name=`ui-action`），metadata 记 surface/component/event + `origin_trace_id` | `agent ui-action` |
| `/api/resume` | 续接 `_PENDING["trace"]` | `agent hitl-resume:<tool>` |
| `/api/plan_confirm` / `plan_decision` / `plan_continue` | 续接 `plan["pending_state"]["trace"]` | `chain plan-confirm` 等 |
| `/api/confidence_decision` | 不开 span，给原 trace 打 `draft_accepted` 分 | — |
| `/api/archive` → 长期记忆摄入 | 独立 trace（name=`ltm-ingest`，同会话 session） | `chain ltm-ingest` |
| CLI 每轮 | 新 trace（name=`cli`，session=`cli-<进程UUID>`） | `agent cli-turn` |

**续接载体（carrier）**：`{"trace_id", "parent_span_id"(=原始 root id), "trace_name", "tags"}`，纯 JSON，存进 `_PENDING[sid]["trace"]` / `plan["pending_state"]["trace"]`，随 runtime_state sidecar 持久化 → 服务重启后仍能续接。

## 5. 埋点清单

| 文件 | 改动 |
|---|---|
| `llm_client.py` | 三个异步 impl 在拿到 usage 时 `yield ("usage", dict)`；同步 impl（`llm` / `llm_chat_with_tools` / `complete` / `embed_texts` 及其 async 包装）加可选 `on_usage` 回调；返回值形态不变 |
| `chat_core.py` | `_traced_turn`（root + `trace_info` 首帧 + 从事件流派生 root output / level / confidence 分 / steer 事件 / 草稿→trace 关联）；`_traced_llm_stream`（generation + 吞掉 `usage`）；`_stream_react_rounds` / `_stream_plan_step_rounds` / `_execute_plan_steps` / `_resume_inner` 加 `obs` 参数与工具、HITL 埋点；carrier 写入 `_PENDING` / plan；responses 路径与 CLI 路径埋点；`schedule_longterm_ingest` 开 `ltm-ingest` trace；`confidence_decision` 打分；re-export 生命周期函数；`_SSE_PROTOCOL_TAGS` 登记 `trace_info → run_started` |
| `longterm_memory.py` | `ingest_async` / `retrieve_injection_async` 及其内部步骤接收可选 `obs`，产出 generation / embedding / span |
| `web_chat_agent.py` | lifespan：启动 `tracing_init()`、退出 `tracing_shutdown()`；`/api/health` 增加 `langfuse` 字段 |
| `common_chat_agent.py` | 每轮打印 trace 链接到 stderr；进程级 CLI session id；退出时 flush |
| `static/index.html` | `trace_info` → AI 气泡底部 trace chip（短 id / 复制 / ↗ Langfuse / 续接标记）；header 增加 Langfuse 会话链接 |
| 文档 | CLAUDE.md（分层、env、SSE、endpoint、新章节）、`docs/调研/SSE事件契约.md`、README 环境变量 |

## 6. SSE：`trace_info`

- payload：`{trace_id, trace_url, session_url, continued, route}`；`trace_url` / `session_url` 在 project id 未解析时为 `null`。
- 仅在追踪启用时发，作为流的**第一帧**（早于 `ui_hint` / `agent_state_snapshot`），保证早期报错也拿得到 id。
- AG-UI 对标 `RUN_STARTED`（threadId=session、runId=trace），登记 `ag_ui_type: run_started`。
- 6 条 SSE 流（chat / ui_action / resume / plan_confirm / plan_decision / plan_continue）都经 `consumeStream`，前端一个分支全覆盖。

## 7. 配置

| Var | 默认 | 说明 |
|---|---|---|
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | 无（缺失 → 追踪关闭） | 项目 API key |
| `LANGFUSE_BASE_URL` | `https://cloud.langfuse.com` | 自托管填 `http://localhost:3000`（SDK 也认 `LANGFUSE_HOST`） |
| `LANGFUSE_TRACING_ENABLED` | `true` | `false` 一键关闭 |
| `LANGFUSE_PROJECT_ID` | 自动解析 | 可选：跳过启动时的 project id 网络查询 |
| `LANGFUSE_TRACING_ENVIRONMENT` / `LANGFUSE_RELEASE` / `LANGFUSE_SAMPLE_RATE` / `LANGFUSE_DEBUG` / `LANGFUSE_MEDIA_UPLOAD_ENABLED` | SDK 默认 | SDK 原生变量，透传生效；采样率建议保持 1.0，否则前端展示的 trace id 可能查不到 |

## 8. 二次反思（对初稿的修订）

| # | 初稿问题 | 修订 |
|---|---|---|
| 1 | 续接段只用 `trace_context` 挂回原 trace | 真实服务端显示续接 span 的 `traceName` 变成自身名、tags 为空 → carrier 额外携带 `trace_name` / `tags`，续接流重新 `propagate_attributes` |
| 2 | 续接段 parent 取"当前父节点"（plan 步骤内 HITL 会挂到步骤 span 下，后续步骤层层嵌套） | carrier 的 `parent_span_id` 固定为**原始 root**（`Obs` 上下文里的 anchor），所有续接段在原 root 下平铺，树更可读 |
| 3 | 新增 `("usage", dict)` 事件，三处消费循环都要显式处理（否则 fallthrough 当正文 → TypeError） | 由 `_traced_llm_stream` 统一吞掉 `usage`，三处消费循环零改动；`llm_client` 文档注明直接消费方必须忽略未知 kind |
| 4 | 断连只处理 `GeneratorExit` | Starlette 断连也可能表现为 `CancelledError`，或靠 `is_disconnected()` 正常 return → 同时捕获两类异常；root 未见 `done` / `error` 即标 WARNING |
| 5 | root output 一律取 chunk 拼接 | plan 流完成时显式写 plan 总结；`Obs.has_output` 让显式 output 优先于拼接兜底 |
| 6 | 草稿采纳分需要 trace id，但 `_finalize_confident_answer` 拿不到 obs | `_traced_turn` 观察到 `confidence_signal.draft_id` 时把 trace id 写进 `_ANSWER_DRAFTS`，`confidence_decision` 据此打分，业务函数零改动 |
| 7 | `get_trace_url()` 首次调用同步网络请求 | 启动时后台线程解析 project id（失败 60s 后惰性重试），URL 自拼，支持 `LANGFUSE_PROJECT_ID` 覆盖 |
| 8 | 验证脚本用旧 traces API | 服务端是 v4 events_only 模式，验证改用 v2 observations + v3 scores API |
| 9 | header 会话链接依赖 `/api/health` 的 project id，启动瞬间可能尚未解析 | `trace_info` 也携带 `session_url`，前端收到后刷新 header 链接 |
| 10 | ui_surface 的 `origin_trace_id` 会在 sidecar 恢复时被丢弃（恢复只保留 components/data/actions） | 恢复逻辑透传 `origin_trace_id`（仅字符串） |
| 11 | `longterm_memory` 是否能依赖 tracing | tracing 是无依赖叶子模块，向下 import 不破坏分层；用 `NOOP` 默认值省掉到处判空 |

### 8.1 实施中新发现（已修复）

| # | 现象 | 根因 | 修复 |
|---|---|---|---|
| 12 | span 正常上报，但 project id 查询超时、score 丢失（SDK 日志 `Unexpected error occurred`） | macOS + Docker Desktop：`localhost` 先解析到 `::1`，httpx 走 IPv6 回环访问 Docker 端口转发会读超时 / 被断开（urllib、裸 socket、IPv4 都正常）；project 查询、score 上报、media 上传都走 SDK 的 httpx client，span 走 OTLP exporter 不受影响。终端里带 `NO_PROXY` 的代理环境能掩盖该问题 | `tracing._loopback_httpx_client`：host 为 `localhost` 时给 SDK 注入绑定 IPv4 的 httpx client；UI 链接仍用配置的 `localhost` |
| 13 | 上游嵌入式错误（`("error", ...)` 事件）时 generation 显示 WARNING「流被关闭」而非 ERROR | 消费方收到 error 事件后提前 `return`，被遗弃的 `_traced_llm_stream` 随后被 aclose（`GeneratorExit`），覆盖了已记录的 ERROR | 流关闭时仅在尚无 level 时记 WARNING（`_traced_llm_stream` / `_traced_turn` 同步修复），并加离线回归用例 |

## 9. 验证结果

1. **静态**：`py_compile` 全部通过；内联 JS `node --check` 通过；无 key 时 `import chat_core` 正常、追踪自动关闭。
2. **离线**（InMemorySpanExporter + 假 LLM / 假 MCP，无网络）：chat 模式 37 项、responses 模式 5 项断言全部通过 —— 覆盖 trace_info 首帧、usage 被吞、generation 完整 messages 快照、工具完整结果、HITL / plan 续接同一 trace 且挂在原始 root 下并沿用 trace name、`is_disconnected()` 与 `aclose()` 两种断连收尾、嵌入式错误保持 ERROR、低置信度草稿 → `draft_accepted` 分、`origin_trace_id` 回链、CLI 两种模式、内置 web_search lifecycle。
3. **端到端**（真实 DashScope + 本地 Langfuse v4.46.0）：普通问答、MCP 联网搜索、ask_user → resume（含**服务重启后** resume 仍挂回原 trace）、create_plan → plan_confirm、render_ui → ui_action、steer、图片（media 实际上传，可下载校验 PNG）、低置信度采纳、归档 → ltm-ingest（extract / reconcile / apply / embed 全链路）、长期记忆检索 `path=inject_all`、CLI（chat 模式）全部通过 v2 observations / v3 scores API 核对；浏览器中核对 trace chip、`续接` 标记与 header 会话链接。
4. **未能在本机实测**：responses 模式真实调用 —— 当前 DashScope key 未绑定 workspace，Responses API 返回 `Missing required parameter: 'workspaceid'`（CLAUDE.md 已记录的环境问题，与本集成无关）；该路径由离线用例覆盖，错误本身也正确记录为 ERROR。
