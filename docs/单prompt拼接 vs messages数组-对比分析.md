# 单 prompt 拼接 vs messages 数组 —— 调研与对比分析

> **调研日期**: 2026-05-24
> **背景**: 当前 demo 调用千问时，自己组装一个大字符串 prompt(把所有历史对话拼进去)，没有使用千问/OpenAI 官方推荐的 `messages=[{role, content}, ...]` 数组形式。本文对比两种做法，并结合现状给出建议。
> **代码现场**: `demo/chat_core.py::build_prompt` / `demo/llm_client.py::_llm_responses` / `_llm_chat`

---

## 一、调研到的核心事实

| # | 来源 | 关键结论 |
|---|---|---|
| 1 | [百炼·多轮对话](https://help.aliyun.com/zh/model-studio/multi-round-conversation) | 千问 API 是**无状态**的，不保存历史。**官方实现多轮对话的方式就是维护 `messages` 数组**——每轮把 user/assistant 轮流追加，下次请求把整个数组作为入参传入。 |
| 2 | [Qwen 官方文档·核心概念](https://qwen.readthedocs.io/zh-cn/latest/getting_started/concepts.html) | Qwen 模型底层用 **ChatML** 训练:<br>`<\|im_start\|>{role}\n{content}<\|im_end\|>`<br>`<\|im_start\|>` / `<\|im_end\|>` 是**单个特殊 token**(token id 例如 151644)，由 chat_template 在 messages → tokens 的过程中自动注入。 |
| 3 | [百炼·上下文缓存(Context Cache)](https://help.aliyun.com/zh/model-studio/context-cache) | DashScope 支持**显式/隐式 prompt cache**;命中时输入按折扣计费(其它厂商通常打到 10%-25%)。**缓存依赖请求前缀逐字节一致**(prefill 阶段的 KV 张量复用)。 |
| 4 | [OpenAI Completions → Chat Completions 迁移指南](https://help.openai.com/zh-hans-cn/articles/7042661-moving-from-completions-to-chat-completions-in-the-openai-api) | OpenAI 已将 `prompt` 字段标为旧式风格，统一推荐 `messages` 数组;同质量任务下 token 消耗最高可降 ~90%(对比老的 davinci-003)。 |
| 5 | [ChatML Special Token 设计](https://blog.csdn.net/ningyanggege/article/details/159696685) | 角色边界 token 的成本对比:<br>- `<\|im_start\|>system` → **2 个 token**<br>- `<system>` 普通文本 → **6 个 token**<br>- 本项目的 `用户: ` 中文前缀 → **2-3 个 token / turn** |
| 6 | [.NET+AI 三大 API 历史管理对比](https://zhuanlan.zhihu.com/p/2033103606249935720) | Chat Completions / Responses / Anthropic Messages 三类主流 API 中，**只有 Responses API** 支持 `previous_response_id` 服务端续话;其余都要求客户端每轮重发完整 messages。 |

---

## 二、本项目现状速览

### 当前 prompt 组装

`build_prompt`(`demo/chat_core.py:323`)把以下内容 join 成**一个字符串**:

```
USER_PROMPT             ← 系统级指令,但当前作为 user content 注入
---------------------
# 工具列表(可选,TOOLS=[] 时整个块跳过)
[ ... ]
使用如下格式:
Thought / Action / Action Input / Observation
注意:...
---------------------
# 对话记录
用户: xxx                  ← Memory.get_all() 拼出来
AI: yyy
用户: zzz
...
# 最新输入
当前问题
```

### 两种 API mode 的入参形态

| mode | 入参形态 | 备注 |
|---|---|---|
| `responses`(默认) | `client.responses.create(input=prompt, ...)` | `input` 接受单字符串或 messages-like 对象;当前传字符串 |
| `chat` | `client.chat.completions.create(messages=[{"role":"user","content":prompt}], ...)` | 把整段大字符串塞进**一条** user message |

两种模式都没用上 `messages` 数组的角色分割能力。

### 项目的明确取向

`CLAUDE.md` 把"Memory 是一段被反复重发的字符串"明确写入 **Non-goals**:

> Memory is one flat string re-sent each turn, not a `messages: [...]` array. Same reason — the visible string makes prompt assembly obvious.

也就是说:**单 prompt 字符串是教学 demo 的有意设计**,而不是历史遗留。

---

## 三、两种方式逐维对比

| 维度 | 单 prompt 字符串(现状) | messages 数组(官方推荐) |
|---|---|---|
| **角色边界** | 用中文 `用户:` / `AI:` 文本前缀模拟,模型靠字面意思推断 | 由 ChatML 特殊 token 显式分割,模型在训练阶段就学过这种结构,对齐严格 |
| **System prompt 待遇** | 当前把 `USER_PROMPT` 塞在 user content 顶部,模型把它当普通用户输入 | `{"role":"system",...}` 独立条目,权重更高、不易被后续 user 内容覆盖;越狱/角色混淆攻击面更小 |
| **Token 效率** | 每个 turn 多 2-3 个 token(`用户: ` / `AI: ` + `:`),长会话累计明显;分隔符不能复用为 prefix cache key | ChatML 角色切换 1-2 个特殊 token;多轮稳定前缀利于命中 prefix cache |
| **prompt cache 命中** | 理论上前缀仍可一致(每轮都从 USER_PROMPT 开头),但任何 USER_PROMPT 内容微调、`build_prompt` 分支变化都会**整段失效** | 缓存边界天然在 message 边界上;只要 system + 历史前 N 条不变,新增一条 user message 仍能命中前缀 |
| **结构化能力** | tools 调用必须自己用文本协议(`Action:` / `Observation:` / 正则解析) | 原生 `tools=[...]` + `tool_calls` 字段;Responses API 还有 `previous_response_id` 服务端续话模式 |
| **多模态** | 拼字符串无法表达图片/音频 | `content` 可以是 `[{"type":"image_url",...},{"type":"text",...}]` 的 part 数组 |
| **prompt 注入风险** | 用户输入若含 `\nAI: ` 或 `<\|im_start\|>` 字面量,可能被模型/tokenizer 误读为角色边界 | 由 chat_template 在 server 侧编码,user content 中的特殊 token 字符串会被转义/拒绝 |
| **可观测性** | `logger.info("prompt=\n%s", prompt)` 一眼能 dump 出模型实际看到啥;**这就是教学 demo 设计的核心收益** | 需要打印结构化 dict + 想象 chat_template 渲染后的样子;偏工程化 |
| **Memory 持久化复杂度** | `Memory.get_all()` 一行 join 完事 | 仍是 list 序列化,复杂度差不多;但需要枚举 role 类型 |
| **ReAct 教学性** | `Thought/Action/Observation` 全在一个字符串里循环,**这就是教学点** | 真要保留 ReAct 文字协议也行,但与 native tools 并存时认知负担变重 |
| **DashScope 兼容性** | OK,两种 API 都接受 | OK,且是官方文档示例形态 |
| **跨模型迁移成本** | 切到 GPT/Claude 时 prompt 形态需要重写 | messages 是事实标准,迁移最平滑 |

---

## 四、对本项目的具体影响排查

把上面的通用结论套到当前代码上,能落到的真实代价只有几条:

### 4.1 System 权重弱(中等影响)

`USER_PROMPT` 里那段"内置联网搜索...不要回复无法获取实时数据"的指令,被当作 user 输入而非 system,遇到强烈对抗性追问时**更容易被压制**——这其实和已记录的 memory [`qwen_builtin_web_search_prompt_conflict`](../.claude/projects/.../memory/qwen_builtin_web_search_prompt_conflict.md) 是同一类问题(prompt 措辞反向影响 API 层 tools 行为)。改成 system role 多半能让 web_search 启用更稳。

### 4.2 Prompt cache 收益拿不到(唯一金钱损失点)

这是**唯一在成本上**有实际损失的点。当对话长到 20+ 轮,每轮都重发上万 token,按 DashScope 的 context cache 文档,命中后输入价格能打折。但要拿到这个折扣,前缀必须**逐字节稳定**。当前 `build_prompt` 在 `TOOLS == []` 与非空之间会切换整个框架块,触发整段 cache miss。

### 4.3 未来加 ReAct 工具时会撞结构化协议(潜在影响)

一旦 `TOOLS != []`,模型会同时面对内置 web_search(原生 `tool_calls`)和自定义工具(文本 `Action:`),两套协议在一个 prompt 里并存,模型偶尔会把内置 web_search 也输出为 `Action:` 字符串。messages + native tools 协议能把这俩干净地分开。

### 4.4 Token 浪费量级估算(可忽略)

每轮多 ~3 token(`用户: \n`),10 轮对话约多 60 token,相对 `USER_PROMPT` 本身的 ~250 token 量级是 ~5% 增量,**可忽略**。

---

## 五、结论与建议

**保留现状作为默认教学层是对的**——这正是 `CLAUDE.md` 钉死的非目标,"prompt 可见"是 demo 的核心卖点,没有必要为了官方推荐而牺牲它。

但建议在不破坏教学性的前提下,可做以下小幅改进:

| 改动 | 收益 | 教学伤害 |
|---|---|---|
| 把 `USER_PROMPT` 单独作为 `{"role":"system"}` 传入(只在 chat 模式下生效;Responses 模式下可用 `instructions` 参数同效) | 解决"system 权重弱"问题,对内置 web_search 行为更稳 | 极小,prompt log 仍能 dump |
| `Memory` 内部仍是 list,序列化时仍可生成单字符串供 `logger.info` 打印;但发给 API 时拆成 messages 数组 | 拿到 prompt cache 折扣;多模态/tools 升级路径变直 | 小,需要双轨——日志看字符串、API 看数组 |
| 把"messages 模式"作为**第三种 `API_MODE` 实现**(`messages_chat`)与现有 `responses` / `chat` 并列 | 教学价值反而**放大**——同一个 demo 同时演示两种主流模式的差异 | 0,反而是加分项 |

### 优先级建议

如果只能改一处,**优先把 `USER_PROMPT` 提到 system role**——成本最低、收益最直接,并不破坏"prompt 可见"的教学契约。

如果允许更激进的改造,可以走方案三(并列第三种 API_MODE),把这份对比从纸面变成可运行的代码演示,教学价值最大化。

---

## 参考链接

- [百炼·多轮对话(messages 数组官方做法)](https://help.aliyun.com/zh/model-studio/multi-round-conversation)
- [百炼·Context Cache 上下文缓存](https://help.aliyun.com/zh/model-studio/context-cache)
- [Qwen 核心概念 ChatML 格式](https://qwen.readthedocs.io/zh-cn/latest/getting_started/concepts.html)
- [OpenAI Completions → Chat Completions 迁移](https://help.openai.com/zh-hans-cn/articles/7042661-moving-from-completions-to-chat-completions-in-the-openai-api)
- [ChatML 特殊 Token 设计哲学](https://blog.csdn.net/ningyanggege/article/details/159696685)
- [Prompt Caching 跨厂商对比](https://blog.csdn.net/weixin_40242845/article/details/153699823)
- [.NET+AI 三大 API 历史管理对比](https://zhuanlan.zhihu.com/p/2033103606249935720)
- [百炼·文本生成模型(System/User/Assistant 角色定义)](https://help.aliyun.com/document_detail/2712581.html)
