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
    DraftNotFound,
    HistoryNotFound,
    InvalidSessionId,
    MODEL,
    PendingMismatch,
    PendingNotFound,
    PlanNotFound,
    PlanStateMismatch,
    PlanUnavailable,
    PlanValidationError,
    UiActionMismatch,
    UiActionNotFound,
    UiActionUnavailable,
    UiSurfaceNotFound,
    archive_session,
    confidence_decision,
    delete_session,
    get_archive_path_if_exists,
    get_or_load,
    list_sessions,
    plan_confirm_response,
    plan_continue_response,
    plan_decision_response,
    read_history,
    reset_session,
    resume_chat_response,
    runtime_state_snapshot,
    session_count,
    stream_agent_response,
    ui_action_response,
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

class ChatContext(BaseModel):
    viewport_width: int | None = None
    selected_text: str | None = None
    session_message_count: int | None = None


class ChatRequest(BaseModel):
    session_id: str
    message: str
    context: ChatContext | None = None


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


class UiActionRequest(BaseModel):
    session_id: str
    surface_id: str
    component_id: str
    event_name: str


class PlanConfirmRequest(BaseModel):
    session_id: str
    plan_id: str
    steps: list[dict]


class PlanDecisionRequest(BaseModel):
    session_id: str
    plan_id: str
    step_id: str
    decision: Literal["skip", "retry", "update"]
    steps: list[dict] | None = None


class PlanContinueRequest(BaseModel):
    session_id: str
    plan_id: str


class ConfidenceDecisionRequest(BaseModel):
    session_id: str
    draft_id: str
    decision: Literal["accept", "discard"]


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


@app.get("/api/runtime_state")
async def runtime_state(session_id: str) -> dict:
    """返回前端可恢复 UI 用的 session 运行态快照。"""
    try:
        return runtime_state_snapshot(session_id)
    except InvalidSessionId:
        raise HTTPException(status_code=400, detail="invalid session_id")


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request) -> StreamingResponse:
    # 用 get_or_load 替代 setdefault:即使前端没主动调 /api/history,
    # 后端也会按需从 disk lazy-load 历史 Memory,保证记忆恢复
    try:
        memory = get_or_load(req.session_id)
    except InvalidSessionId:
        raise HTTPException(status_code=400, detail="invalid session_id")
    if req.context is None:
        context = None
    elif hasattr(req.context, "model_dump"):
        context = req.context.model_dump(exclude_none=True)
    else:
        context = req.context.dict(exclude_none=True)
    events = stream_agent_response(memory, req.message, request.is_disconnected, req.session_id, context)
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


@app.post("/api/ui_action")
async def ui_action(req: UiActionRequest, request: Request) -> StreamingResponse:
    """声明式 UI button action 回传:校验后新启一条 SSE 流继续 ReAct。"""
    try:
        events = ui_action_response(
            req.session_id,
            req.surface_id,
            req.component_id,
            req.event_name,
            request.is_disconnected,
        )
    except InvalidSessionId:
        raise HTTPException(status_code=400, detail="invalid session_id")
    except UiActionUnavailable as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except UiSurfaceNotFound:
        raise HTTPException(status_code=404, detail="ui surface not found")
    except UiActionNotFound:
        raise HTTPException(status_code=404, detail="ui action not found")
    except UiActionMismatch:
        raise HTTPException(status_code=409, detail="ui action mismatch")
    return StreamingResponse(
        _sse_stream(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/plan_confirm")
async def plan_confirm(req: PlanConfirmRequest, request: Request) -> StreamingResponse:
    """用户确认/编辑计划后,新启 SSE 流逐步执行计划。"""
    try:
        events = plan_confirm_response(
            req.session_id,
            req.plan_id,
            req.steps,
            request.is_disconnected,
        )
    except InvalidSessionId:
        raise HTTPException(status_code=400, detail="invalid session_id")
    except PlanUnavailable as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PlanValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PlanNotFound:
        raise HTTPException(status_code=404, detail="plan not found")
    except PlanStateMismatch:
        raise HTTPException(status_code=409, detail="plan state mismatch")
    return StreamingResponse(
        _sse_stream(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/plan_decision")
async def plan_decision(req: PlanDecisionRequest, request: Request) -> StreamingResponse:
    """计划步骤失败后的跳过/重试/修改后继续。"""
    try:
        events = plan_decision_response(
            req.session_id,
            req.plan_id,
            req.step_id,
            req.decision,
            req.steps,
            request.is_disconnected,
        )
    except InvalidSessionId:
        raise HTTPException(status_code=400, detail="invalid session_id")
    except PlanUnavailable as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PlanValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PlanNotFound:
        raise HTTPException(status_code=404, detail="plan not found")
    except PlanStateMismatch:
        raise HTTPException(status_code=409, detail="plan state mismatch")
    return StreamingResponse(
        _sse_stream(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/plan_continue")
async def plan_continue(req: PlanContinueRequest, request: Request) -> StreamingResponse:
    """恢复 running plan 的执行流。"""
    try:
        events = plan_continue_response(
            req.session_id,
            req.plan_id,
            request.is_disconnected,
        )
    except InvalidSessionId:
        raise HTTPException(status_code=400, detail="invalid session_id")
    except PlanUnavailable as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PlanValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PlanNotFound:
        raise HTTPException(status_code=404, detail="plan not found")
    except PlanStateMismatch:
        raise HTTPException(status_code=409, detail="plan state mismatch")
    return StreamingResponse(
        _sse_stream(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/confidence_decision")
async def confidence_decision_route(req: ConfidenceDecisionRequest) -> dict:
    """低置信度草稿采纳/丢弃。accept 才写入 Memory。"""
    try:
        return confidence_decision(req.session_id, req.draft_id, req.decision)
    except InvalidSessionId:
        raise HTTPException(status_code=400, detail="invalid session_id")
    except DraftNotFound:
        raise HTTPException(status_code=404, detail="draft not found")


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
