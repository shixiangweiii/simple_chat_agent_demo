"""底层 LLM 基础层 —— 仅负责"prompt / messages → 文本 / 流式 (kind, payload) tuple"。

不知道 Memory、ReAct、HTTP、SSE、MCP 等上层概念。底层与传输/业务的唯一抽象边界是
`(kind, payload)` 元组协议:
    - ("thinking", text:str)        思考摘要增量
    - ("content", text:str)         最终回复增量
    - ("search_status", phase:str)  内置 web_search 阶段(仅 responses 模式;chat 模式不发)
    - ("tool_calls", list[dict])    Chat Completions 原生 function calling 拼装完成的工具调用列表,
                                    每项形如 {"id":..., "name":..., "arguments": str(JSON 文本)};
                                    仅 llm_stream_chat_with_tools 入口会 yield
    - ("error", message:str)        服务端错误

env:
    DASHSCOPE_API_KEY  必须;首次调用 LLM 时校验,缺失直接抛 RuntimeError
    QWEN_MODEL         可选,默认 qwen3.7-max
    API_MODE           可选,chat | responses(默认),大小写不敏感;非法值在模块加载时抛错
"""

import asyncio
import logging
import os
import time
from typing import AsyncGenerator

import httpx  # openai 的传递依赖,已在 .venv;rerank 走裸 HTTP(非 OpenAI SDK 方法)
from openai import OpenAI

logger = logging.getLogger(__name__)


# ============================================================
# 常量
# ============================================================

# 模型名走环境变量,默认 qwen3.7-max(Responses API + 内置 web_search 推荐模型)
MODEL = os.environ.get("QWEN_MODEL", "qwen3.7-max")

# 底层 LLM 调用协议切换:
# - "responses"(默认): 兼容 OpenAI 格式的 Responses API(client.responses.create),支持内置 web_search lifecycle 事件
# - "chat":           OpenAI 兼容-Chat Completions(client.chat.completions.create) + native function calling
#                     联网搜索改通过 mcp_web_search.py 调 DashScope WebSearch MCP server,可见性由
#                     tool_call/tool_result 事件承载,不再用 extra_body.enable_search。
#                     入口为 llm_chat_with_tools / llm_stream_chat_with_tools,绕开 llm()/llm_stream() 老分发器。
# 大小写不敏感;非法值在模块加载时直接抛错。
_API_MODE_RAW = os.environ.get("API_MODE", "responses").strip().lower()
_VALID_API_MODES = ("chat", "responses")
if _API_MODE_RAW not in _VALID_API_MODES:
    raise RuntimeError(
        f"非法的 API_MODE={_API_MODE_RAW!r},仅支持 {_VALID_API_MODES} 之一(大小写不敏感)\n"
        "用法: export API_MODE=chat 或 export API_MODE=responses"
    )
API_MODE = _API_MODE_RAW

# 跨会话长期记忆(Phase 11)用到的三类底层模型调用:
# - complete: 无工具一次性补全,用于事实抽取 / 协调。**不带 web_search / enable_search**,
#             避免抽取时误触发联网搜索;与 API_MODE 解耦(始终走 chat.completions)。
# - embedding: text-embedding-v4(Qwen3-Embedding),OpenAI 兼容 client.embeddings.create,
#              与 LLM 共用同一 compatible-mode 网关与 client 单例。
# - rerank:   qwen3-rerank,**不是** OpenAI SDK 方法,需裸 HTTP POST 到 compatible-api 路径
#             (与 embedding/chat 的 compatible-mode 不同路径)。
EMBED_MODEL = os.environ.get("QWEN_EMBED_MODEL", "text-embedding-v4")
EMBED_DIM = int(os.environ.get("QWEN_EMBED_DIM", "512"))  # 512 平衡检索质量与 JSON 体积
EMBED_BATCH_MAX = 10  # text-embedding-v3/v4 单次最多 10 条
RERANK_MODEL = os.environ.get("QWEN_RERANK_MODEL", "qwen3-rerank")  # gte-rerank 2026-05-30 下线
RERANK_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-api/v1/reranks"

# 长期记忆抽取/协调用的模型(L1):默认复用主 MODEL(零行为变化),成本敏感时可单独指向
# 更便宜的模型(如 export QWEN_LTM_MODEL=qwen-plus)。抽取是结构化小任务,不必用最贵的旗舰模型。
LTM_MODEL = os.environ.get("QWEN_LTM_MODEL", MODEL)
# 长期记忆三类调用的请求超时(L2):抽取/协调持全局 _LTM_LOCK,给硬超时把最坏锁持有时长
# 从 OpenAI client 默认(分钟级)压到秒级,避免一次卡死的调用长时间阻塞 delete / 下一次 ingest。
LTM_COMPLETE_TIMEOUT = float(os.environ.get("QWEN_LTM_COMPLETE_TIMEOUT", "60"))
LTM_EMBED_TIMEOUT = float(os.environ.get("QWEN_LTM_EMBED_TIMEOUT", "30"))


# ============================================================
# 客户端 - 模块级 lazy 单例
# ============================================================

_client: OpenAI | None = None


def _build_client() -> OpenAI:
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "未配置环境变量 DASHSCOPE_API_KEY\n"
            "用法: export DASHSCOPE_API_KEY=sk-xxx"
        )
    return OpenAI(
        api_key=api_key,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )


def _get_client() -> OpenAI:
    """首次调用时构建 OpenAI 客户端并缓存,后续调用返回同一实例。

    懒加载让 `import llm_client` 不强依赖 `DASHSCOPE_API_KEY`,
    入口模块可以先打印友好的"未配置"提示再决定是否进入 LLM 调用。
    """
    global _client
    if _client is None:
        _client = _build_client()
    return _client


def _extract_output_text(response_obj) -> str:
    """从一个完整 response 对象里抠出所有 message 类型 output 的文本拼接。

    DashScope 流式实现有时不发逐字 `output_text.delta`,而是把完整正文塞在
    `response.completed` 事件携带的 `response.output[].content[].text` 里。
    用此函数兜底取出来。结构参考 docs/兼容 OpenAI 格式的 Responses API-获取响应.md。
    """
    if response_obj is None:
        return ""
    output = getattr(response_obj, "output", None) or []
    parts: list[str] = []
    for item in output:
        if getattr(item, "type", None) != "message":
            continue
        contents = getattr(item, "content", None) or []
        for c in contents:
            if getattr(c, "type", None) == "output_text":
                t = getattr(c, "text", None)
                if t:
                    parts.append(t)
    return "".join(parts)


# ============================================================
# 同步流式 (CLI) - llm()
# ============================================================

def llm(prompt: str) -> str:
    """同步流式调用 LLM,CLI 模式专用。根据 API_MODE 路由到 Chat Completions 或 Responses API。

    返回最终回复文本字符串。无论底层走哪个 API,签名稳定,中间事件(思考、搜索 lifecycle 等)全部丢弃。
    """
    client = _get_client()
    if API_MODE == "chat":
        return _llm_chat(prompt, client)
    return _llm_responses(prompt, client)


def _llm_responses(prompt: str, client: OpenAI) -> str:
    """[Responses API 实现] 流式调用 client.responses.create。

    - 监听 `response.output_text.delta` 累积文本
    - `response.completed` 检查 status,错误时抛 RuntimeError;同时记录 usage
    - 整轮无 delta 时从最后一次完整 response.output 兜底取文本(DashScope 偶发情况)
    - 嵌入式错误 chunk 防御(HTTP 200 但 body 是错误,典型场景:API key 未绑定 workspace)

    开发期日志:请求参数、首次出现 chunk type 全文、关键 lifecycle、结束摘要(elapsed/计数/字符数/兜底)。
    """
    request_kwargs = {
        "model": MODEL,
        "tools": [{"type": "web_search"}],
        "extra_body": {"enable_thinking": True},
        "store": False,
        "stream": True,
    }
    logger.info(
        "LLM 请求(mode=responses):model=%s tools=%s extra_body=%s store=%s stream=%s prompt_chars=%d",
        request_kwargs["model"], request_kwargs["tools"], request_kwargs["extra_body"],
        request_kwargs["store"], request_kwargs["stream"], len(prompt),
    )

    started_at = time.monotonic()
    stream = client.responses.create(input=prompt, **request_kwargs)
    logger.info("LLM 流式连接已建立,开始消费 chunk(elapsed=%.0fms)",
                (time.monotonic() - started_at) * 1000)

    parts: list[str] = []
    last_full_response = None
    type_counts: dict[str, int] = {}
    response_id: str | None = None
    thinking_chars = 0
    for chunk in stream:
        raw_type = getattr(chunk, "type", None)
        ctype = raw_type or "<no-type>"
        type_counts[ctype] = type_counts.get(ctype, 0) + 1
        if type_counts[ctype] == 1:
            try:
                dump = chunk.model_dump_json()
            except Exception:
                dump = repr(chunk)
            logger.info("首次出现 chunk type=%s 内容=%s", ctype, dump)

        # 嵌入式错误 chunk 检测(HTTP 200 但 body 是错误,典型场景:API key 未绑定 workspace)
        err_code = getattr(chunk, "code", None)
        err_message = getattr(chunk, "message", None)
        if raw_type is None and (err_code or err_message):
            request_id = getattr(chunk, "request_id", None)
            full_msg = f"上游 API 错误 code={err_code} message={err_message} request_id={request_id}"
            logger.warning(full_msg)
            raise RuntimeError(full_msg)

        chunk_response = getattr(chunk, "response", None)
        if chunk_response is not None:
            last_full_response = chunk_response
            if response_id is None:
                rid = getattr(chunk_response, "id", None)
                if rid:
                    response_id = rid
                    logger.info("LLM 响应 id=%s", response_id)

        if ctype == "response.output_text.delta":
            delta = getattr(chunk, "delta", None)
            if delta:
                parts.append(delta)
        elif ctype == "response.reasoning_summary_text.delta":
            delta = getattr(chunk, "delta", None)
            if delta:
                thinking_chars += len(delta)
        elif ctype in (
            "response.web_search_call.in_progress",
            "response.web_search_call.searching",
            "response.web_search_call.completed",
        ):
            logger.info("web_search 调用 %s", ctype.rsplit(".", 1)[-1])
        elif ctype == "response.completed":
            status = getattr(chunk_response, "status", None) if chunk_response else None
            if status in ("failed", "incomplete"):
                err = getattr(chunk_response, "error", None)
                logger.warning("LLM 响应未成功: status=%s error=%s", status, err)
                raise RuntimeError(f"LLM 响应未成功: status={status}, error={err}")
            logger.info("response.completed status=%s", status)
            usage = getattr(chunk_response, "usage", None) if chunk_response else None
            if usage is not None:
                usage_payload = (
                    usage.model_dump() if hasattr(usage, "model_dump") else usage
                )
                logger.info("usage=%s", usage_payload)
        # 其他事件（output_item.*、content_part.*、created、in_progress、reasoning_summary_text.done 等）忽略

    elapsed_ms = (time.monotonic() - started_at) * 1000
    fallback_triggered = False
    result = "".join(parts)
    if not parts and last_full_response is not None:
        fallback_text = _extract_output_text(last_full_response)
        if fallback_text:
            fallback_triggered = True
            preview = fallback_text[:200].replace("\n", " ")
            if len(fallback_text) > 200:
                preview += "..."
            logger.info("流式无 delta,从 response.output 兜底取文本(%d 字),预览=%s",
                        len(fallback_text), preview)
            result = fallback_text
    logger.info(
        "llm 结束(mode=responses):elapsed=%.0fms response_id=%s chunks=%s "
        "content_chars=%d thinking_chars=%d fallback=%s",
        elapsed_ms, response_id, type_counts, len(result),
        thinking_chars, fallback_triggered,
    )
    return result


def _llm_chat(prompt: str, client: OpenAI) -> str:
    """[Chat Completions API 实现 / 保留作教学对照] 运行时业务路径已不再走本函数 ——
    chat 模式现走 _llm_chat_with_tools(native function calling + MCP)。本函数仍保留
    展示 enable_search 后台搜索方案与 native function calling 的对照。

    - prompt 包成 messages=[{"role":"user","content":prompt}],保持"完整对话拼成一个字符串"的架构
    - extra_body: enable_thinking + enable_search(联网搜索由 DashScope 后台执行,无 lifecycle 事件)
    - stream_options.include_usage 让流尾出现一个 choices=[] 的 usage chunk
    - 取流字段:delta.reasoning_content(思考)/ delta.content(答复);思考累计字符不返回主文本
    - finish_reason 异常值 warning,不中断(usage chunk 通常在后)
    - 嵌入式错误防御:Chat Completions 错误一般走 HTTP 4xx,但仍保留同形状的兜底
    """
    request_kwargs = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "extra_body": {"enable_thinking": True, "enable_search": True},
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    logger.info(
        "LLM 请求(mode=chat):model=%s messages_roles=%s extra_body=%s stream=%s stream_options=%s prompt_chars=%d",
        request_kwargs["model"], [m["role"] for m in request_kwargs["messages"]],
        request_kwargs["extra_body"], request_kwargs["stream"], request_kwargs["stream_options"],
        len(prompt),
    )

    started_at = time.monotonic()
    stream = client.chat.completions.create(**request_kwargs)
    logger.info("LLM 流式连接已建立,开始消费 chunk(elapsed=%.0fms)",
                (time.monotonic() - started_at) * 1000)

    parts: list[str] = []
    type_counts: dict[str, int] = {}
    response_id: str | None = None
    thinking_chars = 0
    for chunk in stream:
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            ctype = "usage_only"
        else:
            fr = getattr(choices[0], "finish_reason", None)
            ctype = f"delta(finish={fr})" if fr else "delta"
        type_counts[ctype] = type_counts.get(ctype, 0) + 1
        if type_counts[ctype] == 1:
            try:
                dump = chunk.model_dump_json()
            except Exception:
                dump = repr(chunk)
            logger.info("首次出现 chunk type=%s 内容=%s", ctype, dump)

        # 嵌入式错误防御(Chat Completions 多走 HTTP 4xx 直接抛,这里兜个底以防 DashScope 后续行为变化)
        err_code = getattr(chunk, "code", None)
        err_message = getattr(chunk, "message", None)
        err_obj = getattr(chunk, "error", None)
        if (err_code or err_obj) and not choices:
            request_id = getattr(chunk, "request_id", None)
            full_msg = f"上游 API 错误 code={err_code} message={err_message} error={err_obj} request_id={request_id}"
            logger.warning(full_msg)
            raise RuntimeError(full_msg)

        if response_id is None:
            rid = getattr(chunk, "id", None)
            if rid:
                response_id = rid
                logger.info("LLM 响应 id=%s", response_id)

        if not choices:
            # 流尾 usage 帧
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                usage_payload = (
                    usage.model_dump() if hasattr(usage, "model_dump") else usage
                )
                logger.info("usage=%s", usage_payload)
            continue

        delta = getattr(choices[0], "delta", None)
        if delta is not None:
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                thinking_chars += len(reasoning)
            content = getattr(delta, "content", None)
            if content:
                parts.append(content)

        finish_reason = getattr(choices[0], "finish_reason", None)
        if finish_reason and finish_reason != "stop":
            logger.warning("LLM 响应 finish_reason=%s(非 stop)", finish_reason)

    elapsed_ms = (time.monotonic() - started_at) * 1000
    result = "".join(parts)
    logger.info(
        "llm 结束(mode=chat):elapsed=%.0fms response_id=%s chunks=%s "
        "content_chars=%d thinking_chars=%d",
        elapsed_ms, response_id, type_counts, len(result), thinking_chars,
    )
    return result


# ============================================================
# 异步流式 (Web) - llm_stream()
# ============================================================

async def llm_stream(prompt: str) -> AsyncGenerator[tuple[str, str], None]:
    """异步流式调用 LLM,Web 模式专用。根据 API_MODE 路由到 Chat Completions 或 Responses API。

    yield 元组形式 (kind, text) 保持稳定:
        - ("thinking", text)        思考摘要增量
        - ("content", text)         最终回复增量
        - ("search_status", phase)  联网搜索阶段(仅 responses 模式;chat 模式不发该事件,前端 banner 自然不出现)
        - ("error", message)        服务端错误
    """
    if API_MODE == "chat":
        async for item in _llm_stream_chat(prompt):
            yield item
        return
    async for item in _llm_stream_responses(prompt):
        yield item


async def _llm_stream_responses(prompt: str) -> AsyncGenerator[tuple[str, str], None]:
    """[Responses API 实现] 流式调用 client.responses.create,同时开启思考模式与内置 web_search 工具。

    Responses API 的 usage 在 response.completed 事件里携带,无需 stream_options。

    开发期日志策略:
    - 调用前打印请求参数与 prompt 长度
    - 每种 chunk type 首次出现时,完整 dump JSON
    - 收到 response.id / web_search lifecycle 关键事件时单独打 INFO
    - 流结束时汇总:elapsed / response_id / 各 type chunk 计数 / content/thinking 累计字符 / 是否走兜底
    """
    client = _get_client()
    request_kwargs = {
        "model": MODEL,
        "tools": [{"type": "web_search"}],
        "extra_body": {"enable_thinking": True},
        "store": False,
        "stream": True,
    }
    logger.info(
        "LLM 请求(mode=responses):model=%s tools=%s extra_body=%s store=%s stream=%s prompt_chars=%d",
        request_kwargs["model"], request_kwargs["tools"], request_kwargs["extra_body"],
        request_kwargs["store"], request_kwargs["stream"], len(prompt),
    )

    started_at = time.monotonic()
    loop = asyncio.get_running_loop()
    stream = await loop.run_in_executor(
        None,
        lambda: client.responses.create(input=prompt, **request_kwargs),
    )
    logger.info("LLM 流式连接已建立,开始消费 chunk(elapsed=%.0fms)",
                (time.monotonic() - started_at) * 1000)

    def next_chunk(iterator):
        try:
            return next(iterator)
        except StopIteration:
            return None

    iterator = iter(stream)
    type_counts: dict[str, int] = {}        # 每种 chunk type 计数,用于结束时摘要
    content_chars = 0                       # 累计 output_text.delta 字符数
    thinking_chars = 0                      # 累计 reasoning_summary_text.delta 字符数
    yielded_content = False                 # 是否已通过 output_text.delta 流出过文本
    last_full_response = None               # 兜底用:最后一次见到的完整 response 对象
    response_id: str | None = None          # 首次见到 response.id 时记录,用于关联日志
    while True:
        chunk = await loop.run_in_executor(None, next_chunk, iterator)
        if chunk is None:
            elapsed_ms = (time.monotonic() - started_at) * 1000
            fallback_triggered = False
            # 流式兜底:整轮一字未流时,从最后一次见到的完整 response.output 抽出 message 文本
            # DashScope 的流式实现有时不发逐字 output_text.delta,而是把全部正文塞在
            # response.completed 事件携带的 response.output[].content[].text 里
            if not yielded_content and last_full_response is not None:
                fallback_text = _extract_output_text(last_full_response)
                if fallback_text:
                    fallback_triggered = True
                    preview = fallback_text[:200].replace("\n", " ")
                    if len(fallback_text) > 200:
                        preview += "..."
                    logger.info("流式无 delta,从 response.output 兜底取文本(%d 字),预览=%s",
                                len(fallback_text), preview)
                    content_chars = len(fallback_text)
                    yield ("content", fallback_text)
            logger.info(
                "llm_stream 结束(mode=responses):elapsed=%.0fms response_id=%s chunks=%s "
                "content_chars=%d thinking_chars=%d fallback=%s",
                elapsed_ms, response_id, type_counts, content_chars,
                thinking_chars, fallback_triggered,
            )
            return
        raw_type = getattr(chunk, "type", None)
        ctype = raw_type or "<no-type>"
        type_counts[ctype] = type_counts.get(ctype, 0) + 1
        if type_counts[ctype] == 1:
            try:
                dump = chunk.model_dump_json()
            except Exception:
                dump = repr(chunk)
            logger.info("首次出现 chunk type=%s 内容=%s", ctype, dump)

        # DashScope 偶尔在 stream 里塞带 code+message 的错误 chunk(HTTP 200 但 body 是错误)
        # 典型场景:API key 未绑定 workspace → code=InvalidParameter, message=Missing required parameter: 'workspaceid'
        err_code = getattr(chunk, "code", None)
        err_message = getattr(chunk, "message", None)
        if raw_type is None and (err_code or err_message):
            request_id = getattr(chunk, "request_id", None)
            full_msg = f"上游 API 错误 code={err_code} message={err_message} request_id={request_id}"
            logger.warning(full_msg)
            yield ("error", full_msg)
            return

        # 任何携带 response 字段的 chunk 都更新兜底快照
        chunk_response = getattr(chunk, "response", None)
        if chunk_response is not None:
            last_full_response = chunk_response
            if response_id is None:
                rid = getattr(chunk_response, "id", None)
                if rid:
                    response_id = rid
                    logger.info("LLM 响应 id=%s", response_id)

        if ctype == "response.output_text.delta":
            delta = getattr(chunk, "delta", None)
            if delta:
                yielded_content = True
                content_chars += len(delta)
                yield ("content", delta)
        elif ctype == "response.reasoning_summary_text.delta":
            delta = getattr(chunk, "delta", None)
            if delta:
                thinking_chars += len(delta)
                yield ("thinking", delta)
        elif ctype == "response.web_search_call.in_progress":
            logger.info("web_search 调用 in_progress")
            yield ("search_status", "in_progress")
        elif ctype == "response.web_search_call.searching":
            logger.info("web_search 调用 searching")
            yield ("search_status", "searching")
        elif ctype == "response.web_search_call.completed":
            logger.info("web_search 调用 completed")
            yield ("search_status", "completed")
        elif ctype == "response.completed":
            status = getattr(chunk_response, "status", None) if chunk_response else None
            if status in ("failed", "incomplete"):
                err = getattr(chunk_response, "error", None)
                logger.warning("LLM 响应未成功: status=%s error=%s", status, err)
                yield ("error", f"LLM 响应未成功: status={status}, error={err}")
                return
            logger.info("response.completed status=%s", status)
            usage = getattr(chunk_response, "usage", None) if chunk_response else None
            if usage is not None:
                usage_payload = (
                    usage.model_dump() if hasattr(usage, "model_dump") else usage
                )
                logger.info("usage=%s", usage_payload)
        # 其他事件（output_item.*、content_part.*、created、in_progress、reasoning_summary_text.done 等）忽略


async def _llm_stream_chat(prompt: str) -> AsyncGenerator[tuple[str, str], None]:
    """[Chat Completions API 实现 / 保留作教学对照] 运行时业务路径已不再走本函数 ——
    chat 模式现走 _llm_stream_chat_with_tools(native function calling + MCP)。

    与 _llm_stream_responses 的关键差异:
    - 入参 messages=[{role:user, content:prompt}](Chat Completions 标准入参形态)
    - 联网搜索靠 extra_body={"enable_search": True},**不发** search_status lifecycle —— 前端 banner 在该模式下自然不出现
    - usage 在拖尾 chunk.choices==[] 那一帧上,需 stream_options.include_usage=True
    - 思考用 chunk.choices[0].delta.reasoning_content,答复用 .content
    - 错误一般走 HTTP 4xx 直接抛(由上层 except 捕获);仍保留同形状的嵌入式错误防御
    """
    client = _get_client()
    request_kwargs = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "extra_body": {"enable_thinking": True, "enable_search": True},
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    logger.info(
        "LLM 请求(mode=chat):model=%s messages_roles=%s extra_body=%s stream=%s stream_options=%s prompt_chars=%d",
        request_kwargs["model"], [m["role"] for m in request_kwargs["messages"]],
        request_kwargs["extra_body"], request_kwargs["stream"], request_kwargs["stream_options"],
        len(prompt),
    )

    started_at = time.monotonic()
    loop = asyncio.get_running_loop()
    stream = await loop.run_in_executor(
        None,
        lambda: client.chat.completions.create(**request_kwargs),
    )
    logger.info("LLM 流式连接已建立,开始消费 chunk(elapsed=%.0fms)",
                (time.monotonic() - started_at) * 1000)

    def next_chunk(iterator):
        try:
            return next(iterator)
        except StopIteration:
            return None

    iterator = iter(stream)
    type_counts: dict[str, int] = {}
    content_chars = 0
    thinking_chars = 0
    response_id: str | None = None
    while True:
        chunk = await loop.run_in_executor(None, next_chunk, iterator)
        if chunk is None:
            elapsed_ms = (time.monotonic() - started_at) * 1000
            logger.info(
                "llm_stream 结束(mode=chat):elapsed=%.0fms response_id=%s chunks=%s "
                "content_chars=%d thinking_chars=%d",
                elapsed_ms, response_id, type_counts, content_chars, thinking_chars,
            )
            return

        choices = getattr(chunk, "choices", None) or []
        if not choices:
            ctype = "usage_only"
        else:
            fr = getattr(choices[0], "finish_reason", None)
            ctype = f"delta(finish={fr})" if fr else "delta"
        type_counts[ctype] = type_counts.get(ctype, 0) + 1
        if type_counts[ctype] == 1:
            try:
                dump = chunk.model_dump_json()
            except Exception:
                dump = repr(chunk)
            logger.info("首次出现 chunk type=%s 内容=%s", ctype, dump)

        # 嵌入式错误防御(Chat Completions 多走 HTTP 4xx 直接抛,这里兜底以防 DashScope 后续行为变化)
        err_code = getattr(chunk, "code", None)
        err_message = getattr(chunk, "message", None)
        err_obj = getattr(chunk, "error", None)
        if (err_code or err_obj) and not choices:
            request_id = getattr(chunk, "request_id", None)
            full_msg = f"上游 API 错误 code={err_code} message={err_message} error={err_obj} request_id={request_id}"
            logger.warning(full_msg)
            yield ("error", full_msg)
            return

        if response_id is None:
            rid = getattr(chunk, "id", None)
            if rid:
                response_id = rid
                logger.info("LLM 响应 id=%s", response_id)

        if not choices:
            # 流尾 usage 帧
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                usage_payload = (
                    usage.model_dump() if hasattr(usage, "model_dump") else usage
                )
                logger.info("usage=%s", usage_payload)
            continue

        delta = getattr(choices[0], "delta", None)
        if delta is not None:
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                thinking_chars += len(reasoning)
                yield ("thinking", reasoning)
            content = getattr(delta, "content", None)
            if content:
                content_chars += len(content)
                yield ("content", content)

        finish_reason = getattr(choices[0], "finish_reason", None)
        if finish_reason and finish_reason != "stop":
            logger.warning("LLM 响应 finish_reason=%s(非 stop)", finish_reason)


# ============================================================
# Chat 模式 native function calling 入口 —— 与 _llm_chat / _llm_stream_chat 并存
# 关键差异:入参是 messages: list[dict] + tools: list[dict],返回值额外携带 tool_calls 列表。
# 联网搜索从 extra_body.enable_search 改为通过 tools 由模型显式调用,因此这里**不再**传 enable_search。
# ============================================================

def llm_chat_with_tools(
    messages: list[dict], tools: list[dict]
) -> tuple[str, list[dict], str]:
    """同步流式调用 Chat Completions + tools,CLI ReAct 循环用。

    返回 (final_content, tool_calls_list, reasoning_content):
        - tool_calls_list 为 [] 表示模型不再请求工具调用,可作为 ReAct 循环退出条件
        - 每个 tool_call 形如 {"id":..., "name":..., "arguments": str(JSON 文本)}
        - reasoning_content 是 thinking 模式累计的全部思考内容,上层应在
          下一轮 assistant 消息上原样回传(参考 docs/OpenAI兼容-Chat接口-Function Calling.md:142
          关于 kimi-k2 thinking + tool_calls 的硬性要求,qwen 系列也建议保留)
    """
    return _llm_chat_with_tools(messages, tools, _get_client())


async def llm_stream_chat_with_tools(
    messages: list[dict],
    tools: list[dict],
):
    """异步流式调用 Chat Completions + tools,Web ReAct 循环用。

    yield (kind, payload) 元组:
        - ("thinking", text:str)        delta.reasoning_content 增量
        - ("content", text:str)         delta.content 增量
        - ("tool_calls", list[dict])    流尾汇总的工具调用列表(每项 {"id","name","arguments"})
        - ("error", message:str)        服务端 / 嵌入式错误
    """
    async for item in _llm_stream_chat_with_tools(messages, tools):
        yield item


def _accumulate_tool_call_chunk(tool_calls_acc: dict[int, dict], delta_tool_calls) -> None:
    """把 ChoiceDeltaToolCall 列表(单 chunk 增量)累加进 index → dict 的累计器。

    OpenAI 流式协议:tool_call.id / function.name 只在该 index 第一次出现的 chunk 上携带;
    function.arguments 每片 chunk 都携带增量片段,需要按 index 拼接。
    """
    for piece in delta_tool_calls or []:
        idx = getattr(piece, "index", 0)
        slot = tool_calls_acc.setdefault(
            idx, {"id": "", "name": "", "arguments": ""}
        )
        if getattr(piece, "id", None):
            slot["id"] = piece.id
        fn = getattr(piece, "function", None)
        if fn is not None:
            if getattr(fn, "name", None):
                slot["name"] = fn.name
            if getattr(fn, "arguments", None):
                slot["arguments"] += fn.arguments


def _finalize_tool_calls(tool_calls_acc: dict[int, dict]) -> list[dict]:
    """把 index → dict 累计器按 index 排序成 list。

    无 name 的 slot 被丢弃,但显式打 warning —— OpenAI 流式协议 name 必在第一片携带,
    若槽存在却无 name,大概率是 DashScope 非标 chunk(参见 dashscope_stream_quirks 记忆),
    静默丢弃会让用户看到模型重复调用的现象但找不到原因,所以这里要留排查痕迹。
    """
    out: list[dict] = []
    for idx in sorted(tool_calls_acc):
        slot = tool_calls_acc[idx]
        if not slot["name"]:
            logger.warning("丢弃无 name 的 tool_call slot: index=%d slot=%s", idx, slot)
            continue
        out.append(slot)
    return out


def _llm_chat_with_tools(
    messages: list[dict],
    tools: list[dict],
    client: OpenAI,
) -> tuple[str, list[dict], str]:
    """[Chat Completions + tools 同步实现] 流式拼装 content / tool_calls / reasoning_content,流结束后一次性返回。

    与 _llm_chat 的关键差异:
    - messages 是结构化数组(支持 system / user / assistant / tool 角色,以及 assistant.tool_calls)
    - 显式传 tools,不传 enable_search
    - 返回 (content, tool_calls, reasoning_content);tool_calls 非空意味着模型请求工具调用,上层应执行后回喂模型再调;
      reasoning_content 上层应在下一轮 assistant 消息上原样透传,避免 thinking 模式 + 多轮 tool_calls 报错
    """
    request_kwargs = {
        "model": MODEL,
        "messages": messages,
        "tools": tools,
        "extra_body": {"enable_thinking": True},
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    logger.info(
        "LLM 请求(mode=chat, tools=native):model=%s messages_roles=%s tools=%s "
        "extra_body=%s stream=%s stream_options=%s msg_count=%d",
        request_kwargs["model"], [m["role"] for m in messages],
        [t.get("function", {}).get("name", "?") for t in tools],
        request_kwargs["extra_body"], request_kwargs["stream"],
        request_kwargs["stream_options"], len(messages),
    )

    started_at = time.monotonic()
    stream = client.chat.completions.create(**request_kwargs)
    logger.info("LLM 流式连接已建立,开始消费 chunk(elapsed=%.0fms)",
                (time.monotonic() - started_at) * 1000)

    parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls_acc: dict[int, dict] = {}
    type_counts: dict[str, int] = {}
    response_id: str | None = None
    thinking_chars = 0
    for chunk in stream:
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            ctype = "usage_only"
        else:
            fr = getattr(choices[0], "finish_reason", None)
            ctype = f"delta(finish={fr})" if fr else "delta"
        type_counts[ctype] = type_counts.get(ctype, 0) + 1
        if type_counts[ctype] == 1:
            try:
                dump = chunk.model_dump_json()
            except Exception:
                dump = repr(chunk)
            logger.info("首次出现 chunk type=%s 内容=%s", ctype, dump)

        # 嵌入式错误防御(同 _llm_chat 形状)
        err_code = getattr(chunk, "code", None)
        err_message = getattr(chunk, "message", None)
        err_obj = getattr(chunk, "error", None)
        if (err_code or err_obj) and not choices:
            request_id = getattr(chunk, "request_id", None)
            full_msg = f"上游 API 错误 code={err_code} message={err_message} error={err_obj} request_id={request_id}"
            logger.warning(full_msg)
            raise RuntimeError(full_msg)

        if response_id is None:
            rid = getattr(chunk, "id", None)
            if rid:
                response_id = rid
                logger.info("LLM 响应 id=%s", response_id)

        if not choices:
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                usage_payload = (
                    usage.model_dump() if hasattr(usage, "model_dump") else usage
                )
                logger.info("usage=%s", usage_payload)
            continue

        delta = getattr(choices[0], "delta", None)
        if delta is not None:
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                thinking_chars += len(reasoning)
                reasoning_parts.append(reasoning)
            content = getattr(delta, "content", None)
            if content:
                parts.append(content)
            delta_tool_calls = getattr(delta, "tool_calls", None)
            if delta_tool_calls:
                _accumulate_tool_call_chunk(tool_calls_acc, delta_tool_calls)

        finish_reason = getattr(choices[0], "finish_reason", None)
        if finish_reason and finish_reason not in ("stop", "tool_calls"):
            logger.warning("LLM 响应 finish_reason=%s(非 stop/tool_calls)", finish_reason)

    elapsed_ms = (time.monotonic() - started_at) * 1000
    content = "".join(parts)
    reasoning_content = "".join(reasoning_parts)
    tool_calls = _finalize_tool_calls(tool_calls_acc)
    logger.info(
        "llm 结束(mode=chat, tools=native):elapsed=%.0fms response_id=%s chunks=%s "
        "content_chars=%d thinking_chars=%d tool_calls=%s",
        elapsed_ms, response_id, type_counts, len(content), thinking_chars,
        [(t["name"], len(t["arguments"])) for t in tool_calls],
    )
    return content, tool_calls, reasoning_content


async def _llm_stream_chat_with_tools(
    messages: list[dict],
    tools: list[dict],
):
    """[Chat Completions + tools 异步流式实现] 边流边发 thinking/content,流尾汇总 tool_calls。

    yield (kind, payload):
        - ("thinking", text:str)
        - ("content", text:str)
        - ("tool_calls", list[dict])  流尾一次性发出,每项 {"id","name","arguments"(JSON 文本)}
        - ("error", message:str)

    设计取舍:tool_calls 不逐 chunk yield —— 单 chunk 不携带完整 arguments,语义不完整。
    上层 ReAct 循环本就需要拿到完整 tool_calls 才能 json.loads 后执行,故汇总后一次性 yield。
    """
    client = _get_client()
    request_kwargs = {
        "model": MODEL,
        "messages": messages,
        "tools": tools,
        "extra_body": {"enable_thinking": True},
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    logger.info(
        "LLM 请求(mode=chat, tools=native):model=%s messages_roles=%s tools=%s "
        "extra_body=%s stream=%s stream_options=%s msg_count=%d",
        request_kwargs["model"], [m["role"] for m in messages],
        [t.get("function", {}).get("name", "?") for t in tools],
        request_kwargs["extra_body"], request_kwargs["stream"],
        request_kwargs["stream_options"], len(messages),
    )

    started_at = time.monotonic()
    loop = asyncio.get_running_loop()
    stream = await loop.run_in_executor(
        None,
        lambda: client.chat.completions.create(**request_kwargs),
    )
    logger.info("LLM 流式连接已建立,开始消费 chunk(elapsed=%.0fms)",
                (time.monotonic() - started_at) * 1000)

    def next_chunk(iterator):
        try:
            return next(iterator)
        except StopIteration:
            return None

    iterator = iter(stream)
    type_counts: dict[str, int] = {}
    content_chars = 0
    thinking_chars = 0
    response_id: str | None = None
    tool_calls_acc: dict[int, dict] = {}
    while True:
        chunk = await loop.run_in_executor(None, next_chunk, iterator)
        if chunk is None:
            tool_calls = _finalize_tool_calls(tool_calls_acc)
            if tool_calls:
                yield ("tool_calls", tool_calls)
            elapsed_ms = (time.monotonic() - started_at) * 1000
            logger.info(
                "llm_stream 结束(mode=chat, tools=native):elapsed=%.0fms response_id=%s chunks=%s "
                "content_chars=%d thinking_chars=%d tool_calls=%s",
                elapsed_ms, response_id, type_counts, content_chars, thinking_chars,
                [(t["name"], len(t["arguments"])) for t in tool_calls],
            )
            return

        choices = getattr(chunk, "choices", None) or []
        if not choices:
            ctype = "usage_only"
        else:
            fr = getattr(choices[0], "finish_reason", None)
            ctype = f"delta(finish={fr})" if fr else "delta"
        type_counts[ctype] = type_counts.get(ctype, 0) + 1
        if type_counts[ctype] == 1:
            try:
                dump = chunk.model_dump_json()
            except Exception:
                dump = repr(chunk)
            logger.info("首次出现 chunk type=%s 内容=%s", ctype, dump)

        # 嵌入式错误防御(同 _llm_stream_chat 形状)
        err_code = getattr(chunk, "code", None)
        err_message = getattr(chunk, "message", None)
        err_obj = getattr(chunk, "error", None)
        if (err_code or err_obj) and not choices:
            request_id = getattr(chunk, "request_id", None)
            full_msg = f"上游 API 错误 code={err_code} message={err_message} error={err_obj} request_id={request_id}"
            logger.warning(full_msg)
            yield ("error", full_msg)
            return

        if response_id is None:
            rid = getattr(chunk, "id", None)
            if rid:
                response_id = rid
                logger.info("LLM 响应 id=%s", response_id)

        if not choices:
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                usage_payload = (
                    usage.model_dump() if hasattr(usage, "model_dump") else usage
                )
                logger.info("usage=%s", usage_payload)
            continue

        delta = getattr(choices[0], "delta", None)
        if delta is not None:
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                thinking_chars += len(reasoning)
                yield ("thinking", reasoning)
            content = getattr(delta, "content", None)
            if content:
                content_chars += len(content)
                yield ("content", content)
            delta_tool_calls = getattr(delta, "tool_calls", None)
            if delta_tool_calls:
                _accumulate_tool_call_chunk(tool_calls_acc, delta_tool_calls)

        finish_reason = getattr(choices[0], "finish_reason", None)
        if finish_reason and finish_reason not in ("stop", "tool_calls"):
            logger.warning("LLM 响应 finish_reason=%s(非 stop/tool_calls)", finish_reason)


# ============================================================
# Phase 11 长期记忆 —— 无工具一次性补全 (extract / reconcile 用)
# ============================================================
# 与 llm()/llm_chat_with_tools() 的区别:
#   - 非流式 (stream=False),一次拿完整文本即可,抽取/协调不需要逐字 UI 反馈;
#   - **不带 tools / 不带 enable_search**,避免抽取时模型误触发联网搜索;
#   - 始终走 chat.completions,与 API_MODE 解耦 (responses 模式的部署也能用)。

def complete(prompt: str, *, temperature: float = 0.0) -> str:
    """同步无工具补全(长期记忆抽取/协调专用)。返回最终文本。失败抛异常,由上层兜底。

    用 LTM_MODEL(默认 = 主 MODEL,可单独配更便宜的模型)+ LTM_COMPLETE_TIMEOUT 硬超时
    (把持锁的最坏时长压到秒级)。
    """
    client = _get_client()
    started_at = time.monotonic()
    resp = client.chat.completions.create(
        model=LTM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        stream=False,
        timeout=LTM_COMPLETE_TIMEOUT,
    )
    choices = getattr(resp, "choices", None) or []
    content = ""
    if choices:
        message = getattr(choices[0], "message", None)
        content = (getattr(message, "content", None) or "") if message is not None else ""
    logger.info(
        "complete 结束:elapsed=%.0fms model=%s prompt_chars=%d content_chars=%d",
        (time.monotonic() - started_at) * 1000, LTM_MODEL, len(prompt), len(content),
    )
    return content


async def complete_async(prompt: str, *, temperature: float = 0.0) -> str:
    """异步无工具补全 —— 把阻塞的 complete() 丢到线程池,避免阻塞事件循环。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: complete(prompt, temperature=temperature))


# ============================================================
# Phase 11 长期记忆 —— 向量 embedding + rerank 精排 (检索层)
# ============================================================

def embed_texts(texts: list[str]) -> list[list[float]]:
    """同步批量 embedding。返回与 texts 等长、同序的向量列表。

    OpenAI 兼容路径 (与 chat 共用 client 单例)。单次最多 EMBED_BATCH_MAX 条,超出分批。
    失败抛异常,由上层 (longterm_memory) 兜底为"不写向量,走降级"。
    """
    if not texts:
        return []
    client = _get_client()
    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH_MAX):
        batch = texts[i:i + EMBED_BATCH_MAX]
        resp = client.embeddings.create(
            model=EMBED_MODEL, input=batch, dimensions=EMBED_DIM, encoding_format="float",
            timeout=LTM_EMBED_TIMEOUT,  # L2: 硬超时,bound 持锁时长
        )
        # data[].index 是 batch 内序号,排序后还原输入顺序
        items = sorted(resp.data, key=lambda d: getattr(d, "index", 0))
        out.extend(list(d.embedding) for d in items)
    return out


async def embed_texts_async(texts: list[str]) -> list[list[float]]:
    """异步批量 embedding —— 阻塞调用丢线程池。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: embed_texts(texts))


def _parse_rerank_response(data: dict, n_docs: int) -> list[tuple[int, float]] | None:
    """解析 qwen3-rerank 响应:results 在顶层 (失败响应只有 code/message)。"""
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        logger.warning("rerank 无 results 字段(可能失败响应): %s",
                       (data or {}).get("message") or (data or {}).get("code") or data)
        return None
    out: list[tuple[int, float]] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        idx = r.get("index")
        score = r.get("relevance_score")
        if isinstance(idx, int) and 0 <= idx < n_docs:
            try:
                out.append((idx, float(score)))
            except (TypeError, ValueError):
                out.append((idx, 0.0))
    return out or None


def _rerank_body(query: str, documents: list[str], top_n: int) -> dict:
    return {
        "model": RERANK_MODEL,
        "query": query,
        "documents": documents,
        "top_n": min(top_n, len(documents)),
        # 长期记忆是对称语义匹配,用"语义相似度"指令而非"问答检索"
        "instruct": "Retrieve semantically similar text.",
    }


def rerank(query: str, documents: list[str], top_n: int) -> list[tuple[int, float]] | None:
    """同步 rerank。返回 [(原始 index, 分数)] 按相关性降序,或 None(任何失败 → 让上层降级)。"""
    if not documents:
        return None
    key = os.environ.get("DASHSCOPE_API_KEY")
    if not key:
        logger.warning("rerank 跳过:未配置 DASHSCOPE_API_KEY")
        return None
    try:
        resp = httpx.post(
            RERANK_ENDPOINT,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=_rerank_body(query, documents, top_n),
            timeout=15.0,
        )
        resp.raise_for_status()
        return _parse_rerank_response(resp.json(), len(documents))
    except Exception:
        logger.warning("rerank 调用失败,降级", exc_info=True)
        return None


async def rerank_async(query: str, documents: list[str], top_n: int) -> list[tuple[int, float]] | None:
    """异步 rerank(httpx.AsyncClient,真异步,不占线程池)。失败返回 None。"""
    if not documents:
        return None
    key = os.environ.get("DASHSCOPE_API_KEY")
    if not key:
        logger.warning("rerank 跳过:未配置 DASHSCOPE_API_KEY")
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                RERANK_ENDPOINT,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=_rerank_body(query, documents, top_n),
            )
            resp.raise_for_status()
            return _parse_rerank_response(resp.json(), len(documents))
    except Exception:
        logger.warning("rerank 调用失败,降级", exc_info=True)
        return None
