# 用户领域

按花名/姓名/工号查询 ATA 用户 ID，或获取当前登录用户信息。ATA 用户 ID 一般是 11 或 12 开头的 11 位数字，用于其他接口的 `userId` 入参。

## 工具列表

### ata::user-comprehensive-page-query

按花名、姓名、工号模糊查询用户，返回 ATA 用户 ID。

- 调用详细说明（调用前必读）：`references/domains/user/user-comprehensive-page-query.json`

### ata::user-list-query-by-work-id

按工号精确查询用户 ID。

- 调用详细说明（调用前必读）：`references/domains/user/user-list-query-by-work-id.json`

### ata::user-self

获取当前登录用户信息。

- 调用详细说明（调用前必读）：`references/domains/user/user-self.json`

## 典型使用流程

### 将花名/姓名转为用户 ID

1. 调 `user-comprehensive-page-query`，`searchKey` 传花名或姓名
2. 从结果中取 `id` 字段，用于 `article-comprehensive-page-query` 等接口的 `userId` 入参

### 将工号转为用户 ID

1. 调 `user-list-query-by-work-id`，传入工号列表
2. 从结果中取 `id` 字段

## 易混淆说明

- 已知花名/姓名时用 `user-comprehensive-page-query`（模糊搜索），已知工号时用 `user-list-query-by-work-id`（精确查询）
- 获取「我是谁」用 `user-self`，不需要传任何查询参数
- 用户 ID（11/12 开头的 11 位数字）与工号不同，其他接口的 `userId` 入参需要的是用户 ID
