# 评论领域

分页查询文章等内容下的评论列表。

## 工具列表

### ata::comment-page-query

分页查询指定内容的评论列表，支持排序。

- 调用详细说明（调用前必读）：`references/domains/comment/comment-page-query.json`

## 典型使用流程

### 查看文章评论

1. 获取文章 ID（若只有链接，先用 `url-analyze-url` 解析）
2. 调 `comment-page-query`，`source` 传 11（文章），`pid` 传文章 ID
3. 通过 selector 的 `replyList` 可获取评论下的回复列表
