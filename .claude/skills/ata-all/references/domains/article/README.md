# 文章领域

搜索文章、按 ID 查详情、首页头条、翰林院推荐、更新 AI 辅助百分比、草稿创建与管理。

## 工具列表

### 文章查询

#### ata::article-comprehensive-page-query

按关键字、文章类型、知识体系标签、用户工号、创建时间等条件搜索文章，支持按访问量、互动量等排序。

- 调用详细说明（调用前必读）：`references/domains/article/article-comprehensive-page-query.json`

#### ata::article-list-query

按文章 ID 批量查询详情。若用户只提供链接，先用 `url-analyze-url` 解析出 ID。

- 调用详细说明（调用前必读）：`references/domains/article/article-list-query.json`

#### ata::article-headline

查看首页头条文章。

- 调用详细说明（调用前必读）：`references/domains/article/article-headline.json`

#### ata::article-article-recommend

查询翰林院推荐的热门文章。翰林院由技术专家组成，负责挖掘和推荐优质技术内容。

- 调用详细说明（调用前必读）：`references/domains/article/article-article-recommend.json`

### 文章更新

#### ata::article-update-with-permission

更新有权限的文章信息，目前仅支持修改 AI 辅助百分比。

- 调用详细说明（调用前必读）：`references/domains/article/article-update-with-permission.json`

### 文章草稿

草稿操作注意事项：
- 新增/修改草稿时参数较大，建议先将内容存储为文件，拼接 `<args>` 时使用命令替换传入
    - 构建参数的草稿 `markdown` 内容时不要删除或压缩内容数据
- 新增/修改草稿保留内容原有的外链（包含图片等）

#### ata::article-draft-create

创建文章草稿。

- 调用详细说明（调用前必读）：`references/domains/article/article-draft-create.json`

#### ata::article-draft-page-query-with-permission

分页查询自己的文章草稿信息。

- 调用详细说明（调用前必读）：`references/domains/article/article-draft-page-query-with-permission.json`

#### ata::article-draft-update-with-permission

修改文章草稿。

- 调用详细说明（调用前必读）：`references/domains/article/article-draft-update-with-permission.json`

## 典型使用流程

### 搜索文章

1. 若用户提供关键词/标签/作者，用 `article-comprehensive-page-query` 搜索
2. 若需要按知识体系标签搜索，先调 `category-list-all` 获取标签 ID

### 查看文章详情

1. 用户提供文章 ID → 直接调 `article-list-query`
2. 用户提供文章链接 → 先调 `url-analyze-url` 解析出 ID，再调 `article-list-query`

### 草稿写作

1. 调 `article-draft-create` 创建草稿
2. 调 `article-draft-page-query-with-permission` 查看已有草稿列表
3. 调 `article-draft-update-with-permission` 修改草稿内容

## 易混淆说明

- 「首页头条」用 `article-headline`，「翰林院推荐」用 `article-article-recommend`，两者不同
- 按 ID 查详情用 `article-list-query`，按关键词搜索用 `article-comprehensive-page-query`
- 用户只提供链接时，先用 `url` 领域的 `url-analyze-url` 解析出 ID
- 修改已发布文章属性用 `article-update-with-permission`，修改草稿内容用 `article-draft-update-with-permission`
