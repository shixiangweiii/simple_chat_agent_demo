"""
通用聊天 Agent —— 基于 ReAct 架构的教学 Demo (Python 版)

保留了完整的 ReAct（Thought-Action-Observation）循环机制、
工具列表框架、多轮对话记忆，去掉了具体业务人设。
工具列表暂时为空，可按需扩展。

使用 OpenAI Python SDK 调用通义千问 API。
"""

import json
import logging
import re
import sys
from openai import OpenAI

logger = logging.getLogger(__name__)


# ============================================================
# 工具定义
# ============================================================

AGENT_ACTION_TEMPLATE = "工具名称，必须是[{tools}]中的一个"

# 工具列表 —— 框架保留，具体工具数据先置空，后续按需添加
TOOLS = []


# ============================================================
# Prompt 模板
# ============================================================

USER_PROMPT = """# 角色设定
你是一位友好、专业的 AI 智能助手，能够帮助用户解答各类问题。

## 能力
1. 理解用户的自然语言输入，进行多轮对话；
2. 当有可用工具时，合理判断是否需要调用工具来辅助回答；
3. 当不需要工具时，直接给出清晰、有帮助的回复。

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

    def __init__(self):
        self.memories = []

    def add(self, role, msg):
        self.memories.append({"role": role, "msg": msg})

    def get_all(self):
        result = ""
        for chat_msg in self.memories:
            result += f"{chat_msg['role']}: \n{chat_msg['msg']}\n"
        return result


# ============================================================
# LLM 调用 - 使用 OpenAI Python SDK
# ============================================================

MODEL = "qwen3.6-plus"


def llm(prompt, api_key):
    """调用通义千问 LLM，开启思考模式"""
    client = OpenAI(
        api_key=api_key,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )

    completion = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "user", "content": prompt}
        ],
        extra_body={"enable_thinking": True},
    )

    message = completion.choices[0].message
    return message.content


# ============================================================
# ReAct 核心逻辑
# ============================================================

def match_tool_action(llm_result):
    """从 LLM 返回中匹配是否有工具调用，返回匹配到的工具名，未匹配返回 None"""
    if "Action" not in llm_result:
        return None
    for tool in TOOLS:
        if tool["name"] in llm_result:
            return tool["name"]
    return None


def parse_action_input(llm_result):
    """从 LLM 返回中解析 Action Input 的 JSON 参数"""
    pattern = r"Action Input:\s*(\{.*\})"
    match = re.search(pattern, llm_result)
    if match:
        return json.loads(match.group(1))
    return {}


def execute_tool(tool_name, params):
    """
    执行工具 —— 框架预留，当前工具列表为空
    扩展时在此处添加工具名到具体实现的路由逻辑
    """
    logger.warning("未找到工具实现:%s", tool_name)
    return json.dumps({"error": f"工具 {tool_name} 暂未实现"}, ensure_ascii=False)


def build_prompt(user_prompt, tools, memory, latest_input):
    """拼装完整的 Prompt = 角色设定 + 工具列表 + 对话记录 + 最新输入"""
    if not tools:
        tool_section = "（当前无可用工具）"
        action_format = "（无可用工具）"
    else:
        tool_section = json.dumps(tools, ensure_ascii=False)
        tool_names = ",".join(t["name"] for t in tools)
        action_format = AGENT_ACTION_TEMPLATE.format(tools=tool_names)

    prompt_template = (
        "${{user_prompt}}\n"
        "---------------------\n"
        "# 工具列表\n"
        "${{tool_definitions}}\n"
        "\n"
        "使用如下格式：\n"
        "Thought: 思考并确定下一步的最佳行动方案\n"
        "Action: ${{agent_action}}\n"
        "Action Input: 工具参数，一定必须是 JSON 对象\n"
        "Observation: 工具执行结果\n"
        "... (Thought/Action/Action Input/Observation 可以重复N次)\n"
        "\n"
        "注意：\n"
        "- 不使用工具时，回复中不要出现 Thought、Action、Action Input；\n"
        "- 使用工具前，先检查是否缺少必要参数，缺少必要参数时直接向用户提问，不要出现 Thought、Action、Action Input；\n"
        "- 工具执行遇到问题时，向用户寻求帮助；\n"
        "- 需要执行同一个工具多次时，Action Input 可以出现多次；\n"
        "\n"
        "---------------------\n"
        "# 对话记录\n"
        "${{history_record}}\n"
        "\n"
        "# 最新输入\n"
        "${{latest_input}}"
    )

    prompt = prompt_template.replace("${{user_prompt}}", user_prompt)
    prompt = prompt.replace("${{tool_definitions}}", tool_section)
    prompt = prompt.replace("${{agent_action}}", action_format)
    prompt = prompt.replace("${{history_record}}", memory.get_all())
    prompt = prompt.replace("${{latest_input}}", latest_input)

    return prompt


def react(api_key, memory, latest_input):
    """ReAct 核心循环：Thought -> Action -> Observation，直到不再需要工具调用"""
    while True:
        prompt = build_prompt(USER_PROMPT, TOOLS, memory, latest_input)
        logger.info("prompt=\n%s", prompt)

        llm_result = llm(prompt, api_key)
        logger.info("llmResult=\n%s", llm_result)

        # 尝试匹配工具调用
        matched_tool_name = match_tool_action(llm_result)

        if matched_tool_name is not None:
            logger.info("执行工具调用:%s,开始", matched_tool_name)

            # 解析 Action Input 中的 JSON 参数
            action_input = parse_action_input(llm_result)
            logger.info("工具参数:%s", action_input)

            # 执行工具
            tool_result = execute_tool(matched_tool_name, action_input)
            logger.info("执行工具调用:%s,结果=%s", matched_tool_name, tool_result)

            # 将 LLM 输出和工具结果作为 Observation 追加，进入下一轮循环
            latest_input += f"\n{llm_result}\nObservation: {tool_result}"
        else:
            # 无需工具调用，跳出循环，返回结果给用户
            return llm_result


# ============================================================
# 主入口
# ============================================================

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )

    if len(sys.argv) != 2:
        print("ak未配置，结束!")
        print("用法: python common_chat_agent.py <your_api_key>")
        return

    api_key = sys.argv[1]
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

        latest_input = user_input
        output = react(api_key, memory, latest_input)
        print(f"AI: {output}", file=sys.stderr)

        memory.add(Memory.USER, user_input)
        memory.add(Memory.AI, output)


if __name__ == "__main__":
    main()
