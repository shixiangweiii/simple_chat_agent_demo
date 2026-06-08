# 点赞领域

对文章等内容点赞（可带点赞原因枚举）、分页查询当前用户的点赞记录，或取消点赞。

## 工具列表

### ata::vote-create-or-update-with-permission

给指定内容点赞。

- 调用详细说明（调用前必读）：`references/domains/vote/vote-create-or-update-with-permission.json`

### ata::vote-delete-with-permission

取消点赞指定内容。

- 调用详细说明（调用前必读）：`references/domains/vote/vote-delete-with-permission.json`

### ata::vote-page-query-with-permission

分页查询当前登录用户的点赞记录。

- 调用详细说明（调用前必读）：`references/domains/vote/vote-page-query-with-permission.json`

## 典型使用流程

### 给文章点赞

1. 获取文章 ID（若只有链接，先用 `url-analyze-url` 解析）
2. 调 `vote-create-or-update-with-permission`，`type` 传 1（文章），可附带 `reasonKindList` 指定点赞原因

### 查看点赞历史

1. 调 `vote-page-query-with-permission` 分页查询
2. 通过 selector 的 `object` 字段可获取点赞对象的详细信息

## 易混淆说明

- 文章列表/详情中的「是否已点赞、点赞原因列表」等通过文章类工具的 `fieldName_1` selector 拉取，见 `references/domains/article/` 下各 JSON 的 `inputSchema` 与 `outputSchema`
- 新增或修改当前用户对文章的点赞与原因用 `vote-create-or-update-with-permission`；仅取消点赞用 `vote-delete-with-permission`
- 按分页查看「当前用户点过哪些赞」用 `vote-page-query-with-permission`；与文章详情里的 `hasVote` 等状态位互补
- 点赞与收藏是不同功能：点赞用 `vote-*`，收藏用 `mark-*`
