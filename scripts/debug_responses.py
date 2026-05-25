"""临时排查脚本：直接打印 client.responses.create 流的每个 chunk type，定位空响应根因。

用法:
    export DASHSCOPE_API_KEY=sk-xxx
    python scripts/debug_responses.py
"""

import os
import sys
from openai import OpenAI

MODEL = os.environ.get("QWEN_MODEL", "qwen3.7-max")
PROMPT = "杭州明天天气如何"

client = OpenAI(
    api_key=os.environ["DASHSCOPE_API_KEY"],
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)

print(f"=== model={MODEL} prompt={PROMPT!r}", file=sys.stderr)

stream = client.responses.create(
    model=MODEL,
    input=PROMPT,
    tools=[{"type": "web_search"}],
    extra_body={"enable_thinking": True},
    store=False,
    stream=True,
)

count = 0
for chunk in stream:
    count += 1
    ctype = getattr(chunk, "type", None)
    print(f"--- chunk #{count} type={ctype!r}")
    try:
        print(chunk.model_dump_json(indent=2))
    except Exception as exc:
        print(f"(model_dump_json failed: {exc})")
        print(repr(chunk))

print(f"=== total chunks: {count}", file=sys.stderr)
