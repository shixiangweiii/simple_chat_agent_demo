# 03 — 消息类型与 Schema

## 3.1 v0.8 消息类型（稳定版）

v0.8 定义 5 种消息类型：

| 消息类型 | 方向 | 用途 |
|---|---|---|
| `beginRendering` | Agent → 客户端 | 通知客户端开始渲染 Surface |
| `surfaceUpdate` | Agent → 客户端 | 定义或更新 UI 组件结构 |
| `dataModelUpdate` | Agent → 客户端 | 更新应用状态/数据模型 |
| `deleteSurface` | Agent → 客户端 | 移除 Surface（清理） |
| `callFunction` | Agent → 客户端 | 调用客户端注册的函数 |

### beginRendering

```json
{
  "beginRendering": {
    "surfaceId": "booking"
  }
}
```

### surfaceUpdate

```json
{
  "surfaceUpdate": {
    "surfaceId": "booking",
    "root": "root",
    "components": [
      {
        "id": "root",
        "component": {
          "Column": {
            "children": { "explicitList": ["title", "date-picker", "submit-btn"] }
          }
        }
      },
      {
        "id": "title",
        "component": {
          "Text": {
            "text": { "literalString": "Book Your Table" },
            "usageHint": "h1"
          }
        }
      },
      {
        "id": "date-picker",
        "component": {
          "DateTimeInput": {
            "value": { "path": "/booking/date" },
            "enableDate": true,
            "enableTime": true
          }
        }
      },
      {
        "id": "submit-btn",
        "component": {
          "Button": {
            "child": "submit-text",
            "action": { "name": "confirm_booking" }
          }
        }
      },
      {
        "id": "submit-text",
        "component": {
          "Text": {
            "text": { "literalString": "确认预订" }
          }
        }
      }
    ]
  }
}
```

**v0.8 组件定义特点**：组件类型是嵌套对象（如 `"Text": { "text": ... }`），`children` 是 `{ "explicitList": [...] }` 结构。

### dataModelUpdate

```json
{
  "dataModelUpdate": {
    "surfaceId": "booking",
    "updates": [
      { "path": "/booking/date", "value": "2026-06-10T19:00" },
      { "path": "/booking/guests", "value": "2" }
    ]
  }
}
```

### deleteSurface

```json
{
  "deleteSurface": {
    "surfaceId": "booking"
  }
}
```

## 3.2 v0.9 消息类型（草案版）

v0.9 简化为 4 种消息类型，Surface 创建与渲染合并：

| 消息类型 | 方向 | 用途 |
|---|---|---|
| `createSurface` | Agent → 客户端 | 创建新 Surface 并指定组件目录 |
| `updateComponents` | Agent → 客户端 | 添加或更新组件 |
| `updateDataModel` | Agent → 客户端 | 更新数据模型 |
| `deleteSurface` | Agent → 客户端 | 删除 Surface |

### createSurface

```json
{
  "version": "v0.9",
  "createSurface": {
    "surfaceId": "booking",
    "catalogId": "restaurant-catalog"
  }
}
```

**v0.9 新增**：`catalogId` 引用外部组件目录，`version` 字段标识协议版本。

### updateComponents

```json
{
  "version": "v0.9",
  "updateComponents": {
    "surfaceId": "booking",
    "components": [
      {
        "id": "root",
        "component": "Column",
        "children": ["title", "date-picker", "submit-btn"]
      },
      {
        "id": "title",
        "component": "Text",
        "text": { "literalString": "Book Your Table" },
        "usageHint": "h1"
      },
      {
        "id": "date-picker",
        "component": "DateTimeInput",
        "value": { "path": "/booking/date" },
        "enableDate": true,
        "enableTime": true
      },
      {
        "id": "submit-btn",
        "component": "Button",
        "child": "submit-text",
        "action": { "name": "confirm_booking" }
      },
      {
        "id": "submit-text",
        "component": "Text",
        "text": { "literalString": "确认预订" }
      }
    ]
  }
}
```

**v0.9 关键简化**：
- 组件类型从嵌套对象变成字符串值（`"component": "Text"` vs `"Text": { ... }`）
- `children` 从 `{ "explicitList": [...] }` 变成普通数组
- 属性平铺到组件对象上（而非嵌套在类型对象内）

### updateDataModel

```json
{
  "version": "v0.9",
  "updateDataModel": {
    "surfaceId": "booking",
    "updates": [
      { "path": "/booking/date", "value": "2026-06-10T19:00" },
      { "path": "/booking/guests", "value": "2" }
    ]
  }
}
```

### deleteSurface

```json
{
  "version": "v0.9",
  "deleteSurface": {
    "surfaceId": "booking"
  }
}
```

## 3.3 v0.8 vs v0.9 Schema 对比

| 维度 | v0.8 | v0.9 |
|---|---|---|
| 版本字段 | 无 | `"version": "v0.9"` |
| Surface 创建 | `beginRendering` + `surfaceUpdate` | `createSurface` (合并) |
| 组件定义 | `"Text": { "text": {...} }` | `"component": "Text", "text": {...}` |
| 子元素引用 | `"children": { "explicitList": [...] }` | `"children": [...]` |
| 组件目录 | 内置 "Standard Components" | `catalogId` 引用外部目录 |
| 客户端函数 | `callFunction` | 移入 `ClientFunction` 组件属性 |
| 数据同步 | 单向 (Agent → 客户端) | 双向 (客户端交互同步回 Agent) |

## 3.4 客户端 → Agent 事件

用户交互事件从客户端发回 Agent：

```json
{
  "surfaceId": "booking",
  "componentId": "submit-btn",
  "event": {
    "name": "confirm_booking",
    "args": {
      "date": "2026-06-10T19:00",
      "guests": "2"
    }
  }
}
```

v0.9 增强了双向同步：DataModel 中绑定路径的用户输入会自动同步回 Agent，无需显式事件。

## 3.5 本项目对应

| A2UI 消息 | 本项目 SSE 事件 | 差异 |
|---|---|---|
| `createSurface` | `ui_surface_create {surface_id}` | A2UI 含 `catalogId`；本项目无 |
| `updateComponents` | `ui_surface_update {surface_id, components}` | 同构；A2UI 组件属性更丰富 |
| `updateDataModel` | `ui_data_update {surface_id, path, value}` | A2UI 支持批量更新；本项目单条 |
| `deleteSurface` | 无 | 本项目无 Surface 销毁机制 |
| `beginRendering` | 无 | v0.9 已移除（合并进 createSurface） |
| 客户端 → Agent 事件 | `POST /api/ui_action` | A2UI 有标准化事件 schema；本项目自定义 |
