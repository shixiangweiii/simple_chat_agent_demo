# 2026-05-31 · `ALLOW_REAL_SHELL` 开关 —— 允许 `execute_shell_command` 真执行

> 适用范围:`API_MODE=chat` 路径下的 HITL approval 类工具
> 影响文件:`demo/chat_core.py`、`CLAUDE.md`、`README.md`
> 默认行为:**不变**(开关默认关闭,仍走 demo stub)

---

## 1. 背景

`simple_chat_agent_demo` 是教学型 ReAct Agent,从一开始就把"`execute_shell_command` 仅做 HITL 流程演示、不真正调起 subprocess"列为 non-goal —— 理由是仓库会被多人 clone,默认就能 shell 注入太危险。

但这条 non-goal 没有任何运行时痕迹:`_resume_inner` 的 approve 分支硬编码返回 `"[demo stub] 已模拟执行命令: ..."` 字符串,完全不提供"如何启真执行"的入口。

### 触发本次改动的具体场景

会话归档 `data/chat_archive/2c7ee6d3-1117-4cea-bdbf-7d555e26a028.md` 中:

```
用户:帮我列出下我本地 "/Users/shixiangweii/Desktop/work" 目录下的所有文件列表

AI:由于这是一个演示环境,我无法实际访问您本地的文件系统。刚才的调用仅用于演示
"执行 Shell 命令"的审批流程,并未真正读取目录内容。
```

环境配置:`API_MODE=chat`、`QWEN_MODEL=qwen3.6-plus`,所有条件都满足 native function calling + HITL,用户也确实点了「同意」按钮 —— 但后端把 stub 字符串作为 `role=tool` 的 content 喂回模型,模型只能如实复述"未真正执行"。

**这是设计行为,不是 bug。** 但教学 demo 没办法给学生展示「真执行了 ls,看到了真实文件列表」这种闭环,缺少一个本地开发者主动开启真执行的入口。

---

## 2. 根因分析(写给排查者)

完整链路 7 步:

1. 环境变量 `API_MODE=chat` 是触发 HITL 的必要条件 —— `responses` 模式根本不会注册 `LOCAL_TOOLS`,`execute_shell_command` 工具不存在。
2. 用户输入「列出目录文件」→ 模型在系统提示词 `USER_PROMPT` 中读到「**危险操作审批**:涉及执行命令...调用 `execute_shell_command` 工具发起审批」→ 调用工具。
3. `_stream_react_rounds` 派发到 `LOCAL_TOOLS` 分支 → 写入 `_PENDING[session_id]` → yield `await_user{kind:"approval"}` + `done` → 关流。
4. 前端 `addHitlBubble` 按 `kind="approval"` 渲染同意/拒绝按钮。
5. 用户点「同意」→ `POST /api/resume` with `decision="approve"`。
6. `resume_chat_response` → `_resume_inner` 走到 `approve` 分支 → **硬编码返回 stub 字符串**。
7. stub 字符串明文写着「不会真正执行 shell」→ 模型如实复述给用户。

**关键代码位置(改动前)** `demo/chat_core.py:2294-2301`:

```text
elif name == "execute_shell_command":
    if decision == "approve":
        # demo 不真执行 shell —— 教学焦点是 HITL 流程,不引入真实 RCE 风险
        cmd = awaiting["args"].get("command", "")
        tool_result = (
            f"[demo stub] 已模拟执行命令: {cmd}\n"
            "(本 demo 不会真正执行 shell,仅演示 HITL 审批流程)"
        )
```

---

## 3. 改法思路

### 3.1 整体目标

- 保留 demo 的安全默认值(clone 后不会默认变成 RCE)。
- 提供一个可观测、单点切换的开关。
- 改动局限在 `_resume_inner` 一处分支 + 新增 helper,不动 HTTP 层、不动前端、不动 CLI。

### 3.2 关键决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 开关形态 | env 变量 `ALLOW_REAL_SHELL=1` | 与 `API_MODE` / `MODEL` 一致(env-once-at-import),改值需重启,符合 demo 心智模型 |
| 默认值 | 关 | 保持 demo non-goal 不变;不能让 clone 后默认 RCE |
| 命令护栏 | 黑名单(14 条敏感模式) | 白名单太严失去演示价值;黑名单挡住"误删 / 反弹 shell / 篡改系统"足够 |
| 超时 | 30s 硬超时 | 防止 `sleep 9999` 之类卡死 ReAct loop |
| 输出截断 | 8KB(喂模型) + 500 字符(SSE 给 UI,复用现有 `_truncate_tool_result`) | 全量喂模型 / 预览给 UI,与现有截断语义对齐 |
| stderr 处理 | 合并到 stdout(`stderr=STDOUT`) | 模型只看到一半输出会误判;失败信息和正常输出一起回喂 |
| 失败语义 | 不抛异常,统一返回字符串 | 与 `mcp_web_search.call_tool_async` 的 `"工具调用失败: ..."` 语义一致,让模型在 ReAct 循环里能恢复 |
| 同步/异步 | `asyncio.create_subprocess_shell` | 业务层全程 `async def`,同步 subprocess 会阻塞事件循环、影响其他 session |
| CLI 是否启用 | 否 | CLI 模式 `_react_chat_native` 命中 `LOCAL_TOOLS` 就短路返回错误字符串,根本走不到 HITL approval |
| 运行时 API 改开关 | 否 | demo 不引入鉴权,运行时改全局开关是新攻击面 |
| 流式拉 stdout | 否 | 本期目标只解锁真执行;流式输出要单独设计 SSE 事件契约,放未来 |
| Shell 选择 | `/bin/sh`(`create_subprocess_shell` 默认) | 与 demo 平台无关性一致 |

### 3.3 改动边界明确

**改的**:
- `_resume_inner` 中 `execute_shell_command + approve` 分支(原 8 行 → 12 行)
- 新增模块级常量(开关 + 超时/截断阈值 + 黑名单)
- 新增 2 个 helper 函数
- 新增 1 个 `import asyncio`

**不改的**:
- `_resume_inner` 的 `reject` 分支(开关开关都走同一路径)
- `_react_chat_native` 中 CLI 短路逻辑
- `_stream_react_rounds` 派发逻辑
- HTTP 路由 / SSE 事件契约 / 前端
- Memory / 归档 / runtime sidecar 格式

---

## 4. 改动内容

### 4.1 `demo/chat_core.py`

#### (a) 复用现有 import

本期**未新增 import**。`asyncio` 在 `demo/chat_core.py:15` 早已存在(此前用于其他业务),`_execute_shell_real` 直接复用,无需调整 import 区。

> 上一版 changelog 误写"新增 `import asyncio`",经评审指出已修正。

#### (b) 新增模块级常量(置于 `CONFIDENCE_REASON_MAX_CHARS` 之后,HITL 段之前)

```python
# ============================================================
# 真 shell 执行开关 (默认关闭,demo 安全态)
# ============================================================
ALLOW_REAL_SHELL = os.getenv("ALLOW_REAL_SHELL", "0") == "1"

SHELL_EXEC_TIMEOUT_SEC = 30
SHELL_EXEC_OUTPUT_MAX_CHARS = 8000

_SHELL_DENY_PATTERNS = (
    "rm -rf", "mkfs", "dd if=", " :(){", "shutdown", "reboot",
    "sudo ", "curl ", "wget ", "| sh", "| bash",
    "/etc/passwd", "/etc/shadow", ">/dev/sda", "chmod 777 /",
)
```

> 黑名单设计要点:全部 `.lower()` 比对,前后空格/管道符号区分,避免误伤同名字段(例如 `password_reset` 不应命中 `passwd`)。"curl `空格`" 而不是 `"curl"`,防止匹配到 `curlywood` 之类无关词。

#### (c) 新增 2 个 helper

`_looks_dangerous(cmd) -> str | None` —— 命中黑名单返回命中的 pattern 字符串,否则返回 `None`(便于上层判别 + 日志记录是哪一条触发)。

`_execute_shell_real(cmd) -> str` —— 异步真执行,返回拼装好的 tool_result。结构:

```python
async def _execute_shell_real(cmd: str) -> str:
    blocked = _looks_dangerous(cmd)
    if blocked:
        return f"[拒绝执行] 命令包含敏感模式 {blocked!r},..."

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=PIPE, stderr=STDOUT,
        )
    except Exception as e:
        return f"[执行失败] 无法启动子进程: {e}"

    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=SHELL_EXEC_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        proc.kill(); await proc.wait()
        return f"[超时] 命令执行超过 {SHELL_EXEC_TIMEOUT_SEC}s,..."

    text = stdout.decode("utf-8", errors="replace")
    if len(text) > SHELL_EXEC_OUTPUT_MAX_CHARS:
        text = text[:SHELL_EXEC_OUTPUT_MAX_CHARS] + "\n...(输出过长,已截断)"

    return f"[exit={proc.returncode}]\n{text}"
```

**3 类错误路径都不抛异常**,转成字符串前缀 `[拒绝执行]` / `[执行失败]` / `[超时]` / `[exit=N]`,模型能区分。

**INFO/WARN 日志覆盖所有分支**:`shell denied:` / `shell exec start:` / `shell spawn failed:` / `shell timeout after Ns:` / `shell exec done:` —— 问题排查时 grep server log 一目了然。

#### (d) 改 `_resume_inner` 的 approve 分支

```diff
     elif name == "execute_shell_command":
         if decision == "approve":
-            # demo 不真执行 shell —— 教学焦点是 HITL 流程,不引入真实 RCE 风险
             cmd = awaiting["args"].get("command", "")
-            tool_result = (
-                f"[demo stub] 已模拟执行命令: {cmd}\n"
-                "(本 demo 不会真正执行 shell,仅演示 HITL 审批流程)"
-            )
+            if ALLOW_REAL_SHELL:
+                tool_result = await _execute_shell_real(cmd)
+            else:
+                tool_result = (
+                    f"[demo stub] 已模拟执行命令: {cmd}\n"
+                    "(本 demo 默认不真执行 shell,导出 ALLOW_REAL_SHELL=1 后才会真执行)"
+                )
         else:  # reject
             tool_result = f"用户拒绝执行。理由: {answer or '(未填写)'}"
```

stub 文案末尾从「仅演示 HITL 审批流程」改为「导出 ALLOW_REAL_SHELL=1 后才会真执行」—— 让用户能从模型输出中看到开关存在,避免被当 bug 反复排查。

### 4.2 `CLAUDE.md`

两处描述同步:

- **`_resume_inner` 描述段** —— 把 approve 那段从"始终 stub"改成"默认 stub / `ALLOW_REAL_SHELL=1` 时真执行",并提到 30s 超时 + 8KB 截断 + 黑名单。
- **"Implications when modifying" 的 HITL 工具实现指南** —— 在末尾追加 `execute_shell_command` 例外说明,建议未来新增其他需要真执行的 approval 类工具时沿用同样的 env-once-at-import 开关模式。

Non-goals 段不动 —— 原话「不真执行 shell」描述的是 demo 默认行为,依然成立。

### 4.3 `README.md`

「跑起来」段加一行环境变量 + 一段说明:

```bash
export ALLOW_REAL_SHELL=0              # 可选,默认 0(不真执行 shell)
```

> `ALLOW_REAL_SHELL=1` 时(仅 `API_MODE=chat`),HITL 审批通过的 `execute_shell_command`
> 会真正调 `asyncio.create_subprocess_shell` 执行(30s 超时、stderr 合并 stdout、8KB 截断、
> 命令含 `rm -rf` / `sudo ` / `curl ` / `| sh` / `/etc/passwd` 等敏感模式时直接拒绝)。
> 默认关闭,demo 仍走 `[demo stub] 已模拟执行命令: ...` 字符串,避免被 clone 后变成默认 RCE 风险。

### 4.4 改动汇总

| 文件 | 性质 | 行数变化 |
|---|---|---|
| `demo/chat_core.py` | 新增 import + 常量 + helper + 改一处分支 | +95 / -7 |
| `CLAUDE.md` | 两处描述同步 | +2 / -2 |
| `README.md` | 跑起来段 +1 行环境变量 + 说明 | +6 / -0 |
| `~/.claude/projects/.../memory/shell_real_exec_opt_in.md` | 新增 memory(项目外文件,不进 git) | 全新 |

---

## 5. 验证

### 5.1 静态验证(已做)

```bash
# 语法
python -c "import ast; ast.parse(open('demo/chat_core.py').read())"

# AST 符号到位
python -c "
import ast
src = open('demo/chat_core.py').read()
tree = ast.parse(src)
# 断言所有新增符号 + _execute_shell_real 是 async + approve 分支引用了 ALLOW_REAL_SHELL/await/demo stub
"
```

两项全通过。

### 5.2 手测(待执行,需要 DASHSCOPE_API_KEY 实环境)

7 条用例,任何一条不过即应回滚:

| # | 场景 | 期望结果 |
|---|---|---|
| 1 | `unset ALLOW_REAL_SHELL` + 列目录 | AI 回复含「默认不真执行 shell」+ 提示 `ALLOW_REAL_SHELL=1` |
| 2 | `ALLOW_REAL_SHELL=1` + 列目录 | AI 回复含真实文件列表;server log 有 `shell exec start/done` |
| 3 | `ALLOW_REAL_SHELL=1` + 模型生成 `rm -rf` | AI 回复含「命令包含敏感模式 'rm -rf'」 |
| 4 | `ALLOW_REAL_SHELL=1` + 模型生成 `sleep 60` | 30s 后 AI 回复含「超时,已强制终止」 |
| 5 | 任意 shell 请求 + 点拒绝 | AI 回复含「用户拒绝执行。理由: ...」(开关开关都一致) |
| 6 | `python demo/common_chat_agent.py` + 列目录 | CLI 应得到固定错误字符串兜底回复,不应真执行 |
| 7 | 开关切换前后看 archive/runtime_state | 文件结构无变化(Memory 只存最终 AI 回复) |

---

## 6. 兼容性与回滚

### 6.1 兼容性

- **默认行为完全不变**(开关默认关,stub 文案末尾微调但语义一致)。
- 不改 SSE 事件契约 → 前端零适配。
- 不改 `_PENDING` / `_UI_SURFACES` / `_PLANS` 结构 → 已持久化的 runtime_state JSON 无需迁移。
- 不改 Memory / archive markdown 格式 → 历史会话原样可读。
- 不改 CLI 流程 → CLI 测试用例不受影响。

### 6.2 回滚

仅需把 `_resume_inner` 中 approve 分支还原成原 8 行 + 删除 import / 常量 / helper / 文档段落即可。运行时数据无 schema 变更,回滚后老数据继续可用。

---

## 7. 已知边界与后续改进方向

### 7.1 已知边界(本期不解决)

- **黑名单不是绝对防御**:`rm -rf` 可被 `rm  -rf`(双空格)、`/bin/rm -rf`、shell 变量拼接绕过。本期定位是「最小护栏 + 人为审批」,真正隔离应该交给容器 / 虚拟用户 / 沙箱。已知的额外绕过姿势(本期不修代码,仅备案):

  | 绕过方式 | 示例 | 备注 |
  |---|---|---|
  | shell 替换 | `$(rm -rf /)`、`` `rm -rf /` `` | **最易被 LLM 无意触发**,黑名单的 substring 匹配看不到展开后的 token |
  | 变量拼接 | `a=rm; b=-rf; $a $b /` | shell 展开后才是危险命令 |
  | 绝对路径前缀 | `/usr/bin/sudo` | 不匹配 `"sudo "` 的尾部空格组合 |
  | tab / IFS 分隔 | `curl$'\t'evil.com` | 不匹配 `"curl "` 的尾部空格 |
  | 无前导空格 fork bomb | `:(){:\|:&};:` | 不匹配 `" :(){"` 的前导空格 |
  | 其他 shell 管道 | `\| zsh`、`\| python -c "import os; ..."` | 只拦了 `\| sh` 和 `\| bash` |

- **没有命令工作目录控制**:`create_subprocess_shell` 继承 server 启动目录。后续可加 `SHELL_EXEC_CWD` env 变量。
- **没有命令环境变量隔离**:子进程继承 server 环境(`env=` 未传)。若 server 进程有敏感 env(如 `DASHSCOPE_API_KEY`),LLM 通过 `env` / `printenv DASHSCOPE_API_KEY` 等命令可把 key 回显到 tool_result(server log 含完整输出,SSE `tool_result` 事件截断到 500 字符给 UI)。**生产部署不应开此开关**;若必须开,应在容器 / 虚拟用户里跑,而不是在 helper 里写不完整的 env 过滤器(按 `KEY/SECRET` 关键词过滤会漏掉用户自定义命名,反而给一种虚假安全感)。
- **stdout 输出依然是一次性返回给模型**:本期 P1 修复后,内存峰值已经从无界压到 ~8KB(`proc.stdout.read(MAX+1)` 有界读 + 超 8KB 立即 kill 进程),但**模型仍然看到的是一次性凑齐的 8KB 字符串,不是逐行流式**。流式输出(`shell_stdout_chunk` SSE 事件)留作后续扩展。
- **超时 / 截断 kill 只杀 `/bin/sh` 本身,不杀子进程组**:`proc.kill()` 发 SIGKILL 给 `create_subprocess_shell` 启动的 `/bin/sh -c <cmd>` 进程,但若 `<cmd>` 内部又 fork 了子进程(如 `sleep 60 & echo done`),那个子进程会成为孤儿继续运行。完整清理需要 `os.setsid` + `os.killpg`,但 Windows 不支持,本期不引入平台分支;教学场景下"主动写后台 fork 命令"的概率极低,且 P1 修复后即便孤儿存在,server 内存也不再爆。

### 7.2 未来扩展候选

| 方向 | 说明 |
|---|---|
| 流式 stdout → SSE | 新增 `shell_stdout_chunk` 事件,实时回显执行进度 |
| 命令白名单可配 | 当前黑名单写死,可改 `_SHELL_DENY_PATTERNS` 从 `data/shell_policy.json` 读取 |
| 工作目录限制 | 加 `SHELL_EXEC_CWD` env,默认 `~`,所有命令 chdir 后执行 |
| 容器化隔离 | docker compose 把 server 跑进容器,开关在容器内开,宿主机零风险 |

---

## 8. 关联引用

- 触发会话:`data/chat_archive/2c7ee6d3-1117-4cea-bdbf-7d555e26a028.md`
- 改动前关键位置:`demo/chat_core.py:2294-2301`
- 实施 plan:`~/.claude/plans/ultrathink-os-getenv-allow-real-shell-nested-tulip.md`
- 项目记忆:`shell-real-exec-opt-in`(`~/.claude/projects/.../memory/shell_real_exec_opt_in.md`)
- CLAUDE.md non-goal 段:仍保留「**demo 不真执行 shell** —— 教学焦点是 HITL 流程,不引入真实 RCE 风险」描述,语义不变

---

## 9. 评审反馈与修订(2026-05-31 复审)

> 评审报告:`docs/temp/codereview/2026-05-31-allow-real-shell-switch-评审.md`
> 反思原则:**逐条核对真伪 + 只修真正影响安全/正确性的 bug,不为了"全修"引入新依赖**。

### 9.1 处置矩阵

| 编号 | 评审意见 | 处置 | 落地位置 |
|---|---|---|---|
| **P1** | `communicate()` 一次性读全部 stdout → 内存可爆 GB | ✅ **改代码** | `_execute_shell_real` 改 `proc.stdout.read(MAX+1)` 有界读,超 8KB 立即 kill 进程止血 |
| P2-1 | 黑名单可被 `$(...)` / 反引号 / 变量拼接绕过 | ✅ 改文档 | 7.1 已知边界补完整的绕过姿势表 |
| P2-2 | 子进程继承敏感 env(`DASHSCOPE_API_KEY`) | ❌ **不改代码**,改文档 | 7.1 补充。简单按关键词过滤会漏自定义命名(`MY_CRED`/`DB_PWD`),给虚假安全感反而更危险;隔离应交给容器/虚拟用户 |
| P2-3 | INFO 日志含完整 cmd → secret 落盘 | ❌ **不改** | cmd 是用户 approve 后的产物,日志保留是合理审计能力;截断到 200 字符会丢失排查 context |
| P3-1 | changelog 4.1(a) 写"新增 `import asyncio`"但实际已存在 | ✅ 改文档 | 4.1(a) 改为"复用现有 import"事实陈述 |
| P3-2 | `proc.kill()` 不杀子进程组 → 孤儿进程 | ❌ **不改代码**,改文档 | 7.1 补充。`os.setsid` Windows 不支持,P1 修复后孤儿不再吃内存,影响极小 |
| P3-3 | 日志 `out_chars` 记的是截断后长度 | ✅ 改代码 | 字段名 `out_chars` → `out_bytes`,统计原始字节数 |

### 9.2 本次代码改动汇总

| 文件 | 改动 | 行数 |
|---|---|---|
| `demo/chat_core.py` | `_execute_shell_real` 函数体:`communicate()` → 有界 `stdout.read(N+1)` + 截断时 kill + 日志字段名修正 | +31 / -19 |
| `docs/changelog/2026-05-31-allow-real-shell-switch.md` | 4.1(a) 改写、7.1 补绕过姿势 + env + 进程组、新增本 9 节 | +约 30 |

### 9.3 不变的契约(回归确认)

- 返回字符串前缀 `[exit=N]` / `[超时]` / `[执行失败]` / `[拒绝执行]` 4 种 case **不变**。
- 截断提示文案 `\n...(输出过长,已截断)` **不变**。
- `_resume_inner` approve 分支签名 / SSE 事件契约 / HTTP 路由 / Memory / archive / runtime_state **全部不变**。
- 默认行为(`ALLOW_REAL_SHELL` 关)与上一版完全一致,stub 文案不变。

### 9.4 兼容性提示

- 日志字段名 `out_chars` → `out_bytes`:本仓库无日志聚合 / 监控接入,字段名只在源码 + 控制台 stderr 出现,无外部依赖。如有团队基于此字段做 grep 解析,需同步关键字。
