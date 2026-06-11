# 2026-06-11 — Phase 11:跨会话长期记忆（Cross-Session Long-Term Memory）

在"每个 session 隔离的对话 Memory"之上,叠加一层 **mem0 式的全局长期记忆**:从每个会话归档时
批量抽取"关于用户的原子事实",经 LLM 协调(ADD/UPDATE/DELETE)并入全局记忆库,在后续任意会话的
system prompt 里注入,让 Agent 跨会话"记得用户"。**不改动 session 隔离的基本面**(Memory 仍是
flat-string,归档格式不变)。

## 设计决策(已锁定)

- 身份:**单一全局用户**(无 user_id),一份全局文件。
- 抽取时机:**会话归档时批量**(非每轮),增量水位线去重 + 后台异步(不阻塞归档响应)。
- 抽什么:**原子事实**(偏好/身份/项目/事实),实体是其子集。
- 更新:**LLM 协调** ADD/UPDATE/DELETE/NOOP(展示"记忆会更新/纠错")。
- 抽取/协调 = **两次** LLM 调用(可观测,各自单一职责)。
- 检索:text-embedding-v4 召回 + qwen3-rerank 精排,**不引第三方向量库**,本地 JSON 模拟。

## 两个 milestone

### Milestone A — 抽取 + 协调 + 存储 + 注入(无检索)
- `demo/longterm_memory.py`(**新建**):`extract_facts_async` → `reconcile_async`(校验幻觉 id)→
  `apply_ops` → 原子写 `data/longterm_memory.json`;`ingest_async`(水位线 + `_LTM_LOCK` + 异常隔离);
  `injection_fragment()`(同步全量,CLI/兜底用)。
- `demo/llm_client.py`:`complete[_async]` —— 无工具一次性补全(**不带 web_search/enable_search**,
  避免抽取误联网;始终走 chat.completions,与 `API_MODE` 解耦)。
- `demo/chat_core.py`:注入点(`stream_agent_response` + `react` + `_react_chat_native`,合并进
  现有 `adaptive_fragment` 通道,零下游签名改动)+ 3 薄 re-export(`schedule_longterm_ingest` /
  `longterm_snapshot` / `delete_longterm_fact`)。
- `demo/web_chat_agent.py`:`/api/archive` 后台挂 `schedule_longterm_ingest`;新增
  `GET /api/longterm_memory`、`DELETE /api/longterm_memory/{fact_id}`。
- `demo/static/index.html`:header「🧠 记忆」按钮 + 模态面板(复用 `modal-overlay` 模式),
  按 category 分组、带删除、`textContent` 渲染(XSS 边界),归档后延迟刷新。

### Milestone B — 检索层(embedding 召回 + rerank 精排)
- `demo/llm_client.py`:`embed_texts[_async]`(OpenAI 兼容 `embeddings.create`,batch ≤10)、
  `rerank[_async]`(裸 httpx POST `compatible-api/v1/reranks`,qwen3-rerank,失败返回 None)。
- `demo/longterm_memory.py`:`data/longterm_vectors.json` 模拟向量库;`apply_ops` 后 `_sync_vectors`
  (ADD/UPDATE 嵌入、DELETE 删向量,best-effort);`retrieve_injection_async(query)` ——
  事实 ≤ `INJECT_ALL_MAX` 全量注入,否则 cosine 召回 top-N → rerank 精排 top-K。
- `demo/chat_core.py`:`stream_agent_response` 改 `await retrieve_injection_async(user_input)`;
  CLI `react` 保持同步全量。

## 优雅降级阶梯(检索绝不阻断聊天)
rerank 失败 → cosine 序前 K;query embedding 失败 → 注入最近更新 K 条;向量缺失/头不匹配 →
全量注入截断;任何异常 → 最近事实兜底。`_retrieve` / `retrieve_injection_async` 全程不抛。

## 数据流
```
归档 → archive_session()写md → schedule_longterm_ingest(sid)  // 快照turns后create_task,立即返回
  └ ingest_async [_LTM_LOCK]: watermark增量 → extract → reconcile → apply → 嵌入写vectors → 推进watermark
下次fresh turn → retrieve_injection_async(query) → 合并adaptive_fragment进system prompt
  （resume/plan流复用已烘焙system message,无需重注入）
```

## 配置(env 覆盖)
`LTM_MIN_TURNS=2`、`LTM_MAX_FACTS=200`、`LTM_MAX_INJECT_CHARS=2000`、`LTM_INJECT_ALL_MAX=12`、
`LTM_RECALL_TOP_N=20`、`LTM_RERANK_TOP_K=6`、`QWEN_EMBED_MODEL=text-embedding-v4`、
`QWEN_EMBED_DIM=512`、`QWEN_RERANK_MODEL=qwen3-rerank`。

## 关键不变式 / Non-goals
- **API 无关**:抽取走无工具补全、注入走 system prompt 片段 → `responses` / `chat` 两模式都生效
  (与 Phase 3–10 全 chat-only 对照,教学加分)。
- 分层:`chat_core → longterm_memory → llm_client`;`longterm_memory` 不 import chat_core(无环);
  web 只依赖 chat_core re-export;embedding/rerank/complete 作为"模型调用"归 `llm_client`。
- 并发:全局 `_LTM_LOCK` 串行 facts+vectors 读改写;多会话并发归档经 create_task + 水位线排队/去重。
- Memory / 归档 md / runtime_state sidecar 格式**不变**;事实库是独立 store。
- 不做:per-user 多用户、实时逐轮摄入、CLI 接检索、reconciliation 接检索(只喂全部事实)、
  会话删除回收其事实、图片多模态 rerank、第三方向量库。

## 验证(摘要,详见 plan)
- 静态:四文件 AST OK;`import` 无循环;`longterm_memory` 纯函数单测(parse/apply_ops/cosine/
  id 幻觉拒绝/format)全通过。
- 端到端(需实 key):说偏好 → 归档 → 面板出现事实 → 新会话注入生效;改偏好 → 归档 → UPDATE 而非
  新增矛盾条;连点归档第二次 NOOP;事实 > 阈值触发 embed→recall→rerank;rerank 置非法值仍不中断。

## 评审反馈与修订(2026-06-11 复审)

二次自评发现 1 真实 bug + 若干权衡项,逐条处置如下:

| 编号 | 问题 | 处置 | 落地 |
|---|---|---|---|
| **H1** | 后台摄入 `create_task` 结果未被引用 → asyncio 弱引用可中途 GC 掉,归档静默不沉淀 | ✅ 改代码 | `chat_core._LTM_INGEST_TASKS` 持强引用 + `add_done_callback(discard)` |
| **M1** | 检索(embed+rerank)在 `stream_agent_response` 阻塞首个 SSE 帧 | ✅ 改代码 | LTM 检索移到 `agent_state_snapshot` 之后;检索前 `is_disconnected()` 探测,断连即跳过 |
| **M2** | 每轮 chat 同步解析整个 facts/vectors 文件(向量文件可 MB 级)阻塞事件循环 | ✅ 改代码 | 进程内读缓存 `_CACHE_FACTS`/`_CACHE_VECTORS` + copy-on-write(写路径 `_load_disk` 读盘新对象、`_save` 整体换引用),读者 lock-free 安全 |
| **M3** | 抽取/协调的坏 JSON 与"真·空"无法区分 → 误推进水位线永久丢 delta | ✅ 改代码 | `_parse_json_array(strict=True)` 对"有 `[` 但解析失败"抛 `_LTMParseError`,ingest 不推进水位线、下次重试;真·空仍推进 |
| **L1** | 抽取/协调用最贵主模型 | ✅ 改代码 | `QWEN_LTM_MODEL`(默认 = 主 MODEL,可指更便宜模型) |
| **L2** | ingest 持 `_LTM_LOCK` 跨网络调用,最坏锁持有到 client 默认超时(分钟级) | ✅ 改代码 | `complete`/`embed_texts` 加硬超时(`QWEN_LTM_COMPLETE_TIMEOUT=60` / `QWEN_LTM_EMBED_TIMEOUT=30`) |
| **L3/L4** | 跨会话记忆是单用户假设 + 持久提示注入面 + 全局无鉴权端点 | ✅ 改文档 | CLAUDE.md Phase 11 段补"安全/隐私(重要边界)":多用户须隔离 + 审核 + 鉴权 |

复审验证(无网络,monkeypatch LLM/embed/rerank):core ingest + NOOP、**M2 copy-on-write 隔离(写不污染已发出的读快照、缓存引用被替换)**、**M3 坏 JSON 不推进水位线 + 重试成功 / 真·空推进**、检索 + 降级 —— 全通过。
