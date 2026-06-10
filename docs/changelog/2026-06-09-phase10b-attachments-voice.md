# 2026-06-09 — Phase 10b + 10c：文本文件附件 & 语音输入

至此 Phase 10 多模态主题完整收尾。demo 输入能力从"文本 + 图片"扩展为"文本 + 图片 + 文本文件 + 语音"。

## Phase 10b：文本文件附件

### 后端

`demo/web_chat_agent.py`
- 新增常量：`MAX_ATTACHMENTS_PER_TURN=5`、`MAX_ATTACHMENT_CHARS=20KB`（软上限，业务层截断）、`MAX_ATTACHMENT_HARD_CHARS=100KB`（单文件硬上限，HTTP 400）、`MAX_ATTACHMENT_TOTAL_CHARS=100KB`、`ALLOWED_ATTACHMENT_MIMES`（20 个严格白名单）
- 新增异常 `AttachmentPayloadInvalid(ValueError)`
- 新增 `_validate_attachments(items)`：校验数量 / 三字段 / filename（无路径分隔符 / 控制字符 / Unicode 双向控制 / 不含 `..`）/ content（无 NUL / 字符上限 + UTF-8 字节上限）/ mime 严格白名单 / 总量上限
- `ChatRequest` 加 `attachments: list[dict] | None`
- `/api/chat` 路由调 `_validate_attachments` 并透传到 `stream_agent_response`

`demo/chat_core.py`
- 新增 `_MIME_TO_LANG` / `_EXT_TO_LANG` 语言映射、`MAX_PER_FILE_CHARS_SOFT=20KB`
- 新增纯函数 `_build_attachment_block(attachments)`：拼装 markdown 代码块，软截断 + `...(已截断, 原 N 字符)` 尾标
- `_memory_to_messages` 签名加 `attachments`；与 `images` 正交，**注入到 user_input 之后**（不是之前 —— 避免长附件挤出短问题的注意力）
- `_stream_chat_native` / `stream_agent_response` 透传 `attachments`
- `responses` 模式：attachments 非空时 yield error SSE 帧（与 images 同模式）
- USER_PROMPT 第 8 条增加"文件附件处理"指引

### 前端 `demo/static/index.html`

- HTML：`<input type="file">` accept 扩展支持 18 种文本扩展名；新增 `.attached-files` 容器；🎤 按钮
- CSS：`.attached-files` / `.file-chip` / `.file-chip .rm`
- JS：
  - 新增 `attachedFiles[]` 数组 + `attachFiles(files)` + `renderAttachedFiles()`（与 `attachImages` 并行存在，**不重命名**减少改动半径）
  - 新增 `dispatchSelectedFiles(fileList)`：按 `file.type` 分流到图片 / 文件两条路径
  - 📎 点击 / `$fileInput.change` / drop 改用 `dispatchSelectedFiles`；paste 保持仅图片
  - `send()` body 同时携带 `images` + `attachments`；user 气泡并列展示图片 stripe + 文件 chip strip

### Non-goals（与 Phase 10a 完全对称）

- HITL pending 不存 attachments
- Memory / markdown 归档 / runtime_state sidecar 不存 attachments
- CLI 模式不接 attachments
- `responses` 模式不支持

## Phase 10c：语音输入（纯前端）

`demo/static/index.html`
- HTML：🎤 按钮（无 SR API 时 `display:none`）
- CSS：`.attach-btn.recording`（红底）、`.voice-interim`（独立 DOM 节点，灰色 italic）
- JS：
  - `SpeechRecognition || webkitSpeechRecognition` 检测；不支持隐藏按钮
  - `toggleVoice` / `onSpeechResult` / `onSpeechEnd` / `onSpeechError`
  - interim 写入独立 `.voice-interim` 节点（不污染 textarea、不覆盖用户已编辑）
  - final 文本**追加**到 textarea 尾部（不覆盖）
  - `recognition.start()` 失败惰性 `display:none` + toast（Safari/Firefox 部分支持兜底）
  - `isAttachDisabled()` 加入 `recognizing` 互锁：录音中禁用 📎

## 文档同步

- `CLAUDE.md`：Phase 10a 段后增加 Phase 10b/10c 说明；HTTP endpoints 表 `/api/chat` body 增加 attachments
- `docs/ROADMAP-智能体感知与生成式UI演化-Phase7-10.md`：Phase 10b 增加"实际实施备注"，记录 100KB → 20KB 限额下调原因（qwen3-max 上下文窗口约束）
- `docs/ROADMAP-智能体感知与生成式UI演化.md`：尾部补 Phase 7-10 完成状态注

## 关键设计决策（不可改回）

1. **限额下调 100KB → 20KB**：原 roadmap 的 100KB × 5 = 50万字符 ≈ 15-20 万 token，超 qwen3-max 默认 32K 输入。下调到 20KB（软）+ 单文件硬 100KB + 总量硬 100KB，可控在 ~30k token 内
2. **注入顺序：user_input 在前，附件在后**：LLM 注意力对消息尾部更敏感，但短问题永远应放消息**首部**，避免被长附件淹没
3. **`_validate_attachments` 安全校验深度**：不仅查路径分隔符，还查 `..`、控制字符（0x00-0x1F, 0x7F）、Unicode 双向控制字符（`U+202A`-`U+202E`）；NUL 字节拒绝；mime 严格白名单（不用 `startswith("text/")`）
4. **`attachImages` 不重命名**：新建 `attachFiles` 并行函数，`dispatchSelectedFiles` 入口分流；改动半径小，命名语义清晰
5. **interim 写独立 DOM 节点**：不写入 textarea.value，避免覆盖用户在录音中手动编辑的内容
6. **`isAttachDisabled` 集中互锁**：录音中禁用 📎，避免文件选择对话框打断 SpeechRecognition.onend
