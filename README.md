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
export DASHSCOPE_API_KEY=sk-xxx        # 必填
export QWEN_MODEL=qwen-plus            # 可选，默认 qwen-plus
```

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
