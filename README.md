# simple_chat_agent_demo

一个用最少代码讲清楚 **ReAct（Thought-Action-Observation）聊天 Agent** 的教学 Demo。后端用 OpenAI Python SDK 调通义千问（DashScope OpenAI-Compat 网关），提供 CLI 和 Web 两个入口共享同一 ReAct 内核。

## 这个 demo 想讲什么

- 一个手写的 ReAct 循环（无任何 Agent 框架封装）：prompt 模板、`Action:` 行解析、`Action Input:` JSON 解析、`Observation` 回灌、最大轮次保护。
- 一份带 `enable_thinking` + `enable_search` 的流式 LLM 调用，区分推理 token 与正文 token。
- 一个 SSE + `<details>` 思考面板的极简 Web UI，把思考过程、工具调用、最终回答分区呈现。

工具列表 `TOOLS = []` 故意留空——这是脚手架，让你直观看到"加一个工具需要改哪两处地方"。

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 跑起来

两个入口都从环境变量读 API key：

```bash
export DASHSCOPE_API_KEY=sk-xxx        # 必填，LLM 调用用
export DASHSCOPE_API_KEY_MCP=sk-xxx    # 可选，MCP 联网搜索用；缺失时 fallback 到 DASHSCOPE_API_KEY
export QWEN_MODEL=qwen3.7-max          # 可选，默认 qwen3.7-max
export API_MODE=responses              # 可选，responses(默认) 或 chat
export ALLOW_REAL_SHELL=0              # 可选，默认 0(不真执行 shell)
```

> `DASHSCOPE_API_KEY_MCP` 仅在 `API_MODE=chat` 下生效（MCP 联网搜索路径需要鉴权）。
> 如果未设置，MCP 调用将 fallback 使用 `DASHSCOPE_API_KEY`。设置独立 key 的好处:
> LLM 和 MCP 可以用不同权限/配额的 API key，避免互相影响。

> `ALLOW_REAL_SHELL=1` 时（仅 `API_MODE=chat`），HITL 审批通过的 `execute_shell_command`
> 会真正调 `asyncio.create_subprocess_shell` 执行（30s 超时、stderr 合并 stdout、8KB 截断、
> 命令含 `rm -rf` / `sudo ` / `curl ` / `| sh` / `/etc/passwd` 等敏感模式时直接拒绝）。
> 默认关闭，demo 仍走 `[demo stub] 已模拟执行命令: ...` 字符串，避免被 clone 后变成默认 RCE 风险。

> Phase 1 Static GenUI 的搜索结果卡片只在 `API_MODE=chat` 下生效：该模式通过 Chat Completions native function calling 调用 DashScope WebSearch MCP 工具，后端能拿到 `tool_call_id` 和完整工具结果并发送 `component_*` SSE 事件。默认 `responses` 模式仍保留内置 `web_search` 的 `search_status` 横幅，不会生成搜索结果卡片。

> Phase 2 上下文感知会在 Web 请求中附带 `viewport_width` / `selected_text` / `session_message_count`。后端据此注入简短自适应 prompt，并仅在需要切换布局时发送 `ui_hint`（如 `focus` / `compact`，payload 含 `reason`）。

> Phase 3 Declarative GenUI 仅在 `API_MODE=chat` 下生效：模型可调用 `render_ui` 输出受控 JSON DSL，后端发送 `ui_surface_*` SSE 事件，前端递归渲染 `text/card/row/column/table/button`。Phase 3b 起，带 `action.event_name` 的按钮会调用 `/api/ui_action` 新启 SSE 流；模型也可调用 `update_ui_data` 按 JSON Pointer 更新已有 surface 数据。UI surface/action 不进入 Memory 或归档。

> Phase 4 Plan-and-Execute 仅在 `API_MODE=chat` 下生效：模型可调用 `create_plan` 发起可编辑计划，前端用 `activity_snapshot` / `activity_delta` 渲染和更新步骤状态，用户通过 `/api/plan_confirm` 确认后逐步执行；失败步骤可在决策卡中编辑后继续。计划不进入 Memory 或归档。

> Phase 5 Checkpoint/Resume 会把 HITL pending、UI surface/action、Plan 状态保存到 `data/runtime_state/{session_id}.json`。页面刷新或服务重启后，前端通过 `GET /api/runtime_state?session_id=...` 恢复待处理卡片和可交互 UI；运行中的计划会显示“继续执行”并调用 `/api/plan_continue`。chat archive markdown 格式保持不变。

> Phase 6 Confidence Signal 会让模型在最终回答末尾输出供后端解析的置信度标记，后端结合工具调用启发式发送 `confidence_signal` SSE。标记剥离采用“疑似 marker 前缀暂存”，不会固定扣留短答案尾部。低置信度回答会显示为草稿，用户通过 `/api/confidence_decision` 采纳后才写入 Memory；丢弃则不进入归档。

### CLI

```bash
python demo/common_chat_agent.py
# 直接在 stdin 上敲消息，输入 exit 退出
```

### Web

```bash
python demo/web_chat_agent.py
# 浏览器打开 http://127.0.0.1:8000
```

## 文件结构

```
demo/
  common_chat_agent.py    ReAct 内核 + CLI 入口
  web_chat_agent.py       FastAPI + SSE 包装
  static/index.html       Web 前端（单文件 HTML/CSS/JS）
docs/
  OpenAI兼容-Chat 接口文档.md
CLAUDE.md                 给 Claude Code 看的架构说明
requirements.txt
```

详细架构、SSE 事件契约、扩展工具的方式见 [CLAUDE.md](./CLAUDE.md)。

## 没有的东西

测试套件、lint、CI、鉴权、生产部署配置。这是 demo，不是脚手架。
