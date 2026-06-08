# URL 解析领域

将 ATA 文章链接解析为文章 ID，供需要 `articleId` 的接口使用。

## 工具列表

### ata::url-analyze-url

从链接中提取文章 ID。例如 `https://ata.atatech.org/articles/11000050331` → `11000050331`。

- 调用详细说明（调用前必读）：`references/domains/url/url-analyze-url.json`

## 典型使用流程

1. 用户提供 ATA 文章链接
2. 调用 `url-analyze-url` 解析出文章 ID
3. 将文章 ID 传入 `ata::article-list-query` 获取文章详情
