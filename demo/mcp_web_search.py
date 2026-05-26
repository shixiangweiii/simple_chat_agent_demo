"""MCP 联网搜索客户端 —— DashScope WebSearch MCP server 的 streamableHttp 封装。

本模块只负责"调用一次拿一段 string 结果",**不感知** Memory / SSE / ReAct / OpenAI tool_calls。
chat_core 在 API_MODE=chat 路径上调用本模块的 discover_tool_spec / call_tool_async。

env:
    DASHSCOPE_API_KEY  调 list_tools / call_tool 时从 env 读取,缺失抛 RuntimeError
"""

import asyncio
import logging
import os
import time
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger(__name__)


# ============================================================
# 常量
# ============================================================

MCP_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/mcps/WebSearch/mcp"

# call_tool_async 失败时返回值的统一前缀,上层通过此前缀判断"工具是否执行失败"
ERROR_PREFIX = "工具调用失败"

# 调用前后日志中的参数 / 结果预览长度,server log 中超过即截断
_PREVIEW_CHARS = 200

# 模块级 schema 缓存:首次 discover_tool_spec 成功后填充,之后命中即返回
_tool_spec_cache: list[dict] | None = None


# ============================================================
# 内部工具
# ============================================================

def _build_headers() -> dict[str, str]:
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "未配置环境变量 DASHSCOPE_API_KEY\n"
            "用法: export DASHSCOPE_API_KEY=sk-xxx"
        )
    return {"Authorization": f"Bearer {api_key}"}


def _mcp_tool_to_openai_spec(tool) -> dict:
    """把 MCP tool 对象(name/description/inputSchema)转成 OpenAI Chat Completions tools 元素格式。

    MCP `tool.inputSchema` 是标准 JSON Schema,可直接塞进 OpenAI `function.parameters` 字段。
    """
    parameters = tool.inputSchema or {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": parameters,
        },
    }


def _flatten_call_result(result) -> str:
    """把 CallToolResult.content 中的 TextContent 拼成一段字符串。

    非 TextContent(图像/音频/资源链接)按 type 占位标记,模型看到也能继续推理。
    """
    parts: list[str] = []
    for item in result.content or []:
        item_type = getattr(item, "type", None)
        if item_type == "text":
            text = getattr(item, "text", "") or ""
            if text:
                parts.append(text)
        else:
            parts.append(f"[非文本内容: type={item_type}]")
    return "\n".join(parts)


# ============================================================
# 对外 API
# ============================================================

async def discover_tool_spec_async() -> list[dict]:
    """异步:首次调用走一次 MCP list_tools,缓存所有工具的 OpenAI 格式 spec 列表。

    后续命中缓存直接返回。失败抛 RuntimeError(由调用方决定回退/直接报错策略)。
    """
    global _tool_spec_cache
    if _tool_spec_cache is not None:
        return _tool_spec_cache

    headers = _build_headers()
    started_at = time.monotonic()
    logger.info("MCP discover_tool_spec 开始: endpoint=%s", MCP_ENDPOINT)
    try:
        async with streamablehttp_client(MCP_ENDPOINT, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listing = await session.list_tools()
    except Exception as exc:
        logger.exception("MCP list_tools 调用失败")
        raise RuntimeError(f"MCP list_tools 失败: {exc}") from exc

    specs = [_mcp_tool_to_openai_spec(t) for t in listing.tools]
    _tool_spec_cache = specs
    elapsed_ms = (time.monotonic() - started_at) * 1000
    logger.info(
        "MCP discover_tool_spec 完成: elapsed=%.0fms tools=%s",
        elapsed_ms, [t["function"]["name"] for t in specs],
    )
    return specs


def discover_tool_spec() -> list[dict]:
    """**仅 CLI / REPL 用**,asyncio.run 在已运行 event loop 的上下文(如 FastAPI handler)
    会抛 "cannot be called from a running event loop"。Web 路径请用 discover_tool_spec_async。
    """
    return asyncio.run(discover_tool_spec_async())


async def call_tool_async(name: str, args: dict[str, Any]) -> str:
    """异步调用 MCP 工具,返回结果文本(已展平)。

    - 任何失败(网络/鉴权/参数/工具自身错误)都返回 `"工具调用失败: ..."` 字符串,**不抛**异常,
      让上层 ReAct 循环把错误信息以 role=tool 喂回模型,由模型自行恢复或向用户求助
    - 每次调用打开一条短连接,DashScope MCP 是无状态的,代价仅 1 次 TCP/TLS + initialize
    """
    args_preview = str(args)
    if len(args_preview) > _PREVIEW_CHARS:
        args_preview = args_preview[:_PREVIEW_CHARS] + "..."
    logger.info("MCP call_tool 开始: name=%s args=%s", name, args_preview)

    started_at = time.monotonic()
    try:
        headers = _build_headers()
        async with streamablehttp_client(MCP_ENDPOINT, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments=args)
    except Exception as exc:
        logger.exception("MCP call_tool 异常: name=%s", name)
        return f"{ERROR_PREFIX}: {exc}"

    elapsed_ms = (time.monotonic() - started_at) * 1000
    if getattr(result, "isError", False):
        text = _flatten_call_result(result) or "(空错误内容)"
        logger.warning(
            "MCP call_tool 业务错误: name=%s elapsed=%.0fms text_chars=%d",
            name, elapsed_ms, len(text),
        )
        return f"{ERROR_PREFIX}: {text}"

    text = _flatten_call_result(result)
    logger.info(
        "MCP call_tool 完成: name=%s elapsed=%.0fms text_chars=%d",
        name, elapsed_ms, len(text),
    )
    return text


def call_tool_sync(name: str, args: dict[str, Any]) -> str:
    """**仅 CLI / REPL 用**,asyncio.run 在已运行 event loop 的上下文(如 FastAPI handler)
    会抛 "cannot be called from a running event loop"。Web 路径请用 call_tool_async。
    """
    return asyncio.run(call_tool_async(name, args))
