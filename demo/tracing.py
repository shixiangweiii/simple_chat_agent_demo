"""可观测性叶子模块 —— Langfuse Python SDK v4 手动埋点的薄封装。

本项目**唯一** import langfuse 的模块;自身不 import 任何项目模块(叶子),可被 chat_core /
longterm_memory 直接使用;入口层(CLI / HTTP)只经 chat_core 的 re-export 接触它,守住分层。

设计要点(实测结论见 docs/调研/Langfuse可观测性集成方案.md):
- 手动 observation(start_observation + 显式 end),不改 OTel 当前上下文 —— 与本项目大量
  async generator 跨 yield 的写法兼容。所有 observation 都要在事件循环线程创建:
  run_in_executor 线程拿不到 contextvars,在那里创建会变成孤立 trace。
- 降级:未安装 langfuse / 缺 key / LANGFUSE_TRACING_ENABLED=false → 全部返回 NOOP,
  调用方无需判空,demo 行为与接入前完全一致。
- 容错:Obs 的所有方法吞异常(只打 debug 日志) —— 可观测性永远不能打断聊天主流程。
- 不阻塞事件循环:拼 UI 链接要的 project id 在后台线程解析并缓存;不用 SDK 的
  get_trace_url()(它首次调用会同步发网络请求,且不捕获异常)。

env(SDK 原生变量均透传生效):
    LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY  缺任一 → 追踪关闭
    LANGFUSE_BASE_URL                          自托管如 http://localhost:3000(SDK 也认 LANGFUSE_HOST)
    LANGFUSE_TRACING_ENABLED                   false → 追踪关闭
    LANGFUSE_PROJECT_ID                        可选,跳过启动时的 project id 网络查询
"""

import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator
from urllib.parse import quote, urlsplit

logger = logging.getLogger(__name__)

try:  # 可选依赖:未安装时整模块降级为 no-op
    from langfuse import Langfuse, propagate_attributes as _propagate_attributes
except Exception:  # noqa: BLE001  ImportError 或 SDK 自身导入失败都按"未安装"处理
    Langfuse = None  # type: ignore[assignment,misc]
    _propagate_attributes = None

_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_PROJECT_RETRY_SEC = 60.0
_METADATA_VALUE_MAX = 200  # propagate_attributes 的 metadata 值上限(SDK v4 校验规则)

_client = None
_init_done = False
_init_lock = threading.Lock()
_project_id: str | None = os.environ.get("LANGFUSE_PROJECT_ID") or None
_project_next_retry = 0.0
_project_lock = threading.Lock()


def _base_url() -> str:
    # 优先用客户端实际解析出的地址;否则按 SDK 的顺序:LANGFUSE_BASE_URL > LANGFUSE_HOST > 云端默认
    url = (
        getattr(_client, "_base_url", None)
        or os.environ.get("LANGFUSE_BASE_URL")
        or os.environ.get("LANGFUSE_HOST")
        or "https://cloud.langfuse.com"
    )
    return str(url).rstrip("/")


def _loopback_httpx_client():
    """base_url 的 host 是 localhost 时,给 SDK 一个绑定 IPv4 的 httpx client。

    实测(macOS + Docker Desktop 自托管):localhost 先解析到 ::1,httpx 经 IPv6 回环访问 Docker
    端口转发会读超时 / 被对端断开(urllib、裸 socket、IPv4 都正常)。SDK 的 project 查询、score 上报、
    media 上传都走这个 httpx client,于是只有 span(走另一条 OTLP exporter)能上报成功。
    只替换 SDK 内部连接方式,UI 链接仍用配置的 base_url(浏览器登录态按 host 区分,不能换成 127.0.0.1)。
    """
    if (urlsplit(_base_url()).hostname or "").lower() != "localhost":
        return None
    try:
        import httpx  # SDK 依赖,已随 langfuse 安装

        return httpx.Client(
            timeout=float(os.environ.get("LANGFUSE_TIMEOUT") or 5),
            transport=httpx.HTTPTransport(local_address="0.0.0.0"),
        )
    except Exception:
        logger.debug("IPv4 httpx client 创建失败,使用 SDK 默认 client", exc_info=True)
        return None


def _get_client():
    """懒初始化 Langfuse 客户端;任何原因不可用都返回 None(= 追踪关闭)。"""
    global _client, _init_done
    if _init_done:
        return _client
    with _init_lock:
        if _init_done:
            return _client
        _init_done = True
        if Langfuse is None:
            logger.info("Langfuse 追踪关闭:未安装 langfuse(pip install -r requirements.txt)")
            return None
        if os.environ.get("LANGFUSE_TRACING_ENABLED", "true").strip().lower() in ("false", "0", "no", "off"):
            logger.info("Langfuse 追踪关闭:LANGFUSE_TRACING_ENABLED=false")
            return None
        if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
            logger.info("Langfuse 追踪关闭:未配置 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY")
            return None
        try:
            _client = Langfuse(httpx_client=_loopback_httpx_client())
        except Exception:
            logger.warning("Langfuse 客户端初始化失败,追踪关闭", exc_info=True)
            _client = None
            return None
        logger.info("Langfuse 追踪已启用:base_url=%s", _base_url())
        _schedule_project_resolve()
        return _client


def _schedule_project_resolve() -> None:
    """后台线程解析 project id;失败后由 trace_url()/session_url() 惰性重试(间隔 60s)。"""
    global _project_next_retry
    if _project_id or _client is None:
        return
    with _project_lock:
        now = time.monotonic()
        if now < _project_next_retry:
            return
        _project_next_retry = now + _PROJECT_RETRY_SEC
    threading.Thread(target=_resolve_project_id, name="langfuse-project-id", daemon=True).start()


def _resolve_project_id() -> None:
    global _project_id
    try:
        data = getattr(_client.api.projects.get(), "data", None) or []
        if data and getattr(data[0], "id", None):
            _project_id = data[0].id
            logger.info("Langfuse 项目已连接:%s(%s)", getattr(data[0], "name", ""), _project_id)
        else:
            logger.warning("Langfuse 未返回项目信息,trace 链接不可用(trace_id 仍可复制)")
    except Exception as exc:  # 网络 / 鉴权失败:只影响链接,不影响上报
        logger.warning("Langfuse 连接或鉴权失败,trace 链接暂不可用: %s", exc)


def _clean(kwargs: dict) -> dict:
    """丢掉值为 None 的参数,避免显式 None 覆盖已写入的属性。"""
    return {k: v for k, v in kwargs.items() if v is not None}


def _str_metadata(metadata: dict | None) -> dict[str, str] | None:
    if not metadata:
        return None
    return {str(k): str(v)[:_METADATA_VALUE_MAX] for k, v in metadata.items() if v is not None}


class Obs:
    """Langfuse observation 的空安全包装。

    - raw 为 None 即空对象(NOOP):所有方法 no-op、child() 返回 NOOP —— 调用方不必判空。
    - end() 幂等:finally 里的兜底收尾与提前收尾(报错 / 断连)可以都写,先到先得。
    - _ctx 在同一条流的所有后代间共享:anchor_id(续接锚点 = 原始 root id)、trace_name、
      tags —— 供 carrier() 生成续接载体。
    """

    __slots__ = ("_raw", "_ended", "_has_output", "_ctx")

    def __init__(self, raw: Any = None, ctx: dict | None = None):
        self._raw = raw
        self._ended = False
        self._has_output = False
        self._ctx = ctx if ctx is not None else {}

    def __bool__(self) -> bool:
        return self._raw is not None

    @property
    def trace_id(self) -> str | None:
        return getattr(self._raw, "trace_id", None) if self._raw is not None else None

    @property
    def id(self) -> str | None:
        return getattr(self._raw, "id", None) if self._raw is not None else None

    @property
    def has_output(self) -> bool:
        """是否已显式写过 output —— 让调用方的兜底 output 不覆盖业务写入的更准确结果。"""
        return self._has_output

    @property
    def sampled(self) -> bool:
        """是否被采样(LANGFUSE_SAMPLE_RATE<1 时,未采样的 trace 在 Langfuse 里查不到)。"""
        if self._raw is None:
            return False
        try:
            return bool(self._raw._otel_span.get_span_context().trace_flags.sampled)
        except Exception:
            return True

    def child(self, name: str, *, as_type: str = "span", **kwargs: Any) -> "Obs":
        """新建子 observation(不改 OTel 当前上下文)。as_type: span/generation/tool/retriever/embedding/chain/agent。"""
        if self._raw is None:
            return NOOP
        try:
            raw = self._raw.start_observation(name=name, as_type=as_type, **_clean(kwargs))
        except Exception:
            logger.debug("Langfuse child(%s) 失败", name, exc_info=True)
            return NOOP
        return Obs(raw, self._ctx)

    def event(self, name: str, **kwargs: Any) -> None:
        """记一条瞬时事件(无时长),如 await_user / steer。"""
        if self._raw is None:
            return
        try:
            self._raw.create_event(name=name, **_clean(kwargs))
        except Exception:
            logger.debug("Langfuse event(%s) 失败", name, exc_info=True)

    def update(self, **kwargs: Any) -> None:
        if self._raw is None or self._ended:
            return
        kwargs = _clean(kwargs)
        if not kwargs:
            return
        if "output" in kwargs:
            self._has_output = True
        try:
            self._raw.update(**kwargs)
        except Exception:
            logger.debug("Langfuse update 失败", exc_info=True)

    def end(self, **kwargs: Any) -> None:
        """可选地最后 update 一次再结束;重复调用无副作用。"""
        if self._raw is None or self._ended:
            return
        self.update(**kwargs)
        self._ended = True
        try:
            self._raw.end()
        except Exception:
            logger.debug("Langfuse end 失败", exc_info=True)

    def score_trace(
        self,
        name: str,
        value: float | str,
        *,
        data_type: str = "NUMERIC",
        comment: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        if self._raw is None:
            return
        try:
            self._raw.score_trace(**_clean({
                "name": name, "value": value, "data_type": data_type,
                "comment": comment, "metadata": metadata,
            }))
        except Exception:
            logger.debug("Langfuse score_trace(%s) 失败", name, exc_info=True)

    def carrier(self) -> dict | None:
        """续接载体(纯 JSON,可进 runtime_state sidecar):下一段流据此挂回原 trace 的原始 root。

        必须同时带 trace_name / tags:实测续接段若不重新 propagate,traceName 会变成
        它自己的 span 名、tags 为空。
        """
        tid = self.trace_id
        if not tid:
            return None
        return {
            "trace_id": tid,
            "parent_span_id": self._ctx.get("anchor_id") or self.id,
            "trace_name": self._ctx.get("trace_name"),
            "tags": list(self._ctx.get("tags") or []),
        }


NOOP = Obs()


def enabled() -> bool:
    return _get_client() is not None


def init() -> bool:
    """入口启动时调用:提前建客户端、后台解析 project id,让首个 trace_info 就带上链接。"""
    return enabled()


@contextmanager
def scope(
    *,
    session_id: str | None = None,
    trace_name: str | None = None,
    tags: list[str] | None = None,
    metadata: dict | None = None,
) -> Iterator[None]:
    """propagate_attributes 薄封装:作用域内新建的 observation 都带上 session / trace 名 / tags。

    必须**先进 scope 再建 root**(实测:root 早于 scope 创建就不带 session_id)。
    可以包住 async generator 的 yield —— SDK 对跨 task 的 context detach 已做安全处理。
    """
    cm = None
    if _get_client() is not None and _propagate_attributes is not None:
        try:
            cm = _propagate_attributes(
                session_id=session_id or None,
                trace_name=trace_name or None,
                tags=[str(t) for t in tags] if tags else None,
                metadata=_str_metadata(metadata),
            )
            cm.__enter__()
        except Exception:
            logger.debug("Langfuse scope 进入失败", exc_info=True)
            cm = None
    try:
        yield
    finally:
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception:
                logger.debug("Langfuse scope 退出失败", exc_info=True)


def _trace_context(carrier: dict | None) -> dict | None:
    if not isinstance(carrier, dict):
        return None
    tid = carrier.get("trace_id")
    if not isinstance(tid, str) or not _TRACE_ID_RE.match(tid):
        return None
    tc = {"trace_id": tid}
    psid = carrier.get("parent_span_id")
    if isinstance(psid, str) and _SPAN_ID_RE.match(psid):
        tc["parent_span_id"] = psid
    return tc


def start_root(
    name: str,
    *,
    as_type: str = "agent",
    input: Any = None,
    metadata: Any = None,
    carrier: dict | None = None,
    trace_name: str | None = None,
    tags: list[str] | None = None,
) -> Obs:
    """新建一条流的 root observation;carrier 有效时续接到原 trace(挂在原始 root 下)。

    trace_name / tags 只写进 Obs 上下文供后代 carrier() 使用 —— 真正生效靠外层 scope()。
    """
    client = _get_client()
    if client is None:
        return NOOP
    tc = _trace_context(carrier)
    kwargs = _clean({"name": name, "as_type": as_type, "input": input, "metadata": metadata})
    if tc:
        kwargs["trace_context"] = tc
    try:
        raw = client.start_observation(**kwargs)
    except Exception:
        logger.debug("Langfuse start_root(%s) 失败", name, exc_info=True)
        return NOOP
    ctx = {
        "anchor_id": tc.get("parent_span_id") if tc else None,
        "trace_name": trace_name,
        "tags": list(tags or []),
    }
    obs = Obs(raw, ctx)
    if not ctx["anchor_id"]:
        ctx["anchor_id"] = obs.id  # 新 trace:自己就是后续续接段的锚点
    return obs


def trace_url(trace_id: str | None) -> str | None:
    if not trace_id or _get_client() is None:
        return None
    if not _project_id:
        _schedule_project_resolve()
        return None
    return f"{_base_url()}/project/{_project_id}/traces/{trace_id}"


def session_url(session_id: str | None) -> str | None:
    if not session_id or _get_client() is None:
        return None
    if not _project_id:
        _schedule_project_resolve()
        return None
    return f"{_base_url()}/project/{_project_id}/sessions/{quote(session_id, safe='')}"


def public_config() -> dict:
    """给前端(/api/health)的非敏感配置:是否启用 + 拼链接所需的 base_url / project_id。"""
    on = enabled()
    if on and not _project_id:
        _schedule_project_resolve()
    return {
        "enabled": on,
        "base_url": _base_url() if on else None,
        "project_id": _project_id if on else None,
    }


def score(
    trace_id: str | None,
    name: str,
    value: float | str,
    *,
    data_type: str = "NUMERIC",
    comment: str | None = None,
    metadata: dict | None = None,
) -> None:
    """按 trace id 打分(流已结束的事后反馈,如低置信度草稿被采纳 / 丢弃)。走 SDK 后台队列,不阻塞。"""
    client = _get_client()
    if client is None or not trace_id:
        return
    try:
        client.create_score(**_clean({
            "name": name, "value": value, "trace_id": trace_id, "data_type": data_type,
            "comment": comment, "metadata": metadata,
        }))
    except Exception:
        logger.debug("Langfuse score(%s) 失败", name, exc_info=True)


def usage_details(usage: Any) -> dict[str, int] | None:
    """OpenAI Chat(prompt/completion_tokens)与 Responses(input/output_tokens)两种 usage
    统一成 Langfuse usage_details:input / output / total + 展平的 *_details(如 output_reasoning_tokens)。"""
    if not isinstance(usage, dict):
        return None
    out: dict[str, int] = {}

    def put(key: str, value: Any) -> None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = int(value)

    put("input", usage.get("prompt_tokens", usage.get("input_tokens")))
    put("output", usage.get("completion_tokens", usage.get("output_tokens")))
    put("total", usage.get("total_tokens"))
    for field, prefix in (
        ("prompt_tokens_details", "input_"), ("input_tokens_details", "input_"),
        ("completion_tokens_details", "output_"), ("output_tokens_details", "output_"),
    ):
        details = usage.get(field)
        if isinstance(details, dict):
            for k, v in details.items():
                put(prefix + str(k), v)
    return out or None


def now() -> datetime:
    """generation 的 completion_start_time(首 token 时间,即 TTFT)用的 tz-aware 时间戳。"""
    return datetime.now(timezone.utc)


def flush() -> None:
    if _client is not None:
        try:
            _client.flush()
        except Exception:
            logger.debug("Langfuse flush 失败", exc_info=True)


def shutdown() -> None:
    """进程退出前调用:flush 队列并停后台线程(SDK 也注册了 atexit,这里是显式兜底)。"""
    if _client is not None:
        try:
            _client.shutdown()
        except Exception:
            logger.debug("Langfuse shutdown 失败", exc_info=True)
