# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

A single-file teaching demo of a ReAct (Thought-Action-Observation) chat agent in Python. It deliberately keeps the agent loop, prompt template, and memory store explicit and minimal so each piece can be read top-to-bottom. The tool list is intentionally empty — `TOOLS = []` and `execute_tool` are scaffolding to be extended.

## Environment & commands

- Dependencies: `openai>=1.0.0` (see `requirements.txt`). A `.venv` is checked into the working tree but git-ignored.
- Install: `pip install -r requirements.txt`
- Run: `python demo/common_chat_agent.py <api_key>` — the API key is a **required positional CLI argument**, not an env var. The script exits immediately if it's missing.

The LLM endpoint is hardcoded to Alibaba DashScope's OpenAI-compatible gateway (`https://dashscope.aliyuncs.com/compatible-mode/v1`) with model `qwen3.6-plus` and `enable_thinking=True` passed via `extra_body`. Changing provider means editing `llm()` and the `MODEL` constant in `demo/common_chat_agent.py`.

There is no test suite, linter, or build step configured.

## Architecture

Everything lives in `demo/common_chat_agent.py`. The flow per user turn:

1. `main()` reads a line from stdin, appends it to `Memory`, and calls `react()`.
2. `react()` runs an unbounded loop:
   - `build_prompt()` assembles `USER_PROMPT` + tool list + full conversation history (`Memory.get_all()`) + the current `latest_input` into one string. **The entire history is re-sent on every LLM call** — there is no message-array-style history; the prompt is one flat string with `${{...}}` placeholders replaced via `str.replace`.
   - `llm()` sends it as a single `user` message.
   - `match_tool_action()` does substring matching on the response for `"Action"` plus any tool name; `parse_action_input()` regex-extracts the JSON after `Action Input:`.
   - If a tool is matched, `execute_tool()` runs it, the LLM output and `Observation: <result>` are appended to `latest_input`, and the loop iterates. Otherwise the response is returned to the user.
3. After return, `main()` appends both the user input and the AI reply to `Memory`.

Implications when modifying:
- **Adding a tool** means editing two places: append a `{"name": ..., "description": ..., ...}` dict to `TOOLS`, and add a name→implementation branch in `execute_tool()`. The prompt template auto-formats `TOOLS` via `json.dumps` and builds the `Action:` line from tool names.
- **The ReAct loop has no max-iteration guard** — a malformed LLM response that keeps emitting `Action:` will loop forever. Add a cap if extending.
- **Tool matching is loose**: any occurrence of a tool name anywhere in the response triggers it. Tool names should be distinctive.
- Comments and prompts are in Chinese; preserve language when editing user-facing strings.

## Skills present in the repo

`.claude/skills/` and `.agents/skills/` contain installed skill bundles (`ata-all`, `ale-file-parser`). These are tooling for Claude Code itself, not part of the demo's runtime.
