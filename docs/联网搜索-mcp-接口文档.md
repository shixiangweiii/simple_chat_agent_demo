### 联网搜索
基于通义实验室 Text-Embedding，GTE-reRank，Query 改写，搜索判定等多种检索模型及语义理解，串接专业搜索工程框架及各类型实时信息检索工具，提供实时互联网全栈信息检索，提升 LLM 回答准确性及时效性。

### JSON格式用法
```
{
  "mcpServers": {
    "WebSearch": {
      "type": "streamableHttp",
      "description": "基于通义实验室 Text-Embedding，GTE-reRank，Query 改写，搜索判定等多种检索模型及语义理解，串接专业搜索工程框架及各类型实时信息检索工具，提供实时互联网全栈信息检索，提升 LLM 回答准确性及时效性。",
      "isActive": true,
      "name": "AliyunBailianMCP_WebSearch",
      "baseUrl": "https://dashscope.aliyuncs.com/api/v1/mcps/WebSearch/mcp",
      "headers": {
        "Authorization": "Bearer ${DASHSCOPE_API_KEY}"
      }
    }
  }
}
```

### 使用 MCP SDK 调用
#### Streamable HTTP Endpoint
https://dashscope.aliyuncs.com/api/v1/mcps/WebSearch/mcp

#### 鉴权方式
- 获取 DASHSCOPE_API_KEY，并添加至 header 中进行鉴权，鉴权方式参考：获取 DASHSCOPE_API_KEY，替换配置文件中的${DASHSCOPE_API_KEY}
- 环境变量配置 DASHSCOPE_API_KEY 使用，例如：export DASHSCOPE_API_KEY=sk-xxxx