# 收藏领域

管理收藏标签（分类）的增删改查，以及收藏记录的创建、取消与分页查询。目前仅文章支持收藏标签。

## 工具列表

### 收藏标签管理

#### ata::mark-category-create

新增收藏标签。

- 调用详细说明（调用前必读）：`references/domains/mark/mark-category-create.json`

#### ata::mark-category-update-with-permission

编辑收藏标签。

- 调用详细说明（调用前必读）：`references/domains/mark/mark-category-update-with-permission.json`

#### ata::mark-category-delete-with-permission

删除收藏标签。

- 调用详细说明（调用前必读）：`references/domains/mark/mark-category-delete-with-permission.json`

#### ata::mark-category-page-query-with-permission

分页查询当前用户的收藏标签。

- 调用详细说明（调用前必读）：`references/domains/mark/mark-category-page-query-with-permission.json`

### 收藏记录管理

#### ata::mark-create-or-update-with-permission

收藏一个内容，如果已经存在则会修改。

- 调用详细说明（调用前必读）：`references/domains/mark/mark-create-or-update-with-permission.json`

#### ata::mark-delete-with-permission

取消收藏指定内容。

- 调用详细说明（调用前必读）：`references/domains/mark/mark-delete-with-permission.json`

#### ata::mark-page-query-with-permission

分页查询当前用户的收藏信息。

- 调用详细说明（调用前必读）：`references/domains/mark/mark-page-query-with-permission.json`

## 典型使用流程

### 收藏文章到指定标签

1. 调 `mark-category-page-query-with-permission` 查询已有标签，匹配目标标签 ID
2. 若标签不存在，调 `mark-category-create` 新建标签
3. 调 `mark-create-or-update-with-permission` 将文章收藏到该标签下

### 查看我的收藏

1. 调 `mark-page-query-with-permission` 分页查询收藏列表
2. 可通过 `cid` 参数按标签筛选

## 易混淆说明

- 管理「标签/文件夹」用 `mark-category-*`；管理「某篇文章是否在收藏里」用 `mark-create-or-update-with-permission` / `mark-delete-with-permission` / `mark-page-query-with-permission`
- 收藏与点赞是不同功能：收藏用 `mark-*`，点赞用 `vote-*`
