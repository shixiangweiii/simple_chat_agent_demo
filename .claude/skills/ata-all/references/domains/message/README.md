# 消息推送领域

通过钉钉机器人「ATA 技小蜜」向当前用户私聊或指定群（webhook）推送消息。

## 工具列表

### ata::message-ding-talk-send-to-me

向当前用户推送一条钉钉私聊消息。

- 调用详细说明（调用前必读）：`references/domains/message/message-ding-talk-send-to-me.json`

### ata::message-ding-talk-send-to-webhook

向指定钉钉群推送消息，需要用户提供群的 webhook 地址。

- 调用详细说明（调用前必读）：`references/domains/message/message-ding-talk-send-to-webhook.json`

## 典型使用流程

### 推送消息给自己

1. 调 `message-ding-talk-send-to-me`，传入 markdown 内容和标题

### 推送消息到钉钉群

1. 用户提供群的 webhook 地址
2. 调 `message-ding-talk-send-to-webhook`，传入 webhook、markdown 内容和标题
3. 若用户未提供 webhook，提示：在需要推送的群搜索并加入机器人「ATA 技小蜜」，复制 webhook 作为参数

## 易混淆说明

- 推送给个人用 `message-ding-talk-send-to-me`，推送到群用 `message-ding-talk-send-to-webhook`
- 用户未提供 webhook 时，提示：在需要推送的群搜索并加入机器人「ATA 技小蜜」，复制 webhook 作为参数
