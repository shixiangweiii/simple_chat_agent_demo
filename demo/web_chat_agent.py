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
    API_MODE,
    PendingMismatch,
    PendingNotFound,
    PlanNotFound,
    PlanStateMismatch,
    PlanUnavailable,
    PlanValidationError,
    SteerUnavailable,
    UiActionMismatch,
    UiActionNotFound,
    UiActionUnavailable,
    UiSurfaceNotFound,
    archive_session,
    confidence_decision,
    delete_session,
    get_archive_path_if_exists,
    get_or_load,
    get_session_lock,
    list_sessions,
    _with_protocol_tags,
    plan_confirm_response,
    plan_continue_response,
    plan_decision_response,
    read_history,
    reset_session,
    resume_chat_response,
    runtime_state_snapshot,
    session_count,
    steer_response,
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

# chat 模式需要 MCP key;如果未单独设 DASHSCOPE_API_KEY_MCP,将 fallback 使用 DASHSCOPE_API_KEY
if not os.environ.get("DASHSCOPE_API_KEY_MCP"):
    # chat_core 已 import,可直接读 API_MODE
    if API_MODE == "chat":
        logger.info(
            "API_MODE=chat 下 MCP 联网搜索将使用 DASHSCOPE_API_KEY (fallback)。"
            "如需 MCP 使用独立 key,请: export DASHSCOPE_API_KEY_MCP=sk-xxx"
        )


STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Simple Chat Agent")


# ============================================================
# Phase 10a: 多模态图片上传限制 (HTTP 边界, 不放 chat_core)
# ============================================================
# 单张原图 ≤ 5MB,base64 编码后膨胀 4/3,留 data URL 头部余量,约 ~7.5MB 字符
MAX_IMAGES_PER_TURN = 3
MAX_IMAGE_DATA_URL_CHARS = 7_500_000


class ImagePayloadInvalid(ValueError):
    """前端送来的 images 字段不通过后端硬校验。HTTP 层翻译为 400。

    校验规则(与前端 attachImages 双层防御):
    - 总数 ≤ MAX_IMAGES_PER_TURN
    - 每条必须 str 且 startswith("data:image/")(白名单 image/*)
    - 每条字符长度 ≤ MAX_IMAGE_DATA_URL_CHARS(约 5MB 原图编码后上限)
    """


def _validate_images(images: list[str] | None) -> list[str] | None:
    """校验并归一化 images 字段。None / [] 都返回 None,让下游短路。"""
    if not images:
        return None
    if len(images) > MAX_IMAGES_PER_TURN:
        raise ImagePayloadInvalid(
            f"最多附带 {MAX_IMAGES_PER_TURN} 张图片(收到 {len(images)} 张)"
        )
    for idx, item in enumerate(images):
        if not isinstance(item, str) or not item.startswith("data:image/"):
            raise ImagePayloadInvalid(
                f"第 {idx + 1} 张图片格式非法,必须是 data:image/* 起始的 data URL"
            )
        if len(item) > MAX_IMAGE_DATA_URL_CHARS:
            raise ImagePayloadInvalid(
                f"第 {idx + 1} 张图片过大,请压缩到 5MB 以内"
            )
    return images


# ============================================================
# Phase 10b: 文本文件附件限制 (HTTP 边界, 不放 chat_core)
# ============================================================
MAX_ATTACHMENTS_PER_TURN = 5
MAX_ATTACHMENT_CHARS = 20 * 1024          # 单文件软上限 (业务层会截断,此处不拒)
MAX_ATTACHMENT_HARD_CHARS = 100 * 1024    # 单文件硬上限 (HTTP 400); ≤ 总量上限
MAX_ATTACHMENT_TOTAL_CHARS = 100 * 1024   # 全部附件总量硬上限
MAX_FILENAME_LEN = 255
ALLOWED_ATTACHMENT_MIMES = frozenset({
    "text/plain", "text/markdown", "text/csv", "text/html", "text/css",
    "text/javascript", "text/x-python", "text/x-go", "text/x-rust",
    "text/x-java", "text/x-c", "text/x-c++", "text/x-typescript",
    "text/x-yaml", "text/x-shellscript",
    "application/json", "application/xml", "application/x-yaml",
    "application/javascript", "application/typescript",
})
# filename 不允许出现的字符集合 (路径分隔符 / 控制字符 / Unicode 双向控制等)
_RTL_CHARS = frozenset(
    "‎‏‪‫‬‭‮"  # LRM/RLM/LRE/RLE/PDF/LRO/RLO
)


class AttachmentPayloadInvalid(ValueError):
    """前端送来的 attachments 字段不通过后端硬校验。HTTP 层翻译为 400。

    校验规则(与前端 attachFiles 双层防御):
    - 总数 ≤ MAX_ATTACHMENTS_PER_TURN
    - 每项必须符合 Attachment Pydantic 模型 (filename / content / mime_type 均为 str)
    - filename: 非空白;长度 ≤ MAX_FILENAME_LEN;无路径分隔符/.. /控制字符/双向控制
    - content: 不含 NUL;字符数 ≤ MAX_ATTACHMENT_HARD_CHARS
    - mime_type: 严格白名单匹配 ALLOWED_ATTACHMENT_MIMES (不要 startswith)
    - 全部累加 content 字符数 ≤ MAX_ATTACHMENT_TOTAL_CHARS
    """


class Attachment(BaseModel):
    """Pydantic 模型: 单个文本文件附件。Pydantic 自动保证三键存在且为 str。"""
    filename: str
    content: str
    mime_type: str


def _validate_attachments(items: list[Attachment] | None) -> list[dict] | None:
    """校验并归一化 attachments 字段。None / [] 都返回 None,让下游短路。

    输入已是 Pydantic Attachment 对象 (ChatRequest 保证),这里只做业务约束校验。
    """
    if not items:
        return None
    if len(items) > MAX_ATTACHMENTS_PER_TURN:
        raise AttachmentPayloadInvalid(
            f"最多附带 {MAX_ATTACHMENTS_PER_TURN} 个文件(收到 {len(items)} 个)"
        )
    total_chars = 0
    for idx, att in enumerate(items):
        filename = att.filename
        if not filename.strip():
            raise AttachmentPayloadInvalid(f"第 {idx + 1} 个附件的文件名不能为空")
        if len(filename) > MAX_FILENAME_LEN:
            raise AttachmentPayloadInvalid(
                f"第 {idx + 1} 个附件的文件名过长(>{MAX_FILENAME_LEN})"
            )
        if (
            "/" in filename or "\\" in filename or ".." in filename
            or any(ord(c) < 0x20 or ord(c) == 0x7f for c in filename)
            or any(c in _RTL_CHARS for c in filename)
        ):
            raise AttachmentPayloadInvalid(
                f"第 {idx + 1} 个附件的文件名含非法字符"
            )
        content = att.content
        if "\x00" in content:
            raise AttachmentPayloadInvalid(
                f"第 {idx + 1} 个附件内容含 NUL 字符"
            )
        if len(content) > MAX_ATTACHMENT_HARD_CHARS:
            raise AttachmentPayloadInvalid(
                f"第 {idx + 1} 个附件过大(>{MAX_ATTACHMENT_HARD_CHARS // 1024}KB)"
            )
        mime_type = att.mime_type
        if mime_type not in ALLOWED_ATTACHMENT_MIMES:
            raise AttachmentPayloadInvalid(
                f"第 {idx + 1} 个附件的 MIME 类型不支持: {mime_type}"
            )
        total_chars += len(content)
    if total_chars > MAX_ATTACHMENT_TOTAL_CHARS:
        raise AttachmentPayloadInvalid(
            f"全部附件总量超限(>{MAX_ATTACHMENT_TOTAL_CHARS // 1024}KB)"
        )
    return [a.model_dump() for a in items]


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
    # Phase 10a: 可选图片附件 base64 data URL list, 详见 _validate_images 校验
    images: list[str] | None = None
    # Phase 10b: 文本文件附件, Pydantic 模型 + 业务约束见 _validate_attachments
    attachments: list[Attachment] | None = None


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
    # Phase 8b: 表单数据快照 —— 前端 sendUiAction 会带上当前 surface.data,
    # 后端 ui_action_response 注入到 [UI Action] 文本供模型读取。可选,兼容旧客户端。
    form_data: dict | None = None


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


class SteerRequest(BaseModel):
    """Phase 9a: 用户在 Agent 流活跃期间发起的方向纠正指令。"""
    session_id: str
    message: str


# ============================================================
# SSE 序列化
# ============================================================

def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _sse_stream(
    events: AsyncGenerator[tuple[str, dict], None],
) -> AsyncGenerator[str, None]:
    """把 chat_core 抛出的 (event_name, payload) 元组逐个套上 SSE 文本帧。

    Phase 7b: 通过 _with_protocol_tags 注入 AG-UI/A2UI 协议标签字段。
    """
    async for event_name, payload in events:
        yield sse(event_name, _with_protocol_tags(event_name, payload))


# ============================================================
# 路由
# ============================================================

@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "model": MODEL, "api_mode": API_MODE, "sessions": session_count()}


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
    # Phase 10a: 图片 payload 后端硬校验(前端 attachImages 也做, 双层防御)
    # Phase 10b: 文本附件 payload 后端硬校验(前端 attachFiles 也做, 双层防御)
    try:
        images = _validate_images(req.images)
        # _validate_attachments 接收 list[Attachment], 返回 list[dict] (给业务层)
        attachments = _validate_attachments(req.attachments)
    except (ImagePayloadInvalid, AttachmentPayloadInvalid) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    events = stream_agent_response(
        memory, req.message, request.is_disconnected, req.session_id,
        context, images, attachments,
    )
    lock = get_session_lock(req.session_id)

    async def _locked():
        async with lock:
            async for chunk in _sse_stream(events):
                yield chunk

    return StreamingResponse(
        _locked(),
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
    lock = get_session_lock(req.session_id)

    async def _locked():
        async with lock:
            async for chunk in _sse_stream(events):
                yield chunk

    return StreamingResponse(
        _locked(),
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
            form_data=req.form_data,
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
    lock = get_session_lock(req.session_id)

    async def _locked():
        async with lock:
            async for chunk in _sse_stream(events):
                yield chunk

    return StreamingResponse(
        _locked(),
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
    lock = get_session_lock(req.session_id)

    async def _locked():
        async with lock:
            async for chunk in _sse_stream(events):
                yield chunk

    return StreamingResponse(
        _locked(),
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
    lock = get_session_lock(req.session_id)

    async def _locked():
        async with lock:
            async for chunk in _sse_stream(events):
                yield chunk

    return StreamingResponse(
        _locked(),
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
    lock = get_session_lock(req.session_id)

    async def _locked():
        async with lock:
            async for chunk in _sse_stream(events):
                yield chunk

    return StreamingResponse(
        _locked(),
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


@app.post("/api/steer")
async def steer_route(req: SteerRequest) -> dict:
    """Phase 9a: 用户在 Agent 流活跃期间注入纠偏指令。

    本路由**不获取** session lock —— 否则与正在持有锁的 SSE 流死锁。
    仅做参数校验 + put 到内存队列, 立即返回。下一轮 ReAct LLM 调用前被消费。

    若无活跃流, steer 进入队列等待下次 /api/chat。
    """
    try:
        return steer_response(req.session_id, req.message)
    except InvalidSessionId:
        raise HTTPException(status_code=400, detail="invalid session_id")
    except SteerUnavailable as exc:
        raise HTTPException(status_code=400, detail=str(exc))


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
