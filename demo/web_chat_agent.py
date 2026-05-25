"""HTTP 接口层 —— FastAPI + SSE。

只做:路由、参数校验、调 chat_core 业务函数,把抽象 (event_name, payload) 元组序列化成 SSE。
不含业务逻辑。

启动:
    export DASHSCOPE_API_KEY=sk-xxx
    python demo/web_chat_agent.py
然后在浏览器打开 http://127.0.0.1:8000
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import AsyncGenerator, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent))
from chat_core import (  # noqa: E402
    HistoryNotFound,
    InvalidSessionId,
    MODEL,
    PendingMismatch,
    PendingNotFound,
    archive_session,
    delete_session,
    get_archive_path_if_exists,
    get_or_load,
    list_sessions,
    read_history,
    reset_session,
    resume_chat_response,
    session_count,
    stream_agent_response,
)


# ============================================================
# 启动期 API key 检查 —— 给一个友好的退出消息,不留 traceback
# ============================================================

if not os.environ.get("DASHSCOPE_API_KEY"):
    logger.error(
        "未配置环境变量 DASHSCOPE_API_KEY\n"
        "用法: export DASHSCOPE_API_KEY=sk-xxx && python demo/web_chat_agent.py"
    )
    sys.exit(1)


STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Simple Chat Agent")


# ============================================================
# 请求体模型
# ============================================================

class ChatRequest(BaseModel):
    session_id: str
    message: str


class ResetRequest(BaseModel):
    session_id: str


class ArchiveRequest(BaseModel):
    session_id: str


class ResumeRequest(BaseModel):
    """HITL resume 请求体。
    decision 三态:
        - "answer"  -> ask_user 工具,answer 字段是用户答复
        - "approve" -> execute_shell_command 工具,同意执行
        - "reject"  -> execute_shell_command 工具,拒绝;answer 字段是可选拒绝理由
    """
    session_id: str
    tool_call_id: str
    decision: Literal["answer", "approve", "reject"]
    answer: str | None = None


# ============================================================
# SSE 序列化
# ============================================================

def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _sse_stream(
    events: AsyncGenerator[tuple[str, dict], None],
) -> AsyncGenerator[str, None]:
    """把 chat_core 抛出的 (event_name, payload) 元组逐个套上 SSE 文本帧。"""
    async for event_name, payload in events:
        yield sse(event_name, payload)


# ============================================================
# 路由
# ============================================================

@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "model": MODEL, "sessions": session_count()}


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request) -> StreamingResponse:
    # 用 get_or_load 替代 setdefault:即使前端没主动调 /api/history,
    # 后端也会按需从 disk lazy-load 历史 Memory,保证记忆恢复
    try:
        memory = get_or_load(req.session_id)
    except InvalidSessionId:
        raise HTTPException(status_code=400, detail="invalid session_id")
    events = stream_agent_response(memory, req.message, request.is_disconnected, req.session_id)
    return StreamingResponse(
        _sse_stream(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/resume")
async def resume(req: ResumeRequest, request: Request) -> StreamingResponse:
    """HITL resume 入口:用户对前端 HITL bubble 操作后调,业务层从 _PENDING 恢复 ReAct 续跑。

    SSE 帧类型与 /api/chat 完全一致(含可能再次出现的 await_user)。
    """
    try:
        events = resume_chat_response(
            req.session_id,
            req.tool_call_id,
            req.decision,
            req.answer,
            request.is_disconnected,
        )
    except InvalidSessionId:
        raise HTTPException(status_code=400, detail="invalid session_id")
    except PendingNotFound:
        raise HTTPException(status_code=404, detail="no pending HITL for session")
    except PendingMismatch:
        raise HTTPException(status_code=409, detail="tool_call_id mismatch")
    return StreamingResponse(
        _sse_stream(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/reset")
async def reset(req: ResetRequest) -> dict:
    """原地重置:清当前 session 内存 + 删归档文件,session_id 保留。"""
    try:
        reset_session(req.session_id)
    except InvalidSessionId:
        raise HTTPException(status_code=400, detail="invalid session_id")
    return {"ok": True}


@app.post("/api/archive")
async def archive(req: ArchiveRequest) -> dict:
    """覆盖式归档当前 session 的 Memory 到 markdown。空 Memory 跳过。"""
    try:
        return archive_session(req.session_id)
    except InvalidSessionId:
        raise HTTPException(status_code=400, detail="invalid session_id")


@app.get("/api/history")
async def history(session_id: str) -> dict:
    """返回 session 的对话历史。优先内存,再 disk lazy-load,都没有则 404。"""
    try:
        mem = read_history(session_id)
    except InvalidSessionId:
        raise HTTPException(status_code=400, detail="invalid session_id")
    except HistoryNotFound:
        raise HTTPException(status_code=404, detail="history not found")
    return {"session_id": session_id, "messages": mem.memories}


@app.get("/api/sessions")
async def sessions_list() -> list[dict]:
    """列出所有归档过的 session,只读文件头元信息;按 updated_at 倒序。"""
    return list_sessions()


@app.delete("/api/sessions/{session_id}")
async def session_delete(session_id: str) -> dict:
    """删除 session:磁盘归档 + 内存 session 同步移除。"""
    try:
        delete_session(session_id)
    except InvalidSessionId:
        raise HTTPException(status_code=400, detail="invalid session_id")
    return {"ok": True}


@app.get("/api/sessions/{session_id}/raw")
async def session_raw(session_id: str) -> FileResponse:
    """返回 session 的归档 markdown 原始文本。无归档则 404。"""
    try:
        path = get_archive_path_if_exists(session_id)
    except InvalidSessionId:
        raise HTTPException(status_code=400, detail="invalid session_id")
    except HistoryNotFound:
        raise HTTPException(status_code=404, detail="archive not found")
    return FileResponse(path, media_type="text/markdown; charset=utf-8")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
