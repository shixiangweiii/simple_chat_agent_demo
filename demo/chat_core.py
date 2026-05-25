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
}

# 每个 LOCAL_TOOL 的前端交互类型:input(等用户输入回答) | approval(等同意/拒绝)
_LOCAL_TOOL_KIND: dict[str, str] = {
    "ask_user": "input",
    "execute_shell_command": "approval",
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
5. 当下方"工具列表"中有自定义工具时，按 Thought/Action/Observation 协议调用；
6. 既不需要联网也不需要自定义工具时，直接给出清晰、有帮助的回复。

## 行为准则
- 回复简洁明了，避免冗余；
- 如果不确定答案，如实告知用户；
- 回复时根据内容选择最合适的展现方式；"""


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
# 标准 UUID 形态:8-4-4-4-12 hex 字符。所有 disk 操作前必须校验,防止路径注入
SESSION_ID_RE = re.compile(r"^[0-9a-f-]{36}$")
# 头部元信息注释格式
_META_RE = re.compile(r"^<!-- (session_id|updated_at|turns|preview): (.*?) -->$")

# 模块级会话内存缓存
sessions: dict[str, Memory] = {}


class InvalidSessionId(ValueError):
    """session_id 不符合 UUID 形态。HTTP 层应翻译为 400。"""


class HistoryNotFound(LookupError):
    """指定 session_id 没有归档文件。HTTP 层应翻译为 404。"""


class PendingNotFound(LookupError):
    """session 当前无 pending HITL(可能已被新 chat 清掉,或本来就没有)。HTTP 层应翻译为 404。"""


class PendingMismatch(ValueError):
    """resume 提交的 tool_call_id 与 pending awaiting.tool_call_id 不匹配,
    通常意味着前端拿了过期的 HITL bubble 提交。HTTP 层应翻译为 409。"""


def _archive_path(session_id: str) -> Path:
    if not SESSION_ID_RE.match(session_id):
        raise InvalidSessionId(session_id)
    return ARCHIVE_DIR / f"{session_id}.md"


def _atomic_write(path: Path, text: str) -> None:
    """先写 .tmp 再 rename,避免崩溃留下半文件。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


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
    sessions.pop(session_id, None)
    path = _archive_path(session_id)  # 同时做 sid 校验
    path.unlink(missing_ok=True)


def delete_session(session_id: str) -> None:
    """删除 session:磁盘归档 + 内存 session 同步移除。"""
    path = _archive_path(session_id)  # 同时做 sid 校验
    path.unlink(missing_ok=True)
    sessions.pop(session_id, None)


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
# Prompt 拼装
# ============================================================

def build_prompt(user_prompt, tools, memory, latest_input):
    """拼装完整的 Prompt:
    - `tools == []`(常态):角色设定 + 对话记录 + 最新输入。完全不输出 ReAct 框架,
      避免"无工具"措辞反向压制 API 层的内置 web_search。
    - `tools` 非空:角色设定 + 工具列表 + ReAct 格式说明 + 注意事项 + 对话记录 + 最新输入。
      框架在 TOOLS 列表添加自定义工具时自动恢复。
    """
    sections = [user_prompt, "---------------------"]

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


def _memory_to_messages(memory: Memory, system_prompt: str, user_input: str) -> list[dict]:
    """把 flat-string Memory 转成 OpenAI Chat Completions messages 数组。

    仅用于 chat-mode-with-tools 路径。Memory 存储格式不变(仍是 role/msg 二元组)。
    角色映射: 用户 -> user, AI -> assistant; 不携带历史 tool_calls(单 turn 内才需要)。
    """
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
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
        _NATIVE_TOOLS_CACHE = [*mcp_tools, *LOCAL_TOOLS.values()]
    return _NATIVE_TOOLS_CACHE


async def _build_native_tools_async() -> list[dict]:
    """异步:首次调用走 mcp_web_search 的 schema 发现 + 合并 LOCAL_TOOLS 并缓存。Web ReAct 循环用。"""
    global _NATIVE_TOOLS_CACHE
    if _NATIVE_TOOLS_CACHE is None:
        mcp_tools = await mcp_web_search.discover_tool_spec_async()
        _NATIVE_TOOLS_CACHE = [*mcp_tools, *LOCAL_TOOLS.values()]
    return _NATIVE_TOOLS_CACHE


def _truncate_tool_result(text: str) -> str:
    """tool_result 事件的截断逻辑,与上面 stream_agent_response 中保持一致的措辞。"""
    if len(text) <= TOOL_RESULT_PREVIEW_CHARS:
        return text
    return text[:TOOL_RESULT_PREVIEW_CHARS] + f"...（截断，原文 {len(text)} 字符见 server log）"


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
            return content

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
) -> AsyncGenerator[tuple[str, dict], None]:
    """ReAct 流式循环主体,支持两种入口:
       - fresh start: start_round=0, pending_remaining=[]
       - resume:      start_round=断点轮次, pending_remaining=断点未消费的剩余 tool_calls

    HITL 触发时把恢复点写入 _PENDING[session_id],yield ("await_user", ...) + ("done", {}) 关流。
    """
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
                        yield ("error", {"message": payload})
                        return
                    # kind == "content"
                    if not answering_flipped:
                        answering_flipped = True
                        yield ("status", {"phase": "answering", "round": round_num})
                    accumulated_content += payload
                    yield ("chunk", {"text": payload})
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
                # 收尾:把本 turn 的 final assistant content 写回 Memory
                memory.add(Memory.USER, user_input)
                memory.add(Memory.AI, accumulated_content)
                yield ("done", {})
                return

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
            yield ("tool_call", {"name": tc["name"], "args": args})

            if tc["name"] in LOCAL_TOOLS:
                # HITL 中断点:存恢复点,yield await_user + done,等 /api/resume 启新流接续
                _PENDING[session_id] = {
                    "user_input": user_input,
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
                }
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

            # 非 HITL:MCP 工具,正常执行
            tool_result = await mcp_web_search.call_tool_async(tc["name"], args)
            logger.info("执行工具调用:%s,结果=%s", tc["name"], tool_result)
            yield ("tool_result", {"name": tc["name"], "result": _truncate_tool_result(tool_result)})
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
) -> AsyncGenerator[tuple[str, dict], None]:
    """[chat 模式 + native function calling] 异步流式 ReAct 循环,Web 用。

    yield 的 (event_name, payload) 元组**完全复用**现有 SSE 契约,新增 await_user 一项:
        status / thinking / chunk / tool_call / tool_result / await_user / done / error
    """
    # 新一轮 chat:清掉旧 pending,防止僵尸 _PENDING 项常驻
    _PENDING.pop(session_id, None)

    messages = _memory_to_messages(memory, USER_PROMPT, user_input)
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
    state = _PENDING.pop(session_id, None)
    if state is None:
        raise PendingNotFound(session_id)
    if state["awaiting"]["tool_call_id"] != tool_call_id:
        # 不匹配:把 pending 还回去,让用户能用正确 id 重试(否则 pending 就丢了)
        _PENDING[session_id] = state
        raise PendingMismatch(tool_call_id)
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
            # demo 不真执行 shell —— 教学焦点是 HITL 流程,不引入真实 RCE 风险
            cmd = awaiting["args"].get("command", "")
            tool_result = (
                f"[demo stub] 已模拟执行命令: {cmd}\n"
                "(本 demo 不会真正执行 shell,仅演示 HITL 审批流程)"
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

    async for event in _stream_react_rounds(
        session_id, memory,
        state["user_input"], state["messages"], state["tools"],
        state["round_num"],
        state["remaining_tool_calls"],
        is_disconnected,
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
            return llm_result

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
        - ("done", {})                           正常结束(也包括 HITL 中断时的"软关流")
        - ("error", {"message": str})            终止流的错误

    is_disconnected: 由调用方注入的"客户端是否断连"探测,在每个 chunk 之间被 await。
    HTTP 层传 `request.is_disconnected`(starlette Request 上的 bound method)。

    session_id: chat 模式 HITL 用,作为 _PENDING 的 key;responses 模式忽略。

    API_MODE=chat 时走 native function calling 路径(_stream_chat_native),
    联网搜索默认开启(MCP WebSearch),通过 tool_call/tool_result 事件可见;不发 search_status。
    """
    if API_MODE == "chat":
        async for event in _stream_chat_native(memory, user_input, is_disconnected, session_id):
            yield event
        return

    latest_input = user_input
    has_tools = bool(TOOLS)

    for round_num in range(MAX_ROUNDS):
        yield ("status", {"phase": "thinking", "round": round_num})
        prompt = build_prompt(USER_PROMPT, TOOLS, memory, latest_input)
        logger.info("ReAct 第 %d 轮开始:prompt_chars=%d latest_input_chars=%d memory_turns=%d",
                    round_num, len(prompt), len(latest_input), len(memory.memories))
        logger.info("prompt=\n%s", prompt)

        full = ""
        buffered: list[str] = []
        answering_flipped = False

        try:
            async for kind, text in llm_stream(prompt):
                if await is_disconnected():
                    return
                if kind == "thinking":
                    yield ("thinking", {"text": text})
                    continue
                if kind == "search_status":
                    yield ("search_status", {"phase": text})
                    continue
                if kind == "error":
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
                    yield ("chunk", {"text": text})
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
            yield ("tool_call", {"name": matched, "args": args})
            tool_result = execute_tool(matched, args)
            logger.info("执行工具调用:%s,结果=%s", matched, tool_result)
            yield ("tool_result", {"name": matched, "result": _truncate_tool_result(tool_result)})
            latest_input += f"\n{full}\nObservation: {tool_result}"
        else:
            if has_tools:
                for c in buffered:
                    yield ("chunk", {"text": c})
            memory.add(Memory.USER, user_input)
            memory.add(Memory.AI, full)
            yield ("done", {})
            return

    yield ("error", {"message": "超过最大 ReAct 轮次，已中断"})
