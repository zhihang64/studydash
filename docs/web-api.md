# studydash Web API 契约

本文是 `studydash web` 后端与前端应用之间**唯一**的接口约定。两边都以本文为准；
改动接口必须先改本文。

- 后端：FastAPI（`src/studydash/web/`），随 `studydash web` 启动。
- 前端：Vue 3 + TypeScript + Vite（`webui/`），构建产物由后端托管。

---

## 1. 运行模型与安全边界

`studydash web` 在 **`127.0.0.1`** 上监听（不可改为局域网地址）。它的设计前提是：
**这台机器上的这个用户，就是账号的主人。** 因此安全模型是「本机 + 一次性令牌」，
而不是多租户鉴权。

| 机制 | 规则 |
| --- | --- |
| 监听地址 | 只监听回环。`--host` 只接受 `127.0.0.1` / `::1` / `localhost`，其它值直接拒绝启动 |
| 来源校验 | 每个请求都检查客户端 IP，非回环一律 `403 forbidden` |
| 令牌 | 每次启动生成新的 `secrets.token_urlsafe(24)`。只写在内存与发出的 `index.html` 里 |
| 令牌传递 | 所有 `/api/*` 请求必须带 `X-Studydash-Token: <token>`，缺失或不符返回 `401 unauthorized` |
| CORS | **完全不开放**。不返回任何 `Access-Control-Allow-*` |
| Cookie | 不使用 Cookie 会话，因此**没有 CSRF 面** |
| 令牌注入 | `GET /` 返回的 `index.html` 里注入 `<script>window.__STUDYDASH__ = {"token": "...", "version": "...", "basePath": ""}</script>` |
| 日志 | 请求日志只记方法、路径、状态、耗时、request-id。**请求体永不入库**（密码只存在于这一次请求的内存里） |

前端从 `window.__STUDYDASH__` 读令牌，塞进 `fetch` 的默认头。

### 静态资源与前端路由

- 后端托管前端构建产物（`src/studydash/web/static/`）。
- 命中真实文件 → 直接返回。
- 未命中且不是 `/api/*` → 返回 `index.html`（SPA history 回退）。
- 未命中且是 `/api/*` → `404 not_found`。

> 令牌校验发生在路由之前，所以**没带令牌**访问一个不存在的 `/api/x` 得到的是
> `401`，不是 `404`。这是有意的：不向未授权的调用者透露哪些接口存在。

---

## 2. 统一响应约定

**成功**：直接返回业务对象（`200` / `202` / `204`）。

**失败**：HTTP 状态码 + 固定错误信封：

```json
{
  "error": {
    "code": "auth_error",
    "message": "这个会话在超星上已经失效了",
    "hint": "重新用 `studydash login` 或网页上的扫码登录取一次会话",
    "detail": {}
  }
}
```

| `code` | HTTP | 含义 |
| --- | --- | --- |
| `unauthorized` | 401 | 令牌缺失或错误 |
| `forbidden` | 403 | 来源不是本机 |
| `invalid_request` | 400 | 参数不合法（`detail.field` 指出哪个字段） |
| `not_found` | 404 | 资源不存在 |
| `auth_error` | 401 | 平台侧会话失效 |
| `captcha_required` | 409 | 平台要求验证码 → 界面应引导改用扫码 |
| `two_factor_required` | 409 | 平台要求二次验证 → 界面应引导改用扫码 |
| `provider_error` | 502 | 平台侧失败（`hint` 里放诊断文本） |
| `busy` | 409 | 同类任务正在跑（`detail.task_id` 指出是哪一个） |
| `conflict` | 409 | 状态冲突，例如账号名已存在且要求唯一 |
| `internal` | 500 | 未预期错误（`detail.request_id` 便于对日志） |

所有响应都带 `X-Request-Id`。

---

## 3. 任务（Task）：一切耗时操作都是任务

登录、抓取、探测都会打网络、都可能要几十秒，**不用同步请求**。统一走任务模型：
`POST` 建任务 → `202 + task_id` → 轮询 `GET` 或订阅 SSE。

### 3.1 建任务

```http
POST /api/tasks
Content-Type: application/json
X-Studydash-Token: <token>
```

```json
{ "kind": "login_qr", "options": { "timeout": 240 } }
```

`kind` 取值与 `options`：

| `kind` | `options` | 说明 |
| --- | --- | --- |
| `login_qr` | `platform`, `timeout`（秒，默认 240） | 出该平台自己的二维码（超星 / 中国大学 MOOC 是两套协议），等手机扫码。头歌没有可复用的扫码协议 → `invalid_request` |
| `login_password` | `username`, `password`, `platform`, `label` | 账号密码登录（目前只有超星）。**密码不落盘、不进日志、不出现在任务快照里**。中国大学 MOOC 与头歌不支持 → `invalid_request` |
| `login_cookie` | `cookie`, `platform`, `label`, `verify`（默认 true） | 手动粘贴 Cookie |
| `login_browser` | `browser`, `profile`, `platform`, `label` | 读本机浏览器配置档（要求该浏览器已完全退出） |
| `login_live` | `browser`, `timeout`, `platform`, `label` | 起一个隔离配置档的浏览器窗口 |
| `fetch` | `only`（平台 id 列表，可空＝全部启用的账号） | 抓取学业数据 |
| `probe` | `platform` | 只读探测该平台的接口，留档原始响应 |

响应 `202`：

```json
{ "task": { "id": "t_9f3c1a", "kind": "login_qr", "state": "pending" } }
```

同类任务已在跑 → `409 busy`，`detail.task_id` 指向它。

### 3.2 任务对象

```json
{
  "id": "t_9f3c1a",
  "kind": "login_qr",
  "state": "running",
  "progress": 0.15,
  "message": "等你用手机确认",
  "created_at": "2026-10-19T14:03:11+08:00",
  "started_at": "2026-10-19T14:03:11+08:00",
  "ended_at": null,
  "cancel_requested": false,
  "logs": [
    { "ts": "2026-10-19T14:03:11+08:00", "level": "info", "text": "向超星要了一张登录二维码" }
  ],
  "artifacts": {
    "qr_data_url": "data:image/png;base64,iVBORw0KG...",
    "qr_expires_at": "2026-10-19T14:05:11+08:00"
  },
  "result": null,
  "error": null
}
```

- `state` ∈ `pending` | `running` | `done` | `failed` | `cancelled`
- `level` ∈ `debug` | `info` | `warn` | `error` | `ok`
- `artifacts` 按 `kind` 决定内容；`login_qr` 会给 `qr_data_url`（PNG 的 data URL，
  换码时**原地更新**，前端据此重画）。`login_live` 会给 `browser_url` 与 `profile_dir`。
- `error` 与第 2 节的错误信封**同构**（`code`/`message`/`hint`/`detail`）。
- `result`：
  - `login_*` → `{ "account": { "platform", "label", "uid", "cookie_count", "ok", "message" } }`
  - `fetch` → `{ "summary": { "courses": 2, "platforms": 2, "pending": 6, "overdue": 1, "avg_progress": 41.3 }, "problems": [ { "platform", "message" } ] }`
  - `probe` → `{ "traces": [ { "method", "url", "status", "bytes", "ms", "note" } ], "archive": "data/raw/xxx.json" }`

### 3.3 读任务

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/tasks` | 最近任务列表（最多 50，倒序）。**不含** `logs` 与 `artifacts` |
| `GET` | `/api/tasks/{id}` | 单个任务全量快照 |
| `POST` | `/api/tasks/{id}/cancel` | 请求取消；返回最新快照。已结束的任务返回 `409 conflict` |
| `GET` | `/api/tasks/{id}/events` | SSE 流，见下 |

### 3.4 SSE

`GET /api/tasks/{id}/events`（`text/event-stream`）依次推：

```
event: task
data: {"id":"t_9f3c1a","state":"running","progress":0.15,"message":"等你用手机确认",...}

event: log
data: {"ts":"...","level":"info","text":"已扫码，请在手机上确认"}

event: done
data: {"id":"t_9f3c1a","state":"done","result":{...}}
```

任务已结束才订阅 → 立刻推一条 `done` 然后关闭。`event: task` 的 `data` 是完整快照
（含 `artifacts`），所以前端只要处理一种消息就能把二维码画出来。流每 15 秒发一次
`: keep-alive` 注释行防中间层超时。

---

## 4. 系统与配置

| 方法 | 路径 | 返回 |
| --- | --- | --- |
| `GET` | `/api/health` | `{ "ok": true, "version": "3.0.0", "schema": 2, "uptime_s": 12.3 }` |
| `GET` | `/api/version` | `{ "version", "schema", "python", "platform", "frozen" }` |
| `GET` | `/api/platforms` | `{ "platforms": [ProviderMeta...], "sources": ["qr","password","live","browser","file","manual"] }` |
| `GET` | `/api/doctor` | `{ "checks": [ { "name", "level": "ok"|"warn"|"fail", "detail", "fix" } ], "failed": 0 }` |
| `GET` | `/api/settings` | `{ "config_path", "home", "http": { "timeout", "delay", "retries", "user_agent" }, "ui": { "theme", "alert_days" } }` |
| `PATCH` | `/api/settings` | 局部更新上面 `http` / `ui` 的字段，返回更新后的同构对象 |

`ProviderMeta` 字段：`id`、`name`、`short`、`status`（`stable`/`beta`/`unverified`/`broken`）、
`status_label`、`color`、`auth`、`login`（后端是否实现了该平台的登录流程；`false` 时前端
不应把它放进登录平台下拉框）、`homepage`、`verified`、`note`。

## 5. 账号

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/accounts` | `{ "accounts": [ { "platform", "label", "enabled", "ok", "message", "fetched_at", "course_count", "cookie_hint", "options" } ] }` |
| `POST` | `/api/accounts` | 加一个账号条目 `{platform, label, enabled, cookie?, options?}` → `201` + 全量列表。`cookie` 可以一起带上（页面「粘贴 Cookie」走的仍是 `login_cookie` 任务，这个字段是给脚本/API 用的）；**注意 pydantic 会丢掉模型里没声明的键，所以这个字段必须显式存在，否则会静默吞掉 Cookie** |
| `PATCH` | `/api/accounts/{platform}/{label}` | 改 `enabled` / `label` 重命名 → `200` + 全量列表 |
| `DELETE` | `/api/accounts/{platform}/{label}` | 删除该账号与它存的 Cookie → `204` |

`cookie_hint` 形如 `"<已设置 412 字符>"`，**绝不返回 Cookie 值**。`label` 需 URL 编码。

## 6. 学业数据

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/payload` | 最近一次抓取的前端契约全文；没有则 `404 not_found`。**已排除隐藏课程** |
| `POST` | `/api/payload/demo` | 用内置演示数据生成一份 payload 并落盘，返回 `{ "summary": {...} }` |
| `GET` | `/api/payload/meta` | `{ "generated_at", "mode", "warnings", "overview", "hidden_count" }`（轻量，用于首屏） |

`payload` 的结构见 `docs/payload.example.json`（`meta` / `overview` / `platforms` /
`timeline` / `courses` / `hidden` / `accounts` 七个顶层键）。后端写入的是同一份契约，
前端直接渲染。

### 6.1 隐藏课程

「结课了」或「这学期不想再看到」的课程可以从视图里摘掉。隐藏只影响**读取**：
`data/progress.json` 始终是完整的一份，所以取消隐藏不需要重新抓取。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/hidden` | `{ "hidden": [课程卡...], "count": 1 }` |
| `POST` | `/api/hidden` | body `{ "key": "平台:课程id:学期id" }`，返回同上。未知 key → `404 not_found`；重复隐藏幂等 |
| `DELETE` | `/api/hidden/{key}` | 取消隐藏，返回同上。key 需 URL 编码；没藏过也返回 `200`（幂等） |

课程键由后端生成，课程卡上带 `key` 字段（`平台:课程id:学期id`）。第三段是学期，
所以同一门课的不同学期互不影响——只按 `platform + id` 隐藏会把高数上学期一起藏掉。

`GET /api/payload` 返回的 `courses` / `timeline` / `overview` / `platforms` 都已排除
隐藏课程；被隐藏的课程卡放在 `hidden` 数组里（不带作业明细），`meta.hidden_count`
是数量。隐藏列表存在 `config.json` 的 `ui.hidden_courses`。

---

## 7. 前端必须处理的状态

1. `window.__STUDYDASH__` 不存在（例如直接双击 `index.html` 打开）→ 显示一整屏说明：
   「这个页面要由 `studydash web` 提供，请从命令行启动后访问提示的地址」，不要白屏。
2. `401 unauthorized` → 提示服务已重启（令牌已换），引导刷新页面。
3. 任务 `failed` → 把 `error.message` 与 `error.hint` 原样展示。诊断文本是给用户看的，
   不要吞掉。
4. 平台 `status != stable` → 在平台卡上显示对应徽标（`beta` → 「试运行」，
   `unverified` → 「未验证」，`broken` → 「暂不可用」），并说明原因。
5. 没有任何课程数据 → 引导去登录或先看演示数据，不要显示成「进度 0%」。
