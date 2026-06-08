# 分类标签领域

查询知识体系或文章类型的标签全量列表，获取标签 ID 后在文章搜索等接口中使用。

## 工具列表

### ata::category-list-all

获取标签全量列表。入参 `cid`：1 为知识体系，2 为文章类型。

- 调用详细说明（调用前必读）：`references/domains/category/category-list-all.json`

## 典型使用流程

1. 调用 `category-list-all` 获取标签列表，匹配用户提到的标签名称，获取标签 ID
2. 将标签 ID 传入 `ata::article-comprehensive-page-query` 进行文章搜索
