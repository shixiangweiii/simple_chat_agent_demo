---
name: ata-all
version: 0.29.0
description: ATA 文章搜索与详情、首页头条与翰林院推荐、文章草稿创建与管理、评论、收藏、点赞、用户查询、钉钉消息推送。
  TRIGGER：用户提到 ATA、技术文章、内部文章、ata.atatech.org 链接，或要求搜索/推送/收藏/评论/点赞文章，或要求创建/查询/修改文章草稿。
  SKIP：与 ATA 无关的通用编程问题。
---

# ATA 官方技能

## 调用说明

### 前置依赖

- 本机已安装 **npm**
- 全局已安装 **aone-kit**，未安装则执行：`npm install -g @ali/aone-kit --registry=https://registry.anpm.alibaba-inc.com`

### 调用方式

```
aone-kit call-tool <tool-id> <args> --provider zetta
```

### 调用步骤

1. 在下方「领域列表」匹配领域，阅读对应的 `references/domains/*/README.md`
2. 定位 `tool-id`（格式 `ata::xxxx`），读取对应 json 文件
3. 根据 json 文件的 `inputSchema` 构造 `args`
   - 遇到「字段选择器」时，结合 `outputSchema` 与用户意图选择字段
   - `fieldName_0`、`fieldName_1` 是真正的参数名，不是序号
   - `args` 用单引号包裹，避免 shell 展开

### 展示约定

- 字段语义以 json 文件的 `outputSchema` 为准
- ATA 链接保留完整 URL，不要去掉 `umt_` 等查询参数

### 示例

```bash
aone-kit call-tool ata::article-list-query '{"fieldName_0":[11020616005],"fieldName_1":{"user":true}}' --provider zetta
aone-kit call-tool ata::url-analyze-url '{"fieldName_0":{"url":"https://ata.atatech.org/articles/11020616005"}}' --provider zetta
```

## 领域列表

| 领域 | 说明 | 详细文档 |
|------|------|----------|
| URL 解析 | 将 ATA 文章链接解析为文章 ID | `references/domains/url/README.md` |
| 分类标签 | 查询知识体系/文章类型标签列表，获取标签 ID | `references/domains/category/README.md` |
| 文章 | 搜索文章、按 ID 查详情、首页头条、翰林院推荐、更新 AI 辅助百分比、草稿创建与管理 | `references/domains/article/README.md` |
| 评论 | 分页查询文章下的评论列表 | `references/domains/comment/README.md` |
| 收藏 | 收藏分类与收藏记录的增删改查 | `references/domains/mark/README.md` |
| 点赞 | 点赞（含原因）、查询点赞记录、取消点赞 | `references/domains/vote/README.md` |
| 用户 | 按花名/姓名/工号查询用户 ID、获取当前用户信息 | `references/domains/user/README.md` |
| 消息推送 | 通过钉钉技小蜜向用户私聊或群推送消息 | `references/domains/message/README.md` |
