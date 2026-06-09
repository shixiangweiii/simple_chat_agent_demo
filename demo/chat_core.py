"""业务逻辑层 —— Memory + ReAct + Sessions + Tools。

依赖底层 `llm_client`(通过 `llm()` / `llm_stream()` 调模型),不依赖 FastAPI / SSE / starlette。

入口模块(CLI / HTTP)通过本模块的下列出口完成业务:
    - Memory                       数据载体 + markdown 序列化
    - react(memory, latest_input)  CLI 同步 ReAct 主循环
    - stream_agent_response(...)   Web 异步 ReAct 流式循环,yield 抽象 (event_name, payload) 元组
    - get_or_load(session_id)      会话内存/disk lazy-load
    - archive_session / reset_session / delete_session / list_sessions / read_history / get_archive_path_if_exists / session_count
    - InvalidSessionId / HistoryNotFound  域异常,HTTP 层翻译为状态码
    - MODEL / API_MODE             从 llm_client 重新导出,便于上层只 import 本层
"""

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator, Awaitable, Callable

# 沿用入口模块已设置的 sys.path,保证可以平级 import llm_client / mcp_web_search
sys.path.insert(0, str(Path(__file__).parent))
from llm_client import (  # noqa: E402,F401
    API_MODE,
    MODEL,
    llm,
    llm_chat_with_tools,
    llm_stream,
    llm_stream_chat_with_tools,
)
import mcp_web_search  # noqa: E402

logger = logging.getLogger(__name__)


# ============================================================
# 常量
# ============================================================

# 工具列表 —— 框架保留,具体工具数据先置空,后续按需添加
TOOLS: list[dict] = []

# ReAct 循环最大轮次,CLI 与 Web 共用同一上限,防止模型反复输出 Action 无限烧 token
MAX_ROUNDS = 5

# tool_result 事件 payload 中 result 字段超过此长度即截断(全文仍在 server log 里)
TOOL_RESULT_PREVIEW_CHARS = 500

# Phase 4 计划执行上限:每个 step 独立预算,避免整个 plan 被 MAX_ROUNDS=5 卡死。
PLAN_MAX_STEPS = 8
PLAN_STEP_MAX_ROUNDS = 3

# Phase 6 置信度信号。流式输出只在尾部疑似 confidence marker 时暂存,
# 避免固定尾缓冲破坏短答案逐字流式体验。
CONFIDENCE_LOW_THRESHOLD = 0.55
CONFIDENCE_MEDIUM_THRESHOLD = 0.8
CONFIDENCE_REASON_MAX_CHARS = 240
# Phase 7a: 尾部缓冲上限,超过此长度立即 flush,防止短答案被完整缓冲。
# 略大于 CONFIDENCE_REASON_MAX_CHARS + marker 开销(~60)。
_CONFIDENCE_TAIL_MAX = 300


# ============================================================
# 真 shell 执行开关 (默认关闭,demo 安全态)
# ============================================================
# 仅在用户显式 export ALLOW_REAL_SHELL=1 时,execute_shell_command + approve 路径
# 才会真正调起 subprocess。否则保持 demo stub 行为 —— 这是 demo 长期以来的
# non-goal,故意不真执行 shell,避免被 clone 后默认变成 RCE 风险。
# 开关只影响 _resume_inner 中 approve 分支;reject 与 CLI 短路逻辑都不动。
# 与 API_MODE / MODEL 一致,采用 env-once-at-import 模式,改值需重启进程。
ALLOW_REAL_SHELL = os.getenv("ALLOW_REAL_SHELL", "0") == "1"

# 真执行时:30s 硬超时,8KB 输出截断后再喂模型(模型仍能看到截断标记)。
SHELL_EXEC_TIMEOUT_SEC = 30
SHELL_EXEC_OUTPUT_MAX_CHARS = 8000

# 最小护栏:命令字符串包含下列任一关键词即拒绝。黑名单不是绝对防御,
# 只是把"误删 / 反弹 shell / 篡改系统"类常见动作挡住;白名单太严会让
# demo 失去演示价值。真正的隔离应该交给容器 / 虚拟用户 / 沙箱。
_SHELL_DENY_PATTERNS = (
    "rm -rf", "mkfs", "dd if=", " :(){", "shutdown", "reboot",
    "sudo ", "curl ", "wget ", "| sh", "| bash",
    "/etc/passwd", "/etc/shadow", ">/dev/sda", "chmod 777 /",
)


def _looks_dangerous(cmd: str) -> str | None:
    """命中黑名单返回拒绝原因字符串(命中的 pattern);否则返回 None。"""
    low = cmd.lower()
    for pat in _SHELL_DENY_PATTERNS:
        if pat in low:
            return pat
    return None


async def _execute_shell_real(cmd: str) -> str:
    """真正执行 shell 命令并返回拼装好的 tool_result 字符串。

    返回字符串会作为 role=tool 的 content 直接喂给模型,所以前缀加 [exit=N]
    让模型能区分成功/失败。stderr 合并到 stdout,避免模型只看到一半。
    超时 / 黑名单 / 异常都不抛,统一转成可读字符串,与 mcp_web_search 的
    `工具调用失败: ...` 失败语义一致 —— 让模型在 ReAct 循环里能恢复。
    """
    blocked = _looks_dangerous(cmd)
    if blocked:
        logger.warning("shell denied: pattern=%r cmd=%s", blocked, cmd)
        return (
            f"[拒绝执行] 命令包含敏感模式 {blocked!r},已被本地黑名单拦截。"
            "如确需执行,请人工在 shell 直接运行或调整黑名单。"
        )

    logger.info("shell exec start: %s", cmd)
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except Exception as e:
        logger.exception("shell spawn failed: %s", cmd)
        return f"[执行失败] 无法启动子进程: {e}"

    # 有界读取:只从 stdout 管道读 MAX+1 字节作探针,内存峰值 ~8KB 而不是无界。
    # 不用 proc.communicate() —— 它会把全部 stdout 读进内存,遇到 `yes` 等
    # 高速产出命令时 30s 窗口内能堆 GB 级数据,直接 OOM 整个 server。
    try:
        raw = await asyncio.wait_for(
            proc.stdout.read(SHELL_EXEC_OUTPUT_MAX_CHARS + 1),
            timeout=SHELL_EXEC_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("shell timeout after %ss: %s", SHELL_EXEC_TIMEOUT_SEC, cmd)
        return f"[超时] 命令执行超过 {SHELL_EXEC_TIMEOUT_SEC}s,已强制终止: {cmd}"

    raw_len = len(raw)
    truncated = raw_len > SHELL_EXEC_OUTPUT_MAX_CHARS
    if truncated:
        # 还有未读字节 → 命令仍可能在产出 → 杀掉进程止血,管道里剩余数据随进程关闭自动 GC
        proc.kill()
        await proc.wait()
        text = raw[:SHELL_EXEC_OUTPUT_MAX_CHARS].decode("utf-8", errors="replace")
        text += "\n...(输出过长,已截断)"
    else:
        await proc.wait()
        text = raw.decode("utf-8", errors="replace")

    logger.info(
        "shell exec done: exit=%s out_bytes=%d truncated=%s cmd=%s",
        proc.returncode, raw_len, truncated, cmd,
    )
    return f"[exit={proc.returncode}]\n{text}"


# ============================================================
# HITL (Human-in-the-Loop) 基础设施
# ============================================================
# 仅在 API_MODE=chat 路径生效:模型调用 LOCAL_TOOLS 中的工具时,业务层暂停 ReAct,
# 把恢复点存进 _PENDING[session_id],SSE 流以 await_user + done 关闭;
# 前端拿到用户操作后 POST /api/resume,新启一条 SSE 流接续 _stream_react_rounds。

_PENDING: dict[str, dict] = {}

# 本地伪工具(不走 MCP)。schema 直接喂给 OpenAI tools 字段,模型按 native function calling 调用。
LOCAL_TOOLS: dict[str, dict] = {
    "ask_user": {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "当你信息不足、需要用户澄清时调用。前端会展示输入框等用户答复,"
                "答复作为本工具结果回传给你。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "向用户提的问题"},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选,推荐选项列表,前端会渲染成快捷按钮",
                    },
                },
                "required": ["question"],
            },
        },
    },
    "execute_shell_command": {
        "type": "function",
        "function": {
            "name": "execute_shell_command",
            "description": (
                "执行 shell 命令(危险操作,需用户审批)。**前端会展示同意/拒绝按钮**,"
                "用户同意才会执行,拒绝时你会收到拒绝原因。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "完整 shell 命令"},
                    "reason": {"type": "string", "description": "为何要执行,展示给用户做决策"},
                },
                "required": ["command", "reason"],
            },
        },
    },
    "create_plan": {
        "type": "function",
        "function": {
            "name": "create_plan",
            "description": (
                "当用户任务较复杂、需要多步搜索/比较/执行时调用。前端会展示可编辑计划,"
                "用户确认后再逐步执行。简单问答不要调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "计划标题"},
                    "steps": {
                        "type": "array",
                        "description": "计划步骤,每步至少包含 title,可选 id。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "title": {"type": "string"},
                            },
                            "required": ["title"],
                        },
                    },
                },
                "required": ["title", "steps"],
            },
        },
    },
}

# 每个 LOCAL_TOOL 的前端交互类型:input(等用户输入回答) | approval(等同意/拒绝)
_LOCAL_TOOL_KIND: dict[str, str] = {
    "ask_user": "input",
    "execute_shell_command": "approval",
    "create_plan": "plan",
}


# 本地立即工具:模型用 native function calling 输出声明式 UI JSON,业务层不走 HITL 也不走 MCP。
RENDER_UI_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "render_ui",
        "description": (
            "当回答需要结构化展示(表格、对比、步骤、卡片等)时调用。"
            "输出受控 JSON UI 描述,前端会按组件目录渲染;不要输出 HTML、CSS 或 JS。"
            "仅在结构化 UI 比纯文本更直观时使用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "surface_id": {
                    "type": "string",
                    "description": "UI 表面唯一 ID,同一轮内应简短稳定,如 flight_compare",
                },
                "components": {
                    "type": "array",
                    "description": (
                        "扁平组件列表,通过 children 数组引用构建树;必须包含 id='root' 的根节点。"
                        "支持 type: text/card/row/column/table/button,以及表单组件 "
                        "text_field/select/toggle。button 可带 action: {event_name, context?} "
                        "供前端点击后回传。"
                        "表单组件 schema: "
                        "text_field {id, type:'text_field', value_path, label?, placeholder?, "
                        "input_type?:'shortText'|'longText'|'number'};"
                        "select {id, type:'select', value_path, options:[{label,value}], "
                        "label?, multiple?};"
                        "toggle {id, type:'toggle', value_path, label?}。"
                        "表单字段的 value_path 是 JSON Pointer,绑定到 surface.data;"
                        "用户提交按钮时,前端会把 surface.data 整体作为 form_data 回传。"
                    ),
                    "items": {
                        "type": "object",
                        "required": ["id", "type"],
                    },
                },
                "data": {
                    "type": "object",
                    "description": (
                        "可选数据对象;表单组件的 value_path 也可用此初始化默认值,"
                        "table.rows_path 与 text.path 用 JSON Pointer 引用。"
                    ),
                },
                "complete": {
                    "type": "boolean",
                    "description": (
                        "true=一次性完整渲染(默认,覆盖语义);"
                        "false=流式增量(多次调用按 id 合并到同一 surface,"
                        "最后一次发出后前端见 begin_rendering 信号确认渲染完成)。"
                    ),
                    "default": True,
                },
            },
            "required": ["surface_id", "components"],
        },
    },
}


UPDATE_UI_DATA_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "update_ui_data",
        "description": (
            "更新当前声明式 UI surface 的数据模型。用于用户点击按钮后刷新表格、文本或状态。"
            "仅更新 data,不重建 components;path 使用 JSON Pointer,如 /flights/0/price。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "surface_id": {
                    "type": "string",
                    "description": "要更新的 UI surface ID,必须来自之前 render_ui 创建的 surface。",
                },
                "path": {
                    "type": "string",
                    "description": "JSON Pointer 路径。/ 表示替换整个 data 对象。",
                },
                "value": {
                    "description": "写入到 path 的新值,可为字符串、数字、布尔、对象、数组或 null。",
                },
            },
            "required": ["surface_id", "path", "value"],
        },
    },
}


# ============================================================
# Prompt 模板
# ============================================================

USER_PROMPT = """# 角色设定
你是一位友好、专业的 AI 智能助手，能够帮助用户解答各类问题。

## 能力
1. 理解用户的自然语言输入，进行多轮对话；
2. **内置联网搜索**：系统已为你接入联网搜索能力。当用户询问天气、新闻、股价、近期事件等需要实时信息的问题，或你不确定的事实性问题时，请获取最新信息并基于搜索结果作答；**不要**输出 Thought/Action 文本，也不要回复"无法获取实时数据"；
3. **澄清提问**：当用户问题关键信息不足（语言/版本/范围/偏好等）时，调用 `ask_user` 工具向用户索取具体信息，可在 `options` 中给出推荐选项，**不要凭空假设**；
4. **危险操作审批**：涉及执行命令、删除数据、修改系统等敏感动作时，调用 `execute_shell_command` 工具发起审批，**不要**直接回复"我无法执行"或自行假设结果；
5. **声明式 UI**：当表格、对比卡片、按钮或**表单（多字段输入）**能让回答更清楚时，调用 `render_ui`；表单字段（`text_field`/`select`/`toggle`）通过 `value_path`（JSON Pointer）绑定到 surface 的 `data`，用户点击按钮提交时表单数据会一并回传给你；如需刷新已有界面数据，调用 `update_ui_data`；
6. **协作式计划**：当任务明显需要多个步骤或用户确认执行顺序时，调用 `create_plan` 提交计划给用户审阅；计划确认后再逐步执行；
7. 当下方"工具列表"中有自定义工具时，按 Thought/Action/Observation 协议调用；
8. **文件附件处理**：用户消息后可能跟随若干 `[附件 N] <文件名> (<mime>, <字符数>)` + ` ``` ` 代码块。它们是用户随消息上传的文本文件(代码/数据/文档),优先围绕附件内容回答。若代码块结尾出现 `...(已截断, 原 N 字符)`,意味着附件被裁剪,**回答前主动提醒用户**哪些部分不可见,必要时建议拆分上传。同时存在图片与附件时,按问题指向选择重点:代码 review / 数据分析 → 看附件;视觉理解 / OCR → 看图片。
9. 既不需要联网也不需要自定义工具时，直接给出清晰、有帮助的回复。

## 行为准则
- 回复简洁明了，避免冗余；
- 如果不确定答案，如实告知用户；
- 回复时根据内容选择最合适的展现方式；
- 每次最终回答末尾追加一行置信度标记,格式严格为 `[confidence: 0.0-1.0 | reason: 简短原因]`；reason 请控制在 80 字以内；该标记供系统解析,不要把它解释为正文内容。"""


# ============================================================
# Memory - 多轮对话记忆
# ============================================================

class Memory:
    USER = "用户"
    AI = "AI"

    # 用于 markdown 持久化的 turn 定界符正则;'用户' / 'AI' 必须严格独占一行
    _TURN_RE = re.compile(r"^<!-- turn: (用户|AI) -->\s*$", re.MULTILINE)

    def __init__(self):
        self.memories = []

    def add(self, role, msg):
        self.memories.append({"role": role, "msg": msg})

    def get_all(self):
        return "".join(f"{m['role']}: {m['msg']}\n" for m in self.memories)

    def to_markdown(self, session_id):
        """序列化为 markdown:头部 4 行 HTML 注释存元信息,正文按 turn 分块。"""
        updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
        preview = ""
        for m in self.memories:
            if m["role"] == self.USER:
                preview = m["msg"].strip().replace("\n", " ")[:20]
                break

        lines = [
            f"<!-- session_id: {session_id} -->",
            f"<!-- updated_at: {updated_at} -->",
            f"<!-- turns: {len(self.memories)} -->",
            f"<!-- preview: {preview} -->",
            "",
        ]
        for m in self.memories:
            lines.append(f"<!-- turn: {m['role']} -->")
            lines.append(m["msg"])
            lines.append("")
        return "\n".join(lines)

    @classmethod
    def from_markdown(cls, text):
        """从 markdown 文本反序列化。解析异常或文本为空时返回空 Memory。"""
        mem = cls()
        try:
            matches = list(cls._TURN_RE.finditer(text))
            for i, m in enumerate(matches):
                role = m.group(1)
                start = m.end()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
                msg = text[start:end].strip()
                if msg:
                    mem.add(role, msg)
        except Exception:
            logger.exception("Memory.from_markdown 解析失败,返回空 Memory")
            return cls()
        return mem


# ============================================================
# 会话存储
# ============================================================

# 归档目录与 session_id 安全校验
ARCHIVE_DIR = Path(__file__).parent.parent / "data" / "chat_archive"
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
RUNTIME_STATE_DIR = Path(__file__).parent.parent / "data" / "runtime_state"
RUNTIME_STATE_DIR.mkdir(parents=True, exist_ok=True)
RUNTIME_STATE_VERSION = 1
# 标准 UUID 形态:8-4-4-4-12 hex 字符。所有 disk 操作前必须校验,防止路径注入
SESSION_ID_RE = re.compile(r"^[0-9a-f-]{36}$")
# 头部元信息注释格式
_META_RE = re.compile(r"^<!-- (session_id|updated_at|turns|preview): (.*?) -->$")

# 模块级会话内存缓存
sessions: dict[str, Memory] = {}

# Phase 3b:声明式 UI 的进程内 surface registry。
# key=session_id, value={surface_id: {components, data, actions}}
# 不进入 Memory / markdown 归档,刷新页面或重启服务后自然丢失。
_UI_SURFACES: dict[str, dict[str, dict]] = {}

# Phase 4:Plan-and-Execute 的进程内计划状态。
# key=session_id, value={plan_id: plan_dict};不进入 Memory / markdown 归档。
_PLANS: dict[str, dict[str, dict]] = {}
_PLAN_SEQ = 0
_RUNTIME_RESTORED: set[str] = set()

# Phase 6:低置信度回答草稿。只存在进程内,不进入 Memory / archive / runtime_state。
_ANSWER_DRAFTS: dict[str, dict[str, dict]] = {}
_DRAFT_SEQ = 0

# Phase 7a M6: 并发流互斥。每个 session 一个 asyncio.Lock,防止多个 SSE 流同时操作共享状态。
_SESSION_LOCKS: dict[str, asyncio.Lock] = {}


def get_session_lock(session_id: str) -> asyncio.Lock:
    """返回(必要时创建)指定 session 的 asyncio.Lock。"""
    if session_id not in _SESSION_LOCKS:
        _SESSION_LOCKS[session_id] = asyncio.Lock()
    return _SESSION_LOCKS[session_id]


# ============================================================
# Phase 9a Agent Steering —— 进程内 steer 队列 + 历史
# ============================================================
# 每个 session 一个 asyncio.Queue, 用户 POST /api/steer 时入队,
# _stream_react_rounds / _stream_plan_step_rounds 在轮次边界 drain。
#
# 设计要点:
# - 不持久化(进程退出后未消费的 steer 无意义, 不写 runtime sidecar);
# - 懒创建在消费侧首次访问时, 生产侧用同一 accessor, 保证 Queue 绑定到唯一事件循环;
# - 单 turn drain 是非阻塞的, 不会卡住 ReAct loop;
# - 历史只为前端 agent_state_snapshot/delta 展示,固定上限防爆;
# - 不获取 _SESSION_LOCKS —— 否则与活跃 SSE 流死锁。
_STEER_QUEUES: dict[str, asyncio.Queue] = {}
_STEER_HISTORY: dict[str, list[dict]] = {}
_STEER_HISTORY_MAX = 10


def get_steer_queue(session_id: str) -> asyncio.Queue:
    """懒创建并返回指定 session 的 steer 队列。

    生产侧 (POST /api/steer) 与消费侧 (_stream_react_rounds) 共用同一 accessor,
    保证 asyncio.Queue 绑定到唯一事件循环 (FastAPI/uvicorn 单循环亦然)。
    """
    if session_id not in _STEER_QUEUES:
        _STEER_QUEUES[session_id] = asyncio.Queue()
    return _STEER_QUEUES[session_id]


def _drain_steers(session_id: str) -> list[str]:
    """非阻塞抽干指定 session 的 steer 队列, 返回所有累计 steer 消息(可能为空 list)。

    在 _stream_react_rounds / _stream_plan_step_rounds 每轮顶部调用。
    """
    queue = _STEER_QUEUES.get(session_id)
    if queue is None:
        return []
    drained: list[str] = []
    while True:
        try:
            drained.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return drained


def _record_steer(session_id: str, message: str) -> None:
    """把消费过的 steer 写入历史(供 agent_state_snapshot/delta 展示), 超上限丢最旧。"""
    history = _STEER_HISTORY.setdefault(session_id, [])
    history.append({
        "ts": datetime.now().isoformat(timespec="seconds"),
        "message": message,
    })
    if len(history) > _STEER_HISTORY_MAX:
        del history[: len(history) - _STEER_HISTORY_MAX]


def _can_append_user_message(messages: list[dict]) -> bool:
    """检查 messages 末尾是否允许追加 role=user。

    OpenAI 约束: 若末尾是带 tool_calls 的 assistant 消息, 下一条必须是 role=tool;
    此时 steer 必须延后到 tool 消费完毕后再注入, 否则 LLM 会 400。
    """
    if not messages:
        return True
    last = messages[-1]
    if last.get("role") == "assistant" and last.get("tool_calls"):
        return False
    return True


# Phase 7b: AG-UI / A2UI 协议标签映射。
# 在 SSE payload 中注入标准协议字段,前端不依赖(向后兼容),仅作标准对齐文档标记。
_SSE_PROTOCOL_TAGS: dict[str, dict[str, str]] = {
    "status":            {"ag_ui_type": "step_started"},
    "chunk":             {"ag_ui_type": "text_message_content"},
    "tool_call":         {"ag_ui_type": "tool_call_start"},
    "tool_result":       {"ag_ui_type": "tool_call_end"},
    "await_user":        {"ag_ui_type": "interrupt"},
    "ui_surface_create": {"a2ui_type": "surfaceUpdate"},
    "ui_surface_update": {"a2ui_type": "surfaceUpdate"},
    "ui_data_update":    {"a2ui_type": "dataModelUpdate"},
    "activity_snapshot": {"ag_ui_type": "state_snapshot"},
    "activity_delta":    {"ag_ui_type": "state_delta"},
    "done":              {"ag_ui_type": "run_finished"},
    "error":             {"ag_ui_type": "run_error"},
    # 无标准映射的事件: thinking, search_status, component_loading,
    # render_component, component_error, confidence_signal, ui_hint, begin_rendering
}


def _with_protocol_tags(event_name: str, payload: dict) -> dict:
    """将 AG-UI/A2UI 协议标签注入 SSE payload(非变异)。"""
    tags = _SSE_PROTOCOL_TAGS.get(event_name)
    if not tags:
        return payload
    return {**payload, **tags}


class InvalidSessionId(ValueError):
    """session_id 不符合 UUID 形态。HTTP 层应翻译为 400。"""


class HistoryNotFound(LookupError):
    """指定 session_id 没有归档文件。HTTP 层应翻译为 404。"""


class PendingNotFound(LookupError):
    """session 当前无 pending HITL(可能已被新 chat 清掉,或本来就没有)。HTTP 层应翻译为 404。"""


class PendingMismatch(ValueError):
    """resume 提交的 tool_call_id 与 pending awaiting.tool_call_id 不匹配,
    通常意味着前端拿了过期的 HITL bubble 提交。HTTP 层应翻译为 409。"""


class UiActionUnavailable(ValueError):
    """当前 API_MODE 不支持 UI action。HTTP 层应翻译为 400。"""


class UiSurfaceNotFound(LookupError):
    """声明式 UI surface 不存在或已过期。HTTP 层应翻译为 404。"""


class UiActionNotFound(LookupError):
    """surface 上没有指定 component/action。HTTP 层应翻译为 404。"""


class UiActionMismatch(ValueError):
    """前端提交的 event_name 与后端 registry 不一致。HTTP 层应翻译为 409。"""


class PlanUnavailable(ValueError):
    """当前 API_MODE 不支持 Plan-and-Execute。HTTP 层应翻译为 400。"""


class PlanNotFound(LookupError):
    """计划不存在或已过期。HTTP 层应翻译为 404。"""


class PlanStateMismatch(ValueError):
    """计划状态与当前操作不匹配。HTTP 层应翻译为 409。"""


class PlanValidationError(ValueError):
    """计划请求体不合法。HTTP 层应翻译为 400。"""


class DraftNotFound(LookupError):
    """低置信度草稿不存在或已处理。HTTP 层应翻译为 404。"""


class SteerUnavailable(ValueError):
    """steer 不可用 —— 非 chat 模式 / session_id 非法 / message 为空。HTTP 层应翻译为 400。"""


def _archive_path(session_id: str) -> Path:
    if not SESSION_ID_RE.match(session_id):
        raise InvalidSessionId(session_id)
    return ARCHIVE_DIR / f"{session_id}.md"


def _runtime_state_path(session_id: str) -> Path:
    if not SESSION_ID_RE.match(session_id):
        raise InvalidSessionId(session_id)
    return RUNTIME_STATE_DIR / f"{session_id}.json"


def _atomic_write(path: Path, text: str) -> None:
    """先写 .tmp 再 rename,避免崩溃留下半文件。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _sync_plan_seq_from(plans: dict[str, dict]) -> None:
    """恢复 runtime state 后推进 plan 序号,避免新 plan_id 与旧计划碰撞。"""
    global _PLAN_SEQ
    for plan_id in plans:
        m = re.fullmatch(r"plan_(\d+)", str(plan_id))
        if m:
            _PLAN_SEQ = max(_PLAN_SEQ, int(m.group(1)))


def _runtime_state_payload(session_id: str) -> dict:
    return {
        "version": RUNTIME_STATE_VERSION,
        "session_id": session_id,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "pending": _PENDING.get(session_id),
        "ui_surfaces": _UI_SURFACES.get(session_id, {}),
        "plans": _PLANS.get(session_id, {}),
    }


def _save_runtime_state(session_id: str) -> None:
    """保存 session 运行态。三类运行态都为空时删除 sidecar。"""
    path = _runtime_state_path(session_id)
    has_state = (
        session_id in _PENDING
        or bool(_UI_SURFACES.get(session_id))
        or bool(_PLANS.get(session_id))
    )
    if not has_state:
        path.unlink(missing_ok=True)
        return
    try:
        _atomic_write(path, json.dumps(_runtime_state_payload(session_id), ensure_ascii=False, indent=2))
    except Exception:
        logger.exception("保存 runtime state 失败: %s", path)


def _delete_runtime_state(session_id: str) -> None:
    path = _runtime_state_path(session_id)
    path.unlink(missing_ok=True)
    _RUNTIME_RESTORED.discard(session_id)


def _restore_runtime_state(session_id: str) -> None:
    """按 session 懒恢复运行态。损坏或版本不兼容时忽略,不影响普通聊天。

    Phase 7a M7: _RUNTIME_RESTORED.add 移到函数末尾,避免部分失败后不再重试。
    Phase 7a M4: running 状态的 plan 恢复为 paused,使其可被前端继续。
    """
    if session_id in _RUNTIME_RESTORED:
        return
    path = _runtime_state_path(session_id)
    if not path.exists():
        _RUNTIME_RESTORED.add(session_id)
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("runtime state 损坏,已忽略: %s", path, exc_info=True)
        _RUNTIME_RESTORED.add(session_id)
        return
    if data.get("version") != RUNTIME_STATE_VERSION or data.get("session_id") != session_id:
        logger.warning("runtime state 版本或 session 不匹配,已忽略: %s", path)
        _RUNTIME_RESTORED.add(session_id)
        return

    try:
        pending = data.get("pending")
        if isinstance(pending, dict) and isinstance(pending.get("awaiting"), dict):
            _PENDING[session_id] = pending

        surfaces = data.get("ui_surfaces")
        if isinstance(surfaces, dict):
            safe_surfaces: dict[str, dict] = {}
            for surface_id, surface in surfaces.items():
                if not isinstance(surface_id, str) or not isinstance(surface, dict):
                    continue
                components = surface.get("components")
                data_obj = surface.get("data")
                if not isinstance(components, list):
                    continue
                safe_surface = {
                    "components": components,
                    "data": data_obj if isinstance(data_obj, dict) else {},
                    "actions": surface.get("actions") if isinstance(surface.get("actions"), dict) else {},
                }
                if not safe_surface["actions"]:
                    try:
                        safe_surface["actions"] = _extract_ui_actions(components)
                    except Exception:
                        logger.warning("ui_actions 提取失败,已跳过: %s", surface_id, exc_info=True)
                        safe_surface["actions"] = {}
                safe_surfaces[surface_id] = safe_surface
            if safe_surfaces:
                _UI_SURFACES[session_id] = safe_surfaces

        plans = data.get("plans")
        if isinstance(plans, dict):
            safe_plans = {
                str(plan_id): plan
                for plan_id, plan in plans.items()
                if isinstance(plan_id, str)
                and isinstance(plan, dict)
                and isinstance(plan.get("steps"), list)
                and isinstance(plan.get("plan_id"), str)
                and isinstance(plan.get("title"), str)
                and isinstance(plan.get("status"), str)
            }
            if safe_plans:
                # Phase 7a M4: 将 running 状态的 plan 恢复为 paused,
                # 并将中断的步骤标记为 error,使其可被前端继续执行。
                for plan in safe_plans.values():
                    if plan.get("status") == "running":
                        plan["status"] = "paused"
                        for step in plan.get("steps", []):
                            if isinstance(step, dict) and step.get("status") not in {"done", "skipped"}:
                                step["status"] = "error"
                                step.setdefault("error_message", "服务重启，步骤执行被中断")
                                break  # 只标记第一个未完成步骤
                _PLANS[session_id] = safe_plans
                _sync_plan_seq_from(safe_plans)
    except Exception:
        logger.warning("runtime state 结构损坏,已忽略: %s", path, exc_info=True)
    _RUNTIME_RESTORED.add(session_id)


def _plan_runtime_snapshot(plan: dict) -> dict:
    status = plan.get("status") or "pending_confirmation"
    current_idx = plan.get("current_step_index", 0)
    step = None
    if isinstance(current_idx, int) and 0 <= current_idx < len(plan.get("steps", [])):
        step = plan["steps"][current_idx]
    continuable = (
        status == "running"
        and isinstance(plan.get("execution_messages"), list)
        and isinstance(current_idx, int)
    )
    return {
        **_plan_snapshot(plan, editable=status == "pending_confirmation"),
        "decision_required": status == "paused",
        "continuable": continuable,
        "step_id": step.get("id") if status == "paused" and isinstance(step, dict) else None,
        "error_message": step.get("error_message") if status == "paused" and isinstance(step, dict) else None,
    }


def runtime_state_snapshot(session_id: str) -> dict:
    """给前端恢复 UI 用的安全快照,不暴露 messages/tools 等后端恢复细节。"""
    _restore_runtime_state(session_id)
    pending = _PENDING.get(session_id)
    awaiting = pending.get("awaiting") if isinstance(pending, dict) else None
    return {
        "session_id": session_id,
        "pending": awaiting if isinstance(awaiting, dict) else None,
        "surfaces": [
            {
                "surface_id": surface_id,
                "components": surface.get("components") or [],
                "data": surface.get("data") or {},
            }
            for surface_id, surface in (_UI_SURFACES.get(session_id) or {}).items()
        ],
        "plans": [
            _plan_runtime_snapshot(plan)
            for plan in (_PLANS.get(session_id) or {}).values()
            if isinstance(plan, dict) and plan.get("status") != "done"
        ],
    }


def _agent_state_payload(
    session_id: str,
    *,
    round_num: int = 0,
    tool_stats: dict | None = None,
) -> dict:
    """构造 agent_state_snapshot/delta 的 payload —— 面向运行时观察的 Agent 全局态纲要。

    与 runtime_state_snapshot 的差异:
        - runtime_state_snapshot: 面向页面恢复的"全量" (含 components / steps 详情)
        - _agent_state_payload:   面向流式观察的"纲要" (含 round / tool_stats / steer_history 等
                                  局部事件凑不出的全局态; 不重复发 components / steps 那些
                                  已有自己 SSE 通道的重量级数据)
    """
    pending = _PENDING.get(session_id)
    awaiting = pending.get("awaiting") if isinstance(pending, dict) else None
    return {
        "session_id": session_id,
        "round": round_num,
        "tool_stats": dict(tool_stats or {}),
        "surfaces": [
            {"surface_id": sid, "component_count": len(s.get("components") or [])}
            for sid, s in (_UI_SURFACES.get(session_id) or {}).items()
        ],
        "plans": [
            {
                "plan_id": p.get("plan_id"),
                "title": p.get("title"),
                "status": p.get("status"),
                "step_count": len(p.get("steps") or []),
            }
            for p in (_PLANS.get(session_id) or {}).values()
        ],
        "pending": awaiting if isinstance(awaiting, dict) else None,
        "steer_history": list(_STEER_HISTORY.get(session_id) or []),
    }


def _read_meta(path: Path) -> dict:
    """只读文件头部连续的 HTML 注释行,返回元信息字典。遇到第一个空行即停。"""
    meta: dict = {}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                break
            m = _META_RE.match(line)
            if m:
                meta[m.group(1)] = m.group(2)
    return meta


def get_or_load(session_id: str) -> Memory:
    """命中内存直接返;否则尝试从 disk 读;最后回退空 Memory。结果写回 sessions 字典。"""
    _restore_runtime_state(session_id)
    if session_id in sessions:
        return sessions[session_id]
    path = _archive_path(session_id)  # 同时做 sid 校验
    if path.exists():
        try:
            mem = Memory.from_markdown(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("加载归档失败,回退空 Memory: %s", path)
            mem = Memory()
    else:
        mem = Memory()
    sessions[session_id] = mem
    return mem


def _cleanup_session_runtime(session_id: str) -> None:
    """Phase 7a M5: 归档后清理已完成的 plan、已处理的 answer draft。保留 UI surfaces 和活跃 pending。"""
    # 清理已完成的计划
    session_plans = _PLANS.get(session_id)
    if isinstance(session_plans, dict):
        done_ids = [pid for pid, p in session_plans.items()
                    if isinstance(p, dict) and p.get("status") in {"done", "completed"}]
        for pid in done_ids:
            session_plans.pop(pid, None)
        if not session_plans:
            _PLANS.pop(session_id, None)
    # 清理已处理的草稿
    _ANSWER_DRAFTS.pop(session_id, None)
    # 更新或清理 sidecar
    if session_id in _PLANS or session_id in _PENDING or session_id in _UI_SURFACES:
        _save_runtime_state(session_id)
    else:
        _delete_runtime_state(session_id)


def archive_session(session_id: str) -> dict:
    """覆盖式归档当前 session 的 Memory 到 markdown。空 Memory 跳过。

    Phase 7a M5: 归档后清理已完成的 plan 和已处理的 answer draft。

    返回与 /api/archive 端点期望响应一致的 dict:
        - 空 Memory: {"ok": True, "skipped": True}
        - 已归档:   {"ok": True, "path": str(path)}
    """
    memory = get_or_load(session_id)
    if not memory.memories:
        return {"ok": True, "skipped": True}
    path = _archive_path(session_id)
    _atomic_write(path, memory.to_markdown(session_id))
    _cleanup_session_runtime(session_id)
    return {"ok": True, "path": str(path)}


def reset_session(session_id: str) -> None:
    """原地重置:清当前 session 内存 + 删归档文件,session_id 保留。"""
    _archive_path(session_id)  # 先做 sid 校验
    sessions.pop(session_id, None)
    _UI_SURFACES.pop(session_id, None)
    _PLANS.pop(session_id, None)
    _PENDING.pop(session_id, None)
    _ANSWER_DRAFTS.pop(session_id, None)
    # Phase 9 fix B1: session_id 保留 + 复用时, 旧 steer 不应残留 ——
    # 否则下次该 sid 的 /api/chat 第一轮顶部 drain 会把过期纠偏注入新对话。
    _STEER_QUEUES.pop(session_id, None)
    _STEER_HISTORY.pop(session_id, None)
    _delete_runtime_state(session_id)
    path = _archive_path(session_id)
    path.unlink(missing_ok=True)


def delete_session(session_id: str) -> None:
    """删除 session:磁盘归档 + 内存 session 同步移除。"""
    path = _archive_path(session_id)  # 同时做 sid 校验
    path.unlink(missing_ok=True)
    sessions.pop(session_id, None)
    _UI_SURFACES.pop(session_id, None)
    _PLANS.pop(session_id, None)
    _PENDING.pop(session_id, None)
    _ANSWER_DRAFTS.pop(session_id, None)
    # Phase 9 fix B1: 与 reset_session 同纲领;即便 session_id 通常不复用,
    # 也避免进程内 dict 泄漏(_STEER_HISTORY 占内存)。
    _STEER_QUEUES.pop(session_id, None)
    _STEER_HISTORY.pop(session_id, None)
    _delete_runtime_state(session_id)


def list_sessions() -> list[dict]:
    """列出所有归档过的 session,只读文件头元信息;按 updated_at 倒序。"""
    items: list[dict] = []
    for path in ARCHIVE_DIR.glob("*.md"):
        try:
            meta = _read_meta(path)
        except Exception:
            logger.exception("读取归档元信息失败,跳过: %s", path)
            continue
        sid = meta.get("session_id", "")
        if not SESSION_ID_RE.match(sid):
            continue  # 跳过损坏/可疑文件
        try:
            turns = int(meta.get("turns", "0"))
        except ValueError:
            turns = 0
        items.append({
            "session_id": sid,
            "preview": meta.get("preview", ""),
            "updated_at": meta.get("updated_at", ""),
            "turns": turns,
        })
    items.sort(key=lambda x: x["updated_at"], reverse=True)
    return items


def read_history(session_id: str) -> Memory:
    """返回 session 的 Memory。优先内存,再 disk lazy-load,都没有则抛 HistoryNotFound。"""
    _restore_runtime_state(session_id)
    if session_id in sessions and sessions[session_id].memories:
        return sessions[session_id]
    path = _archive_path(session_id)
    if not path.exists():
        raise HistoryNotFound(session_id)
    mem = Memory.from_markdown(path.read_text(encoding="utf-8"))
    sessions[session_id] = mem
    return mem


def get_archive_path_if_exists(session_id: str) -> Path:
    """返回归档文件路径,文件不存在抛 HistoryNotFound。"""
    path = _archive_path(session_id)  # 同时做 sid 校验
    if not path.exists():
        raise HistoryNotFound(session_id)
    return path


def session_count() -> int:
    return len(sessions)


# ============================================================
# ReAct 协议解析
# ============================================================

_ACTION_LINE = re.compile(r"^Action:\s*(\S.*?)\s*$", re.MULTILINE)


def match_tool_action(llm_result):
    """从 LLM 返回中匹配工具调用。要求 `Action:` 必须独占一行,工具名必须精确等于 TOOLS 中某项。"""
    match = _ACTION_LINE.search(llm_result)
    if not match:
        return None
    candidate = match.group(1).strip()
    for tool in TOOLS:
        if tool["name"] == candidate:
            return tool["name"]
    return None


def parse_action_input(llm_result):
    """从 LLM 返回中解析 Action Input 后的 JSON 对象。

    用 json.JSONDecoder().raw_decode 做大括号配平扫描,能正确处理多行 JSON、
    字符串中含 `}`、JSON 后面跟有其它文本等情况。
    """
    marker = "Action Input:"
    idx = llm_result.find(marker)
    if idx == -1:
        return {}
    tail = llm_result[idx + len(marker):]
    brace = tail.find("{")
    if brace == -1:
        return {}
    try:
        obj, _ = json.JSONDecoder().raw_decode(tail[brace:])
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError as exc:
        logger.warning("Action Input JSON 解析失败: %s", exc)
        return {}


def execute_tool(tool_name, params):
    """执行工具 —— 框架预留,当前工具列表为空。扩展时在此处加路由。"""
    logger.warning("未找到工具实现:%s", tool_name)
    return json.dumps({"error": f"工具 {tool_name} 暂未实现"}, ensure_ascii=False)


# ============================================================
# 上下文感知 —— Phase 2
# ============================================================


def _context_int(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _memory_user_message_count(memory: "Memory") -> int:
    return sum(1 for m in memory.memories if m.get("role") == Memory.USER)


def _compute_adaptive_prompt(context: dict | None, memory: "Memory") -> tuple[str, str, str]:
    """基于前端上报的上下文计算: (补充 system prompt 片段, ui_hint mode, reason)。

    仅做简单一维判断演示管道,不追求分类准确性。
    """
    ctx = context if isinstance(context, dict) else {}
    msg_count = _context_int(ctx.get("session_message_count"))
    if msg_count is None:
        msg_count = _memory_user_message_count(memory)
    viewport_width = _context_int(ctx.get("viewport_width"))
    selected = ctx.get("selected_text")
    selected_text = str(selected).strip() if selected is not None else ""

    if selected_text:
        return (
            f"用户选中了一段文本：「{selected_text[:200]}」，优先围绕选中内容回答。",
            "focus",
            "selected_text",
        )
    if viewport_width is not None and viewport_width <= 640:
        return ("当前视口较窄，回复保持紧凑，优先给出最关键的信息。", "compact", "narrow_viewport")
    if msg_count > 10:
        return ("对话已较长，保持简洁，避免重复已说过的内容。", "compact", "long_session")
    return ("", "chat", "default")


# ============================================================
# Prompt 拼装
# ============================================================

def build_prompt(user_prompt, tools, memory, latest_input, adaptive_fragment: str = ""):
    """拼装完整的 Prompt:
    - `tools == []`(常态):角色设定 + 对话记录 + 最新输入。完全不输出 ReAct 框架,
      避免"无工具"措辞反向压制 API 层的内置 web_search。
    - `tools` 非空:角色设定 + 工具列表 + ReAct 格式说明 + 注意事项 + 对话记录 + 最新输入。
      框架在 TOOLS 列表添加自定义工具时自动恢复。
    """
    prompt_head = user_prompt
    if adaptive_fragment:
        prompt_head = user_prompt + "\n\n" + adaptive_fragment
    sections = [prompt_head, "---------------------"]

    if tools:
        tool_names = ",".join(t["name"] for t in tools)
        sections.extend([
            "# 工具列表",
            json.dumps(tools, ensure_ascii=False),
            "",
            "使用如下格式：",
            "Thought: 思考并确定下一步的最佳行动方案",
            f"Action: 工具名称，必须是[{tool_names}]中的一个",
            "Action Input: 工具参数，一定必须是 JSON 对象",
            "Observation: 工具执行结果",
            "... (Thought/Action/Action Input/Observation 可以重复N次)",
            "",
            "注意：",
            "- 不使用工具时，回复中不要出现 Thought、Action、Action Input；",
            "- 使用工具前，先检查是否缺少必要参数，缺少必要参数时直接向用户提问，不要出现 Thought、Action、Action Input；",
            "- 工具执行遇到问题时，向用户寻求帮助；",
            "- 需要执行同一个工具多次时，Action Input 可以出现多次；",
            "---------------------",
        ])

    sections.extend([
        "# 对话记录",
        memory.get_all(),
        "",
        "# 最新输入",
        latest_input,
    ])
    return "\n".join(sections)


# ============================================================
# Chat 模式 native function calling 支持
# 与上面的"手写 Action:/Observation: 字符串协议"并存:
#   - API_MODE=responses(默认): 走 build_prompt + 内置 web_search,完全不动
#   - API_MODE=chat:           走 native tools=[...] / tool_calls 协议 + MCP WebSearch
# ============================================================

# 模块级缓存:首次调用 _get_native_tools 时通过 mcp_web_search.discover_tool_spec 填充
_NATIVE_TOOLS_CACHE: list[dict] | None = None


# ============================================================
# Phase 10b: 文本文件附件 —— prompt 拼装辅助
# ============================================================
# 与 web_chat_agent.MAX_ATTACHMENT_CHARS 对齐; 超此值业务层软截断, 不抛 (HTTP 层硬上限
# 200KB 才硬拒)。教学定位: 让大附件可见但提醒模型该附件不完整, 而非直接 400 把用户推走。
MAX_PER_FILE_CHARS_SOFT = 20 * 1024

# mime_type → markdown 代码块语言标识 (优先级高于扩展名)
_MIME_TO_LANG = {
    "application/json": "json",
    "application/xml": "xml",
    "application/javascript": "javascript",
    "application/typescript": "typescript",
    "application/x-yaml": "yaml",
    "text/x-python": "python",
    "text/x-go": "go",
    "text/x-rust": "rust",
    "text/x-java": "java",
    "text/x-c": "c",
    "text/x-c++": "cpp",
    "text/x-typescript": "typescript",
    "text/x-yaml": "yaml",
    "text/x-shellscript": "bash",
    "text/markdown": "markdown",
    "text/csv": "csv",
    "text/html": "html",
    "text/css": "css",
    "text/javascript": "javascript",
}
# 扩展名 fallback (mime_type 未命中时用; 浏览器对很多扩展返回空 mime)
_EXT_TO_LANG = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".md": "markdown", ".json": "json", ".csv": "csv",
    ".html": "html", ".css": "css", ".xml": "xml",
    ".yaml": "yaml", ".yml": "yaml", ".sh": "bash",
    ".go": "go", ".rs": "rust", ".java": "java",
    ".c": "c", ".cpp": "cpp", ".h": "c",
}


def _max_backtick_run(text: str) -> int:
    """返回 text 中连续反引号的最大数量。用于决定外层 fence 长度。"""
    import re as _re
    runs = _re.findall(r"`+", text)
    return max((len(r) for r in runs), default=0)


def _build_attachment_block(attachments: list[dict]) -> str:
    """把校验过的 attachments 列表序列化为人类可读 + LLM 友好的 markdown 文本块。

    格式 (注入位置在 user_input **之后**, 避免长附件挤出短问题的注意力):

        [以下是用户附带的 N 个文件]

        [附件 1] file.py (text/x-python, 2345 字符)
        ```python
        ...content...
        ```

        [附件 2] data.csv (text/csv, 512 字符)
        ```csv
        ...content...
        ```

    单文件超过 MAX_PER_FILE_CHARS_SOFT 时软截断, 加 `...(已截断, 原 N 字符)` 尾标。
    fence 长度自适应: 若 content 含 ``` 等反引号序列, 外层 fence 自动加长到比内层多 1
    (CommonMark fence 长度竞争规则), 防止内容提前关闭围栏。
    """
    lines: list[str] = [f"[以下是用户附带的 {len(attachments)} 个文件]"]
    for idx, att in enumerate(attachments, start=1):
        filename = att["filename"]
        mime_type = att["mime_type"]
        content = att["content"]
        original_len = len(content)
        truncated = False
        if original_len > MAX_PER_FILE_CHARS_SOFT:
            content = content[:MAX_PER_FILE_CHARS_SOFT]
            truncated = True
        # 语言标识: mime 优先, 扩展名 fallback
        lang = _MIME_TO_LANG.get(mime_type, "")
        if not lang:
            ext = os.path.splitext(filename)[1].lower()
            lang = _EXT_TO_LANG.get(ext, "")
        # fence 长度自适应: 至少 3 个反引号, 若内容含更多则 +1
        fence_len = max(3, _max_backtick_run(content) + 1)
        fence = "`" * fence_len
        lines.append("")
        lines.append(f"[附件 {idx}] {filename} ({mime_type}, {original_len} 字符)")
        lines.append(f"{fence}{lang}")
        lines.append(content)
        if truncated:
            lines.append(f"...(已截断, 原 {original_len} 字符)")
        lines.append(fence)
    return "\n".join(lines)


def _memory_to_messages(
    memory: Memory, system_prompt: str, user_input: str, adaptive_fragment: str = "",
    images: list[str] | None = None,
    attachments: list[dict] | None = None,
) -> list[dict]:
    """把 flat-string Memory 转成 OpenAI Chat Completions messages 数组。

    仅用于 chat-mode-with-tools 路径。Memory 存储格式不变(仍是 role/msg 二元组)。
    角色映射: 用户 -> user, AI -> assistant; 不携带历史 tool_calls(单 turn 内才需要)。

    Phase 10a: 若 `images` 非空, 把最后一条 user 节点的 content 从纯 str 改为
    OpenAI vision content parts 数组 [{type:"text",...}, {type:"image_url",...}*N]。
    历史 turn 的 content 保持 str (Memory 不存图, 自然只能是文本)。
    `llm_client._llm_stream_chat_with_tools` 透传 messages 给 OpenAI SDK,
    SDK 对 str/list 两种 content 都原生支持, 无需任何修改 —— 这是协议无感的优雅样本。

    Phase 10b: 若 `attachments` 非空, 把 _build_attachment_block 拼出的 markdown
    代码块文本追加到 user_input **之后**(不是之前 —— 长附件在前会把短问题挤出
    模型注意力窗口尾部); 与 images 正交, 两者可共存。
    """
    sys_content = system_prompt
    if adaptive_fragment:
        sys_content = system_prompt + "\n\n" + adaptive_fragment
    messages: list[dict] = [{"role": "system", "content": sys_content}]
    for m in memory.memories:
        role = "user" if m["role"] == Memory.USER else "assistant"
        messages.append({"role": role, "content": m["msg"]})
    # Phase 10b: 附件追加在 user_input 后
    text_part = user_input
    if attachments:
        text_part = f"{user_input}\n\n{_build_attachment_block(attachments)}"
    if images:
        user_content: list[dict] = [{"type": "text", "text": text_part}]
        for url in images:
            user_content.append({"type": "image_url", "image_url": {"url": url}})
        messages.append({"role": "user", "content": user_content})
    else:
        messages.append({"role": "user", "content": text_part})
    return messages


def _build_native_tools() -> list[dict]:
    """同步:首次调用走 mcp_web_search 的 schema 发现 + 合并 LOCAL_TOOLS 并缓存。CLI ReAct 循环用。

    LOCAL_TOOLS 在 CLI 模式下也对模型可见 —— 由 _react_chat_native 在执行时短路,
    避免 CLI/Web 两条路径的 tools 列表语义分裂。
    """
    global _NATIVE_TOOLS_CACHE
    if _NATIVE_TOOLS_CACHE is None:
        mcp_tools = mcp_web_search.discover_tool_spec()
        _warn_unmatched_component_tools(mcp_tools)
        _NATIVE_TOOLS_CACHE = [*mcp_tools, RENDER_UI_TOOL, UPDATE_UI_DATA_TOOL, *LOCAL_TOOLS.values()]
    return _NATIVE_TOOLS_CACHE


async def _build_native_tools_async() -> list[dict]:
    """异步:首次调用走 mcp_web_search 的 schema 发现 + 合并 LOCAL_TOOLS 并缓存。Web ReAct 循环用。"""
    global _NATIVE_TOOLS_CACHE
    if _NATIVE_TOOLS_CACHE is None:
        mcp_tools = await mcp_web_search.discover_tool_spec_async()
        _warn_unmatched_component_tools(mcp_tools)
        _NATIVE_TOOLS_CACHE = [*mcp_tools, RENDER_UI_TOOL, UPDATE_UI_DATA_TOOL, *LOCAL_TOOLS.values()]
    return _NATIVE_TOOLS_CACHE


def _truncate_tool_result(text: str) -> str:
    """tool_result 事件的截断逻辑,与上面 stream_agent_response 中保持一致的措辞。"""
    if len(text) <= TOOL_RESULT_PREVIEW_CHARS:
        return text
    return text[:TOOL_RESULT_PREVIEW_CHARS] + f"...（截断，原文 {len(text)} 字符见 server log）"


_CONFIDENCE_MARKER_RE = re.compile(
    r"\s*\[\s*confidence\s*:\s*(?P<score>0(?:\.\d+)?|1(?:\.0+)?|\.\d+)\s*"
    r"(?:\|\s*reason\s*:\s*(?P<reason>[^\]]*))?\]\s*$",
    re.IGNORECASE,
)
_REALTIME_RE = re.compile(
    r"(今天|现在|当前|最新|实时|天气|新闻|股价|汇率|价格|today|current|latest|weather|news|stock|price)",
    re.IGNORECASE,
)


def _new_tool_stats() -> dict:
    return {"tool_calls": 0, "tool_failures": 0, "search_calls": 0, "errors": 0}


def _confidence_level(score: float) -> str:
    if score < CONFIDENCE_LOW_THRESHOLD:
        return "low"
    if score < CONFIDENCE_MEDIUM_THRESHOLD:
        return "medium"
    return "high"


def _confidence_buffer_start(text: str) -> int | None:
    """若 text 尾部正在形成 confidence marker,返回应暂存的起点。

    Phase 7a 修复: 前缀门控 —— 只有当 candidate 至少为 "[c" 时才触发缓冲,
    避免任意 "[" (如引用 "[1]") 误触。同时增加长度守卫,超过上限立即放弃。
    """
    if not text:
        return None
    bracket = text.rfind("[")
    if bracket >= 0:
        candidate = text[bracket:].lower()
        # 前缀门控: candidate 必须是 "[confidence..." 或 "[confidence" 的真前缀(至少 "[c")
        if candidate.startswith("[confidence") or (
            len(candidate) >= 2 and "[confidence".startswith(candidate)
        ):
            # 长度守卫: 尾部已超上限,放弃缓冲立即 flush
            if len(text) - bracket > _CONFIDENCE_TAIL_MAX:
                return None
            start = bracket
            while start > 0 and text[start - 1].isspace():
                start -= 1
            return start

    line_start = max(text.rfind("\n"), text.rfind("\r")) + 1
    trailing_line = text[line_start:]
    if line_start > 0 and trailing_line.strip() == "":
        return line_start - 1
    return None


def _buffer_confidence_delta(tail: str, delta: str) -> tuple[str, str]:
    """仅暂存疑似 confidence marker 的尾部,其余内容立即流式输出。

    Phase 7a: 增加长度守卫,如果尾部已超 _CONFIDENCE_TAIL_MAX 立即 flush 全部。
    """
    combined = tail + delta
    start = _confidence_buffer_start(combined)
    if start is None:
        return "", combined
    # 长度守卫: 尾部超过上限,放弃缓冲立即 flush
    if len(combined) - start > _CONFIDENCE_TAIL_MAX:
        return "", combined
    return combined[start:], combined[:start]


def _extract_confidence_signal(text: str, tool_stats: dict | None = None, user_input: str = "") -> tuple[str, dict]:
    """解析模型自评 marker,并叠加轻量工具启发式。返回 clean_text + signal。"""
    stats = tool_stats or {}
    match = _CONFIDENCE_MARKER_RE.search(text or "")
    if match:
        clean_text = text[:match.start()].rstrip()
        try:
            score = float(match.group("score"))
        except (TypeError, ValueError):
            score = 0.7
        reason = (match.group("reason") or "model_self_assessed").strip() or "model_self_assessed"
    else:
        clean_text = (text or "").rstrip()
        # Phase 7a M2: 二次清理 —— 如果尾部有未闭合的 [confidence 前缀,截掉
        _leak = re.search(r"\[\s*confidence[:\s][^]]*$", clean_text)
        if _leak:
            clean_text = clean_text[:_leak.start()].rstrip()
        score = 0.7
        reason = "model_unspecified"

    score = max(0.0, min(1.0, score))
    reasons = [reason]
    if stats.get("tool_failures", 0) > 0:
        score = min(score, 0.5)
        reasons.append("tool_failure")
    if stats.get("errors", 0) > 0:
        score = min(score, 0.45)
        reasons.append("stream_error")
    if _REALTIME_RE.search(user_input or "") and stats.get("search_calls", 0) == 0:
        score = min(score, 0.5)
        reasons.append("realtime_without_search")

    rounded = round(score, 2)
    return clean_text, {
        "score": rounded,
        "level": _confidence_level(rounded),
        "reason": "; ".join(dict.fromkeys(reasons))[:CONFIDENCE_REASON_MAX_CHARS],
        "draft": False,
        "draft_id": None,
    }


def _next_draft_id() -> str:
    global _DRAFT_SEQ
    _DRAFT_SEQ += 1
    return f"draft_{_DRAFT_SEQ:04d}"


def _register_answer_draft(session_id: str, user_input: str, answer: str, confidence: dict) -> str:
    if not SESSION_ID_RE.match(session_id):
        raise InvalidSessionId(session_id)
    draft_id = _next_draft_id()
    _ANSWER_DRAFTS.setdefault(session_id, {})[draft_id] = {
        "user_input": user_input,
        "answer": answer,
        "confidence": confidence,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    return draft_id


async def _finalize_confident_answer(
    memory: Memory,
    session_id: str,
    user_input: str,
    full_text: str,
    emitted_chars: int,
    tool_stats: dict | None,
) -> AsyncGenerator[tuple[str, dict], None]:
    """发送未 flush 的干净正文、置信度事件,并按草稿策略写 Memory。"""
    clean_text, signal = _extract_confidence_signal(full_text, tool_stats, user_input)
    remainder = clean_text[emitted_chars:]
    if remainder:
        yield ("chunk", {"text": remainder})
    if signal["level"] == "low" and session_id:
        draft_id = _register_answer_draft(session_id, user_input, clean_text, signal)
        signal = {**signal, "draft": True, "draft_id": draft_id}
        yield ("confidence_signal", signal)
    else:
        yield ("confidence_signal", signal)
        memory.add(Memory.USER, user_input)
        memory.add(Memory.AI, clean_text)
    yield ("done", {})


def confidence_decision(session_id: str, draft_id: str, decision: str) -> dict:
    """低置信度草稿采纳/丢弃。accept 才写入 Memory。"""
    if not SESSION_ID_RE.match(session_id):
        raise InvalidSessionId(session_id)
    drafts = _ANSWER_DRAFTS.get(session_id) or {}
    draft = drafts.pop(draft_id, None)
    if draft is None:
        raise DraftNotFound(draft_id)
    if not drafts:
        _ANSWER_DRAFTS.pop(session_id, None)
    if decision == "accept":
        memory = get_or_load(session_id)
        memory.add(Memory.USER, draft["user_input"])
        memory.add(Memory.AI, draft["answer"])
    return {"ok": True}


def _next_plan_id() -> str:
    global _PLAN_SEQ
    _PLAN_SEQ += 1
    return f"plan_{_PLAN_SEQ:04d}"


def _normalize_plan_steps(steps, *, allow_empty: bool = False) -> list[dict]:
    if not isinstance(steps, list):
        raise PlanValidationError("steps 必须是数组")
    normalized: list[dict] = []
    seen: set[str] = set()
    for idx, step in enumerate(steps[:PLAN_MAX_STEPS]):
        if not isinstance(step, dict):
            raise PlanValidationError(f"steps[{idx}] 必须是对象")
        title = step.get("title")
        if not isinstance(title, str) or not title.strip():
            raise PlanValidationError(f"steps[{idx}].title 必须是非空字符串")
        raw_id = step.get("id")
        step_id = raw_id.strip() if isinstance(raw_id, str) and raw_id.strip() else f"s{idx + 1}"
        step_id = re.sub(r"[^a-zA-Z0-9_-]", "_", step_id)[:40] or f"s{idx + 1}"
        while step_id in seen:
            step_id = f"{step_id}_{idx + 1}"
        seen.add(step_id)
        normalized.append({
            "id": step_id,
            "title": title.strip()[:200],
            "status": step.get("status") if step.get("status") in {"done", "skipped"} else "pending",
            "result_summary": step.get("result_summary") if isinstance(step.get("result_summary"), str) else None,
            "error_message": step.get("error_message") if isinstance(step.get("error_message"), str) else None,
        })
    if not normalized and not allow_empty:
        raise PlanValidationError("steps 不能为空")
    return normalized


def _merge_updated_plan_steps(existing_steps: list[dict], incoming_steps: list[dict], target_step_id: str) -> tuple[list[dict], int]:
    """合并用户修改后的计划:保留已完成/跳过步骤状态,只重置失败目标步骤。"""
    normalized = _normalize_plan_steps(incoming_steps)
    existing_by_id = {
        step.get("id"): step
        for step in existing_steps
        if isinstance(step, dict) and isinstance(step.get("id"), str)
    }
    target_idx = -1
    merged: list[dict] = []
    for idx, step in enumerate(normalized):
        step_id = step["id"]
        old = existing_by_id.get(step_id)
        if step_id == target_step_id:
            target_idx = idx
            step["status"] = "pending"
            step["error_message"] = None
            step["result_summary"] = None
        elif isinstance(old, dict) and old.get("status") in {"done", "skipped", "running"}:
            step["status"] = old["status"]
            step["result_summary"] = old.get("result_summary")
            step["error_message"] = old.get("error_message")
        else:
            step["status"] = "pending"
            step["error_message"] = None
            step["result_summary"] = None
        merged.append(step)
    if target_idx < 0:
        raise PlanValidationError("修改后的步骤必须保留当前失败 step_id")
    return merged, target_idx


def _plan_snapshot(plan: dict, editable: bool) -> dict:
    return {
        "plan_id": plan["plan_id"],
        "title": plan["title"],
        "steps": plan["steps"],
        "editable": editable,
        "status": plan["status"],
    }


def _activity_replace(plan_id: str, path: str, value) -> tuple[str, dict]:
    return ("activity_delta", {
        "plan_id": plan_id,
        "patch": [{"op": "replace", "path": path, "value": value}],
    })


def _set_plan_step_status(
    plan: dict,
    step_index: int,
    status: str,
    *,
    result_summary: str | None = None,
    error_message: str | None = None,
) -> list[tuple[str, dict]]:
    step = plan["steps"][step_index]
    step["status"] = status
    if result_summary is not None:
        step["result_summary"] = result_summary
    if error_message is not None:
        step["error_message"] = error_message
    events = [_activity_replace(plan["plan_id"], f"/steps/{step_index}/status", status)]
    if result_summary is not None:
        events.append(_activity_replace(plan["plan_id"], f"/steps/{step_index}/result_summary", result_summary))
    if error_message is not None:
        events.append(_activity_replace(plan["plan_id"], f"/steps/{step_index}/error_message", error_message))
    return events


def _get_plan(session_id: str, plan_id: str) -> dict:
    if not SESSION_ID_RE.match(session_id):
        raise InvalidSessionId(session_id)
    _restore_runtime_state(session_id)
    plan = _PLANS.get(session_id, {}).get(plan_id)
    if plan is None:
        raise PlanNotFound(plan_id)
    return plan


def _register_plan(session_id: str, args: dict, pending_state: dict) -> dict:
    if not SESSION_ID_RE.match(session_id):
        raise InvalidSessionId(session_id)
    title = args.get("title")
    if not isinstance(title, str) or not title.strip():
        title = "执行计划"
    steps = _normalize_plan_steps(args.get("steps"))
    plan = {
        "plan_id": _next_plan_id(),
        "title": title.strip()[:120],
        "steps": steps,
        "status": "pending_confirmation",
        "current_step_index": 0,
        "pending_state": pending_state,
    }
    _PLANS.setdefault(session_id, {})[plan["plan_id"]] = plan
    _save_runtime_state(session_id)
    return plan


# ============================================================
# Static Generative UI — 工具结果卡片化
# ============================================================

# 工具名 → 前端 component_type 映射。注册在此的工具执行后会额外触发 render_component 事件。
TOOL_COMPONENT_MAP: dict[str, str] = {
    "web_search": "search_results",
}

# 卡片 fallback markdown 的最大长度。比 TOOL_RESULT_PREVIEW_CHARS(500)长,
# 因为 tool_result 是调试预览(短即可),而卡片 fallback 是用户主要消费媒介。
_COMPONENT_MARKDOWN_MAX_CHARS = 2000

_SEARCH_RESULT_RE = re.compile(
    r"\[(?P<title>[^\]]+)\]\((?P<url>[^)]+)\)\s*\n(?P<snippet>[^\n]+)",
    re.MULTILINE,
)


def _warn_unmatched_component_tools(mcp_tools: list[dict]) -> None:
    """检查 Static GenUI 注册的工具名是否真的存在于 MCP discovery 结果里。"""
    discovered = {
        t.get("function", {}).get("name")
        for t in mcp_tools
        if t.get("type") == "function" and t.get("function")
    }
    unmatched = sorted(set(TOOL_COMPONENT_MAP) - discovered)
    if unmatched:
        logger.warning(
            "Static GenUI 组件映射未命中 MCP 工具: unmatched=%s discovered=%s; "
            "对应组件事件不会触发,请确认 MCP tool name 是否漂移",
            unmatched, sorted(n for n in discovered if n),
        )


def _truncate_component_markdown(text: str) -> str:
    if len(text) <= _COMPONENT_MARKDOWN_MAX_CHARS:
        return text
    return text[:_COMPONENT_MARKDOWN_MAX_CHARS] + "\n\n...（内容过长已截断）"


def _parse_search_results(raw_text: str, query: str) -> dict:
    """从 MCP web_search markdown 文本提取结构化结果;失败时保留 markdown fallback。"""
    results = []
    for match in _SEARCH_RESULT_RE.finditer(raw_text):
        results.append({
            "title": match.group("title").strip(),
            "url": match.group("url").strip(),
            "snippet": match.group("snippet").strip(),
        })

    if results:
        return {
            "query": query,
            "results": results,
            "total_count": len(results),
        }
    return {
        "query": query,
        "results": [],
        "raw_markdown": _truncate_component_markdown(raw_text),
    }


def _build_component_props(tool_name: str, args: dict, result_text: str) -> dict | None:
    """根据工具名 + 参数 + 返回文本,构建前端组件 props。返回 None 表示不渲染(如工具失败)。"""
    if tool_name == "web_search":
        if result_text.startswith(mcp_web_search.ERROR_PREFIX):
            return None
        # MCP schema 中搜索词字段名为 "query",兼容早期版本可能出现的 "search_query"
        query = args.get("query") or args.get("search_query") or ""
        return _parse_search_results(result_text, query)
    # 注册了 component_type 但没有 props 构建分支 — 开发期预警
    if tool_name in TOOL_COMPONENT_MAP:
        logger.warning(
            "TOOL_COMPONENT_MAP 注册了 %s 但 _build_component_props 无对应分支", tool_name,
        )
    return None


# ============================================================
# Declarative Generative UI — Phase 3
# ============================================================

_SUPPORTED_UI_COMPONENT_TYPES = {
    "text", "card", "row", "column", "table", "button",
    # Phase 8b: 表单组件
    "text_field", "select", "toggle",
}
_FORM_UI_COMPONENT_TYPES = {"text_field", "select", "toggle"}
_SUPPORTED_TEXT_FIELD_INPUT_TYPES = {"shortText", "longText", "number"}


def _extract_ui_actions(components: list[dict]) -> dict[str, dict]:
    """从 button 组件中提取后端可信 action registry。"""
    actions: dict[str, dict] = {}
    for comp in components:
        if not isinstance(comp, dict):
            continue
        if comp.get("type") != "button":
            continue
        comp_id = comp.get("id")
        if not isinstance(comp_id, str) or not comp_id.strip():
            continue
        action = comp.get("action")
        event_name = action.get("event_name") if isinstance(action, dict) else None
        if not isinstance(event_name, str) or not event_name.strip():
            continue
        context = action.get("context", {})
        actions[comp_id] = {
            "event_name": event_name.strip()[:120],
            "context": context if isinstance(context, dict) else {},
            "label": str(comp.get("label") or comp_id)[:120],
        }
    return actions


def _register_ui_surface(
    session_id: str,
    surface_id: str,
    components: list[dict],
    data: dict,
    *,
    merge: bool = False,
) -> None:
    """注册 Phase 3b 内存态 surface。

    Phase 8a: 当 merge=True 且 surface 已存在,按 component.id 合并 components
    (旧顺序在前,同 id 新覆盖旧,新 id 追加在后);data 浅合并(传入非空时);
    actions 用合并后的 components 重新提取。merge=False 保持覆盖语义,
    兼容 complete=true 一次性渲染。
    """
    if not SESSION_ID_RE.match(session_id):
        raise InvalidSessionId(session_id)
    existing = _UI_SURFACES.get(session_id, {}).get(surface_id)
    if merge and existing is not None:
        # 旧 + 新 按 id 合并,保持顺序
        merged_map: dict[str, dict] = {}
        for comp in existing.get("components") or []:
            if isinstance(comp, dict) and isinstance(comp.get("id"), str):
                merged_map[comp["id"]] = comp
        for comp in components:
            if isinstance(comp, dict) and isinstance(comp.get("id"), str):
                merged_map[comp["id"]] = comp
        merged_components = list(merged_map.values())
        # data 浅合并:仅当 incoming data 非空时合并
        merged_data = dict(existing.get("data") or {})
        if data:
            merged_data.update(data)
        _UI_SURFACES.setdefault(session_id, {})[surface_id] = {
            "components": merged_components,
            "data": merged_data,
            "actions": _extract_ui_actions(merged_components),
        }
    else:
        _UI_SURFACES.setdefault(session_id, {})[surface_id] = {
            "components": components,
            "data": data,
            "actions": _extract_ui_actions(components),
        }
    _save_runtime_state(session_id)


def _get_ui_surface(session_id: str, surface_id: str) -> dict:
    if not SESSION_ID_RE.match(session_id):
        raise InvalidSessionId(session_id)
    _restore_runtime_state(session_id)
    surface = _UI_SURFACES.get(session_id, {}).get(surface_id)
    if surface is None:
        raise UiSurfaceNotFound(surface_id)
    return surface


def _decode_json_pointer(path: str) -> list[str]:
    if not isinstance(path, str) or (path != "/" and not path.startswith("/")):
        raise ValueError("path 必须是合法 JSON Pointer")
    if path == "/":
        return []
    return [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]


def _json_pointer_set(data: dict, path: str, value, *, auto_create: bool = False) -> dict:
    """按 JSON Pointer 写入 data。/ 表示整体替换,深路径要求父节点已存在。

    Phase 8b: auto_create=True 时,中间路径的 dict key 不存在则自动建 {};
    数组索引仍要求已存在(语义不清,不自动建)。仅前端表单写回路径使用此模式;
    模型主动 update_ui_data 仍走严格模式以保持契约。
    """
    tokens = _decode_json_pointer(path)
    if not tokens:
        if not isinstance(value, dict):
            raise ValueError("path=/ 时 value 必须是对象")
        return value

    cur = data
    for token in tokens[:-1]:
        if isinstance(cur, dict):
            if token not in cur:
                if auto_create:
                    cur[token] = {}
                else:
                    raise ValueError(f"path 父节点不存在: {token}")
            cur = cur[token]
        elif isinstance(cur, list):
            try:
                idx = int(token)
            except ValueError as exc:
                raise ValueError(f"数组下标非法: {token}") from exc
            if idx < 0 or idx >= len(cur):
                raise ValueError(f"数组下标越界: {token}")
            cur = cur[idx]
        else:
            raise ValueError("path 父节点不是对象或数组")

    last = tokens[-1]
    if isinstance(cur, dict):
        cur[last] = value
    elif isinstance(cur, list):
        try:
            idx = int(last)
        except ValueError as exc:
            raise ValueError(f"数组下标非法: {last}") from exc
        if idx < 0 or idx >= len(cur):
            raise ValueError(f"数组下标越界: {last}")
        cur[idx] = value
    else:
        raise ValueError("path 父节点不是对象或数组")
    return data


def _validate_form_component(comp: dict) -> str | None:
    """Phase 8b: 校验 text_field/select/toggle 必填字段;返回错误字符串或 None。"""
    comp_id = comp.get("id")
    comp_type = comp.get("type")
    value_path = comp.get("value_path")
    if not isinstance(value_path, str) or not (value_path == "/" or value_path.startswith("/")):
        return f"组件 {comp_id} ({comp_type}) 缺少合法的 value_path (JSON Pointer)"
    if comp_type == "text_field":
        input_type = comp.get("input_type", "shortText")
        if input_type not in _SUPPORTED_TEXT_FIELD_INPUT_TYPES:
            return f"组件 {comp_id} input_type 非法: {input_type}"
    elif comp_type == "select":
        options = comp.get("options")
        if not isinstance(options, list) or not options:
            return f"组件 {comp_id} (select) options 必须是非空数组"
        for opt_idx, opt in enumerate(options):
            if not isinstance(opt, dict) or "value" not in opt:
                return f"组件 {comp_id} options[{opt_idx}] 必须含 value 字段"
    elif comp_type == "toggle":
        pass  # value_path 已校验
    return None


def _has_form_components(args: dict) -> bool:
    """Phase 8b: 检测 render_ui 参数中是否含 text_field/select/toggle 组件。CLI 短路用。"""
    components = args.get("components") if isinstance(args, dict) else None
    if not isinstance(components, list):
        return False
    for comp in components:
        if isinstance(comp, dict) and comp.get("type") in _FORM_UI_COMPONENT_TYPES:
            return True
    return False


def _execute_render_ui(args: dict) -> dict:
    """校验 render_ui 参数并返回前端可消费的 surface payload。"""
    surface_id = args.get("surface_id")
    if not isinstance(surface_id, str) or not surface_id.strip():
        return {"ok": False, "error": "render_ui 参数错误: surface_id 必须是非空字符串"}
    surface_id = surface_id.strip()[:80]

    components = args.get("components")
    if not isinstance(components, list):
        return {"ok": False, "error": "render_ui 参数错误: components 必须是数组"}
    if not components:
        return {"ok": False, "error": "render_ui 参数错误: components 不能为空"}

    cleaned: list[dict] = []
    seen_ids: set[str] = set()
    has_root = False
    for idx, comp in enumerate(components):
        if not isinstance(comp, dict):
            return {"ok": False, "error": f"render_ui 参数错误: components[{idx}] 必须是对象"}
        comp_id = comp.get("id")
        comp_type = comp.get("type")
        if not isinstance(comp_id, str) or not comp_id.strip():
            return {"ok": False, "error": f"render_ui 参数错误: components[{idx}].id 必须是非空字符串"}
        if comp_id in seen_ids:
            return {"ok": False, "error": f"render_ui 参数错误: 组件 id 重复: {comp_id}"}
        if not isinstance(comp_type, str) or comp_type not in _SUPPORTED_UI_COMPONENT_TYPES:
            return {"ok": False, "error": f"render_ui 参数错误: 不支持的组件 type: {comp_type}"}
        # Phase 8b: 表单组件 schema 校验。失败立即返回,前端不渲染半成品。
        if comp_type in _FORM_UI_COMPONENT_TYPES:
            err = _validate_form_component(comp)
            if err:
                return {"ok": False, "error": f"render_ui 参数错误: {err}"}
        seen_ids.add(comp_id)
        has_root = has_root or comp_id == "root"
        cleaned.append(comp)

    if not has_root:
        # Phase 8a: 流式增量场景下,后续调用可不带 root —— root 由
        # _execute_render_ui_for_session 检查是否已在 surface 中。
        # 此处只在"找不到根 + complete=true(默认)"且无 surface 上下文时才会触发硬错;
        # 真正的 root 存在性最终由 for_session 兜底。
        # 把信号传上去,让上层决定是否容忍。
        pass

    data = args.get("data")
    if data is not None and not isinstance(data, dict):
        return {"ok": False, "error": "render_ui 参数错误: data 必须是对象"}

    return {
        "ok": True,
        "surface_id": surface_id,
        "components": cleaned,
        "data": data or {},
        "complete": bool(args.get("complete", True)),
        "has_root": has_root,
    }


def _execute_update_ui_data(args: dict, session_id: str) -> dict:
    """更新已注册 surface 的 data model。失败以工具结果文本返回,不抛到流外。"""
    surface_id = args.get("surface_id")
    if not isinstance(surface_id, str) or not surface_id.strip():
        return {"ok": False, "error": "update_ui_data 参数错误: surface_id 必须是非空字符串"}
    path = args.get("path")
    if not isinstance(path, str):
        return {"ok": False, "error": "update_ui_data 参数错误: path 必须是字符串"}
    if "value" not in args:
        return {"ok": False, "error": "update_ui_data 参数错误: 缺少 value"}

    try:
        surface = _get_ui_surface(session_id, surface_id.strip()[:80])
        surface["data"] = _json_pointer_set(surface.get("data") or {}, path, args.get("value"))
    except (InvalidSessionId, UiSurfaceNotFound, ValueError) as exc:
        return {"ok": False, "error": f"update_ui_data 失败: {exc}"}
    _save_runtime_state(session_id)

    return {
        "ok": True,
        "surface_id": surface_id.strip()[:80],
        "path": path,
        "value": args.get("value"),
        "tool_result": "UI 数据已更新",
    }


def _execute_render_ui_for_session(args: dict, session_id: str) -> dict:
    result = _execute_render_ui(args)
    if not result.get("ok"):
        return result

    # Phase 8a: complete=false 走增量合并;complete=true 保持覆盖语义。
    merge = not result.get("complete", True)

    # Phase 8a fix: 流式增量场景下,后续调用可不带 root —— 只要已存在 surface 含 root 即可。
    # 一次性渲染场景(complete=true),或新 surface 的首次流式调用,都必须含 root。
    if not result.get("has_root"):
        existing = _UI_SURFACES.get(session_id, {}).get(result["surface_id"])
        existing_components = existing.get("components") if existing else None
        existing_has_root = any(
            isinstance(c, dict) and c.get("id") == "root"
            for c in (existing_components or [])
        )
        if not (merge and existing_has_root):
            return {
                "ok": False,
                "error": "render_ui 参数错误: components 必须包含 id='root' 的根节点"
                         "(流式增量场景下首次调用必须含 root)",
            }

    _register_ui_surface(
        session_id,
        result["surface_id"],
        result["components"],
        result["data"],
        merge=merge,
    )
    result["tool_result"] = "UI 已渲染"
    # Phase 7b: 流式渲染信号 —— complete=false 时,通知前端可开始首次渲染。
    if not result.get("complete", True):
        result["begin_rendering"] = {
            "surface_id": result["surface_id"],
            "root": "root",
            "catalog_id": "simple_chat_agent_demo",
        }
    return result


IMMEDIATE_LOCAL_TOOLS = {
    "render_ui": _execute_render_ui_for_session,
    "update_ui_data": _execute_update_ui_data,
}


def _execute_immediate_local_tool(tool_name: str, args: dict, session_id: str) -> dict:
    executor = IMMEDIATE_LOCAL_TOOLS.get(tool_name)
    if executor is None:
        return {"ok": False, "error": f"未知本地立即工具: {tool_name}"}
    return executor(args, session_id)


def _react_chat_native(memory: Memory, user_input: str) -> str:
    """[chat 模式 + native function calling] 同步 ReAct 循环,CLI 用。

    与 react() 等价契约:返回最终答复文本字符串(由 common_chat_agent.main 写回 Memory)。
    内部维护一个本地 messages 数组承载 tool_calls / tool_call_id 链接,循环结束就丢弃。
    """
    messages = _memory_to_messages(memory, USER_PROMPT, user_input)
    try:
        tools = _build_native_tools()
    except Exception as exc:
        logger.exception("MCP schema 发现失败")
        return f"MCP schema 发现失败: {exc}"

    for round_num in range(MAX_ROUNDS):
        logger.info(
            "ReAct 第 %d 轮开始(chat+native):msg_count=%d tools=%s",
            round_num, len(messages), [t["function"]["name"] for t in tools],
        )

        content, tool_calls, reasoning_content = llm_chat_with_tools(messages, tools)
        logger.info(
            "第 %d 轮 llmResult(content=%d 字, tool_calls=%d 个, reasoning=%d 字)",
            round_num, len(content), len(tool_calls), len(reasoning_content),
        )

        if not tool_calls:
            clean, _ = _extract_confidence_signal(content, _new_tool_stats(), user_input)
            return clean

        # 把 assistant 这一轮的 tool_calls 追加进 messages,准备喂回模型。
        # reasoning_content 在 thinking 模式下必须跨轮保留(参考 docs:142),不需要时透传也无害。
        assistant_msg: dict = {
            "role": "assistant",
            "content": content or None,
            "tool_calls": [
                {"id": tc["id"], "type": "function",
                 "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                for tc in tool_calls
            ],
        }
        if reasoning_content:
            assistant_msg["reasoning_content"] = reasoning_content
        messages.append(assistant_msg)

        # 顺序执行每个 tool_call(parallel_tool_calls=true 时模型可一次性返回多个)
        for tc in tool_calls:
            try:
                args = json.loads(tc["arguments"]) if tc["arguments"] else {}
            except json.JSONDecodeError as exc:
                logger.warning("tool_calls.arguments JSON 解析失败: %s, 用空对象兜底", exc)
                args = {}
            logger.info("执行工具调用:%s,开始", tc["name"])
            logger.info("工具参数:%s", args)
            if tc["name"] in LOCAL_TOOLS:
                # HITL 工具在 CLI 模式下无法交互,直接喂回错误字符串让模型换路
                tool_result = (
                    f"[HITL 工具 {tc['name']} 在 CLI 模式下不可用，"
                    "请直接以文本方式向用户说明或寻求其他途径]"
                )
            elif tc["name"] in IMMEDIATE_LOCAL_TOOLS:
                # Phase 8b: render_ui 含表单组件时,给出更明确的回退提示。
                if tc["name"] == "render_ui" and _has_form_components(args):
                    tool_result = (
                        "[render_ui 含 text_field/select/toggle 表单组件,"
                        "仅 Web 模式可见。请直接以文本方式询问用户对应字段。]"
                    )
                else:
                    tool_result = f"{tc['name']} 仅 Web 模式可见，请用文本继续说明。"
            else:
                tool_result = mcp_web_search.call_tool_sync(tc["name"], args)
            logger.info("执行工具调用:%s,结果=%s", tc["name"], tool_result)
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": tool_result,
            })

    logger.warning("超过最大 ReAct 轮次（%d, chat+native），中断", MAX_ROUNDS)
    return f"（已达到最大 ReAct 轮次 {MAX_ROUNDS}，对话中断，请重试或换个问法）"


async def _stream_react_rounds(
    session_id: str,
    memory: Memory,
    user_input: str,
    messages: list[dict],
    tools: list[dict],
    start_round: int,
    pending_remaining: list[dict],
    is_disconnected: Callable[[], Awaitable[bool]],
    tool_stats: dict | None = None,
) -> AsyncGenerator[tuple[str, dict], None]:
    """ReAct 流式循环主体,支持两种入口:
       - fresh start: start_round=0, pending_remaining=[]
       - resume:      start_round=断点轮次, pending_remaining=断点未消费的剩余 tool_calls

    HITL 触发时把恢复点写入 _PENDING[session_id],yield ("await_user", ...) + ("done", {}) 关流。
    """
    tool_stats = tool_stats or _new_tool_stats()
    for round_num in range(start_round, MAX_ROUNDS):
        # ============ 0) Phase 9a: drain steer 队列 ============
        # 仅在 "非 resume 接续" 且 "OpenAI 顺序允许 user 追加" 时消费 steer。
        # OpenAI 约束: assistant.tool_calls 后必须紧跟 role=tool, 此时不能塞 user。
        # 不满足条件的 steer 留在队列, 下一轮顶部再 drain。
        if not pending_remaining and _can_append_user_message(messages):
            steers = _drain_steers(session_id)
            for steer_msg in steers:
                messages.append({
                    "role": "user",
                    "content": (
                        "[Steering] 用户在 Agent 运行期间发起方向纠正,"
                        f"优先采纳以下指令:\n{steer_msg}"
                    ),
                })
                _record_steer(session_id, steer_msg)
                yield ("steer_applied", {"message": steer_msg, "round": round_num})
            if steers and session_id:
                # Phase 9b: agent_state_delta 整段 replace steer_history
                # (朴素实现, 避免数组 add(/-) 的索引边界争议)
                yield ("agent_state_delta", {
                    "patch": [{
                        "op": "replace",
                        "path": "/steer_history",
                        "value": list(_STEER_HISTORY.get(session_id) or []),
                    }],
                })

        # ============ 1) 取本轮 tool_calls ============
        # resume 第一轮:直接复用断点处尚未消费的剩余队列,**跳过** LLM 调用
        if pending_remaining:
            accumulated_tool_calls = pending_remaining
            pending_remaining = []
            accumulated_content = ""
            accumulated_reasoning = ""
            logger.info(
                "ReAct 第 %d 轮(resume 接续):跳过 LLM 调用,直接派发剩余 tool_calls=%d 个",
                round_num, len(accumulated_tool_calls),
            )
        else:
            yield ("status", {"phase": "thinking", "round": round_num})
            if session_id:
                # Phase 9b: round 推进 delta (与 status 同时机, 标记新轮次)
                yield ("agent_state_delta", {
                    "patch": [{"op": "replace", "path": "/round", "value": round_num}],
                })
            logger.info(
                "ReAct 第 %d 轮开始(chat+native):msg_count=%d tools=%s",
                round_num, len(messages), [t["function"]["name"] for t in tools],
            )

            accumulated_content = ""
            accumulated_reasoning = ""
            accumulated_tool_calls = []
            confidence_tail = ""
            emitted_answer_chars = 0
            answering_flipped = False

            try:
                async for kind, payload in llm_stream_chat_with_tools(messages, tools):
                    if await is_disconnected():
                        return
                    if kind == "thinking":
                        accumulated_reasoning += payload
                        yield ("thinking", {"text": payload})
                        continue
                    if kind == "tool_calls":
                        accumulated_tool_calls = payload
                        continue
                    if kind == "error":
                        tool_stats["errors"] += 1
                        yield ("error", {"message": payload})
                        return
                    # kind == "content"
                    if not answering_flipped:
                        answering_flipped = True
                        yield ("status", {"phase": "answering", "round": round_num})
                    accumulated_content += payload
                    confidence_tail, emit_text = _buffer_confidence_delta(confidence_tail, payload)
                    if emit_text:
                        emitted_answer_chars += len(emit_text)
                        yield ("chunk", {"text": emit_text})
            except Exception as exc:
                logger.exception("LLM 调用失败(chat+native)")
                yield ("error", {"message": f"LLM 调用失败: {exc}"})
                return

            logger.info(
                "第 %d 轮 llmResult(content=%d 字, tool_calls=%d 个, reasoning=%d 字)",
                round_num, len(accumulated_content), len(accumulated_tool_calls),
                len(accumulated_reasoning),
            )

            if not accumulated_tool_calls:
                async for event in _finalize_confident_answer(
                    memory, session_id, user_input, accumulated_content,
                    emitted_answer_chars, tool_stats,
                ):
                    yield event
                return

            if confidence_tail:
                emitted_answer_chars += len(confidence_tail)
                yield ("chunk", {"text": confidence_tail})

            # reasoning_content 在 thinking 模式下必须跨轮保留(参考 docs:142),不需要时透传也无害
            assistant_msg: dict = {
                "role": "assistant",
                "content": accumulated_content or None,
                "tool_calls": [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                    for tc in accumulated_tool_calls
                ],
            }
            if accumulated_reasoning:
                assistant_msg["reasoning_content"] = accumulated_reasoning
            messages.append(assistant_msg)

        # ============ 2) tool 派发:按 name 分发到 LOCAL_TOOLS(HITL) 或 MCP ============
        while accumulated_tool_calls:
            tc = accumulated_tool_calls.pop(0)
            if await is_disconnected():
                return
            try:
                args = json.loads(tc["arguments"]) if tc["arguments"] else {}
            except json.JSONDecodeError as exc:
                logger.warning("tool_calls.arguments JSON 解析失败: %s, 用空对象兜底", exc)
                args = {}
            logger.info("执行工具调用:%s,开始", tc["name"])
            logger.info("工具参数:%s", args)
            tool_stats["tool_calls"] += 1
            if tc["name"] == "web_search":
                tool_stats["search_calls"] += 1
            yield ("tool_call", {"name": tc["name"], "args": args})
            if session_id:
                # Phase 9b: 工具计数更新 delta (在 tool_call emit 后立即同步, 前端能看到累计)
                yield ("agent_state_delta", {
                    "patch": [{"op": "replace", "path": "/tool_stats", "value": dict(tool_stats)}],
                })

            if tc["name"] == "create_plan":
                pending_state = {
                    "user_input": user_input,
                    "messages": messages,
                    "tools": tools,
                    "round_num": round_num,
                    "remaining_tool_calls": accumulated_tool_calls,
                    "tool_call_id": tc["id"],
                }
                try:
                    plan = _register_plan(session_id, args, pending_state)
                except (InvalidSessionId, PlanValidationError) as exc:
                    tool_stats["tool_failures"] += 1
                    tool_result = f"create_plan 参数错误: {exc}"
                    yield ("tool_result", {"name": tc["name"], "result": _truncate_tool_result(tool_result)})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": tool_result,
                    })
                    continue
                yield ("activity_snapshot", _plan_snapshot(plan, editable=True))
                yield ("await_user", {
                    "tool_call_id": tc["id"],
                    "name": tc["name"],
                    "args": args,
                    "kind": "plan",
                    "plan_id": plan["plan_id"],
                    "title": plan["title"],
                    "steps": plan["steps"],
                })
                yield ("done", {})
                return

            if tc["name"] in LOCAL_TOOLS:
                # HITL 中断点:存恢复点,yield await_user + done,等 /api/resume 启新流接续
                _PENDING[session_id] = {
                    "user_input": user_input,
                    "messages": messages,
                    "tools": tools,
                        "round_num": round_num,
                        "remaining_tool_calls": accumulated_tool_calls,
                        "tool_stats": tool_stats,
                        "awaiting": {
                        "tool_call_id": tc["id"],
                        "name": tc["name"],
                        "args": args,
                        "kind": _LOCAL_TOOL_KIND[tc["name"]],
                    },
                }
                _save_runtime_state(session_id)
                logger.info(
                    "HITL 中断:session=%s tool=%s tool_call_id=%s remaining=%d 个",
                    session_id, tc["name"], tc["id"], len(accumulated_tool_calls),
                )
                yield ("await_user", {
                    "tool_call_id": tc["id"],
                    "name": tc["name"],
                    "args": args,
                    "kind": _LOCAL_TOOL_KIND[tc["name"]],
                })
                yield ("done", {})
                return

            if tc["name"] in IMMEDIATE_LOCAL_TOOLS:
                result = _execute_immediate_local_tool(tc["name"], args, session_id)
                if result.get("ok") and tc["name"] == "render_ui":
                    surface_id = result["surface_id"]
                    yield ("ui_surface_create", {"surface_id": surface_id})
                    # Phase 8a fix B1: mode 字段对齐双端合并语义。
                    # complete=true → replace(前端清空 Map 再装入); complete=false → merge(前端按 id 合并)。
                    yield ("ui_surface_update", {
                        "surface_id": surface_id,
                        "components": result["components"],
                        "mode": "merge" if not result.get("complete", True) else "replace",
                    })
                    if result.get("data"):
                        yield ("ui_data_update", {
                            "surface_id": surface_id,
                            "path": "/",
                            "value": result["data"],
                        })
                    # Phase 7b: 流式渲染信号
                    if result.get("begin_rendering"):
                        yield ("begin_rendering", result["begin_rendering"])
                    tool_result = result.get("tool_result") or "UI 已渲染"
                elif result.get("ok") and tc["name"] == "update_ui_data":
                    yield ("ui_data_update", {
                        "surface_id": result["surface_id"],
                        "path": result["path"],
                        "value": result["value"],
                    })
                    tool_result = result.get("tool_result") or "UI 数据已更新"
                else:
                    tool_stats["tool_failures"] += 1
                    tool_result = result.get("error") or f"{tc['name']} 参数错误"
                logger.info("执行本地立即工具:%s,结果=%s", tc["name"], tool_result)
                yield ("tool_result", {"name": tc["name"], "result": _truncate_tool_result(tool_result)})
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_result,
                })
                continue

            # 非 HITL:MCP 工具,正常执行
            component_type = TOOL_COMPONENT_MAP.get(tc["name"])
            if component_type:
                yield ("component_loading", {
                    "component_type": component_type,
                    "tool_call_id": tc["id"],
                    "placeholder_text": f"正在执行 {tc['name']}…",
                })

            tool_result = await mcp_web_search.call_tool_async(tc["name"], args)
            if tool_result.startswith(mcp_web_search.ERROR_PREFIX):
                tool_stats["tool_failures"] += 1
            logger.info("执行工具调用:%s,结果=%s", tc["name"], tool_result)
            yield ("tool_result", {"name": tc["name"], "result": _truncate_tool_result(tool_result)})

            if component_type:
                props = _build_component_props(tc["name"], args, tool_result)
                if props is not None:
                    yield ("render_component", {
                        "component_type": component_type,
                        "tool_call_id": tc["id"],
                        "props": props,
                    })
                else:
                    yield ("component_error", {
                        "component_type": component_type,
                        "tool_call_id": tc["id"],
                        "error_message": tool_result[:100] if tool_result else "工具执行失败",
                    })

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": tool_result,
            })

    yield ("error", {"message": "超过最大 ReAct 轮次，已中断"})


async def _stream_chat_native(
    memory: Memory,
    user_input: str,
    is_disconnected: Callable[[], Awaitable[bool]],
    session_id: str,
    adaptive_fragment: str = "",
    images: list[str] | None = None,
    attachments: list[dict] | None = None,
) -> AsyncGenerator[tuple[str, dict], None]:
    """[chat 模式 + native function calling] 异步流式 ReAct 循环,Web 用。

    yield 的 (event_name, payload) 元组**完全复用**现有 SSE 契约,新增 await_user 一项:
        status / thinking / chunk / tool_call / tool_result / await_user / done / error

    Phase 10a: `images` (data URL list) 仅注入到 fresh turn 的初始 user 节点。
    后续 ReAct 多轮通过 append tool result / assistant 到同一份 messages list 续跑,
    user 节点保持 vision content 不变 —— 下一个 fresh /api/chat 是新调用,
    Memory 重建时 images 默认 None, 历史对话天然不携带图片 (D2/D4 安全防线)。

    Phase 10b: `attachments` (list[{filename, content, mime_type}]) 同 images 一样
    只注入 fresh turn, 由 _memory_to_messages 拼成 markdown 代码块追加到 user_input 后。
    HITL pending 不单独存 attachments; 附件已烘焙进 messages, 随 _PENDING["messages"]
    间接持久化到 runtime_state sidecar (与 images 对称, resume 后模型需看原始附件内容才能继续推理);
    Memory / runtime_state 不存 attachments (D2/D4 安全防线, 与 images 对称)。
    """
    # 新一轮 chat：清掉旧 pending，防止僵尸 _PENDING 项常驻
    if _PENDING.pop(session_id, None) is not None:
        _save_runtime_state(session_id)

    # Phase 9 fix B2: 同纲领清掉旧 steer 队列残留。
    # 触发场景: 用户在旧流活跃中点击 steer 后关闭浏览器(SSE 流断, 但 steer 残留队列),
    # 重开页面发起新 chat 时, 旧 steer 不应注入新对话。
    # _STEER_HISTORY 不清 —— 那是只供前端展示的历史, 跨 chat 累计才有意义。
    stale_queue = _STEER_QUEUES.pop(session_id, None)
    if stale_queue is not None and stale_queue.qsize() > 0:
        logger.info(
            "新 chat 启动:丢弃 %d 条未消费的旧 steer (session=%s)",
            stale_queue.qsize(), session_id,
        )

    messages = _memory_to_messages(
        memory, USER_PROMPT, user_input, adaptive_fragment, images=images,
        attachments=attachments,
    )
    try:
        tools = await _build_native_tools_async()
    except Exception as exc:
        logger.exception("MCP schema 发现失败")
        yield ("error", {"message": f"MCP schema 发现失败: {exc}"})
        return

    async for event in _stream_react_rounds(
        session_id, memory, user_input, messages, tools, 0, [], is_disconnected,
    ):
        yield event


# ============================================================
# Plan-and-Execute —— Phase 4
# ============================================================

def _build_plan_confirm_tool_result(plan: dict) -> str:
    lines = [f"用户确认了计划: {plan['title']}"]
    for idx, step in enumerate(plan["steps"], start=1):
        lines.append(f"{idx}. {step['title']}")
    return "\n".join(lines)


def _build_plan_step_input(plan: dict, step_index: int) -> str:
    completed = [
        f"- {s['title']}: {s.get('result_summary') or s['status']}"
        for s in plan["steps"][:step_index]
        if s["status"] in {"done", "skipped"}
    ]
    step = plan["steps"][step_index]
    return "\n".join([
        "[Plan Step]",
        f"计划: {plan['title']}",
        f"当前步骤: {step_index + 1}/{len(plan['steps'])}",
        f"step_id: {step['id']}",
        f"step_title: {step['title']}",
        "已完成步骤:",
        "\n".join(completed) if completed else "(无)",
        "请只完成当前步骤。需要工具时可以调用工具;完成后用简短文字总结本步骤结果。",
    ])


async def _stream_plan_step_rounds(
    session_id: str,
    plan: dict,
    step_index: int,
    messages: list[dict],
    tools: list[dict],
    is_disconnected: Callable[[], Awaitable[bool]],
    start_round: int = 0,
    pending_remaining: list[dict] | None = None,
    append_step_input: bool = True,
) -> AsyncGenerator[tuple[str, dict], None]:
    """执行单个 plan step。结果写回 plan['steps'][step_index]['status']。"""
    step = plan["steps"][step_index]
    if append_step_input and not pending_remaining:
        messages.append({"role": "user", "content": _build_plan_step_input(plan, step_index)})
    pending_remaining = pending_remaining or []

    for round_num in range(start_round, PLAN_STEP_MAX_ROUNDS):
        # Phase 9a: drain steer 队列 —— 与 _stream_react_rounds 同纲领, 但 steer 文本
        # 带 [计划 | 步骤] 上下文提示, 让模型理解当前在 plan-step 内部, 自行决定是否结束步骤。
        # 不在 plan-step 内 emit /round delta (避免与全局 round 概念混淆), 仅 emit steer_history delta。
        if not pending_remaining and _can_append_user_message(messages):
            steers = _drain_steers(session_id)
            for steer_msg in steers:
                messages.append({
                    "role": "user",
                    "content": (
                        f"[Steering | 计划 {plan.get('title') or ''} | 步骤 {step.get('title') or ''}] "
                        f"用户在计划步骤执行期间发起方向纠正,优先采纳以下指令:\n{steer_msg}"
                    ),
                })
                _record_steer(session_id, steer_msg)
                yield ("steer_applied", {"message": steer_msg, "round": round_num})
            if steers and session_id:
                yield ("agent_state_delta", {
                    "patch": [{
                        "op": "replace",
                        "path": "/steer_history",
                        "value": list(_STEER_HISTORY.get(session_id) or []),
                    }],
                })

        if pending_remaining:
            accumulated_tool_calls = pending_remaining
            pending_remaining = []
            accumulated_content = ""
            accumulated_reasoning = ""
        else:
            yield ("status", {"phase": "thinking", "round": round_num})
            accumulated_content = ""
            accumulated_reasoning = ""
            accumulated_tool_calls = []
            answering_flipped = False
            try:
                async for kind, payload in llm_stream_chat_with_tools(messages, tools):
                    if await is_disconnected():
                        return
                    if kind == "thinking":
                        accumulated_reasoning += payload
                        yield ("thinking", {"text": payload})
                        continue
                    if kind == "tool_calls":
                        accumulated_tool_calls = payload
                        continue
                    if kind == "error":
                        step["status"] = "error"
                        step["error_message"] = payload
                        return
                    if not answering_flipped:
                        answering_flipped = True
                        yield ("status", {"phase": "answering", "round": round_num})
                    accumulated_content += payload
                    yield ("chunk", {"text": payload})
            except Exception as exc:
                logger.exception("计划步骤 LLM 调用失败")
                step["status"] = "error"
                step["error_message"] = f"LLM 调用失败: {exc}"
                return

            if not accumulated_tool_calls:
                step["status"] = "done"
                step["result_summary"] = accumulated_content.strip()[:300] or "已完成"
                return

            assistant_msg: dict = {
                "role": "assistant",
                "content": accumulated_content or None,
                "tool_calls": [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                    for tc in accumulated_tool_calls
                ],
            }
            if accumulated_reasoning:
                assistant_msg["reasoning_content"] = accumulated_reasoning
            messages.append(assistant_msg)

        while accumulated_tool_calls:
            tc = accumulated_tool_calls.pop(0)
            if await is_disconnected():
                return
            try:
                args = json.loads(tc["arguments"]) if tc["arguments"] else {}
            except json.JSONDecodeError:
                args = {}
            yield ("tool_call", {"name": tc["name"], "args": args})

            if tc["name"] == "create_plan":
                tool_result = "计划执行中不支持嵌套 create_plan;请继续完成当前步骤。"
                yield ("tool_result", {"name": tc["name"], "result": _truncate_tool_result(tool_result)})
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": tool_result})
                continue

            if tc["name"] in LOCAL_TOOLS:
                step["status"] = "waiting"
                _PENDING[session_id] = {
                    "user_input": plan["pending_state"]["user_input"],
                    "messages": messages,
                    "tools": tools,
                    "round_num": round_num,
                    "remaining_tool_calls": accumulated_tool_calls,
                    "awaiting": {
                        "tool_call_id": tc["id"],
                        "name": tc["name"],
                        "args": args,
                        "kind": _LOCAL_TOOL_KIND[tc["name"]],
                    },
                    "plan_resume": {
                        "plan_id": plan["plan_id"],
                        "step_index": step_index,
                    },
                }
                _save_runtime_state(session_id)
                yield ("await_user", {
                    "tool_call_id": tc["id"],
                    "name": tc["name"],
                    "args": args,
                    "kind": _LOCAL_TOOL_KIND[tc["name"]],
                })
                yield ("done", {})
                return

            if tc["name"] in IMMEDIATE_LOCAL_TOOLS:
                result = _execute_immediate_local_tool(tc["name"], args, session_id)
                if result.get("ok") and tc["name"] == "render_ui":
                    surface_id = result["surface_id"]
                    yield ("ui_surface_create", {"surface_id": surface_id})
                    # Phase 8a fix B1: mode 字段对齐双端合并语义。
                    yield ("ui_surface_update", {
                        "surface_id": surface_id,
                        "components": result["components"],
                        "mode": "merge" if not result.get("complete", True) else "replace",
                    })
                    if result.get("data"):
                        yield ("ui_data_update", {"surface_id": surface_id, "path": "/", "value": result["data"]})
                    # Phase 7b: 流式渲染信号
                    if result.get("begin_rendering"):
                        yield ("begin_rendering", result["begin_rendering"])
                    tool_result = result.get("tool_result") or "UI 已渲染"
                elif result.get("ok") and tc["name"] == "update_ui_data":
                    yield ("ui_data_update", {
                        "surface_id": result["surface_id"],
                        "path": result["path"],
                        "value": result["value"],
                    })
                    tool_result = result.get("tool_result") or "UI 数据已更新"
                else:
                    tool_result = result.get("error") or f"{tc['name']} 参数错误"
                yield ("tool_result", {"name": tc["name"], "result": _truncate_tool_result(tool_result)})
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": tool_result})
                continue

            component_type = TOOL_COMPONENT_MAP.get(tc["name"])
            if component_type:
                yield ("component_loading", {
                    "component_type": component_type,
                    "tool_call_id": tc["id"],
                    "placeholder_text": f"正在执行 {tc['name']}…",
                })
            tool_result = await mcp_web_search.call_tool_async(tc["name"], args)
            yield ("tool_result", {"name": tc["name"], "result": _truncate_tool_result(tool_result)})
            if component_type:
                props = _build_component_props(tc["name"], args, tool_result)
                if props is not None:
                    yield ("render_component", {
                        "component_type": component_type,
                        "tool_call_id": tc["id"],
                        "props": props,
                    })
                else:
                    yield ("component_error", {
                        "component_type": component_type,
                        "tool_call_id": tc["id"],
                        "error_message": tool_result[:100] if tool_result else "工具执行失败",
                    })
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": tool_result})

    step["status"] = "error"
    step["error_message"] = "超过单步 ReAct 轮次"


async def _execute_plan_steps(
    session_id: str,
    plan: dict,
    is_disconnected: Callable[[], Awaitable[bool]],
    start_index: int | None = None,
) -> AsyncGenerator[tuple[str, dict], None]:
    memory = get_or_load(session_id)
    messages = plan["execution_messages"]
    tools = plan["pending_state"]["tools"]
    plan["status"] = "running"
    _save_runtime_state(session_id)
    idx = plan.get("current_step_index", 0) if start_index is None else start_index

    while idx < len(plan["steps"]):
        step = plan["steps"][idx]
        if step["status"] in {"done", "skipped"}:
            idx += 1
            continue
        plan["current_step_index"] = idx
        for event in _set_plan_step_status(plan, idx, "running", error_message=""):
            yield event
        _save_runtime_state(session_id)
        async for event in _stream_plan_step_rounds(
            session_id, plan, idx, messages, tools, is_disconnected,
        ):
            yield event
        if step["status"] == "waiting":
            _save_runtime_state(session_id)
            return
        if step["status"] == "error":
            for event in _set_plan_step_status(plan, idx, "error", error_message=step.get("error_message") or "步骤失败"):
                yield event
            plan["status"] = "paused"
            _save_runtime_state(session_id)
            yield ("await_user", {
                "kind": "plan_decision",
                "plan_id": plan["plan_id"],
                "step_id": step["id"],
                "title": plan["title"],
                "steps": plan["steps"],
                "error_message": step.get("error_message") or "步骤失败",
            })
            yield ("done", {})
            return
        for event in _set_plan_step_status(plan, idx, "done", result_summary=step.get("result_summary") or "已完成"):
            yield event
        _save_runtime_state(session_id)
        idx += 1

    plan["status"] = "done"
    yield _activity_replace(plan["plan_id"], "/status", "done")
    summary = "\n".join(
        f"{idx + 1}. {step['title']} - {step['status']}: {step.get('result_summary') or ''}"
        for idx, step in enumerate(plan["steps"])
    )
    memory.add(Memory.USER, plan["pending_state"]["user_input"])
    memory.add(Memory.AI, f"计划「{plan['title']}」已完成。\n{summary}")
    session_plans = _PLANS.get(session_id)
    if isinstance(session_plans, dict):
        session_plans.pop(plan["plan_id"], None)
        if not session_plans:
            _PLANS.pop(session_id, None)
    _save_runtime_state(session_id)
    yield ("done", {})


def plan_confirm_response(
    session_id: str,
    plan_id: str,
    steps: list[dict],
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncGenerator[tuple[str, dict], None]:
    if API_MODE != "chat":
        raise PlanUnavailable("plan_confirm 仅在 API_MODE=chat 下可用")
    plan = _get_plan(session_id, plan_id)
    if plan["status"] != "pending_confirmation":
        raise PlanStateMismatch(plan["status"])
    normalized_steps = _normalize_plan_steps(steps)
    plan["steps"] = normalized_steps
    plan["status"] = "confirmed"
    pending = plan["pending_state"]
    messages = pending["messages"]
    messages.append({
        "role": "tool",
        "tool_call_id": pending["tool_call_id"],
        "content": _build_plan_confirm_tool_result(plan),
    })
    plan["execution_messages"] = messages
    _save_runtime_state(session_id)
    return _plan_confirm_inner(session_id, plan, is_disconnected)


async def _plan_confirm_inner(
    session_id: str,
    plan: dict,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncGenerator[tuple[str, dict], None]:
    yield ("activity_snapshot", _plan_snapshot(plan, editable=False))
    async for event in _execute_plan_steps(session_id, plan, is_disconnected, 0):
        yield event


def plan_decision_response(
    session_id: str,
    plan_id: str,
    step_id: str,
    decision: str,
    steps: list[dict] | None,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncGenerator[tuple[str, dict], None]:
    if API_MODE != "chat":
        raise PlanUnavailable("plan_decision 仅在 API_MODE=chat 下可用")
    if decision not in {"skip", "retry", "update"}:
        raise PlanValidationError("decision 必须是 skip/retry/update")
    plan = _get_plan(session_id, plan_id)
    if plan["status"] != "paused":
        raise PlanStateMismatch(plan["status"])
    idx = next((i for i, s in enumerate(plan["steps"]) if s["id"] == step_id), -1)
    if idx < 0:
        raise PlanValidationError("step_id 不存在")
    if decision == "update":
        plan["steps"], idx = _merge_updated_plan_steps(plan["steps"], steps or [], step_id)
    elif decision == "skip":
        plan["steps"][idx]["status"] = "skipped"
        plan["steps"][idx]["error_message"] = None
    else:
        plan["steps"][idx]["status"] = "pending"
        plan["steps"][idx]["error_message"] = None
    plan["status"] = "running"
    _save_runtime_state(session_id)
    return _plan_decision_inner(session_id, plan, idx, decision, is_disconnected)


async def _plan_decision_inner(
    session_id: str,
    plan: dict,
    step_index: int,
    decision: str,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncGenerator[tuple[str, dict], None]:
    yield ("activity_snapshot", _plan_snapshot(plan, editable=False))
    if decision == "skip":
        yield _activity_replace(plan["plan_id"], f"/steps/{step_index}/status", "skipped")
        start = step_index + 1
    else:
        start = step_index
    async for event in _execute_plan_steps(session_id, plan, is_disconnected, start):
        yield event


def plan_continue_response(
    session_id: str,
    plan_id: str,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncGenerator[tuple[str, dict], None]:
    """服务重启/页面刷新后恢复 running plan 的执行流。"""
    if API_MODE != "chat":
        raise PlanUnavailable("plan_continue 仅在 API_MODE=chat 下可用")
    plan = _get_plan(session_id, plan_id)
    if plan.get("status") != "running":
        raise PlanStateMismatch(plan.get("status") or "")
    if not isinstance(plan.get("execution_messages"), list):
        raise PlanValidationError("计划缺少 execution_messages,无法继续")
    pending_state = plan.get("pending_state")
    if not isinstance(pending_state, dict) or not isinstance(pending_state.get("tools"), list):
        raise PlanValidationError("计划缺少 tools,无法继续")
    current_idx = plan.get("current_step_index", 0)
    if not isinstance(current_idx, int) or current_idx < 0:
        raise PlanValidationError("计划缺少 current_step_index,无法继续")
    for step in plan.get("steps", []):
        if isinstance(step, dict) and step.get("status") in {"running", "waiting"}:
            step["status"] = "pending"
            step["error_message"] = None
    current_idx = min(current_idx, max(len(plan.get("steps", [])) - 1, 0))
    plan["current_step_index"] = current_idx
    _save_runtime_state(session_id)
    return _plan_continue_inner(session_id, plan, is_disconnected, current_idx)


async def _plan_continue_inner(
    session_id: str,
    plan: dict,
    is_disconnected: Callable[[], Awaitable[bool]],
    start_index: int,
) -> AsyncGenerator[tuple[str, dict], None]:
    yield ("activity_snapshot", _plan_snapshot(plan, editable=False))
    async for event in _execute_plan_steps(session_id, plan, is_disconnected, start_index):
        yield event


# ============================================================
# UI action —— /api/ui_action 入口
# ============================================================

def ui_action_response(
    session_id: str,
    surface_id: str,
    component_id: str,
    event_name: str,
    is_disconnected: Callable[[], Awaitable[bool]],
    *,
    form_data: dict | None = None,
) -> AsyncGenerator[tuple[str, dict], None]:
    """声明式 UI button 点击入口:校验内存态 action 后,作为结构化用户事件进入 ReAct。

    Phase 8b: form_data 是前端 surface.data 快照(含表单字段值),会注入到
    [UI Action] 文本供模型读取,并同步写回 _UI_SURFACES[..]['data'] 保持
    服务端镜像与客户端一致。
    """
    if API_MODE != "chat":
        raise UiActionUnavailable("ui_action 仅在 API_MODE=chat 下可用")
    if not SESSION_ID_RE.match(session_id):
        raise InvalidSessionId(session_id)
    surface = _get_ui_surface(session_id, surface_id)
    action = surface.get("actions", {}).get(component_id)
    if action is None:
        raise UiActionNotFound(component_id)
    if action["event_name"] != event_name:
        raise UiActionMismatch(event_name)

    # Phase 8b: 将表单快照写回服务端 surface,保持双端一致。
    # Fix B5: 浅合并而非整体覆盖 —— 避免擦掉模型通过 update_ui_data 写入的、
    # 前端不可见的私有字段(如 token/internal state)。前端表单的 value_path
    # 通常是顶层 key,浅合并已足够;深嵌套表单字段会整体覆盖对应顶层 key,
    # 这是浅合并的固有限制,可接受。
    if isinstance(form_data, dict) and form_data:
        existing_data = surface.get("data")
        if not isinstance(existing_data, dict):
            existing_data = {}
            surface["data"] = existing_data
        existing_data.update(form_data)
        _save_runtime_state(session_id)

    context_json = json.dumps(action.get("context") or {}, ensure_ascii=False)
    lines = [
        "[UI Action]",
        "用户点击了声明式 UI 上的按钮。",
        f"surface_id: {surface_id}",
        f"component_id: {component_id}",
        f"label: {action.get('label') or component_id}",
        f"event_name: {event_name}",
        f"context: {context_json}",
    ]
    if isinstance(form_data, dict) and form_data:
        lines.append(f"form_data: {json.dumps(form_data, ensure_ascii=False)}")
    lines.append("请基于这个用户交互继续完成任务;如需更新当前 UI,调用 update_ui_data。")
    user_event = "\n".join(lines)
    memory = get_or_load(session_id)
    return stream_agent_response(memory, user_event, is_disconnected, session_id, None)


# ============================================================
# Phase 9a Agent Steering —— /api/steer 入口
# ============================================================

def steer_response(session_id: str, message: str) -> dict:
    """把用户的纠偏指令入队, 由当前/下次 ReAct 流在轮次边界消费。

    **关键设计**: 本函数立即返回 dict (非 SSE 流), 且不获取 _SESSION_LOCKS ——
    否则与正在持有锁的 SSE 流死锁。允许无活跃流时入队 (等待下一次 /api/chat 消费),
    简化前端时序; 也允许同一活跃流期间入队多条 (单 turn drain 时一并消费)。

    raises:
        SteerUnavailable: 非 chat 模式 / message 非空校验失败
        InvalidSessionId: session_id 非 UUID 形态
    """
    if API_MODE != "chat":
        raise SteerUnavailable("steer 仅在 API_MODE=chat 下可用")
    if not SESSION_ID_RE.match(session_id):
        raise InvalidSessionId(session_id)
    if not isinstance(message, str) or not message.strip():
        raise SteerUnavailable("steer message 不能为空")

    queue = get_steer_queue(session_id)
    payload = message.strip()
    queue.put_nowait(payload)
    logger.info(
        "steer 入队: session=%s msg_chars=%d queue_size=%d",
        session_id, len(payload), queue.qsize(),
    )
    return {"ok": True, "queued": True, "queue_size": queue.qsize()}


# ============================================================
# HITL resume —— /api/resume 入口
# ============================================================

def resume_chat_response(
    session_id: str,
    tool_call_id: str,
    decision: str,
    answer: str | None,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncGenerator[tuple[str, dict], None]:
    """HITL resume 入口:**同步外壳**做参数校验 + 弹出 _PENDING(可抛异常被 HTTP 翻译),
    返回内层 _resume_inner 异步生成器供 StreamingResponse 消费。

    必须 split 是因为 async generator 的 body 只有在第一次 __anext__ 时才执行,
    若直接写 async def + yield,异常将延迟到流开始后才抛出,HTTP 层无法翻译。

    decision 取值:
        - "answer"  (ask_user)  : answer=用户答复文本
        - "approve" (execute_shell_command): 同意执行(本 demo 用 stub 字符串)
        - "reject"  (execute_shell_command): 拒绝,answer=可选拒绝理由
    """
    if not SESSION_ID_RE.match(session_id):
        raise InvalidSessionId(session_id)
    _restore_runtime_state(session_id)
    state = _PENDING.pop(session_id, None)
    if state is None:
        raise PendingNotFound(session_id)
    if state["awaiting"]["tool_call_id"] != tool_call_id:
        # 不匹配:把 pending 还回去,让用户能用正确 id 重试(否则 pending 就丢了)
        _PENDING[session_id] = state
        _save_runtime_state(session_id)
        raise PendingMismatch(tool_call_id)
    _save_runtime_state(session_id)
    return _resume_inner(session_id, state, decision, answer, is_disconnected)


async def _resume_inner(
    session_id: str,
    state: dict,
    decision: str,
    answer: str | None,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncGenerator[tuple[str, dict], None]:
    """resume 的真正流式体:构造 tool result → append role=tool → 继续 _stream_react_rounds。"""
    memory = get_or_load(session_id)
    awaiting = state["awaiting"]
    name = awaiting["name"]

    # 构造 tool result(模型看到的字符串)
    if name == "ask_user":
        tool_result = answer or "(用户未提供答复)"
    elif name == "execute_shell_command":
        if decision == "approve":
            cmd = awaiting["args"].get("command", "")
            if ALLOW_REAL_SHELL:
                # 开关开启:真执行。失败/超时/黑名单都被 _execute_shell_real 转成字符串。
                tool_result = await _execute_shell_real(cmd)
            else:
                # 默认安全态:不真执行,沿用原 demo stub。文案提示开关存在,避免被误判为 bug。
                tool_result = (
                    f"[demo stub] 已模拟执行命令: {cmd}\n"
                    "(本 demo 默认不真执行 shell,导出 ALLOW_REAL_SHELL=1 后才会真执行)"
                )
        else:  # reject
            tool_result = f"用户拒绝执行。理由: {answer or '(未填写)'}"
    else:
        tool_result = "(未知 HITL 工具)"

    logger.info(
        "HITL resume:session=%s tool=%s decision=%s tool_result=%s",
        session_id, name, decision, tool_result[:120],
    )

    state["messages"].append({
        "role": "tool",
        "tool_call_id": awaiting["tool_call_id"],
        "content": tool_result,
    })
    yield ("tool_result", {"name": name, "result": _truncate_tool_result(tool_result)})

    if state.get("plan_resume"):
        resume = state["plan_resume"]
        plan = _get_plan(session_id, resume["plan_id"])
        step_index = resume["step_index"]
        async for event in _stream_plan_step_rounds(
            session_id, plan, step_index,
            state["messages"], state["tools"],
            is_disconnected,
            state["round_num"],
            state["remaining_tool_calls"],
            append_step_input=False,
        ):
            yield event
        step = plan["steps"][step_index]
        if step["status"] == "done":
            for event in _set_plan_step_status(
                plan, step_index, "done", result_summary=step.get("result_summary") or "已完成",
            ):
                yield event
            _save_runtime_state(session_id)
            async for event in _execute_plan_steps(session_id, plan, is_disconnected, step_index + 1):
                yield event
        elif step["status"] == "error":
            for event in _set_plan_step_status(
                plan, step_index, "error", error_message=step.get("error_message") or "步骤失败",
            ):
                yield event
            plan["status"] = "paused"
            _save_runtime_state(session_id)
            yield ("await_user", {
                "kind": "plan_decision",
                "plan_id": plan["plan_id"],
                "step_id": step["id"],
                "title": plan["title"],
                "steps": plan["steps"],
                "error_message": step.get("error_message") or "步骤失败",
            })
            yield ("done", {})
        return

    async for event in _stream_react_rounds(
        session_id, memory,
        state["user_input"], state["messages"], state["tools"],
        state["round_num"],
        state["remaining_tool_calls"],
        is_disconnected,
        state.get("tool_stats"),
    ):
        yield event


# ============================================================
# ReAct 主循环 - CLI 同步版
# ============================================================

def react(memory: Memory, latest_input: str) -> str:
    """ReAct 核心循环:Thought -> Action -> Observation,受 MAX_ROUNDS 保护。CLI 用。

    API_MODE=chat 时走 native function calling 路径(_react_chat_native),
    联网搜索默认开启(MCP WebSearch),由模型自主判断是否调用。
    """
    if API_MODE == "chat":
        return _react_chat_native(memory, latest_input)

    for round_num in range(MAX_ROUNDS):
        prompt = build_prompt(USER_PROMPT, TOOLS, memory, latest_input)
        logger.info("ReAct 第 %d 轮开始:prompt_chars=%d latest_input_chars=%d memory_turns=%d",
                    round_num, len(prompt), len(latest_input), len(memory.memories))
        logger.info("prompt=\n%s", prompt)

        llm_result = llm(prompt)
        logger.info("第 %d 轮 llmResult(%d 字)=\n%s", round_num, len(llm_result), llm_result)

        matched_tool_name = match_tool_action(llm_result)
        if matched_tool_name is None:
            clean, _ = _extract_confidence_signal(llm_result, _new_tool_stats(), latest_input)
            return clean

        logger.info("执行工具调用:%s,开始", matched_tool_name)
        action_input = parse_action_input(llm_result)
        logger.info("工具参数:%s", action_input)
        tool_result = execute_tool(matched_tool_name, action_input)
        logger.info("执行工具调用:%s,结果=%s", matched_tool_name, tool_result)

        latest_input += f"\n{llm_result}\nObservation: {tool_result}"

    logger.warning("超过最大 ReAct 轮次（%d），中断", MAX_ROUNDS)
    return f"（已达到最大 ReAct 轮次 {MAX_ROUNDS}，对话中断，请重试或换个问法）"


# ============================================================
# ReAct 主循环 - Web 异步流式版
# ============================================================

async def stream_agent_response(
    memory: Memory,
    user_input: str,
    is_disconnected: Callable[[], Awaitable[bool]],
    session_id: str = "",
    context: dict | None = None,
    images: list[str] | None = None,
    attachments: list[dict] | None = None,
) -> AsyncGenerator[tuple[str, dict], None]:
    """ReAct 流式循环,yield 抽象 (event_name, payload_dict) 元组,与 SSE/HTTP 层解耦。

    event_name 与 payload 字段直接对齐 SSE 事件契约:
        - ("status", {"phase": "thinking"|"answering", "round": int})
        - ("thinking", {"text": str})            思考摘要增量
        - ("chunk", {"text": str})               答复增量(无 ReAct 工具时直接转发,有工具时缓冲到非工具回合再转发)
        - ("search_status", {"phase": str})      内置 web_search 阶段,仅 responses 模式
        - ("tool_call", {"name": str, "args": dict})
        - ("tool_result", {"name": str, "result": str})  result 已截断到 TOOL_RESULT_PREVIEW_CHARS
        - ("await_user", {"tool_call_id":..., "name":..., "args":..., "kind": "input"|"approval"})
                                                  HITL 工具触发,流即将关,等 /api/resume(仅 chat 模式)
        - ("ui_hint", {"mode": "focus"|"compact", "reason": str})
                                                  上下文感知推荐的 UI 模式(Phase 2)
        - ("ui_surface_create", {"surface_id": str})
        - ("ui_surface_update", {"surface_id": str, "components": list})
        - ("ui_data_update", {"surface_id": str, "path": "/", "value": dict})
                                                  声明式 UI 渲染事件(Phase 3a,仅 chat 模式 render_ui)
        - ("confidence_signal", {"score": float, "level": str, "reason": str, "draft": bool})
                                                  置信度信号(Phase 6);低置信度草稿需用户采纳才写 Memory
        - ("done", {})                           正常结束(也包括 HITL 中断时的"软关流")
        - ("error", {"message": str})            终止流的错误

    is_disconnected: 由调用方注入的"客户端是否断连"探测,在每个 chunk 之间被 await。
    HTTP 层传 `request.is_disconnected`(starlette Request 上的 bound method)。

    session_id: chat 模式 HITL 用,作为 _PENDING 的 key;responses 模式忽略。
    context: 前端上报的环境上下文(viewport_width, selected_text, session_message_count)。

    API_MODE=chat 时走 native function calling 路径(_stream_chat_native),
    联网搜索默认开启(MCP WebSearch),通过 tool_call/tool_result 事件可见;不发 search_status。
    """
    adaptive_fragment, ui_mode, ui_reason = _compute_adaptive_prompt(context, memory)
    if ui_mode != "chat":
        yield ("ui_hint", {"mode": ui_mode, "reason": ui_reason})

    # Phase 9b: 流开头发一次完整 agent 状态快照, 前端用于初始化 reducer。
    # 仅 chat 模式 (responses 模式不参与 Phase 3+ 新功能, 与一贯约束一致)。
    if API_MODE == "chat" and session_id:
        yield ("agent_state_snapshot", _agent_state_payload(session_id, round_num=0))

    if API_MODE == "chat":
        async for event in _stream_chat_native(
            memory, user_input, is_disconnected, session_id, adaptive_fragment,
            images=images, attachments=attachments,
        ):
            yield event
        return

    # Phase 10a: responses 模式是字符串 prompt 接口, 无法承载结构化 vision content。
    # 与 Phase 3+ 一贯约束一致: 不为 responses 模式适配多模态。显式失败而不静默丢图。
    if images:
        yield ("error", {
            "message": "图片附件仅在 chat 模式可用。请重启服务并设置 export API_MODE=chat",
        })
        return

    # Phase 10b: responses 模式同理,无法把附件注入字符串 prompt;显式失败。
    if attachments:
        yield ("error", {
            "message": "文件附件仅在 chat 模式可用。请重启服务并设置 export API_MODE=chat",
        })
        return

    latest_input = user_input
    has_tools = bool(TOOLS)
    tool_stats = _new_tool_stats()

    for round_num in range(MAX_ROUNDS):
        yield ("status", {"phase": "thinking", "round": round_num})
        prompt = build_prompt(USER_PROMPT, TOOLS, memory, latest_input, adaptive_fragment)
        logger.info("ReAct 第 %d 轮开始:prompt_chars=%d latest_input_chars=%d memory_turns=%d",
                    round_num, len(prompt), len(latest_input), len(memory.memories))
        logger.info("prompt=\n%s", prompt)

        full = ""
        buffered: list[str] = []
        confidence_tail = ""
        emitted_answer_chars = 0
        answering_flipped = False

        try:
            async for kind, text in llm_stream(prompt):
                if await is_disconnected():
                    return
                if kind == "thinking":
                    yield ("thinking", {"text": text})
                    continue
                if kind == "search_status":
                    if text == "searching":
                        tool_stats["search_calls"] += 1
                    yield ("search_status", {"phase": text})
                    continue
                if kind == "error":
                    tool_stats["errors"] += 1
                    yield ("error", {"message": text})
                    return
                # 正式回答首字 —— 补发一帧 answering 状态,方便前端折叠思考面板
                if not answering_flipped:
                    answering_flipped = True
                    yield ("status", {"phase": "answering", "round": round_num})
                full += text
                if has_tools:
                    buffered.append(text)
                else:
                    confidence_tail, emit_text = _buffer_confidence_delta(confidence_tail, text)
                    if emit_text:
                        emitted_answer_chars += len(emit_text)
                        yield ("chunk", {"text": emit_text})
        except Exception as exc:  # 网络 / API 错误
            logger.exception("LLM 调用失败")
            yield ("error", {"message": f"LLM 调用失败: {exc}"})
            return

        logger.info("第 %d 轮 llmResult(%d 字)=\n%s", round_num, len(full), full)

        matched = match_tool_action(full)
        if matched:
            try:
                args = parse_action_input(full)
            except Exception as exc:
                logger.exception("工具参数解析失败")
                yield ("error", {"message": f"工具参数解析失败: {exc}"})
                return
            logger.info("执行工具调用:%s,开始", matched)
            logger.info("工具参数:%s", args)
            tool_stats["tool_calls"] += 1
            yield ("tool_call", {"name": matched, "args": args})
            tool_result = execute_tool(matched, args)
            logger.info("执行工具调用:%s,结果=%s", matched, tool_result)
            yield ("tool_result", {"name": matched, "result": _truncate_tool_result(tool_result)})
            latest_input += f"\n{full}\nObservation: {tool_result}"
        else:
            if has_tools:
                emitted_answer_chars = 0
            async for event in _finalize_confident_answer(
                memory, session_id, user_input, full, emitted_answer_chars, tool_stats,
            ):
                yield event
            return

    yield ("error", {"message": "超过最大 ReAct 轮次，已中断"})
