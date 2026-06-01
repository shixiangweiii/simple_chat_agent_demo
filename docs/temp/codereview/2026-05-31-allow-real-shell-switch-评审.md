# Code Review: `ALLOW_REAL_SHELL` 开关

> 评审对象: `docs/changelog/2026-05-31-allow-real-shell-switch.md` 及对应源码改动
> 评审日期: 2026-05-31
> 涉及文件: `demo/chat_core.py`（常量 L64-85, helper L88-144, `_resume_inner` L2378-2391）

## 整体评价

改动精准,设计决策合理,与项目现有模式(env-once-at-import、no-throw-return-string、层级隔离)高度一致。改动边界极其精准——只碰 `_resume_inner` 的一个分支 + 两个纯函数,HTTP 层 / SSE 契约 / 前端 / Memory 格式全部不动。

---

## P1 — 内存耗尽风险（应修）

**位置**: `chat_core.py:124-127`

`proc.communicate()` 一次性把**全部 stdout 读进内存**,然后才做 8KB 截断。

如果命令在 30s 超时窗口内高速产出（比如 `yes`、`cat /dev/urandom | base64`、`find /`），内存峰值可以到 **GB 级别**,而截断只在读完之后才生效。同一个事件循环里的其他 session 会一起被拖慢甚至 OOM kill。

**当前代码**:

```python
stdout, _ = await asyncio.wait_for(
    proc.communicate(), timeout=SHELL_EXEC_TIMEOUT_SEC,
)
text = stdout.decode("utf-8", errors="replace")
if len(text) > SHELL_EXEC_OUTPUT_MAX_CHARS:
    text = text[:SHELL_EXEC_OUTPUT_MAX_CHARS] + "\n...(输出过长,已截断)"
```

**建议改法**:

```python
raw = await asyncio.wait_for(
    proc.stdout.read(SHELL_EXEC_OUTPUT_MAX_CHARS + 1),
    timeout=SHELL_EXEC_TIMEOUT_SEC,
)
# 有多余字节 → 还有未读输出 → 杀掉进程 + 标记截断
if len(raw) > SHELL_EXEC_OUTPUT_MAX_CHARS:
    proc.kill()
    await proc.wait()
    text = raw[:SHELL_EXEC_OUTPUT_MAX_CHARS].decode("utf-8", errors="replace")
    text += "\n...(输出过长,已截断)"
else:
    await proc.wait()
    text = raw.decode("utf-8", errors="replace")
```

内存上限从无界降到 ~8KB。工作量约 15 行。

---

## P2 — 中等问题

### P2-1. 黑名单绕过面比 changelog 7.1 描述的更宽

**位置**: `chat_core.py:81-85`（`_SHELL_DENY_PATTERNS`）、`chat_core.py:88-94`（`_looks_dangerous`）

Changelog 7.1 提到双空格和绝对路径绕过,但还有:

| 绕过方式 | 示例 | 说明 |
|---|---|---|
| shell 替换 | `$(rm -rf /)`、`` `rm -rf /` `` | **最易被 LLM 无意触发** |
| 变量拼接 | `a=rm; b=-rf; $a $b /` | shell 展开后才是危险命令 |
| fork bomb 无前导空格 | `:(){:\|:&};:` | 不匹配 `" :(){"` 的前导空格 |
| 其他 shell | `\| zsh`、`\| python -c "import os; ..."` | 只拦了 `\| sh` 和 `\| bash` |
| tab/IFS 绕过 | `curl$'\t'evil.com` | 不匹配 `"curl "` 的尾部空格 |
| 绝对路径前缀 | `/usr/bin/sudo` | 不匹配 `"sudo "` |

教学 demo 的定位下这些可以接受（人在循环里审批），但建议在代码注释或 changelog 7.1 中把 **shell 替换**（`$(...)`/`` `...` ``）也列为已知盲区——这是最容易被 LLM 无意触发的一种。

### P2-2. 子进程继承全部环境变量

**位置**: `chat_core.py:114-119`

`create_subprocess_shell` 默认继承 server 进程环境,包括 `DASHSCOPE_API_KEY`。LLM 生成 `env` 或 `printenv DASHSCOPE_API_KEY` 就能把 key 回显到 tool_result → SSE `tool_result` 事件（虽然 UI 截断到 500 字符,但 server log 会有完整输出）。

低成本缓解方案:

```python
safe_env = {k: v for k, v in os.environ.items()
            if not any(s in k.upper() for s in ("KEY", "SECRET", "TOKEN", "PASSWORD"))}
proc = await asyncio.create_subprocess_shell(cmd, env=safe_env, ...)
```

### P2-3. 敏感命令内容进 INFO 日志

**位置**: `chat_core.py:113`

`logger.info("shell exec start: %s", cmd)` 把 LLM 生成的完整命令写入 INFO 日志。如果模型从上下文里拼出含 secret 的命令,secret 会落盘到日志。

建议把 `cmd` 截断到合理长度（比如 200 字符），或者把 start/done 降级为 DEBUG。

---

## P3 — 低优先级 / Nits

### P3-1. Changelog 不准确：说"新增 `import asyncio`"但实际已存在

**位置**: changelog 4.1(a)

`chat_core.py:15` 的 `import asyncio` 是原有代码（被 `_execute_shell_real` 复用），不是本次新增。Changelog 4.1(a) 应删除或改为"复用现有 import"。

### P3-2. `proc.kill()` 只杀 shell 不杀子进程

**位置**: `chat_core.py:129-130`

`create_subprocess_shell` 通过 `/bin/sh -c cmd` 运行。超时时 `proc.kill()` 只 SIGKILL shell 进程本身,子进程可能成为孤儿继续运行。完整清理需要 process group kill:

```python
proc = await asyncio.create_subprocess_shell(
    cmd, stdout=PIPE, stderr=STDOUT,
    preexec_fn=os.setsid,
)
# timeout 时:
os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
```

教学 demo 场景下影响很小,记个 TODO 即可。

### P3-3. 日志 `out_chars` 统计的是截断后长度

**位置**: `chat_core.py:140-143`

`len(text)` 是截断 + 拼上 `"...(输出过长,已截断)"` 之后的值,不反映原始输出大小。排查问题时会误导。

建议:

```python
original_len = len(text)
if original_len > SHELL_EXEC_OUTPUT_MAX_CHARS:
    text = text[:SHELL_EXEC_OUTPUT_MAX_CHARS] + "\n...(输出过长,已截断)"
logger.info("... out_chars=%d ...", original_len, ...)
```

---

## 做得好的地方

- **改动边界极其精准** — 只碰 `_resume_inner` 的一个分支 + 两个纯函数,HTTP 层 / SSE 契约 / 前端 / Memory 格式全部不动。
- **错误设计统一** — 3 条错误路径（黑名单 / spawn 失败 / 超时）都返回带方括号前缀的字符串（`[拒绝执行]` / `[执行失败]` / `[超时]`），模型可解析,ReAct 循环能自然恢复；与 `mcp_web_search` 的 `"工具调用失败: ..."` 语义一致。
- **日志覆盖完整** — 每个分支（deny / start / spawn-fail / timeout / done）都有日志,level 选择合理（WARNING for deny/timeout, INFO for normal, exception for spawn-fail）。
- **Stub 文案改进** — 默认态 stub 末尾从"仅演示审批流程"改为"导出 ALLOW_REAL_SHELL=1 后才会真执行",用户能从模型回复里发现开关,减少误报。
- **Memory 质量高** — `shell_real_exec_opt_in.md` 的 Why / How to apply 指向正确的排查路径。

---

## 建议优先级汇总

| 优先级 | 编号 | 建议 | 工作量 |
|---|---|---|---|
| **P1 应修** | P1 | `communicate()` → 受限 `stdout.read()` 防内存耗尽 | ~15 行 |
| P2 可选 | P2-1 | changelog 7.1 补充 `$(...)` shell 替换盲区 | 1 行文档 |
| P2 可选 | P2-2 | `env=safe_env` 过滤敏感环境变量 | ~3 行 |
| P2 可选 | P2-3 | 日志中 cmd 截断或降级 DEBUG | ~2 行 |
| P3 随手 | P3-1 | 修正 changelog "新增 import asyncio" 描述 | 1 行文档 |
| P3 随手 | P3-2 | process group kill 或记 TODO | ~5 行 |
| P3 随手 | P3-3 | `out_chars` 改记原始长度 | 2 行 |

**P1 建议本轮修,其余可以 backlog。**
