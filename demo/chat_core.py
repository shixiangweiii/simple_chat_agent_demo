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
                        "支持 type: text/card/row/column/table/button。button 可带 "
                        "action: {event_name, context?} 供前端点击后回传。"
                    ),
                    "items": {
                        "type": "object",
                        "required": ["id", "type"],
                    },
                },
                "data": {
                    "type": "object",
                    "description": "可选数据对象,table.rows_path 与 text.path 可用 JSON Pointer 引用。",
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
5. **声明式 UI**：当表格、对比卡片或按钮能让回答更清楚时，调用 `render_ui`；用户点击 UI 按钮后，如需刷新已有界面数据，调用 `update_ui_data`；
6. **协作式计划**：当任务明显需要多个步骤或用户确认执行顺序时，调用 `create_plan` 提交计划给用户审阅；计划确认后再逐步执行；
7. 当下方"工具列表"中有自定义工具时，按 Thought/Action/Observation 协议调用；
8. 既不需要联网也不需要自定义工具时，直接给出清晰、有帮助的回复。

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
    """按 session 懒恢复运行态。损坏或版本不兼容时忽略,不影响普通聊天。"""
    if session_id in _RUNTIME_RESTORED:
        return
    path = _runtime_state_path(session_id)
    _RUNTIME_RESTORED.add(session_id)
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("runtime state 损坏,已忽略: %s", path, exc_info=True)
        return
    if data.get("version") != RUNTIME_STATE_VERSION or data.get("session_id") != session_id:
        logger.warning("runtime state 版本或 session 不匹配,已忽略: %s", path)
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
                    safe_surface["actions"] = _extract_ui_actions(components)
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
                _PLANS[session_id] = safe_plans
                _sync_plan_seq_from(safe_plans)
    except Exception:
        logger.warning("runtime state 结构损坏,已忽略: %s", path, exc_info=True)


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


def archive_session(session_id: str) -> dict:
    """覆盖式归档当前 session 的 Memory 到 markdown。空 Memory 跳过。

    返回与 /api/archive 端点期望响应一致的 dict:
        - 空 Memory: {"ok": True, "skipped": True}
        - 已归档:   {"ok": True, "path": str(path)}
    """
    memory = get_or_load(session_id)
    if not memory.memories:
        return {"ok": True, "skipped": True}
    path = _archive_path(session_id)
    _atomic_write(path, memory.to_markdown(session_id))
    return {"ok": True, "path": str(path)}


def reset_session(session_id: str) -> None:
    """原地重置:清当前 session 内存 + 删归档文件,session_id 保留。"""
    _archive_path(session_id)  # 先做 sid 校验
    sessions.pop(session_id, None)
    _UI_SURFACES.pop(session_id, None)
    _PLANS.pop(session_id, None)
    _PENDING.pop(session_id, None)
    _ANSWER_DRAFTS.pop(session_id, None)
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


def _memory_to_messages(
    memory: Memory, system_prompt: str, user_input: str, adaptive_fragment: str = "",
) -> list[dict]:
    """把 flat-string Memory 转成 OpenAI Chat Completions messages 数组。

    仅用于 chat-mode-with-tools 路径。Memory 存储格式不变(仍是 role/msg 二元组)。
    角色映射: 用户 -> user, AI -> assistant; 不携带历史 tool_calls(单 turn 内才需要)。
    """
    sys_content = system_prompt
    if adaptive_fragment:
        sys_content = system_prompt + "\n\n" + adaptive_fragment
    messages: list[dict] = [{"role": "system", "content": sys_content}]
    for m in memory.memories:
        role = "user" if m["role"] == Memory.USER else "assistant"
        messages.append({"role": role, "content": m["msg"]})
    messages.append({"role": "user", "content": user_input})
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
    """若 text 尾部正在形成 confidence marker,返回应暂存的起点。"""
    if not text:
        return None
    bracket = text.rfind("[")
    if bracket >= 0:
        candidate = text[bracket:].lower()
        if candidate.startswith("[confidence") or "[confidence".startswith(candidate):
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
    """仅暂存疑似 confidence marker 的尾部,其余内容立即流式输出。"""
    combined = tail + delta
    start = _confidence_buffer_start(combined)
    if start is None:
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
        elif isinstance(old, dict) and old.get("status") in {"done", "skipped"}:
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

_SUPPORTED_UI_COMPONENT_TYPES = {"text", "card", "row", "column", "table", "button"}


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


def _register_ui_surface(session_id: str, surface_id: str, components: list[dict], data: dict) -> None:
    """注册 Phase 3b 内存态 surface。"""
    if not SESSION_ID_RE.match(session_id):
        raise InvalidSessionId(session_id)
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


def _json_pointer_set(data: dict, path: str, value) -> dict:
    """按 JSON Pointer 写入 data。/ 表示整体替换,深路径要求父节点已存在。"""
    tokens = _decode_json_pointer(path)
    if not tokens:
        if not isinstance(value, dict):
            raise ValueError("path=/ 时 value 必须是对象")
        return value

    cur = data
    for token in tokens[:-1]:
        if isinstance(cur, dict):
            if token not in cur:
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
        seen_ids.add(comp_id)
        has_root = has_root or comp_id == "root"
        cleaned.append(comp)

    if not has_root:
        return {"ok": False, "error": "render_ui 参数错误: components 必须包含 id='root' 的根节点"}

    data = args.get("data")
    if data is not None and not isinstance(data, dict):
        return {"ok": False, "error": "render_ui 参数错误: data 必须是对象"}

    return {
        "ok": True,
        "surface_id": surface_id,
        "components": cleaned,
        "data": data or {},
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
    if result.get("ok"):
        _register_ui_surface(
            session_id,
            result["surface_id"],
            result["components"],
            result["data"],
        )
        result["tool_result"] = "UI 已渲染"
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
                    yield ("ui_surface_update", {
                        "surface_id": surface_id,
                        "components": result["components"],
                    })
                    if result.get("data"):
                        yield ("ui_data_update", {
                            "surface_id": surface_id,
                            "path": "/",
                            "value": result["data"],
                        })
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
) -> AsyncGenerator[tuple[str, dict], None]:
    """[chat 模式 + native function calling] 异步流式 ReAct 循环,Web 用。

    yield 的 (event_name, payload) 元组**完全复用**现有 SSE 契约,新增 await_user 一项:
        status / thinking / chunk / tool_call / tool_result / await_user / done / error
    """
    # 新一轮 chat：清掉旧 pending，防止僵尸 _PENDING 项常驻
    if _PENDING.pop(session_id, None) is not None:
        _save_runtime_state(session_id)

    messages = _memory_to_messages(memory, USER_PROMPT, user_input, adaptive_fragment)
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
                    yield ("ui_surface_update", {"surface_id": surface_id, "components": result["components"]})
                    if result.get("data"):
                        yield ("ui_data_update", {"surface_id": surface_id, "path": "/", "value": result["data"]})
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
) -> AsyncGenerator[tuple[str, dict], None]:
    """声明式 UI button 点击入口:校验内存态 action 后,作为结构化用户事件进入 ReAct。"""
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

    context_json = json.dumps(action.get("context") or {}, ensure_ascii=False)
    user_event = "\n".join([
        "[UI Action]",
        "用户点击了声明式 UI 上的按钮。",
        f"surface_id: {surface_id}",
        f"component_id: {component_id}",
        f"label: {action.get('label') or component_id}",
        f"event_name: {event_name}",
        f"context: {context_json}",
        "请基于这个用户交互继续完成任务;如需更新当前 UI,调用 update_ui_data。",
    ])
    memory = get_or_load(session_id)
    return stream_agent_response(memory, user_event, is_disconnected, session_id, None)


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

    if API_MODE == "chat":
        async for event in _stream_chat_native(
            memory, user_input, is_disconnected, session_id, adaptive_fragment,
        ):
            yield event
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
