# 创建响应

## 说明

```
通过兼容 OpenAI 格式的 Responses API 调用千问模型，查看输入输出参数说明及调用示例。

相较于OpenAI Chat Completions API 的优势：

内置工具：内置联网搜索、网页抓取、代码解释器、文搜图、图搜图、知识库搜索等工具，可在处理复杂任务时获得更优效果，详情参考工具调用。

更灵活的输入：支持直接传入字符串作为模型输入，也兼容 Chat 格式的消息数组。

简化上下文管理：通过传递上一轮响应的 previous_response_id，无需手动构建完整的消息历史数组。

便捷的上下文缓存：只需在请求头中添加 x-dashscope-session-cache: enable，服务端即可自动缓存对话上下文，无需改动业务代码即可降低多轮对话的推理延迟与成本，详情参考Session 缓存。

兼容性说明与限制
本 API 在接口设计上兼容 OpenAI，以降低开发者迁移成本，但在参数、功能和具体行为上存在差异。

核心原则：请求将仅处理本文档明确列出的参数，任何未提及的 OpenAI 参数都会被忽略。

以下是几个关键的差异点，以帮助您快速适配：

部分参数不支持：不支持部分 OpenAI Responses API 参数，例如异步执行参数background（当前仅支持同步调用）等。

思考强度控制：通过 reasoning.effort 参数控制模型的思考强度，具体用法请参考相应参数的说明。
```

## 请求参数

```
model string （必选）

模型名称。

支持的模型

input string 或 array （必选）

模型输入，支持以下格式：

string：纯文本，如 "你好"。

array：消息数组，按对话顺序排列。

array 输入项类型

instructions string （可选）

作为系统指令插入到上下文的起始位置。使用 previous_response_id 时，上一轮指定的 instructions 不会传入本轮上下文。

previous_response_id string （可选）

上一个响应的唯一 ID，当前响应id有效期为7天。使用此参数可创建多轮对话，服务端会自动检索并组合该轮次的输入与输出作为上下文。当同时提供 input 消息数组和 previous_response_id 时，input 中的新消息会追加到历史上下文之后。不能与 conversation 同时使用。

conversation string （可选）

当前响应所属的会话（参考Conversations API）。会话中的历史项会自动作为上下文传入本次请求，本次请求的输入和输出也会在响应完成后自动添加到会话中。不能与 previous_response_id 同时使用。

stream boolean （可选）默认值为 false

是否开启流式输出。设置为 true 时，模型响应数据将实时流式返回给客户端。

store boolean （可选）默认值为 true

是否储存本次会话生成的模型响应。

false：不储存，对话内容不能被 previous_response_id 和后续 API 使用。

true：储存，当前模型响应可被 previous_response_id 和后续 API 使用。

tools array （可选）

模型在生成响应时可调用的工具数组。支持内置工具和自定义 function 工具，可混合使用。

为了获得最佳回复效果，建议同时开启 code_interpreter、web_search 和 web_extractor 工具。
属性

tool_choice string or object （可选）默认值为 auto

控制模型如何选择和调用工具。此参数支持两种赋值格式：字符串模式和对象模式。

字符串模式

auto：模型自动决定是否调用工具。

none：禁止模型调用任何工具。

required：强制模型调用工具（仅当 tools 列表中只有一个工具时可用）。

对象模式

为模型设定可用的工具范围，仅限在预定义的工具列表中进行选择和调用。

属性

temperature float （可选）

采样温度，控制模型生成文本的多样性。

temperature越高，生成的文本更多样，反之，生成的文本更确定。

取值范围： [0, 2)

temperature与top_p均可以控制生成文本的多样性，建议只设置其中一个值。更多说明，请参见概述。

top_p float （可选）

核采样的概率阈值，控制模型生成文本的多样性。

top_p越高，生成的文本更多样。反之，生成的文本更确定。

取值范围：（0,1.0]

temperature与top_p均可以控制生成文本的多样性，建议只设置其中一个值。更多说明，请参见概述。

enable_thinking boolean （可选）

是否开启思考模式。开启后，模型会在回复前进行思考，思考内容将通过 reasoning 类型的输出项返回。开启思考模式时，建议开启内置工具，以在处理复杂任务时获得最佳的模型效果。

可选值：

true：开启

false：不开启

不同模型的默认值：支持的模型

该参数非OpenAI标准参数。Python SDK 通过 extra_body={"enable_thinking": True} 传递；Node.js SDK 和 curl 直接使用 enable_thinking: true 作为顶层参数。建议使用 reasoning.effort 替代，enable_thinking 后续将不再支持。
reasoning object （可选）

控制模型的思考强度。模型会在回复前进行思考，思考内容将通过 reasoning 类型的输出项返回。

属性

reasoning.effort 的优先级高于 enable_thinking，建议优先使用 reasoning.effort，enable_thinking 后续将不再支持。
```

### 例子

```
import os
from openai import OpenAI

client = OpenAI(
    # 若没有配置环境变量，请用百炼API Key将下行替换为：api_key="sk-xxx"
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)

response = client.responses.create(
    model="qwen3.6-plus",
    input="你能做些什么？"
)

# 获取模型回复
print(response.output_text)
```

```
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)

stream = client.responses.create(
    model="qwen3.6-plus",
    input="请简单介绍一下人工智能。",
    stream=True
)

print("开始接收流式输出:")
for event in stream:
    if event.type == 'response.output_text.delta':
        print(event.delta, end='', flush=True)
    elif event.type == 'response.completed':
        print("\n流式输出完成")
        print(f"总Token数: {event.response.usage.total_tokens}")
```

```
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)

response = client.responses.create(
    model="qwen3.6-plus",
    input="帮我找一下阿里云官网，并提取首页的关键信息",
    # 建议同时开启内置工具以取得最佳效果
    tools=[
        {"type": "web_search"},
        {"type": "code_interpreter"},
        {"type": "web_extractor"}
    ],
)

# 取消以下注释查看中间过程输出
# print(response.output)
print(response.output_text)
```

```
from openai import OpenAI
import json
import os
import random

# 初始化客户端
client = OpenAI(
    # 若没有配置环境变量，请用阿里云百炼API Key将下行替换为：api_key="sk-xxx",
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)
# 模拟用户问题
USER_QUESTION = "北京天气咋样"
# 定义工具列表
tools = [
    {
        "type": "function",
        "name": "get_current_weather",
        "description": "当你想查询指定城市的天气时非常有用。",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "城市或县区，比如北京市、杭州市、余杭区等。",
                }
            },
            "required": ["location"],
        },
    }
]


# 模拟天气查询工具
def get_current_weather(arguments):
    weather_conditions = ["晴天", "多云", "雨天"]
    random_weather = random.choice(weather_conditions)
    location = arguments["location"]
    return f"{location}今天是{random_weather}。"


# 封装模型响应函数
def get_response(input_data):
    response = client.responses.create(
        model="qwen3.6-plus",  
        input=input_data,
        tools=tools,
    )
    return response


# 维护对话上下文
conversation = [{"role": "user", "content": USER_QUESTION}]

response = get_response(conversation)
function_calls = [item for item in response.output if item.type == "function_call"]
# 如果不需要调用工具，直接输出内容
if not function_calls:
    print(f"助手最终回复：{response.output_text}")
else:
    # 进入工具调用循环
    while function_calls:
        for fc in function_calls:
            func_name = fc.name
            arguments = json.loads(fc.arguments)
            print(f"正在调用工具 [{func_name}]，参数：{arguments}")
            # 执行工具
            tool_result = get_current_weather(arguments)
            print(f"工具返回：{tool_result}")
            # 将工具调用和结果成对追加到上下文中
            conversation.append(
                {
                    "type": "function_call",
                    "name": fc.name,
                    "arguments": fc.arguments,
                    "call_id": fc.call_id,
                }
            )
            conversation.append(
                {
                    "type": "function_call_output",
                    "call_id": fc.call_id,
                    "output": tool_result,
                }
            )
        # 携带完整上下文再次调用模型
        response = get_response(conversation)
        function_calls = [
            item for item in response.output if item.type == "function_call"
        ]
    print(f"助手最终回复：{response.output_text}")
```

## 返回结果 Response 响应对象（非流式输出）

```
id string

本次响应的唯一标识符，为 UUID 格式的字符串，有效期为7天。可用于 previous_response_id 参数以创建多轮对话。

created_at integer

本次请求的 Unix 时间戳（秒）。

object string

对象类型，固定为 response。

status string

响应生成的状态。枚举值：

completed：生成完成

failed：生成失败

in_progress：生成中

cancelled：已取消

queued：请求排队中

incomplete：生成不完整

model string

用于生成响应的模型 ID。

output array

模型生成的输出项数组。数组中的元素类型和顺序取决于模型的响应。

数组元素属性

usage object

本次请求的 Token 消耗信息。

属性

error object

当模型生成响应失败时返回的错误对象。成功时为 null。

tools array

回显请求中 tools 参数的完整内容，结构与请求体中的 tools 参数相同。

tool_choice string

回显请求中 tool_choice 参数的值，枚举值为 auto、none、required。

```

### 例子

```
{
    "created_at": 1771165743,
    "id": "c9f9c06b-032d-4525-a422-ac8ab5eccxxx",
    "model": "qwen3.6-plus",
    "object": "response",
    "output": [
        {
            "content": [
                {
                    "annotations": [],
                    "text": "你好！我是 Qwen3.5，阿里巴巴最新推出的通义千问大语言模型，具备强大的语言理解、逻辑推理、代码生成及多模态处理能力，旨在为用户提供精准高效的智能服务。",
                    "type": "output_text"
                }
            ],
            "id": "msg_544b2907-e88e-40d2-9a83-c30d6d1f9xxx",
            "role": "assistant",
            "status": "completed",
            "type": "message"
        }
    ],
    "parallel_tool_calls": false,
    "status": "completed",
    "tool_choice": "auto",
    "tools": [],
    "usage": {
        "input_tokens": 55,
        "input_tokens_details": {
            "cached_tokens": 0
        },
        "output_tokens": 43,
        "output_tokens_details": {
            "reasoning_tokens": 0
        },
        "total_tokens": 98,
        "x_details": [
            {
                "input_tokens": 55,
                "output_tokens": 43,
                "total_tokens": 98,
                "x_billing_type": "response_api"
            }
        ]
    }
}
```

## Response 响应 chunk 对象（流式输出）

```
流式输出返回一系列 JSON 对象。每个对象包含 type 字段标识事件类型，sequence_number 字段标识事件顺序。response.completed 事件标志着流式传输的结束。

type string

事件类型标识符。枚举值：

response.created：响应创建时触发，状态为 queued。

response.in_progress：响应开始处理时触发，状态变为 in_progress。

response.output_item.added：新的输出项（如 message、web_extractor_call）被添加到 output 数组时触发。当 item.type 为 web_extractor_call 时，表示网页抽取工具调用开始。

response.content_part.added：输出项的 content 数组中新增内容块时触发。

response.output_text.delta：增量文本生成时触发，多次触发，delta 字段包含新增文本片段。

response.output_text.done：文本生成完成时触发，text 字段包含完整文本。

response.content_part.done：内容块完成时触发，part 对象包含完整内容块。

response.output_item.done：输出项生成完成时触发，item 对象包含完整输出项。当 item.type 为 web_extractor_call 时，表示网页抽取工具调用完成。

response.reasoning_summary_text.delta：（开启思考模式时）推理摘要增量文本，delta 字段包含新增摘要片段。

response.reasoning_summary_text.done：（开启思考模式时）推理摘要完成，text 字段包含完整摘要。

response.web_search_call.in_progress / searching / completed：（使用 web_search 工具时）搜索状态变化事件。

response.code_interpreter_call.in_progress / interpreting / completed：（使用 code_interpreter 工具时）代码执行状态变化事件。

注意：使用 web_extractor 工具时，没有专门的事件类型标识符。网页抽取工具调用通过通用的 response.output_item.added 和 response.output_item.done 事件传递，通过 item.type 字段（值为 web_extractor_call）来识别。

response.mcp_call_arguments.delta / response.mcp_call_arguments.done：（使用 mcp 工具时）MCP 调用参数的增量和完成事件。

response.mcp_call.completed：（使用 mcp 工具时）MCP 服务调用完成。

response.file_search_call.in_progress / searching / completed：（使用 file_search 工具时）知识库搜索状态变化事件。

注意：使用 web_search_image 和 image_search 工具时，没有专门的中间状态事件。工具调用通过 response.output_item.added（调用开始）和 response.output_item.done（调用完成）事件传递。

response.completed：响应生成完成时触发，response 对象包含完整响应（含 usage）。此事件标志流式传输结束。

sequence_number integer

事件序列号，从 0 开始递增。用于确保客户端按正确顺序处理事件。

response object

响应对象。出现在 response.created、response.in_progress 和 response.completed 事件中。在 response.completed 事件中包含完整的响应数据（包括 output 和 usage），其结构与非流式响应的 Response 对象一致。

item object

输出项对象。出现在 response.output_item.added 和 response.output_item.done 事件中。在 added 事件中为初始骨架（content 为空数组），在 done 事件中为完整对象。

属性

part object

内容块对象。出现在 response.content_part.added 和 response.content_part.done 事件中。

属性

delta string

增量文本内容。出现在 response.output_text.delta 事件中，包含本次新增的文本片段。客户端应将所有 delta 拼接以获得完整文本。

text string

完整文本内容。出现在 response.output_text.done 事件中，包含该内容块的完整文本，可用于校验 delta 拼接结果。

item_id string

输出项的唯一标识符。用于关联同一输出项的相关事件。

output_index integer

输出项在 output 数组中的索引位置。

content_index integer

内容块在 content 数组中的索引位置。

summary_index integer

摘要数组索引。出现在 response.reasoning_summary_text.delta 和 response.reasoning_summary_text.done 事件中。
```