"""CLI 入口 —— 接 stdin/stdout,调 chat_core.react()。

启动:
    export DASHSCOPE_API_KEY=sk-xxx
    python demo/common_chat_agent.py
"""

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from chat_core import Memory, react  # noqa: E402


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )

    if not os.environ.get("DASHSCOPE_API_KEY"):
        print(
            "未配置环境变量 DASHSCOPE_API_KEY\n"
            "用法: export DASHSCOPE_API_KEY=sk-xxx && python demo/common_chat_agent.py",
            file=sys.stderr,
        )
        return

    # chat 模式需要 MCP key;如果未单独设 DASHSCOPE_API_KEY_MCP,将 fallback 使用 DASHSCOPE_API_KEY
    from chat_core import API_MODE  # noqa: E402 (已在上面 sys.path.insert)
    if API_MODE == "chat" and not os.environ.get("DASHSCOPE_API_KEY_MCP"):
        print(
            "提示: API_MODE=chat 下 MCP 联网搜索将使用 DASHSCOPE_API_KEY (fallback)。\n"
            "如需 MCP 使用独立 key,请: export DASHSCOPE_API_KEY_MCP=sk-xxx",
            file=sys.stderr,
        )

    memory = Memory()
    print("通用聊天 Agent 已启动，请开始对话（输入 exit 退出）", file=sys.stderr)

    while True:
        try:
            user_input = input()
        except EOFError:
            break

        if user_input.strip().lower() == "exit":
            print("检测到退出指令，对话结束！")
            break

        output = react(memory, user_input)
        print(f"AI: {output}", file=sys.stderr)

        memory.add(Memory.USER, user_input)
        memory.add(Memory.AI, output)


if __name__ == "__main__":
    main()
