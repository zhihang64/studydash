# 交接文档（studydash）

> 写给接手的下一个对话/下一个人。目标：读完这份，不用回溯历史就能继续干活，
> 并且**不会重踩已经踩过的坑**。
>
> 最后更新：v3.0.0 + 超星作业/待办接入 + MOOC 数据接入 + 隐藏课程 + 头歌接入
> （工作区未提交），2026-10-20。

---

## 1. 这个项目是什么

**studydash**：本机运行的多平台学习进度聚合看板。把你在超星学习通 / 中国大学 MOOC /
智慧树上的课程、进度、作业截止时间汇总成一个页面，一眼看清哪门课落后、什么要到期。

**它不是**：不是刷课工具、不是代刷/自动答题/自动提交工具。它只发 GET（少数查询型 POST
打白名单地址），不发任何写操作。这条边界写在 `src/studydash/http.py:43`
（`_FORBIDDEN_IN_POST_ENDPOINT`：命中 `submit|save|answer|commit|upload|create|delete|
update|modify|doWork|doHomeWork|finish|report|sign|checkin|submitWork` 直接拒），
也写在 README 和每个页面的页脚。**接手后不要把这条边界松掉**，松掉整个项目的性质就变了。

- 仓库：`https://github.com/zhihang64/studydash`（public）
- 本地路径：`D:\DSH chat\studydash`
- 版本：`3.0.0`（唯一源头 `src/studydash/__init__.py:15`，CI/README/exe 都从这读）
- 已打 tag：`v1.0.0` `v2.0.0` `v2.1.0` `v2.2.0`。**v3.0.0 还没打**（原因见 §7）

### 技术栈与刻意的取舍

| 层 | 选型 | 为什么 |
| --- | --- | --- |
| 后端 | Python 3.10+，**运行时零第三方依赖** | 单 exe 分发、无原生依赖、少被杀软误杀 |
| 网页后端 | FastAPI + uvicorn（**可选**组 `studydash[web]`） | 命令行仍然零依赖；web 是可选增强 |
| 前端 | Vue 3 + TS + Vite + vue-router + pinia（**用 npm，机器上没 pnpm**） | 构建产物进仓库，用户不用装 Node |
| 无 lxml / requests | 自写 `htmlx.py` / `http.py` | 同上，零依赖 |

---

## 2. 现在真实能做什么（别信 CHANGELOG，信这一节）

| 能力 | 状态 | 验证方式 |
| --- | --- | --- |
| 超星：登录（扫码 / 密码 / 粘 Cookie / 读浏览器） | ✅ 真账号可用 | 手工，四个页签都跑过 |
| 超星：课程列表 | ✅ 22 门课，课名+教师全对 | `data/_raw_list.json` 真实 126612B |
| 超星：课程进度（任务点 X/Y） | ✅ 22 门课，平均 55.3% | 真账号 9 秒跑完，见 §4.2 |
| 超星：章节卡片 | 🟡 实验性（`options.chapters` 默认关） | — |
| 超星：**作业 / 待办列表** | ✅ 21 条跨 9 门课，4 项未提交 | 真账号 2026-10-19，见 §4.2.1 |
| 中国大学 MOOC：登录 | ✅ 扫码协议实测 2026-10-19 | 取码/轮询/自检对真站跑通；扫码确认需手机 |
| 中国大学 MOOC：课程/作业数据 | ✅ 12 门课 / 147 条作业，**beta** | 真账号 2026-10-20，见 §4.2.3 |
| 头歌：登录 | ✅ Cookie 自检 / 读浏览器 / 独立窗口 | 真账号 2026-10-20（无扫码、无账号密码） |
| 头歌：课程 + 实训 + 图文作业 | ✅ 9 门课 / 154 条任务（实训 96 + 图文 58），**beta** | 真账号 2026-10-20，见 §4.2.4 |
| 智慧树 | 🟡 标 `unverified`，从未用真账号跑通 | — |
| 手动导入 / 内置样例 | ✅ stable | `manual` / `demo` 适配器 |
| 隐藏课程（二级页「已隐藏课程」） | ✅ | 真数据工作区实测 34→33 门，见 §4.8 |
| 网页版六个页面（含二级页） | ✅ | headless Edge 截图逐页看过 |
| 命令行全部子命令 | ✅ | 555 例测试 |

**端到端真实验证过的平台是超星、中国大学 MOOC 和头歌**（后两个标 `beta`，不是
`stable`：MOOC 的两个 RPC、头歌的三个接口都只用过一个真账号验过）。智慧树的
数据适配器仍是按接口形状写的，界面上标了「未验证」徽标——保持这个诚实标注。
**别再按“未验证过 = 没做”去读旧文档**，`icourse163` 和 `educoder` 的课程/任务
已经能出真数据了。

---

## 3. 代码地图

```
src/studydash/
├── __init__.py            版本号唯一源头
├── cli.py                 所有子命令（init/demo/fetch/build/all/open/web/serve/
│                          import/probe/platforms/config/doctor/version）
├── model.py               Assignment/Course/Account/Progress + classify/sort；SCHEMA_VERSION=2
├── store.py               Workspace（数据目录、配置文件）、Config；写配置前 _scrub 脱敏
├── http.py                全项目唯一网络出口；只读约束在这里（L43）
├── htmlx.py               自写 HTML 解析，替代 lxml（iter_nodes 不含自身，易错）
├── aggregate.py           把各平台抓取结果合成看板 payload；filter_payload 做隐藏过滤
├── render.py + templates/dashboard.html   静态看板（兜底，v3 起不是主界面）
├── login.py               Cookie 归一化、六种来源、登录态判定（L47 LOGIN_CHECK）
├── browsers.py            Chromium 系 Cookie 库直读（DPAPI + AES-GCM）
├── cdp.py                 纯 stdlib DevTools 客户端（裸 socket 手写 WebSocket 帧）
├── passlogin.py           账号密码登录（AES-128-CBC）
├── qrlogin.py + qr.py + aes.py   超星扫码登录 + 自写 PNG 解码/二维码点阵还原
├── mooclogin.py           中国大学 MOOC 扫码登录（icourse163 自己的协议）
├── educoder_login.py      头歌会话：Cookie 自检 / 读浏览器 / 独立窗口登录
├── diagnostics.py         doctor，CLI 与 /api/doctor 共用
├── providers/
│   ├── base.py            REGISTRY/register、ProviderMeta、Provider 基类、AccountConfig
│   ├── chaoxing.py        ★ 主力适配器，进度解析全在这
│   ├── icourse163.py      ★ RPC 适配器，课程/进度/作业已真账号验证（beta）
│   ├── educoder.py        ★ 头歌：GET + PC 端签名，课程/实训/图文作业已验证（beta）
│   ├── zhihuishu.py       RPC 风格，未验证
│   ├── rpc.py             RPC 风格平台共用的基类/工具
│   ├── manual.py          手动导入
│   └── demo.py            内置样例数据
└── web/
    ├── app.py             中间件、令牌注入、SPA 回退、_safe_join、run()
    ├── security.py        回环判定、令牌、assert_bind_host
    ├── tasks.py           任务状态机（7 种任务、同 kind 互斥、日志上限 200、
    │                      快照永不输出 options——密码在里面）
    ├── operations.py      任务真正干的活
    ├── state.py           AppState
    ├── errors.py          错误信封 {error:{code,message,hint,detail}}
    ├── routers/           system / accounts / tasks / data / deps
    └── static/            ★ 前端构建产物，必须进仓库

web/                       前端源码（含二级页 HiddenView）
tests/                     555 例
tools/                     build_exe.ps1 / build_web.ps1 / make_icon.py / entry.py
docs/web-api.md            网页版 API 契约（冻结）
docs/plugin.md             写自己的适配器
```

### 超星适配器关键位置（改这块先看这里）

| 什么 | 位置 |
| --- | --- |
| 课程列表接口 | `providers/chaoxing.py:38` `EP_COURSE_LIST` |
| 课程页接口 | `providers/chaoxing.py:42` `EP_COURSE_PAGE` |
| 课程列表解析（嵌套 ul/li 坑） | `:448` `_parse_html_course_list` |
| 进度总入口 | `:582` `_fill_progress` |
| 拉单门课页面 | `:613` `_course_page_info` |
| **解析任务点 / 章节节点** | `:630` `_parse_course_page`（docstring 里有真实 HTML 结构） |
| 章节卡片 | `:907` `_chapters_of` |
| 进度表头正则 | `:130` `RE_HEAD_TASK` |
| **作业列表端点** | `:54` `EP_WORK_LIST`（手机版 `work/stu-work`） |
| **作业挂载入口** | `:693` `_fill_works` |
| **解析作业列表** | `:781` `_parse_work_list`（docstring 里有真实 HTML 结构） |
| **解析作业详情（截止时间/分数）** | `:848` `_parse_work_detail` |

---

## 4. 关键事实与踩过的坑（**最值钱的一节**）

### 4.1 超星登录态怎么判（错误判据会毁掉一切）

- `https://i.chaoxing.com/base/settings` **对完全有效的会话也返回登录页**
  （200 / 33302B / `<title>用户登录</title>`）。曾拿它当自检端点，导致四条登录路
  全部误报「会话不管用」。**别再拿它判登录。**
- 干净判据：`GET https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courses/list?rss=1&start=0&size=500&catalogId=0&searchname=`
  - 有效 Cookie → 200 / ~126612B / 有 `id="courseList"` / 52 个 `li`
  - 无 Cookie → 200 / ~33990B / 登录页 / 无 courseList
  - 伪造 `_uid=1; _d=deadbeefdeadbeef` → 与无 Cookie 完全一致
- 判据落在 `login.py:57` `RE_COURSE_LIST`。教训：**自检端点的唯一选择标准是
  「登录与否响应不同」，不是「名字像需要登录」。改之前先用真 Cookie + 伪造 Cookie
  各打一遍做对照。**

### 4.2 超星进度：数据在课程页的静态 HTML 里

- 课程列表页**不带**进度和作业。进度在
  `EP_COURSE_PAGE`（`/mooc2-ans/mycourse/studentcourse?courseid=…&clazzid=…&cpi=…`）
  的**服务端直出 HTML** 里。CDP 渲染确认过：该页零 AJAX，章节树就在首屏 HTML。
- 页面有**两种变体**：
  - **变体 A（新版，有表头）**：`已完成任务点: <span style="color:#00B368">57</span>/1442`
    → 能算百分比。数字被 `<span>` 包住，正则**两侧都要允许穿标签**（`RE_HEAD_TASK`）。
  - **变体 B（旧版，整页没有「已完成任务点」字样）**：只有每个节点的
    `<span class="catalog_points_yi">2</span>` = 该节点剩余任务点。→ **算不出**
    done/total，只能报「还剩 N 个任务点」。不伪造百分比。
- ⚠️ **`knowledgeJobCount` 的 value 是该节点的「待完成数」，不是总任务点数**
  （实测 sum 与 pending 合计完全相等：1385==1385、1305==1305、14==14）。
  想用它推 done/total 是死路，**别再试**。
- 交叉验证公式：`total − Σ(节点 pending) == header done`（1442−1385==57，1366−1305==61）。
  改动解析后必须重跑这个校验。
- 真实数字（2026-10-19，22 门课）：平均 55.3%；计算机网络 100/100、马原 25/25、
  工科大学物理 123/123 已满分；大数据统计分析基础 0/77、石文化 0/98、算法设计 0/34；
  形势与政策 `pending=14`、大学英语III 精读+听说 `pending=31`（变体 B）。
- `knowledgeid` 这个参数在 2026-10 的课程页里**出现 0 次**。旧实现靠扫它拿章节，
  所以章节数一直恒为 0（已修，`_chapters_of` 改为复用 `_parse_course_page` 的 nodes）。

### 4.2.1 超星作业 / 待办：手机版 `work/stu-work` 是唯一入口

- **能用的端点只有一个**：
  `GET https://mooc1.chaoxing.com/mooc-ans/work/stu-work?ut=s`
  - 不带任何课程参数，一次返回**全账号**的作业（2026-10-19 实测 21 条 / 9 门课）。
    带 `courseId`/`classId` 的响应与不带完全相同，参数会被忽略。
  - 页面是服务端直出 HTML，不需要 CDP、不需要浏览器。
- **已知死路，别再试**：
  - `https://mooc1.chaoxing.com/mooc2/work/list`（课程页里「作业」页签的
    `data-url`）→ 200 但只有 1.1KB 的「提示」页；
  - `mooc-ans/work/getAllWork?...` → 886B「温馨提示 …[3004]」；
  - `mooc2/work/index` → 404。
- **列表结构**（`_parse_work_list` 的 docstring 里有完整样例）：
  - 每条是 `<li onclick="goTask(this);" data="...">`，`data` 里带
    `taskrefId` / `courseId` / `clazzId` / `type=work` / `enc_task`；
  - 标题在 `<p aria-hidden="true">`；
  - **状态有两种形状**：未提交项是 `<span class="status">未提交</span>`，
    已完成/待批阅没有 `class="status"`，直接写
    `<span aria-hidden="true">已完成</span>`。只认一种会漏掉一大半；
  - 未提交项才有「剩余995小时51分钟」，已完成/待批阅**没有**剩余时间；
  - 课程名被截断成「《大数据统计分...》」，**必须按 `courseId` 回查课程列表**，
    别把显示文本当课名。
- **绝对截止时间在详情页**：列表项的 `data` URL 会 302 到
  `mooc-ans/work/phone/task-work?...`，页里有
  `截止时间：11-30 10:04`（可见）和 `aria-label="截止时间11月30日10时04分46秒"`
  （带秒）。**没有年份**——用列表里的「剩余 X 小时」当锚点，在
  `due.year-1/0/+1` 里挑最近的，跨年才不会把明年 1 月算成今年已逾期。
- **只给未提交项发详情请求**：已提交/已完成的没有截止时间需求，
  `classify_all` 会按状态直接归到 `done`。详情请求数由
  `options.max_work_details` 封顶（默认 60）；拿不到详情时退回
  `now + 剩余时间`，不编日期。
- 真实数字（2026-10-19）：21 条 = 4 未提交（全在「大数据统计分析基础」，
  截止 2026-11-30 10:00–10:04）+ 9 待批阅 + 8 已完成。接入后看板
  「待办 4 项 / 已完成 17 项」。
- ⚠️ **副作用**：4 门没有任务点数据、作业又全部完成的课（大学英语IV 等）会走
  `Course.ratio` 的「作业完成率」兜底，显示 100%，于是平均进度从 55.3% 变成
  67.2%。这不是新 bug，是 `model.py` 本来就有的兜底；变体 B 课（有 pending
  任务点、没有作业）仍然没有百分比，不受影响。

### 4.2.2 中国大学 MOOC 扫码：跟超星完全是两套协议

- 之前网页版选 `icourse163` 仍然弹超星的码，MOOC App 根本扫不了——因为
  `operations.py` 的五个登录操作全部硬编码调超星。现在按 `platform` 分流到
  `mooclogin.py`。
- 真站抓到的流程（2026-10-19，`https://www.icourse163.org/member/login.htm`）：
  1. `GET /logonByQRCode/code.do?width=182&height=182` → **JSON**（不是图片）：
     `{"code":0,"result":{"codeUrl":"http://mooc-image.nosdn.127.net/<key>-qrcode-logon.png","pollKey":"<key>"}}`
     `codeUrl` 是 `http://`，在 https 页面/请求里要换成 `https://`。
  2. `GET /logonByQRCode/poll.do?pollKey=<key>` 每 2 秒一次 →
     `{"code":0,"result":{"codeStatus":0,"token":null}}`
     状态：0 未扫 / 1 已扫待确认 / 2 已确认 / 3 已过期 / 4 别的 App 扫的。
  3. `codeStatus=2` 且 `token` 非空 → `GET /passport/logingate/mocMobChangeCookie.htm?token=…&returnUrl=<base64>`，
     响应会 302 回 returnUrl，会话 Cookie 在跳转过程中下发。
  4. **必须再问一次用户信息**：`POST /web/j/mocMemberFollowBean.userInfo.rpc?csrfKey=<NTESSTUDYSI>`
     （body 也带 `csrfKey`）。未登录返回 `{"code":0,"result":null}`，登录后 `result`
     是用户对象。`NTESSTUDYSI` 在未登录的登录页上**也会下发**，拿「有 Cookie」当
     登录判据会把游客当成已登录。
- 死路：这些 RPC 用 GET 一律返回 `{"code":-1010,"message":"Get Method Not Support"}`；
  `mocUserBean.getUserInfo.rpc` 连类名都不对（`code:-1003`）。
- 只读边界：用户信息 RPC 是查询，已显式列进该客户端的 `query_endpoints` 白名单，
  没有放开其它 POST。
- 未做：MOOC 账号密码登录（网易 URS，会碰验证码/二次验证）。网页版选 MOOC 时
  已隐藏该页签，后端也会明确拒绝。
- **平台登录能力是显式声明**：`ProviderMeta.login`（`providers/base.py`）只有
  `chaoxing` / `icourse163` 是 `True`。网页版下拉框按它过滤，后端
  `_require_login_support` 与 CLI 都会拒绝没有实现的平台；智慧树这类平台仍可用
  「粘贴 Cookie + 关闭验证」或 `--source manual --no-check` 导入。**别再改回
  「auth != none 就能登录」那种推断。**

### 4.2.3 中国大学 MOOC 数据：两个 RPC 就够（2026-10-20 真账号验证）

- 课程面板：`POST https://www.icourse163.org/web/j/learnerCourseRpcBean.getMyLearnedCoursePanelList.rpc`
  - form body 只认 **`{"type": 30}`**（学习中的课）。`type` 换成别的值会拿不到这批课，
    `pageIndex/pageSize` 之类的参数**根本不存在**（见下面的错误语义）。
  - 响应是 `result.result[]`（共 12 门：4 MOOC `mode=0` + 8 SPOC `mode=20`），
    每项带课名、`id`、`schoolPanel.name`、`termPanel.id`、`termPanel.lessonsCount`、
    `learnedCount`。
  - **`lessonsCount` 在 `termPanel` 里，不在课程面板顶层**；`learnedCount` 在面板顶层。
    进度条 = `Progress("已学小节", learnedCount, termPanel.lessonsCount)`，与网页显示
    的 93/96、114/115 对得上。
- 学期内容：`POST https://www.icourse163.org/web/j/courseBean.getMocTermDto.rpc`
  - form body `{"termId": <termPanel.id>, "isDraft": 0, "excludeUnrelease": 0}`。
    布尔值必须是数字 `0`/`1`，传字符串 `"false"` 会 `-1005 参数类型不对 :isDraft`。
  - `chapters[].homeworks[]` / `chapters[].quizs[]` 里各有一个 `test`
    `{id,type,name,deadline(毫秒),userScore,totalScore,usedTryCount}`；
    章节级 `exam` 和顶层 `exams[]` 也可能有，**按 id 去重**。
- `type` 映射：`1=quiz`、`2=exam`、`3=homework`（`_KIND_BY_TEST_TYPE`）。
  状态：`userScore` 非空（含 0）→ 已批阅；否则 `usedTryCount>0` → 已提交待批阅；
  否则待完成。`deadline` 是毫秒时间戳，不是秒。
- **CSRF**：`csrfKey` = `NTESSTUDYSI` Cookie 的值，**不是**名叫 `csrfKey` 的 Cookie。
  拼在 URL 查询串上（body 里也带一份也无妨）。缺它会 `-1002 非法跨域请求`。
- **错误语义是反向的，别被字面骗了**：
  - `-1007 不含有该参数 :X` = **这个方法没有名为 X 的参数**，不是「缺少 X」。
    当初把它读成「缺 pageIndex」，一直往 body 里补参数，越补越错。
  - `-1005 参数类型不对 :X` = 类型错（布尔要 `0`/`1`）。
  - `-1002 非法跨域请求` = csrfKey 缺失/不对。
  - `-2 尚未登录!` 或 message 里出现「登录」→ `AuthError`。
- 所有 RPC 用 GET 一律 `-1010 Get Method Not Support`；body 必须 form-encode，
  发 JSON 会被当成一对畸形表单键（`-1007 不含有该参数 :{"termId":...}`）。
- 课程键 `platform:courseId:termId` 里的第三段就是 `termPanel.id`——同一门课的不同
  学期必须分开，否则隐藏一个学期会把另一个也藏掉。
- 真实数字（2026-10-20）：12 门课 / 147 条作业（作业 + 测验 + 考试）。请求要带
  `HttpClient` 的 delay/retry，裸 `urllib` 连打会超时。

### 4.2.4 头歌（educoder）数据：GET + PC 端签名（2026-10-20 真账号验证）

网页是 React 单页应用，数据全在 `/api/*.json`。**每个请求都要带签名**，否则
拿不到数据：

- 头：`X-EDU-Type: pc`、`X-EDU-Timestamp`（毫秒）、`X-EDU-Signature`、
  `X-Request-Id`、`X-Original-Protocol/Host/Origin`。
- `X-EDU-Signature = md5(base64("method=GET&ak=<ak>&sk=<sk>&time=<毫秒>"))`。
  `ak`/`sk` 写在前端 umi bundle 里（模块 `527951`，字符串是**双重 base64**，
  `getKey = s => atob(atob(s))`）。现在的常量在 `providers/educoder.py` 的
  `SIGN_AK` / `SIGN_SK`；签名只跟方法有关、不带路径，但时间戳有有效期，每次现算。
- 少带签名/本机时间偏差大 → `{"status":-102,"message":"服务器时间与您的设备时间不匹配…"}`。
  **这句报错是幌子**，别去做时间同步，先看签名。
- `/api/users/get_user_info.json`：登录判据。游客也返回 **HTTP 200**，
  只是 `{"login":"","username":"游客"}`。
- `/api/courses/mine.json`：`data[]`。匿名/会话失效同样返回 `status:0` + 空数组，
  **不能拿它判登录**，否则「会话过期」会显示成「0 门课」。
- `/api/courses/{id}/homework_commons.json`：**靠 `type` 分两类**，
  `type=4` 实训/作业（`is_shixun:true`，`homeworks[]` 带 `shixun_finished_status`、
  `student_passed_time`、`challenge_count/finished_challenge_count`、
  `shixun_identifier`；顶层 `published_count`/`unpublished_count`/
  `finished_task_count`），`type=1` 图文作业（`is_shixun:false`，带
  `work_status`/`un_commit_work`/`student_work_id`）。
  **默认只回 `type=4`**，不传 `type=1` 就整门课的图文作业一条都看不到；
  `type` 是数字枚举（传 `"normal"`/`5` 会拿到 `-1 type参数有误`），`3` 在这些
  教室里是空列表。**默认每页 20 条**，要传 `per_page=200` 才一次取全。
- 实训完成状态看 `shixun_finished_status == 1` 或 `student_passed_time` 非空；
  `status:["提交中"]` 是「还能交」，不是「已交」。
- 图文作业完成状态只看 `un_commit_work is False`。`work_status` 在没交的作业上
  可能是空数组，`student_work_id` 没交也有值，两个都不能拿来判。
- 进度 = 两类合并 `(finished_task_count + 已交图文作业数) / (published_count 之和)`，
  任务粒度。`online_learning.json` 对实训型课程全是 0，别用。
- 任务链接用**教室里的作业页**：
  `/classrooms/{course_id}/shixun_homework/{homework_id}/detail?tabs=0`（实训）、
  `/classrooms/{course_id}/common_homework/{homework_id}/detail`（图文作业）。
  头歌 `task_operation` 给的 `/shixuns/{shixun_identifier}/shixun_exec` 是
  「进入实战」的**动作**地址，从看板直接跳过去会渲染成「404 您访问的页面已经
  离开地球」（2026-10-20 用无头 Edge 复现过），别用。
  课程 id 本身即一次开课，`extra["term_id"]` 留空。
- 登录：`/user/login` 是表单 + 验证码，没有可复用的扫码协议，也不替你输密码。
  `educoder_login.py` 只做三件事：Cookie 自检、读本机浏览器、起独立窗口等你登
  （`cdp` + 轮询 `_educoder_session`）。`meta.login=True` 是对的，但网页/命令行
  必须只暴露 `cookie` 和 `browser`（含独立窗口），别给 `qr`/`password`。

### 4.3 网页版安全模型（别擅自简化）

- 只监听回环（`security.py:72` `assert_bind_host` 拒绝非回环 host）。
- 逐请求校验来源 IP（`app.py:106` `client_host`）。测试里 `TestClient` 的
  `scope["client"]` 是 `"testclient"`，要 monkeypatch `app_mod.client_host` 才不被 403 挡。
- 每次启动生成一次性令牌，注入 `index.html` 的 `window.__STUDYDASH__`，
  请求带 `x-studydash-token`（`security.py:27`）。
- **不使用 Cookie 会话** → 没有 CSRF 面。关掉 `/docs`。
- 所有耗时操作都是任务（`login_*/fetch/probe` 等 7 种），同 kind 互斥，SSE 推日志。
  任务快照**永不输出 options**，因为密码在里面。

### 4.4 Chromium Cookie 直读

- 浏览器运行期间对 Cookies 库持**独占句柄**（share 三种模式全部
  `ERROR_SHARING_VIOLATION`）。代码绕不过去，只能提示用户关浏览器。
- 解密链：`Local State` 的 `os_crypt.encrypted_key` → base64 → 去 `DPAPI` 5 字节前缀
  → `CryptUnprotectData` 得 32 字节主密钥；`v10/v11` = AES-256-GCM
  （nonce=blob[3:15]，密文+tag=blob[15:]）。
- **Chrome/Edge ≥24 会在明文前拼 `sha256(host_key)` 32 字节**，必须按哈希比对后才剥，
  不能盲剥、不能靠版本号判。
- `v20`（app-bound）读不了，要明确报错而不是静默失败。

### 4.5 CDP 客户端（自写，纯 stdlib）

- `free_port()` 必须在 `PORT_RANGE=(24000,32000)` 里挑；系统临时端口段 49152+ 会被
  浏览器自己的出站连接抢走。
- `_http_get` **不能读到 EOF**（Chromium 不关连接），按 `Content-Length`/chunked 读满即返。
- `Network.getAllCookies` 是页面级命令；`Storage.getCookies` 失败后要
  `Target.attachToTarget` 拿 `sessionId`。
- 典型调用链：`cdp.launch(url)` → `launched.port` → `CdpSession(port).call(...)`
  → `Target.createTarget` → `Target.attachToTarget(flatten=True)` 取 `sessionId`
  → `Network.setCookies`（cookie 要包成 `{name,value,domain:".chaoxing.com",path:"/"}`）
  → `Page.navigate` → `Runtime.evaluate`。

### 4.6 踩过的产品级 bug（都已修，但同类错误容易再犯）

| 症状 | 根因 | 教训 |
| --- | --- | --- |
| 所有网页版任务都失败 `'Task' object has no attribute 'workspace'` | `TaskManager._run` 把 `Task` 当 `AppState` 递给 runner，且 `Runner` 类型别名也写成 `Task` | 两处类型一起写错时**静态检查会一路点头**，只能靠跑 |
| Cookie 过期被显示成「0 门课」，和真没选课一模一样 | `fetch` 把 `AuthError` 吞成 `return []` | 认证失败必须抛出去，由 `aggregate()` 收进 `Account(ok=False)` |
| 看板所有预警静默失效，页面看起来完全正常 | `build_payload` 没调 `classify_all`，时间线每条 `state` 停在 `"none"` | 「页面正常但数据全错」是最危险的一类 |
| `POST /api/accounts` 静默丢弃 Cookie | pydantic 丢掉未声明字段 | 请求模型字段要跟实际传的一一对齐 |
| `cmd_all` 崩在 `args.demo` | 共用 `cmd_*` 不能假设 args 来自自己的 parser | 一律走 `_flag(args,name)`/`_opt(args,name)` |
| 徽标被撑成整行、圆点跑到文字上方 | 徽标写 `class="badge pf"`，而 `.pf` 同时是平台卡组件类（含 `width:100%`） | **修饰类名不许和组件类重名** |
| `0/77` 的课顶「已全部完成」绿标、右边却是 0% | 徽标按「有没有数据」判完成 | 完成度必须按 `ratio >= 1` 判 |
| 「进度告急」标题说「最低的几门」实际是原始顺序 | `filter(has_data).slice(0,4)`，没排序没排除已完成 | 标题承诺的语义必须由代码兑现 |
| 「总进度 100%」和「任务点 100/100 100%」重复 | 单个进度项时总进度条冗余 | 只有一项时不要画汇总条 |
| 量纲混用显示成 6666.7% | 有的地方 0–1、有的地方 0–100 | `Course.ratio` 是 0–1，`Progress.done/total` 是计数，别混 |
| `pytest` 挂死 2 分钟 | `_report_crash` 的 `MessageBoxW` 在测试里弹窗 | 只在 `os.name=="nt" and sys.stderr is None` 且未设 `STUDYDASH_NO_GUI` 时弹 |
| 管道输出中文乱码 | Windows 上 stdout 走 GBK | `_setup_console()` 在 Windows 上一律 `reconfigure(encoding="utf-8")` |

### 4.7 PyInstaller / 发布

- 给了 `--specpath` 之后，`--icon` / `--add-data` / `--version-file` **全部按 spec 目录
  解析**，必须传绝对路径。
- ⚠️ 不要写 `--exclude-module distutils`：`hook-distutils` 会把它 alias 成
  `setuptools._distutils`，撞 ExcludedModule 抛 ValueError。
- 打包 job 必须装 `fastapi uvicorn`，否则 PyInstaller 收不到 web 模块——
  **exe 里没有网页版，而且只在运行时才暴露**。`tools/build_exe.ps1` 有依赖预检。
- GitHub Release 资产名不支持非 ASCII，中文会被吞 → 固定用
  `studydash.exe` / `studydash-onedir.zip`。
- CI 只在 **tag 或手动触发**时打包；推 main 只跑测试（`.github/workflows/build-exe.yml`）。
- 取 push token 的固定手法：
  `$env:GCM_INTERACTIVE='never'` 后 `git credential fill` 取 `password=`（`gho_` 前缀，
  账号 `zhihang64`）。
- **历史遗留未复现问题**：v1 的 onefile exe 双击「黑框一闪就消失」，本机复现不出
  （命令行跑正常、无 Defender 记录、无受控文件夹访问），推断国产杀软误杀 PyInstaller
  onefile。v2 起改 onedir 规避，但没有真机验证过。

### 4.8 隐藏课程：只读过滤，不删数据

- 用户诉求：结课/不再需要的课一直占位置。做法是从**视图**里摘掉，不是从数据里删。
- **`data/progress.json` 永远保存完整一份**（`build_payload` 的结果），隐藏只是
  `GET /api/payload` 读取时由 `aggregate.filter_payload(payload, hidden_keys)` 过滤。
  所以取消隐藏不需要重新抓取，也不会因为误藏而丢作业。
- 过滤范围必须**同时**覆盖 `courses` / `timeline` / `overview` / `platforms`：
  - 时间线按 `course_id` + `(platform, course_id)` 双条件剔除——只按 course_id 会在
    两个平台的课 id 撞车时误删别的平台的作业；
  - `overview` 与 `platforms` 的计数要**重算**，不能留着旧数字（否则总览说 34 门、
    列表却只有 33 门）；
  - 被隐藏的课程卡放进顶层 `hidden` 数组，并**剥掉作业明细**（隐藏页不显示作业，
    没必要再传一遍），`meta.hidden_count` 是数量。
- 课程键 **`platform:courseId:termId`**，第三段必须带学期（`termPanel.id`）：
  只按 `platform+id` 隐藏会把同一门课的其它学期一起藏掉。
  课卡上带 `key` 字段；老 `progress.json` 没有 `key` 时由 `_with_key` 现补。
- 存储位置是 `config.json` 的 `ui.hidden_courses`（`Store.hidden_courses()` /
  `set_hidden_courses()`，空列表时删键而不是写 `[]`）。
- 接口：`GET /api/hidden`、`POST /api/hidden {key}`（未知 key → 404，重复幂等）、
  `DELETE /api/hidden/{key}`（没藏过也 200，幂等）。前端二级页 `/hidden`
  （`web/src/views/HiddenView.vue`），不在导航栏里，从「课程」页标题旁的入口进。
- 真数据工作区实测：34→33 门、时间线 168→150 条，平台卡 icourse163 12→11、
  待办 2→1；取消隐藏恢复 34 门。

---

## 5. 本地开发环境（照抄即可）

```powershell
cd 'D:\DSH chat\studydash'
[Console]::OutputEncoding=[Text.Encoding]::UTF8
$env:PYTHONUTF8='1'
$env:PYTHONPATH="$PWD\src"      # 系统 python 没装 studydash，必须给
$env:STUDYDASH_NO_GUI='1'       # 否则崩溃上报会弹 MessageBox 卡住测试

python -m pytest -q             # 555 passed，约 6 秒
```

前端改动后**必须重建**，否则页面还是旧产物：

```powershell
cd 'D:\DSH chat\studydash\web'
npm run build                   # = vue-tsc --noEmit && vite build
# 产物落 src/studydash/web/static/，要一起提交
```

起服务看效果：

```powershell
cd 'D:\DSH chat\studydash'
python -m studydash web --port 8813        # 日志可重定向到 data\_web.out / _web.err
python -m studydash fetch                   # 抓一次真实数据
```

⚠️ **改完后端要重启服务**（改代码不会热载，旧进程会一直用旧逻辑）。

### 截图验证（本项目的主要视觉验收手段）

```powershell
$edge = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
if (-not (Test-Path $edge)) { $edge = 'C:\Program Files\Microsoft\Edge\Application\msedge.exe' }
$ud  = Join-Path $env:TEMP 'edge_shot_studydash'
$out = 'D:\DSH chat\studydash\data\shot.png'
& $edge --headless=new --disable-gpu --hide-scrollbars --user-data-dir=$ud `
  --window-size=1440,1200 --virtual-time-budget=9000 --screenshot=$out 'http://127.0.0.1:8813/courses'
```

要点：`--user-data-dir` / `--screenshot` 必须**先存变量**（内联 `(Join-Path ...)`
不展开）；路径必须绝对；SPA 路由可直接访问 `/`、`/courses`、`/accounts`；
令牌由服务端注入 `index.html`，不用手工带。

### 调试真实超星数据的惯用姿势

写一次性脚本放 `tools/_probe_*.py`，用 `Workspace.resolve()` +
`Config.load(ws.config_path)`（`config_path` 是 **property，不加括号**）拿账号，
再用 `studydash.aggregate.build(account, cfg.http)` 造 provider 打桩调用。
**用完删掉，不要提交。**

---

## 6. 测试

- 555 例，`tests/`。覆盖面：aes / aggregate / cdp / chaoxing / cli / educoder /
  htmlx / http / icourse163 / login / mooclogin / model / providers / qr / render /
  store / web。
- `tests/fixture_course_list.html` 是真实课程列表页的缩减版。
- 血泪教训：**写解析的测试夹具必须故意混合两种 HTML 结构**（有表头 / 无表头），
  否则「A 结构命中就再也不试 B」这类回退链 bug 测不出来。
- 界面 bug 大多**测不出来**——测试全绿时页面照样错（0% 顶绿标、告急不排序都是这时
  发现的）。视觉验收靠 §5 的截图。

---

## 7. 为什么 v3.0.0 还没发

用户明确指示（原话）：「exe 可执行文件可以先不编译，等完全可以执行之后再编译」。
所以 v3.0.0 代码已提交，tag 曾建后删除（出现过「有 tag 无 Release」），**现在 tag
不存在**。

发版 checklist：
1. `pytest` 全绿
2. `npm run build` 后 `git status` 干净（产物已提交）
3. 真账号整链跑通：登录 → `fetch` → 网页版六个页面（含 `/hidden`）都对
4. `CHANGELOG.md` 加版本节
5. 打 tag `v3.0.0` → CI 自动打包 → 建 Release，资产名 ASCII
6. 装 `studydash[web]` 的干净环境里验证 exe 里有网页版

---

## 8. 接下来该干什么（按优先级）

### ✅ TODO-1（已完成）超星作业 / 待办列表
2026-10-19 接入，细节见 §4.2.1。真实账号 21 条（4 未提交 / 9 待批阅 / 8 已完成），
网页版「待办」页已按截止时间分组，「课程」页每门课有作业明细。
**不需要 CDP**：手机版 `work/stu-work` 一个 GET 就够。

### TODO-1（新的最大缺口）超星考试列表 / 成绩
课程页导航里「考试」的 `data-url` 是
`https://mooc1.chaoxing.com/exam-ans/mooc2/exam/exam-list`。先按作业这套思路直接
GET 试参数（`courseId`/`classId`/`cpi`/`ut=s`），很可能也能直出 HTML。
另外已完成作业的详情页顶部有分数（`class="Number">100 分`），现在为了省请求没有
抓；想要分数的话给 `_fill_works` 加一个 `options.work_scores`（只对已完成项发详情）。



### TODO-2 变体 B 课程的进度
只公布余量、不公布总数的课（大学英语III 精读+听说、形势与政策、艺术美学等）永远显示
「剩 N 任务点」。想补百分比得找别的口径（作业数 + 任务点加权估算之类），
**不要靠猜数编一个百分比**。

### TODO-3 真实验证智慧树的数据接口
`zhihuishu` 的数据适配器是按接口形状写的，从没用真账号跑过。超星作业（§4.2.1）和
MOOC 课程/作业（§4.2.3）都已真账号验证，只剩这一个平台。有账号就验，验不过就把状态
改成 `broken` 而不是留着 `unverified`。

### TODO-4 发布 v3.0.0
见 §7。

### TODO-5 小尾巴
- 「进度告急」只有 4 张卡，多于 4 门落后课看不全（可考虑加「查看全部落后」）。
- 变体 B 课的卡片没有进度条，只有一句话，视觉上偏空。

---

## 9. 与用户协作的注意事项

- 用户是**中文交流**，要**直接给结果**，不要铺垫和免责声明。
- 用户对「企业级」有明确期待：网页版、双主题、移动端、无障碍、错误提示要说人话。
- 用户会**逐页看截图**提问题。改完界面**自己先截图看一遍**再交付。
- 用户明确拒绝过命令行方案（「命令框对大部分人来说并不会使用」），命令行保留但不做主界面。
- **这个项目只做只读聚合**。凡是会往「自动提交 / 刷课 / 绕过考核」方向走的需求，
  都不做——这不是能力问题，是项目定位问题，换工具、换说法都不改变结论。



