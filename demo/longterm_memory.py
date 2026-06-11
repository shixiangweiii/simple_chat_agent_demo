"""跨会话长期记忆(Phase 11)—— mem0 式原子事实抽取 + LLM 协调 + 向量检索注入。

定位:在"每个 session 隔离的对话 Memory"之上,叠加一层**全局**(单一用户)的长期记忆。
从每个会话归档时批量抽取"关于用户的原子事实",经 LLM 协调(ADD/UPDATE/DELETE)并入全局
记忆库,在后续任意会话的 system prompt 里注入,让 Agent 跨会话"记得用户"。

分层:本模块在 chat_core **下层**,只依赖 llm_client(向下),**禁止** import chat_core(避免环)。
只吃原语(turns: list[{role,msg}] / query: str),不依赖 Memory 类。

存储(均 git-ignored,与 chat_archive / runtime_state 同级):
    - data/longterm_memory.json   人类可读的事实库 + 摄入水位线
    - data/longterm_vectors.json  "模拟向量库":{fact_id: [floats]}(Milestone B)

检索(Milestone B):text-embedding-v4 召回(纯 Python cosine)→ qwen3-rerank 精排;
任何环节失败都优雅降级(降级阶梯),绝不阻断聊天。
"""

import asyncio
import json
import logging
import math
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# 沿用入口模块已设置的 sys.path,保证可平级 import llm_client
sys.path.insert(0, str(Path(__file__).parent))
from llm_client import (  # noqa: E402
    EMBED_DIM,
    EMBED_MODEL,
    complete_async,
    embed_texts_async,
    rerank_async,
)

logger = logging.getLogger(__name__)


# ============================================================
# 常量与配置(均可 env 覆盖)
# ============================================================

_DATA_DIR = Path(__file__).parent.parent / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
LTM_PATH = _DATA_DIR / "longterm_memory.json"
VECTORS_PATH = _DATA_DIR / "longterm_vectors.json"
LTM_VERSION = 1

MIN_TURNS_TO_INGEST = int(os.environ.get("LTM_MIN_TURNS", "2"))   # 少于一个完整问答不抽取
MAX_FACTS = int(os.environ.get("LTM_MAX_FACTS", "200"))           # 事实库上限,超出淘汰最旧
MAX_INJECT_CHARS = int(os.environ.get("LTM_MAX_INJECT_CHARS", "2000"))
INJECT_ALL_MAX = int(os.environ.get("LTM_INJECT_ALL_MAX", "12"))  # ≤此数直接全量注入,不检索
RECALL_TOP_N = int(os.environ.get("LTM_RECALL_TOP_N", "20"))      # cosine 召回
RERANK_TOP_K = int(os.environ.get("LTM_RERANK_TOP_K", "6"))       # rerank 精排后注入条数
_MAX_FACT_TEXT_CHARS = 300

_CATEGORIES = {"preference", "identity", "project", "fact"}
FACT_ID_RE = re.compile(r"^fact_\d{4,}$")

# 全局读-改-写串行锁(facts + vectors 同锁)。读路径(snapshot / retrieve)不取锁,
# 依赖原子写保证读到的是完整文件;跨两文件的瞬时不一致由检索侧优雅降级吸收。
_LTM_LOCK = asyncio.Lock()

# M2: 进程内读缓存。读路径(每轮 chat 都走)避免反复同步解析整个文件(vectors 文件可达 MB 级)。
# 一致性纲领:**写路径用 _load_disk / _load_vectors_disk 始终读盘的新对象去 mutate**,
# **_save / _save_vectors 写盘后把缓存引用整体替换(copy-on-write)**。读路径拿到的永远是某次
# _save 定格的不可变对象,写者从不原地改它 → 读者 lock-free 迭代也安全(与"每次 _load 返回独立
# 解析结果"的旧语义等价,只是省掉了重复解析)。单进程 demo 下本进程是唯一写者,故不做 mtime 失效。
_CACHE_FACTS: dict | None = None
_CACHE_VECTORS: dict | None = None


class _LTMParseError(ValueError):
    """模型输出里有 JSON 数组起始但解析失败(疑似坏 JSON)。区别于"没有数组/空数组"(合法空)。"""


# ============================================================
# Prompt 模板
# ============================================================

_EXTRACT_PROMPT = """你是一个长期记忆抽取器。从下面这段「用户与AI的对话」中,抽取关于**用户本人**的、值得跨会话长期记住的**原子事实**(偏好 / 身份 / 正在做的项目 / 稳定的背景信息)。

规则:
- 只抽取用户明确陈述或可明确推断的**稳定**信息;不要臆测、不要编造。
- 不要抽取一次性的、临时的、与用户长期无关的内容(本次的具体问题、闲聊、AI 的回答内容)。
- 每条事实是一句独立、自包含、不含指代的中文短句。
- category 取值只能是: preference(偏好) | identity(身份) | project(在做的事) | fact(其它稳定事实)。
- 如果没有值得长期记住的内容,返回空数组 []。

只输出 JSON 数组,不要任何解释或 markdown 代码块,格式示例:
[{{"text": "用户偏好简洁的回答", "category": "preference"}}]

对话:
{dialogue}"""

_RECONCILE_PROMPT = """你是一个长期记忆协调器。下面是已有记忆库(每条带 id)与本次新抽取的候选事实。请决定如何把候选并入记忆库,输出操作列表。

操作类型:
- ADD: 候选是全新信息,记忆库里没有 → {{"op":"ADD","text":"...","category":"..."}}
- UPDATE: 候选是对某条已有记忆的更新 / 纠正(如偏好改变) → {{"op":"UPDATE","id":"已有记忆id","text":"更新后的完整事实","category":"..."}}
- DELETE: 某条已有记忆被候选明确否定 / 过时 → {{"op":"DELETE","id":"已有记忆id"}}
- 候选与某条已有记忆语义重复、无新信息 → 不产生任何操作(忽略该候选)

规则:
- UPDATE / DELETE 的 id 必须来自下方"已有记忆"列表,**不得编造**。
- 一个候选最多对应一个操作。
- category 取值只能是 preference / identity / project / fact。
- 只输出 JSON 数组,不要解释或 markdown。格式示例: [{{"op":"ADD","text":"...","category":"fact"}}]

已有记忆:
{existing}

本次候选事实:
{candidates}"""


# ============================================================
# 存储底层
# ============================================================

def _atomic_write(path: Path, text: str) -> None:
    """先写 .tmp 再 rename,避免崩溃留半文件(与 chat_core._atomic_write 同款,避免造环不复用)。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _fresh_store() -> dict:
    return {"version": LTM_VERSION, "updated_at": _now(), "facts": [], "ingested": {}}


def _load_disk() -> dict:
    """从磁盘读事实库(写路径用,始终拿新对象)。损坏 / 版本不符 → 内存态新库(不落盘)。"""
    if not LTM_PATH.exists():
        return _fresh_store()
    try:
        data = json.loads(LTM_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("longterm_memory.json 损坏,本次按空库处理(不覆盖磁盘)", exc_info=True)
        return _fresh_store()
    if not isinstance(data, dict) or data.get("version") != LTM_VERSION:
        logger.warning("longterm_memory.json 版本不符,按空库处理")
        return _fresh_store()
    if not isinstance(data.get("facts"), list):
        data["facts"] = []
    if not isinstance(data.get("ingested"), dict):
        data["ingested"] = {}
    return data


def _load() -> dict:
    """读路径:返回缓存(冷启动读盘)。**调用方只读、不得 mutate**(写路径用 _load_disk)。"""
    global _CACHE_FACTS
    if _CACHE_FACTS is None:
        _CACHE_FACTS = _load_disk()
    return _CACHE_FACTS


def _save(data: dict) -> None:
    """写盘 + copy-on-write 刷新缓存引用(data 是写者从 _load_disk 拿的独立对象)。"""
    global _CACHE_FACTS
    data["updated_at"] = _now()
    _atomic_write(LTM_PATH, json.dumps(data, ensure_ascii=False, indent=2))
    _CACHE_FACTS = data


def _fresh_vectors() -> dict:
    return {"embed_model": EMBED_MODEL, "dim": EMBED_DIM, "vectors": {}}


def _load_vectors_disk() -> dict:
    """从磁盘读向量库(写路径用)。"""
    if not VECTORS_PATH.exists():
        return _fresh_vectors()
    try:
        data = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("longterm_vectors.json 损坏,按空向量处理", exc_info=True)
        return _fresh_vectors()
    if not isinstance(data, dict) or not isinstance(data.get("vectors"), dict):
        return _fresh_vectors()
    return data


def _load_vectors() -> dict:
    """读路径:返回缓存(冷启动读盘)。**只读不 mutate**。"""
    global _CACHE_VECTORS
    if _CACHE_VECTORS is None:
        _CACHE_VECTORS = _load_vectors_disk()
    return _CACHE_VECTORS


def _save_vectors(vdata: dict) -> None:
    """写盘 + copy-on-write 刷新缓存引用。"""
    global _CACHE_VECTORS
    _atomic_write(VECTORS_PATH, json.dumps(vdata, ensure_ascii=False))
    _CACHE_VECTORS = vdata


def _next_fact_id(facts: list[dict]) -> str:
    mx = 0
    for f in facts:
        m = re.fullmatch(r"fact_(\d+)", str(f.get("id", "")))
        if m:
            mx = max(mx, int(m.group(1)))
    return f"fact_{mx + 1:04d}"


# ============================================================
# 文本清洗 / 渲染
# ============================================================

def _clean_text(s) -> str:
    if not isinstance(s, str):
        return ""
    s = re.sub(r"\s+", " ", s).strip()
    return s[:_MAX_FACT_TEXT_CHARS]


def _clean_cat(c) -> str:
    return c if c in _CATEGORIES else "fact"


def _render_turns(turns: list[dict]) -> str:
    lines = []
    for t in turns:
        if not isinstance(t, dict):
            continue
        role = t.get("role", "")
        msg = t.get("msg", "")
        if msg:
            lines.append(f"{role}: {msg}")
    return "\n".join(lines)


def _render_existing(existing: list[dict]) -> str:
    if not existing:
        return "(空)"
    return "\n".join(
        f"{f.get('id')} | {f.get('category', 'fact')} | {f.get('text', '')}" for f in existing
    )


def _render_candidates(candidates: list[dict]) -> str:
    return "\n".join(f"- [{c.get('category', 'fact')}] {c.get('text', '')}" for c in candidates)


def _parse_json_array(text: str, *, strict: bool = False) -> list:
    """从模型输出里抠出第一个 JSON 数组(容忍前后多余文本 / markdown 围栏)。

    M3: `strict=True` 时,**找到了 `[` 但解析失败**(疑似坏 JSON)抛 `_LTMParseError`,
    让上层把它当"瞬时失败、不推进水位线、下次重试",而不是误判成"没有事实"(合法空)。
    "没有 `[`"或合法的 `[]` 仍返回 [](真·空,可推进水位线)。
    """
    if not text:
        return []
    start = text.find("[")
    if start < 0:
        return []
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
        return obj if isinstance(obj, list) else []
    except json.JSONDecodeError:
        logger.warning("LTM JSON 数组解析失败,前 200 字: %s", text[:200])
        if strict:
            raise _LTMParseError("模型输出含数组起始但解析失败")
        return []


# ============================================================
# 抽取 + 协调 + 应用
# ============================================================

async def extract_facts_async(turns: list[dict]) -> list[dict]:
    """从对话抽取候选原子事实 [{text, category}]。真·空返回 [];坏 JSON 抛 _LTMParseError(上层不推进水位线)。"""
    rendered = _render_turns(turns)
    if not rendered.strip():
        return []
    raw = await complete_async(_EXTRACT_PROMPT.format(dialogue=rendered))
    out: list[dict] = []
    for it in _parse_json_array(raw, strict=True):
        if isinstance(it, dict):
            t = _clean_text(it.get("text"))
            if t:
                out.append({"text": t, "category": _clean_cat(it.get("category"))})
        elif isinstance(it, str):  # 模型偶尔直接给字符串数组
            t = _clean_text(it)
            if t:
                out.append({"text": t, "category": "fact"})
    return out


async def reconcile_async(existing: list[dict], candidates: list[dict]) -> list[dict]:
    """LLM 协调:候选 vs 已有 → 操作列表。校验 UPDATE/DELETE 的 id 真实存在,丢弃幻觉 id。"""
    if not candidates:
        return []
    raw = await complete_async(_RECONCILE_PROMPT.format(
        existing=_render_existing(existing), candidates=_render_candidates(candidates),
    ))
    valid_ids = {f.get("id") for f in existing}
    ops: list[dict] = []
    for op in _parse_json_array(raw, strict=True):
        if not isinstance(op, dict):
            continue
        kind = str(op.get("op", "")).upper()
        if kind == "ADD":
            if _clean_text(op.get("text")):
                ops.append(op)
        elif kind in ("UPDATE", "DELETE"):
            if op.get("id") in valid_ids:
                ops.append(op)
            else:
                logger.warning("LTM reconcile 丢弃非法 id 的 %s op: %s", kind, op.get("id"))
        # NOOP / 未知 op 忽略
    return ops


def apply_ops(data: dict, ops: list[dict], sid: str) -> tuple[dict, set, set]:
    """把操作应用到 data['facts']。返回 (stats, 待嵌入的 fact_id 集合, 已删除的 fact_id 集合)。"""
    facts = data["facts"]
    by_id = {f["id"]: f for f in facts}
    stats = {"added": 0, "updated": 0, "deleted": 0}
    to_embed: set[str] = set()
    deleted: set[str] = set()
    now = _now()
    for op in ops:
        if not isinstance(op, dict):
            continue
        kind = str(op.get("op", "")).upper()
        if kind == "ADD":
            text = _clean_text(op.get("text"))
            if not text:
                continue
            fid = _next_fact_id(facts)
            rec = {
                "id": fid, "text": text, "category": _clean_cat(op.get("category")),
                "created_at": now, "updated_at": now, "source_sessions": [sid],
            }
            facts.append(rec)
            by_id[fid] = rec
            stats["added"] += 1
            to_embed.add(fid)
        elif kind == "UPDATE":
            rec = by_id.get(op.get("id"))
            if rec is None:
                logger.warning("LTM apply UPDATE 非法 id: %s", op.get("id"))
                continue
            text = _clean_text(op.get("text"))
            if text:
                rec["text"] = text
            if op.get("category"):
                rec["category"] = _clean_cat(op.get("category"))
            rec["updated_at"] = now
            if sid not in rec.get("source_sessions", []):
                rec.setdefault("source_sessions", []).append(sid)
            stats["updated"] += 1
            to_embed.add(rec["id"])
        elif kind == "DELETE":
            rec = by_id.get(op.get("id"))
            if rec is None:
                logger.warning("LTM apply DELETE 非法 id: %s", op.get("id"))
                continue
            facts.remove(rec)
            by_id.pop(rec["id"], None)
            stats["deleted"] += 1
            deleted.add(rec["id"])
            to_embed.discard(rec["id"])

    # 容量上限:超出按 updated_at 淘汰最旧
    if len(facts) > MAX_FACTS:
        facts.sort(key=lambda f: f.get("updated_at", ""))
        drop = facts[: len(facts) - MAX_FACTS]
        for f in drop:
            deleted.add(f["id"])
            to_embed.discard(f["id"])
        data["facts"] = facts[len(facts) - MAX_FACTS:]
    return stats, to_embed, deleted


# ============================================================
# 向量同步(Milestone B)
# ============================================================

async def _sync_vectors(data: dict, to_embed_ids: set, deleted_ids: set) -> None:
    """同步向量库:嵌入新增/改动事实,删掉已删事实。best-effort —— 失败只 log,不影响事实保存。"""
    try:
        vdata = _load_vectors_disk()  # 写路径:读盘新对象,不动读缓存
        if vdata.get("embed_model") != EMBED_MODEL or vdata.get("dim") != EMBED_DIM:
            # 模型/维度变了 → 旧向量失效,重置并重嵌当前全部事实
            vdata = _fresh_vectors()
            to_embed_ids = {f["id"] for f in data["facts"]}
            deleted_ids = set()
        vectors = vdata["vectors"]
        for fid in deleted_ids:
            vectors.pop(fid, None)
        targets = [f for f in data["facts"] if f["id"] in to_embed_ids]
        if targets:
            vecs = await embed_texts_async([f["text"] for f in targets])
            for f, v in zip(targets, vecs):
                vectors[f["id"]] = v
        _save_vectors(vdata)
    except Exception:
        logger.warning("LTM 向量同步失败(事实已保存,检索将走降级)", exc_info=True)


# ============================================================
# 摄入编排(归档时后台调用)
# ============================================================

async def ingest_async(sid: str, turns: list[dict]) -> dict | None:
    """从一个会话的 turns 增量摄入长期记忆。返回 stats 或 None(NOOP/失败)。

    增量水位线去重:只处理"自上次摄入以来"的新增 turns;无足够新增直接 NOOP。
    全程持 _LTM_LOCK 串行(含 LLM/embedding 调用,单用户 demo 可接受);失败不推进水位线,
    下次归档可重试。
    """
    async with _LTM_LOCK:
        data = _load_disk()  # 写路径:读盘新对象去 mutate,不动读缓存
        watermark = int(data["ingested"].get(sid, 0) or 0)
        new_turns = turns[watermark:]
        if len(new_turns) < MIN_TURNS_TO_INGEST:
            logger.info("LTM ingest NOOP:sid=%s new_turns=%d(< %d)", sid, len(new_turns), MIN_TURNS_TO_INGEST)
            return None
        try:
            candidates = await extract_facts_async(new_turns)
            if not candidates:
                # 没抽到事实也推进水位线,避免下次重复抽同一段
                data["ingested"][sid] = len(turns)
                _save(data)
                logger.info("LTM ingest:sid=%s 抽取 0 条候选,推进水位线", sid)
                return {"added": 0, "updated": 0, "deleted": 0}
            ops = await reconcile_async(data["facts"], candidates)
            stats, to_embed, deleted = apply_ops(data, ops, sid)
            await _sync_vectors(data, to_embed, deleted)
            data["ingested"][sid] = len(turns)
            _save(data)
            logger.info(
                "LTM ingest 完成:sid=%s 候选=%d 操作=%d stats=%s 事实总数=%d",
                sid, len(candidates), len(ops), stats, len(data["facts"]),
            )
            return stats
        except Exception:
            logger.warning("LTM ingest 失败(不推进水位线,下次重试):sid=%s", sid, exc_info=True)
            return None


# ============================================================
# 注入(A: 全量;B: 检索)
# ============================================================

def _format_facts(facts: list[dict]) -> str:
    if not facts:
        return ""
    head = "# 关于用户的长期记忆（跨会话沉淀）"
    intro = "以下是从历史会话中沉淀的、关于用户的已知事实；回答时可自然参考，但不要生硬复述；若事实间冲突，以较新的为准："
    lines = [head, intro]
    used = len(head) + len(intro)
    for f in facts:
        line = f"- {f.get('text', '')}"
        if used + len(line) > MAX_INJECT_CHARS:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def _recent_facts(facts: list[dict], k: int) -> list[dict]:
    return sorted(facts, key=lambda f: f.get("updated_at", ""), reverse=True)[:k]


def injection_fragment() -> str:
    """同步全量注入(CLI 用,以及检索失败的最终兜底)。无事实返回空串。"""
    facts = _load().get("facts", [])
    return _format_facts(facts)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


async def _retrieve(query: str, facts: list[dict]) -> list[dict]:
    """召回(cosine)→ 精排(rerank),带降级。要求 facts 数 > INJECT_ALL_MAX 才进来。"""
    vdata = _load_vectors()
    vectors = vdata.get("vectors") if isinstance(vdata, dict) else None
    header_ok = (
        isinstance(vdata, dict)
        and vdata.get("embed_model") == EMBED_MODEL
        and vdata.get("dim") == EMBED_DIM
    )
    if not header_ok or not vectors:
        return _recent_facts(facts, RERANK_TOP_K)  # 无可用向量 → 最近 K

    qvec = (await embed_texts_async([query]))[0]
    scored = [(_cosine(qvec, v), f) for f in facts if (v := vectors.get(f["id"]))]
    if not scored:
        return _recent_facts(facts, RERANK_TOP_K)
    scored.sort(key=lambda x: x[0], reverse=True)
    recalled = [f for _, f in scored[:RECALL_TOP_N]]

    rr = await rerank_async(query, [f["text"] for f in recalled], RERANK_TOP_K)
    if rr is None:  # rerank 失败 → 用 cosine 序前 K
        return recalled[:RERANK_TOP_K]
    return [recalled[idx] for idx, _ in rr[:RERANK_TOP_K]]


async def retrieve_injection_async(query: str) -> str:
    """注入主入口(Web)。事实少→全量;多→召回+精排;任何失败→降级,绝不抛。"""
    facts = _load().get("facts", [])
    if not facts:
        return ""
    if len(facts) <= INJECT_ALL_MAX:
        return _format_facts(facts)
    try:
        selected = await _retrieve(query, facts)
    except Exception:
        logger.warning("LTM 检索失败,降级为最近事实", exc_info=True)
        selected = _recent_facts(facts, RERANK_TOP_K)
    return _format_facts(selected)


# ============================================================
# 只读快照 / 管理(给 HTTP 层经 chat_core re-export)
# ============================================================

def snapshot() -> dict:
    """前端面板用的只读快照。不暴露内部水位线 ingested。"""
    facts = _load().get("facts", [])
    return {
        "count": len(facts),
        "facts": [
            {k: f.get(k) for k in ("id", "text", "category", "created_at", "updated_at", "source_sessions")}
            for f in facts
        ],
    }


async def delete_fact_async(fact_id: str) -> dict:
    """删除一条事实(用户在面板上操作)。校验 fact_id 形态;同步删向量。"""
    if not isinstance(fact_id, str) or not FACT_ID_RE.match(fact_id):
        return {"ok": False, "error": "invalid fact_id"}
    async with _LTM_LOCK:
        data = _load_disk()  # 写路径:读盘新对象,不动读缓存
        before = len(data["facts"])
        data["facts"] = [f for f in data["facts"] if f.get("id") != fact_id]
        if len(data["facts"]) == before:
            return {"ok": True, "deleted": False}
        _save(data)
        try:
            vdata = _load_vectors_disk()
            vdata["vectors"].pop(fact_id, None)
            _save_vectors(vdata)
        except Exception:
            logger.warning("删除事实向量失败: %s", fact_id, exc_info=True)
        return {"ok": True, "deleted": True}
