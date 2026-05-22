"""
Web 版 ReAct 聊天 Agent
----------------------
基于 FastAPI + SSE，复用 common_chat_agent 中的 ReAct 核心逻辑，
向浏览器提供 ChatGPT 风格的流式聊天界面。

启动：
    export DASHSCOPE_API_KEY=sk-xxx
    python demo/web_chat_agent.py
然后在浏览器打开 http://127.0.0.1:8000
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse
from openai import OpenAI
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent))
from common_chat_agent import (  # noqa: E402
    MODEL,
    TOOLS,
    USER_PROMPT,
    Memory,
    build_prompt,
    execute_tool,
    match_tool_action,
    parse_action_input,
)


API_KEY = os.environ.get("DASHSCOPE_API_KEY")
if not API_KEY:
    logger.error(
        "未配置环境变量 DASHSCOPE_API_KEY\n"
        "用法: export DASHSCOPE_API_KEY=sk-xxx && python demo/web_chat_agent.py"
    )
    sys.exit(1)

client = OpenAI(
    api_key=API_KEY,
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)

STATIC_DIR = Path(__file__).parent / "static"
MAX_ROUNDS = 5
sessions: dict[str, Memory] = {}

app = FastAPI(title="Simple Chat Agent")


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ResetRequest(BaseModel):
    session_id: str


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def llm_stream(prompt: str) -> AsyncGenerator[tuple[str, str], None]:
    """流式调用 LLM，同时开启思考模式与联网搜索；区分 reasoning_content 与 content。

    yield ("thinking", text) 或 ("content", text)。
    末帧只携带 usage 时记录到日志，不向上抛出。
    """
    loop = asyncio.get_running_loop()
    stream = await loop.run_in_executor(
        None,
        lambda: client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            extra_body={
                "enable_thinking": True,
                "enable_search": True,
            },
            stream=True,
            stream_options={"include_usage": True},
        ),
    )

    def next_chunk(iterator):
        try:
            return next(iterator)
        except StopIteration:
            return None

    iterator = iter(stream)
    while True:
        chunk = await loop.run_in_executor(None, next_chunk, iterator)
        if chunk is None:
            return
        if not chunk.choices:
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                usage_payload = (
                    usage.model_dump() if hasattr(usage, "model_dump") else usage
                )
                logger.info("usage=%s", usage_payload)
            continue
        delta = chunk.choices[0].delta
        reasoning = getattr(delta, "reasoning_content", None)
        if reasoning:
            yield ("thinking", reasoning)
        content = getattr(delta, "content", None)
        if content:
            yield ("content", content)


async def stream_agent_response(
    memory: Memory, user_input: str, request: Request
) -> AsyncGenerator[str, None]:
    latest_input = user_input
    has_tools = bool(TOOLS)

    for round_num in range(MAX_ROUNDS):
        yield sse("status", {"phase": "thinking", "round": round_num})
        prompt = build_prompt(USER_PROMPT, TOOLS, memory, latest_input)
        logger.info("prompt=\n%s", prompt)

        full = ""
        buffered: list[str] = []
        answering_flipped = False

        try:
            async for kind, text in llm_stream(prompt):
                if await request.is_disconnected():
                    return
                if kind == "thinking":
                    yield sse("thinking", {"text": text})
                    continue
                # 正式回答首字 —— 补发一帧 answering 状态，方便前端折叠思考面板
                if not answering_flipped:
                    answering_flipped = True
                    yield sse("status", {"phase": "answering", "round": round_num})
                full += text
                if has_tools:
                    buffered.append(text)
                else:
                    yield sse("chunk", {"text": text})
        except Exception as exc:  # 网络 / API 错误
            logger.exception("LLM 调用失败")
            yield sse("error", {"message": f"LLM 调用失败: {exc}"})
            return

        logger.info("llmResult=\n%s", full)

        matched = match_tool_action(full)
        if matched:
            try:
                args = parse_action_input(full)
            except Exception as exc:
                logger.exception("工具参数解析失败")
                yield sse("error", {"message": f"工具参数解析失败: {exc}"})
                return
            logger.info("执行工具调用:%s,开始", matched)
            logger.info("工具参数:%s", args)
            yield sse("tool_call", {"name": matched, "args": args})
            tool_result = execute_tool(matched, args)
            logger.info("执行工具调用:%s,结果=%s", matched, tool_result)
            yield sse("tool_result", {"name": matched, "result": tool_result})
            latest_input += f"\n{full}\nObservation: {tool_result}"
        else:
            if has_tools:
                for c in buffered:
                    yield sse("chunk", {"text": c})
            memory.add(Memory.USER, user_input)
            memory.add(Memory.AI, full)
            yield sse("done", {})
            return

    yield sse("error", {"message": "超过最大 ReAct 轮次，已中断"})


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "model": MODEL, "sessions": len(sessions)}


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request) -> StreamingResponse:
    memory = sessions.setdefault(req.session_id, Memory())
    return StreamingResponse(
        stream_agent_response(memory, req.message, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/reset")
async def reset(req: ResetRequest) -> dict:
    sessions.pop(req.session_id, None)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
