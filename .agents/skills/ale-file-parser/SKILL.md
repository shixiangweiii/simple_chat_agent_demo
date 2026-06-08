---
name: ale-file-parser
version: 0.13.0
description: 文档解析专家。当用户需要解析或转换 PDF、图片、Word、PPT 等文档,提取文本、表格、公式、图片等结构化内容,或将文档转为 AI 友好的 Markdown 格式时激活此 skill。支持 PDF(.pdf)、图片(.png/.jpg/.jpeg/.webp)、Word(.doc/.docx)、PPT(.ppt/.pptx);核心能力:高精度还原表格为标准 Markdown、识别并保留数学公式(LaTeX)、自动提取标题与正文结构,输出适合知识库构建、RAG 检索、Agent 分析等场景。典型触发语句:「把这份 PDF 转成带表格的 Markdown」「帮我提取 PPT 里的公式和列表」「解析这个 Word 文档」。
---

# 文档解析专家

通过 `ale parse` 一条命令完成文档解析。CLI 内部已自动处理:`a1`/`aone-kit` 探测与登录校验、文件存在性与 150MB 上限、OSS 直传、任务提交、结果轮询(每 3 秒一次,默认 600s 超时)、URL 与本地路径分类。Agent 不需要替 CLI 重复这些检查,**以最终 stdout/stderr 为准**。

## 硬性约束

1. **只用 `ale parse`**:禁止用视觉识别、OCR、其他服务、`aone-kit call-tool ak47-ale-main::*`、MCP 客户端等任何替代方式。
2. **失败即停止**:CLI 报错后向用户原样反馈错误信息,不要自行兜底、伪造结果或换方式重试。
3. **不支持的类型直接拒绝**:仅支持 PDF / 图片(.png/.jpg/.jpeg/.webp) / Word(.doc/.docx) / PPT(.ppt/.pptx)。Excel、txt、压缩包等不支持,不要伪装后继续。
4. **完整返回内容**:除非用户明确要求摘要,否则不省略解析结果;大结果优先用 `-o` 写文件,只回摘要或文件路径。

## 前置:确保依赖已就绪

执行 `ale parse` 前先验证;CLI 启动会复检并报错,但提前确认能给出更友好的引导。

### 1. `a1` / `aone-kit`

```bash
a1 --version          # 验证安装
a1 auth whoami        # 验证登录
```

未安装:

| 系统 | 安装命令 |
|------|----------|
| macOS / Linux | `curl -fsSL https://git.cn-hangzhou.oss-cdn.aliyun-inc.com/aone-cli/install.sh \| sh` |
| Windows (PowerShell) | `irm https://git.cn-hangzhou.oss-cdn.aliyun-inc.com/aone-cli/install.ps1 \| iex` |

升级:`a1 update`。未登录:`a1 auth login --buc`(BUC SSO,推荐)。

### 2. `ale` CLI

```bash
ale version
```

未安装:

| 系统 | 安装命令 |
|------|----------|
| macOS / Linux | `curl -sSL https://cli-hub.alibaba-inc.com/ale/install.sh \| sh` |
| Windows (PowerShell) | `irm https://cli-hub.alibaba-inc.com/ale/install.ps1 \| iex` |

升级:`ale update`。安装后如未进入 PATH,提示用户重新打开终端或 `source` 对应 shell 配置。

## 命令用法

```bash
ale parse <source> [source...] [--json] [-o <output_path>]
```

- `<source>`:本地文件路径(相对或绝对均可,建议绝对路径避免歧义),可重复传多个
- 默认输出渲染好的 **Markdown** 到 stdout
- `--json`:输出含分页、blocks 的完整 **JSON** 结构,字段级抽取(表格/公式/图片块)时使用
- `-o <path>`:把结果写入文件,避免大结果污染对话上下文

### 示例

```bash
# 单文件,默认 Markdown
ale parse /Users/me/docs/report.pdf

# 取结构化字段(表格/公式),写入文件
ale parse /path/file.pdf --json -o /tmp/file.json
```

### JSON 输出关键字段

`--json` 模式下,从 `parse_result` 中按需筛选:

- 全文:`markdown`
- 表格:`pages[n].blocks` 中 `type == "table"`
- 公式:`type == "formula"` 的 `latex`
- 正文:`is_body == true`,排除 `header` / `footer` / `page_number`
- 图片/图表块:`type == "image"`

## 错误处理

CLI 非零退出时,把 stderr 原样反馈用户并停止。常见情况:

| 错误 | 用户提示 |
|------|----------|
| `a1` / `aone-kit` 未安装或未登录 | 给出上方安装命令,或执行 `a1 auth login --buc` |
| `ale` CLI 未安装 | 给出上方 ale 安装命令 |
| 文件类型不支持 | 告知支持范围(PDF / 图片 / Word / PPT) |
| 文件不存在 / 超 150MB / 上传失败 / 轮询超时 | 复述 CLI 错误,提示用户排查 |
