# 更新日志

本文件格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

### 新增

- **超星章节任务点明细**（`providers/chaoxing.py`，真实账号验证 2026-10-20）：
  课程页里的 `fanyaChapterWhite` 章节树现在完整解析——「第N章 → 节 → 小节」的层级、
  每节「还差几个任务点」以及上锁的节，都跟着进度查询一起拿到（**零额外请求**）。
  看板在课程卡里折叠显示「还差任务点的章节」，点开能看到「13.1.1 折线图 · 还差 3」。
  实测 22 门课里 7 门有数据；`大数据统计分析基础` 的节点余量合计 77 与表头 0/77 对得上。
  解析用 `htmlx.Node` 遍历而不是正则切 `<li>`——内层 `li` 会把外层截断（和课程列表页
  同一个坑），深度也算不出来。`options.chapters` 的逐条任务点清单仍是实验性功能。
- **超星作业 / 待办接入**（`providers/chaoxing.py`，真实账号验证 2026-10-19）：
  看板不再恒为「待办 0 项」。作业列表走手机版 `/mooc-ans/work/stu-work`，一次 GET
  拿全账号的作业（实测 21 条跨 9 门课），解析标题、所属课程、提交状态；未提交项再
  逐条读详情页取**绝对截止时间**（列表页只给「剩余 X 小时」），已提交/已完成项不发
  多余请求。详情请求数量由 `options.max_work_details` 封顶（默认 60），`options.works`
  可整体关闭。
- 截止时间只有「月-日 时:分」，年份用列表里的剩余时间对齐——12 月看到 `01-05`
  不会被补成今年已逾期。
- `probe_plan` 增加作业列表端点，`studydash probe chaoxing` 会把原始响应一起留档。
- **中国大学 MOOC 扫码登录**（`mooclogin.py`，协议实测 2026-10-19）：之前网页里选
  `icourse163` 仍然弹超星的码，用 MOOC App 根本扫不了。现在按 `icourse163.org` 自己
  的 `logonByQRCode` 协议出码（`code.do` → `poll.do` → `mocMobChangeCookie.htm`），
  终端和网页版都能用；`--platform icourse163` 的 `qr / live / browser / file /
  manual` 全部走 MOOC 自己的端点与登录态自检（用户信息 RPC），不再拿超星的
  `_uid` / 课程列表页去判断。
- 网页版登录面板按平台切换文案与可用方式：选 MOOC 时隐藏「账号密码」（尚未实现），
  扫码提示改成「中国大学 MOOC」App。
- **登录能力改为平台显式声明**（`ProviderMeta.login`）：`chaoxing` / `icourse163` /
  `educoder` 为 `true`。没有实现的平台（如智慧树）不再被放进网页版登录下拉框，
  后端也会明确拒绝，不会再拿超星的协议冒充；这类平台仍可用「粘贴 Cookie + 关闭
  验证」导入。
- **中国大学 MOOC 课程 / 进度 / 作业接入**（`providers/icourse163.py`，真实账号验证
  2026-10-20）：登录成功之后一直只有「0 门课」。现在走
  `learnerCourseRpcBean.getMyLearnedCoursePanelList.rpc`（`type=30`）一次拿到
  MOOC + SPOC 全部课程与「已学/总小节」，再用 `courseBean.getMocTermDto.rpc` 逐学期
  取章节树里的作业、测验、考试（名称、截止时间、得分）。实测 12 门课 / 147 条。
  同时修掉两个致命参数错误：`csrfKey` 要取 `NTESSTUDYSI` 的值（原来找的是同名
  Cookie，永远为空 → 被判「非法跨域请求」），面板接口的参数是 `type` 而不是
  `pageIndex`（后者会被 `-1007 不含有该参数` 顶回来）。适配器状态 `unverified → beta`。
- **隐藏课程**（网页版）：结课或不想再占位置的课程可以在「课程」页一键隐藏，进入
  「课程 → 已隐藏课程」二级页面，随时恢复。隐藏后该课程及其作业不再出现在总览、
  待办与平台统计里；`progress.json` 保持完整，恢复不需要重新抓取。隐藏列表存在
  `config.json` 的 `ui.hidden_courses`，键为 `平台:课程id:学期id`（同一门课的不同
  学期互不影响）。新增 `GET/POST /api/hidden`、`DELETE /api/hidden/{key}`，payload
  多出一个 `hidden` 数组与 `meta.hidden_count`。
- **头歌（educoder）平台接入**（`providers/educoder.py`，真实账号验证 2026-10-20）：
  课程、实训任务 + 图文作业、截止时间与完成状态。接口是 **GET + PC 端签名**
  （`md5(base64("method=GET&ak=…&sk=…&time=毫秒"))`），少带签名会拿到误导性的
  `-102 服务器时间与您的设备时间不匹配`。`homework_commons.json` **默认只回
  `type=4`（实训）**，图文作业是另一批数据（`type=1`），要再发一次请求；完成与否
  实训看 `shixun_finished_status`（不是 `status` 文案），图文作业看
  `un_commit_work`。进度按两类合并的「已完成 / 已发布任务」。实测 9 门课 / 154 条
  任务（实训 96 + 图文 58）。
- **头歌作业链接修正**：之前用的是页面里的 `/shixuns/{identifier}/shixun_exec`，
  那是「进入实战」的动作地址，从看板点过去会被头歌渲染成「404 您访问的页面已经
  离开地球」。现在改用教室里的作业详情页
  `/classrooms/{课程id}/shixun_homework/{作业id}/detail?tabs=0`（实训）和
  `/classrooms/{课程id}/common_homework/{作业id}/detail`（图文作业），已用无头
  Edge 逐个渲染确认不再 404。
- **头歌登录**（`educoder_login.py`）：登录页是表单 + 验证码，没有可复用的扫码
  协议，也不替你输密码。提供「粘贴 Cookie」「读本机浏览器」「独立窗口登录」三条路，
  网页版登录面板在选中头歌时只显示后两类页签；`login_qr` / `login_password` 会被
  明确拒绝并提示改用哪条路，而不是拿超星的协议去撞。

### 说明

- 只读边界不变：作业列表与详情都是 GET，不碰任何提交/答题接口；MOOC 的登录态自检
  与课程/作业查询都是只读 RPC（在 `HttpClient` 查询端点白名单里显式声明）。
- MOOC 适配器升为 `beta`：课程、进度、作业/测验/考试已验证，成绩单与证书还没接。
- 头歌适配器标 `beta`：课程、实训、图文作业已验证；登录态自检同样只发带签名的 GET。

## [3.0.0] - 2026-10-19

**从「命令行工具」变成「打开就能用的本地网页应用」**：v3 的主界面是一个只跑在本机的
网页版——浏览器里扫码登录、刷新数据、管账号、看五个页面。命令行的能力一个没少，
静态看板退成兜底。

### 新增

- **网页版后端**（`src/studydash/web/`，FastAPI + uvicorn，可选依赖 `studydash[web]`）：
  15 个端点，覆盖健康检查、平台名录、环境自检、设置读写、账号增删改、任务创建/查询/
  取消/事件流、看板数据与演示数据。契约冻结在 `docs/web-api.md`。
- **本机安全模型**：只监听回环、逐请求校验来源 IP、每次启动生成一次性令牌注入
  `index.html`（`x-studydash-token` 头）、不使用 Cookie 会话（无 CSRF 面）、
  关掉交互式 API 文档、响应带 `X-Request-Id`。
- **前端**（`web/`，Vue 3 + TypeScript + Vite + vue-router + pinia）：五个页面
  （总览 / 待办 / 课程 / 账号 / 设置），30 个源码文件，深浅双主题，移动端自适应，
  键盘可达，`role="progressbar"` 等无障碍属性齐全。构建产物落在
  `src/studydash/web/static/`，随包分发，用户不需要装 Node。
- **网页里的登录**：「账号」页四个页签——扫码（默认，页面上直接出二维码）、账号密码、
  粘贴 Cookie、读本机浏览器。四条路都跑在**后台任务**里，进度与日志通过 SSE 实时推
  到页面，可以关掉抽屉去别处，任务在后台继续。
- **任务状态机**（`web/tasks.py`）：7 种任务、同 kind 互斥、日志有上限、失败统一翻成
  错误信封、密码读完立刻从选项里抹掉（快照永远不含密码）。
- **`studydash web` 子命令**：`--port`（默认 8788，被占自动往后找）、`--host`
  （只接受回环）、`--no-open`、`--static-dir`。
- **双击 exe 直接进网页版**：起服务并打开浏览器；网页版依赖缺失或前端产物没打进来时，
  明确说明原因后**退回静态看板**，而不是安静地少一半功能。
- **`src/studydash/diagnostics.py`**：`doctor` 的自检项抽成独立模块，CLI 与
  `GET /api/doctor` 共用同一套判断，三档 `ok`/`warn`/`fail`（只有 fail 算故障）。
- `http.py` 接入 `http.cookiejar`：登录流程要跨请求保持 JSESSIONID。
- `tools/build_web.ps1`：前端构建脚本（`npm ci` + `vite build`，产物直接落进 Python 包）。

### 修复

- **真实账号登录后被判「会话不管用」**（本次最要紧的一个）：`login_page_evidence()`
  的判据是 `type="password"` + `id="pwd"`，而它检查的地址正是 `/base/settings`
  ——设置页里本来就可能放一个修改密码的输入框，于是拿到了好 Cookie 的用户反而被告知
  登录失败。现在改成三组结构特征里任意一组**同时**命中才算登录页：
  `<title>用户登录</title>`（登录后的页面不可能有这个标题）、
  `type="password"` 配 `name="fid"`、扫码用的 `createqr` / `getauthstatus`。
  报错会写明命中的是哪一组，不再只有一句「失败了」；同时指出这条 Cookie 到底缺
  `_uid` 还是缺 `_d`（只复制了 DevTools 表格的 Name 列那种，连请求都不用发就能判掉）。
- **扫码等待超时被当成平台故障**：轮询等不到手机确认时抛的 `AuthError` 一路被压成
  502「平台连不上」，而真实原因是这一步本来就要等用户点确认。现在单列
  `login_timeout`（408），并给出「重新生成二维码 / 手机要联网」这类可操作提示。
- **二维码任务失败后前端不说原因**：出错时只显示一句笼统的失败，用户不知道该重来
  还是该换登录方式。现在把 `code` / `hint` 一起渲染出来。
- **所有网页版任务都失败在 `'Task' object has no attribute 'workspace'`**：
  `TaskManager._run` 把 `Task` 当成 `AppState` 递给了任务实现。更阴的是扫码那条路
  ——它在用到 `state` 之前就已经把二维码画进快照了，界面看着完全正常，直到真的登录
  成功才炸。修法：`create()` 显式接收 `state`，`Runner` 的类型别名也改对（原来写的是
  `Task`，静态检查一路点头）。
- **`AuthError` 被当成 `provider_error`（502）**：它是 `ProviderError` 的子类，而判定
  顺序把它排在后面，于是「Cookie 过期了，重新登录一下」被压成「平台故障」，前端也就
  不会引导用户去重新登录。改成先判子类。
- **`POST /api/accounts` 静默丢弃 Cookie**：pydantic 默认丢掉模型里没有的键，所以
  「贴一份 Cookie 建账号」会返回 201 但不存 Cookie，用户以为绑上了、抓取时才发现是空
  会话。接口现在收 `cookie` 与 `options`。
- **`doctor` 依赖检查结果错位**：`_web_check()` 成功时只返回两个值，调用处要三个，
  导致 `doctor` 直接崩；顺带修了 level 判断写反（依赖齐全反而标 `!`）。
- **量纲混用**：`overview.avg_progress` 是 0–1 而卡片百分比是 0–100，模板拿
  `toPercent()` 处理后者，导致 66.7% 显示成「6666.7%」。
- **主题只在设置页生效**：`dashboard.html` 与 `App.vue` 各自只管自己那份，
  切完主题回到总览又变回旧色。改成挂载前 `initTheme()` + 后端返回的 `ui.theme`
  作为权威。
- 前端 `ProgressBar` 的 props 与调用方不一致（传 `value` 收 `ratio`），进度条永远空。

### 变更

- 版本号 2.2.0 → **3.0.0**（主界面换了，属于不兼容的大版本）。
- `pyproject.toml` 新增可选依赖组 `[web]`（fastapi + uvicorn）；**运行命令行版仍然零
  第三方依赖**。
- `aggregate.demo_courses()` 抽出，命令行 `demo` 与网页版「先看演示数据」共用一份样例。
- `tests` 从 361 例涨到 **435 例**，新增 `tests/test_web.py`。

## [2.2.0] - 2026-10-19

**「登录」不再需要另一个窗口**：扫码和账号密码这两种登录方式直接坐在工具里。
v2.1.0 的登录要你切到一个新开的浏览器窗口里去完成，使用体验上仍然是「另一个程序在
办事」；这一版把登录收回到了命令行本身。

### 新增

- **终端扫码登录**（`studydash login`，现为默认）。工具直接向超星要一张一次性登录
  票据，用纯标准库把返回的 PNG 解成点阵，**画在终端里**；手机上的「学习通」App 扫一下
  并确认即可拿到会话。不用开浏览器、不用装扩展、不用 F12 手抄 Cookie。二维码约两分钟
  失效，程序会自动换一张新码，不用重跑命令。扫码全程在终端里完成，登录完就直接是学业
  总览页。
- **账号密码登录**（`studydash login --source password`）。密码走 `getpass`
  （不回显、不进 shell 历史、不用命令行参数），按超星当前协议做 AES-128-CBC 加密后
  提交，**不落盘、不写日志**——`config.json` 里永远只有会话 Cookie。平台要求验证码或
  二次验证时明确报错并建议改用扫码，不猜也不绕。
- **`src/studydash/qr.py`**：零依赖的二维码渲染。自带 PNG 解码（位深 1/2/4/8、色彩类型
  0/2/3/4/6、五种行过滤器、非隔行），从像素反推模块点阵，再渲染成终端字符。彩色模式
  **显式给出前景色与背景色**（浅色终端下是浅底深码，深色终端下是深底浅码，两者扫码
  结果相同）——刻意不依赖终端默认色，因为深色终端会把二维码反相，而反相的码常常扫不出来。
- **`src/studydash/aes.py`**：零依赖 AES-128-CBC 加密（只做加密方向，PKCS#7），
  让「零第三方运行时依赖」这条设计约束在密码登录上也不破例。
- **`src/studydash/qrlogin.py`** / **`src/studydash/passlogin.py`**：两条登录流程的
  协议实现与等待状态机（含票据刷新、轮询节流、服务端消息去重）。
- **`http.py` 支持 Cookie 会话**：接入 `http.cookiejar`，登录过程中服务端下发的
  `JSESSIONID` / `route` 等能自动带上；新增 `jar_cookies()` / `cookie_header()` /
  `clear_jar()`。手写 `cookie=` 仍优先于 cookie jar。

### 修复

- **彩色渲染把二维码的上下两半写反了**（`qr.py` 的 `_color_line`）。半块字符 `▀`
  （U+2580）的前景色画的是**上半格**，而代码把底部模块喂给了前景色、顶部模块喂给了
  背景色，于是每个字符内部上下颠倒——打印出来依然是个方块、看着完全正常，但**扫不出来**。
  用 OpenCV 逐模块比对才暴露出来：2809 个模块里错了 1492 个，改对后 0 个错。已在代码里
  写明这个陷阱，防止后人改回去。
- **截断的 PNG 会漏出裸 `zlib.error`**。`qr.decode_png()` 现在把「没有 IDAT」
  「解压失败」都转成带说明的 `QrError`，并把所有位深统一归一化成 0/255 灰度。

### 变更

- `studydash login` 的 `--source` **默认值从 `live` 改为 `qr`**。`live` 仍可用
  （想在自己熟悉的浏览器界面里登录、再让程序从调试端口读会话）。
- `login` 新增 `--user` / `--password-stdin` / `--port` / `--platform` 参数；
  `--timeout` 默认值改为按来源决定（`qr` 240 秒、`live` 300 秒）。

### 测试

- 新增 `tests/test_qr.py`：测试里自己写 PNG（含 1 位位深），再用**模拟真实终端语义**的
  解析器把渲染结果读回点阵，断言与原图逐模块一致（`margin` 取 0/1/2/4 全跑）。这个往返
  测试正是上面那个「上下颠倒」bug 的回归防线。
- 新增 `tests/test_aes.py`：FIPS-197 C.1 官方向量，外加 8 组用生产密钥生成的固定向量
  （空明文、15/16/17 字节的块边界、含中文、长明文）。
- `tests/test_login.py` 增到 49 个用例，钉住「默认来源是 qr」「没有任何参数能接命令行
  传进来的密码」「扫码失败不吐 traceback」「密码只从 stdin 读」等边界。

## [2.1.0] - 2026-10-19

**「能装上看」变成「能真正用上」**：补上登录与会话获取。v2.0.0 的 `fetch` 要求你自己
去 F12 手抄 Cookie，这一步劝退了所有人——看板做得再好看，数据进不来就等于不能用。

### 新增

- **`studydash login`**：一条命令完成「登录 → 拿会话 → 抓取 → 渲染 → 打开」。
  默认新开一个**隔离配置档的浏览器窗口**（不碰你日常那个，所以不用关掉正在用的
  Edge/Chrome），你在里面正常登录（扫码 / 验证码 / 二次验证都你自己过），程序从调试
  端口把登录后的会话读出来，**立刻**接着抓课程并打开看板——登录完面对的就已经是学业
  总览页。
- **四条会话来源**，按场景挑：
  - `--source live`（默认）隔离窗口 + CDP，登录后即取，**不用关浏览器**。
  - `--source browser` 读日常配置档的 Cookie 库。Chromium 对该库持有独占锁，所以这条
    路要求先退出浏览器——程序会把这件事讲清楚，而不是丢一个 `PermissionError`。
  - `--source file` 吃扩展导出的 JSON 与 Netscape `cookies.txt`。
  - `--source manual` 手抄整条 `Cookie`。
- **`src/studydash/browsers.py`**：跨浏览器 Cookie 读取（Edge / Chrome / Brave /
  Chromium / Firefox）。Windows 上走完整解密链：`Local State` → DPAPI 解出主密钥 →
  CNG `BCryptDecrypt` 解 AES-256-GCM。先复制库文件再只读打开，不改动浏览器数据。
- **`src/studydash/cdp.py`**：零依赖的 CDP 客户端（原始 socket 手写 WebSocket 帧），
  用来跟本机浏览器的调试端口说话。只连 `127.0.0.1`。
- **`src/studydash/login.py`**：`normalize_cookie()` 能吃四种输入形态（裸 `a=1; b=2`、
  带 `Cookie:` 前缀、扩展 JSON、`cookies.txt`），去重、去空白；`check()` 真的拿会话去
  请求一次登录态接口再判断有效性，不把死 Cookie 写进配置；`save()` 写进 `config.json`
  并按 `(平台, 账号名)` 去重。
- **解析失败会自己讲清楚原因**。超星课程列表解析为空时，报错里带上四句诊断：响应多少
  字节、正文像不像登录页、有没有 `<ul id=courseList>`、里面几个 `<li>`、有没有出现过
  `courseId`。用户据此能自己分清「Cookie 失效」「账号确实没课」「平台改版」三种情况。
- **`doctor` 的只读扫描支持「本机例外」**：`cdp.py` 被逐文件白名单放行，但必须证明
  自己只连回环地址（含 `127.0.0.1` 之类主机常量、且不出现任何网站 URL）才放行。

### 修复

- **Chromium Cookie 的 32 字节域名哈希没被剥掉**（导致新版本浏览器的 Cookie 全是乱码）。
  Chromium 从 DB `meta.version >= 24` 起，会把 `sha256(host_key)` 前插到明文前面。原先
  按「读 32 字节」盲切，遇到超星那种域名短到明文不足 32 字节的 Cookie 会被整条吃掉。
  现在先算 `sha256(host_key)` 和实际前缀比对，比对上了才剥——剥不剥得掉有据可依，不是
  猜的。
- **`cdp._http_get` 死等响应结束**：Chromium 不会因为 `Connection: close` 就真关连接，
  于是每次 `GET /json/version` 都等到超时，「端口起不来」的假象由此而来。现在按
  `Content-Length`（或 chunked 分块）精确读够长度就返回。
- **CDP 取 Cookie 用错了域**：浏览器级连接上没有 `Network.getAllCookies`，改用
  `Storage.getCookies`，并保留一条 `Target.attachToTarget` + 会话级 `getAllCookies` 的
  回退路径。
- **WebSocket 二进制帧被当成文本**：CDP 偶尔发二进制帧，原先把它的载荷拼进结果并解码，
  会让 JSON 解析莫名失败。现在二进制帧连同其后续 continuation 帧整体丢弃。
- **`login --label` 被覆盖**：显式指定的账号名会被自动生成的 `默认（uid）` 顶掉，导致
  两个账号同名。现在有显式名就用显式名。
- **`launch()` 挑端口的范围错了**：原先让系统随便给，拿到的是 49152 以上的临时端口段，
  浏览器自己也在这段里抢端口，于是反复「端口没起来」。现在固定在 24000–32000 里找，
  失败重试并每次都换一个。
- **超星课名解析多了兜底**：`title` 属性 / `course-name` 类 / `<h3>` 都没命中时，退到
  链接正文。原先这种情况下课名会退化成一串编号，看板上每门课都长得一样。
- **`_diagnose_list_page` 认不出 JSON 形式的 `courseId`**：原正则只匹配 URL 里的
  `courseid=111`，含 `"courseId": 111` 的响应会被误报成「页面结构变了」。

### 变更

- 测试套件 196 → **272 个用例**（新增 `test_login.py` 33、`test_cdp.py` 26、
  `test_chaoxing.py` 13）。
- README 重写「接上你自己的账号」与「命令」两节，新增 `login` 参数表与隐私说明
  （读浏览器 Cookie 的边界：默认只取 `*.chaoxing.com`、读副本、不写日志、解密依赖你的
  Windows 登录凭据）。

## [2.0.0] - 2026-10-19

**破坏性重构**：从「超星单平台看板」变成「多平台学习进度聚合器」。

### 新增

- **多平台聚合**。适配器（provider）是插件，内置：
  - `chaoxing` 超星学习通（beta）
  - `icourse163` 中国大学 MOOC（未验证）
  - `zhihuishu` 智慧树（未验证）
  - `manual` 手动导入 JSON / CSV（stable）
  - `demo` 离线演示（stable）
  第三方适配器可通过 `studydash.providers` 这个 entry_point 组挂进来，不必改本仓库。
- **统一数据模型**：课程（分项进度 + 学期 + 教师 + 最近活跃）、作业（类型 / 状态 / 截止 /
  得分 / 满分 / 链接）、账号状态。派生计算（逾期分类、紧急度排序、日期解析）集中在模型层。
- **看板重写**：平台分布卡（含适配器状态徽标与失败原因）、待办时间线按
  已逾期 / 3 天内 / 7 天内 / 以后 / 无截止 / 已完成 分组、平台筛选 chips、课程卡可展开
  作业明细、搜索与排序、深浅色主题、响应式、键盘可达、`#dark` / `#light` 强制主题。
- **`studydash` 命令行**：`init` `demo` `platforms` `probe` `fetch` `import` `build` `open`
  `serve` `all` `config` `doctor` `version`；不带子命令即一键模式。
- **`import` 可给任意平台打标**：线下课、教务系统这类没有接口的课程，也能用手写 JSON/CSV
  喂进来，并在看板上显示成对应平台的徽标。
- **`probe` 只读探测**：逐个请求适配器声明的端点，请求记录与原始响应存到 `data/raw/`
  （Cookie 已脱敏）。平台改版时，贡献者可以把存档贴进 issue，维护者不需要自己的账号
  也能定位解析问题。
- **`doctor` 自检**：环境、工作区、模板、以及「只读性」源码扫描（除 `http.py` 外还有谁
  私自联网）。
- **崩溃日志**：崩溃时写 `crash.log` 并在窗口模式下弹提示，不再「双击一闪就消失」而
  什么都不留。
- **`docs/plugin.md`**：适配器开发指南，含最小实现、RPC 风格平台、排查表、PR 清单。
- **测试套件**：196 个用例（pytest），覆盖模型、HTML 解析器、只读网络层（含白名单拒绝
  路径）、工作区与配置脱敏、聚合的失败隔离、渲染转义、适配器注册表、CLI 契约。

### 变更

- **只读模型调整**：原来只允许 GET；现在改为 **GET 任意 + POST 白名单**。原因是慕课、
  智慧树的查询接口本身就是 POST 到 `*.rpc`，一刀切禁 POST 等于放弃这些平台。改为按
  **地址**判定——地址里含 `submit` / `save` / `answer` / `commit` / `upload` / `finish` /
  `report` / `sign` / `checkin` 等写动作词的端点会被直接拒绝，PUT/PATCH/DELETE 仍然禁用。
  仍然不存在任何提交作业、交卷、答题、刷课、签到、上报进度的调用。
- **分发形态**：从单个 `main`/onefile exe 变为「可 pip 安装的包 + onedir/onefile 双产物」。
  onedir 版本启动更快、更不容易被杀软误报。
- **版本号单一来源**：只有 `src/studydash/__init__.py` 的 `__version__`；`pyproject.toml`
  动态读取它，exe 的 Windows 版本资源由 `tools/version_info.tmpl.txt` 在打包时渲染。
- **零第三方运行时依赖**：自带 `htmlx.py`（基于标准库 `html.parser` 的宽松解析器）与
  `http.py`（urllib 客户端），不再需要 `lxml` / `requests`。
- 仓库名 `chaoxing-dashboard` → `studydash`。

### 移除

- 单文件 `dashboard.py`（功能已拆进 `src/studydash/`）。
- `lxml` / `requests` 等运行时依赖。

### 修复

- **无数据的课程/平台显示成 0%**，与「真的没学」无法区分。现在后端给
  `has_data` / `has_progress`，前端显示「无数据」，并且不画进度条、不计入平均进度
  （原先只有总览的平均进度排除了这类课程，卡片和平台卡都没排除）。
- 抓取成功但零课程的平台不再生成空白平台卡（典型：demo 账号）。

### 破坏性变更

- `data/progress.json` 的 schema 变成 v2（`SCHEMA_VERSION = 2`），与 v1 不兼容；
  升级后重新 `fetch` 或 `import` 一次即可。
- `config.json` 结构重写（`accounts[]` + `http` + `ui`，替代 v1 的 `cookie` / `endpoints` /
  `deadlines`）。
- 命令入口从 `python dashboard.py <cmd>` 变为 `studydash <cmd>`（`python -m studydash <cmd>`
  等价）。
- 截止时间不再要在配置里手工登记：数据源给了就用，没给就用 `import` 补。

## [1.0.0] - 2026-10-18

首个版本。

### 新增

- **聚合看板**：总览指标、课程卡片（分项进度条）、落后预警、截止时间线、搜索/筛选/排序。
- **三条数据接入路径**
  - `fetch`：用你自己的 Cookie，只读拉取课程列表。
  - `import`：从浏览器另存的 HTML 导入，不需要 Cookie。
  - 手工维护 `data/progress.json`。
- **一键模式**：不带子命令直接运行（或双击 exe）＝ 自动选数据源 → 渲染 → 打开浏览器。
- **独立可执行文件**：`学习进度看板.exe`（PyInstaller onefile，约 8.6 MB），零运行时依赖。
- 主题跟随系统，可用 `#dark` / `#light` 强制；键盘可达；打印友好；窄屏自适应。
- 应用图标由 `tools/make_icon.py` 用 Pillow 纯代码绘制，不依赖外部素材。

### 设计约束

- 全部网络动作只有一处 **HTTP GET**；没有任何 POST 数据体、上报接口或提交/签到/答题逻辑。
- 只读代码，不写入任何平台数据；看板只用于你自己的学习进度自查。

### 已知限制

- `fetch` 的解析依赖平台当前的课程列表页结构，平台改版后需要更新 `parse_html_course_list()`；
  原始响应会留存在 `data/raw/` 以便排查，必要时用 `import` 兜底。
- 课程列表页通常只给总进度百分比，不给分项任务点数与截止时间；
  截止时间线需要在 `config.json` 的 `deadlines` 里手工登记。

[3.0.0]: https://github.com/zhihang64/studydash/releases/tag/v3.0.0
[2.2.0]: https://github.com/zhihang64/studydash/releases/tag/v2.2.0
[2.1.0]: https://github.com/zhihang64/studydash/releases/tag/v2.1.0
[2.0.0]: https://github.com/zhihang64/studydash/releases/tag/v2.0.0
[1.0.0]: https://github.com/zhihang64/studydash/releases/tag/v1.0.0
