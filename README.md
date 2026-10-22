# studydash · 学习进度聚合看板

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE) [![Python](https://img.shields.io/badge/python-%E2%89%A53.10-3776AB.svg)](https://www.python.org/) [![Runtime deps](https://img.shields.io/badge/runtime%20deps-none-success.svg)](pyproject.toml) [![Version](https://img.shields.io/badge/version-3.0.0-informational.svg)](CHANGELOG.md)

> 多平台学习进度聚合看板 · 只读 · 本地运行 · 运行时零第三方依赖

把**不同平台**的学习进度、作业与截止时间读出来，聚合成一个本地页面。

课程散在超星、中国大学 MOOC、智慧树，作业又散在各自的公告和作业页里——这门课
进度到哪了、哪几个 DDL 快到了，没人替你算。studydash 把这些读到一个页面上：
整体进度、分项任务点、待办与逾期、DDL 时间轴、平台分布。

**v3 起主界面是网页版**：一条命令（或双击 exe）起一个只跑在本机的网页应用，
在浏览器里就能扫码登录、刷新数据、管理账号、看五个页面。不需要记命令行参数，
也不需要先装 Python —— 打包版已经带好了。想要「一个 HTML 文件」那种用法的话，
静态看板仍然在（`studydash all`），只是退成了兜底。

![看板预览（浅色）](docs/preview-light.png)

<details>
<summary>深色主题预览 / 移动端预览</summary>

![看板预览（深色）](docs/preview-dark.png)

![看板预览（移动端）](docs/preview-mobile.png)

</details>

---

## 目录

- [这是什么，不是什么](#这是什么不是什么)
- [快速开始](#快速开始)
- [网页版（推荐）](#网页版推荐)
- [写操作（实验性，只对超星）](#写操作实验性只对超星)
  - [上报任务点（音视频 / 阅读 / 文档 / 图书 / 直播）](#上报任务点音视频--阅读--文档--图书--直播)
  - [章节测验自动答题 / 刷学习次数](#章节测验自动答题--刷学习次数)
- [接上你自己的账号](#接上你自己的账号)
- [命令](#命令)
- [看板能看到什么](#看板能看到什么)
- [数据结构与契约](#数据结构与契约)
- [写一个适配器](#写一个适配器)
- [设计取舍](#设计取舍)
- [打包](#打包)
- [测试](#测试)
- [版本与发布](#版本与发布)
- [已知限制](#已知限制)
- [隐私](#隐私)
- [相关文档](#相关文档)
- [License](#license)

---

## 这是什么，不是什么

**是**：一个只读的进度聚合器。它替你「看」，不替你「做」。唯一的例外是
`studydash writeback --apply`（见下方「只读承诺」末条）——它把任务点补成已完成。

**只读承诺**（这条是硬约束，写进了代码而不是文档）：

- **平台流量的唯一出口是 `src/studydash/http.py`**，别处不许直接 `urllib`／`requests`。
  `studydash doctor` 会扫源码，有别的文件私自联网就报出来。
- 唯一的例外是 `src/studydash/cdp.py`：它用原始 socket 跟**本机**浏览器的调试端口
  （`127.0.0.1`）说话，用来把你自己登录好的会话读出来。这个例外是**逐文件白名单**的，
  而且白名单里的文件必须证明自己只连回环地址（`doctor` 会查它有没有 `127.0.0.1`
  这种主机常量、有没有出现任何网站 URL），不是「说了算」。
- **GET 可以打任何地址**（读列表页、读进度页）。
- **POST 只允许打到适配器显式声明的查询端点白名单**。慕课、智慧树这类平台的接口是
  RPC 风格（查询也用 POST 到 `*.rpc`），一刀切禁 POST 等于放弃这些平台；所以改成按
  **地址**判定：地址里出现 `submit` / `save` / `answer` / `commit` / `upload` /
  `create` / `delete` / `update` / `modify` / `doWork` / `finish` / `report` /
  `sign` / `checkin` / `signin` 这类写动作词，直接拒绝。
- **只读链路里不存在任何写调用**：`providers/` 与 `http.py` 的写闸门保证抓取路径
  永远不会提交作业、交卷、签到。
- **写能力被隔离成三个独立模块，默认全部只演练**（见
  [写操作](#写操作实验性只对超星)）：`writeback.py`（上报任务点）、
  `quiz.py`（章节测验答题）、`learning.py`（刷学习次数）。三条硬规则：不加
  `--apply` 一个请求都不发；上报/提交地址钉死在常量白名单里，别的地址拒发；它们
  不出现在 `fetch` / `all` / `web` 的任何调用链里，只在你亲手触发时运行。网络出口
  仍然只有一个 `http.py`，没有第二个 socket。
- **不碰密码**。`studydash login` 开一个隔离浏览器窗口让你自己登录（扫码、验证码、
  二次验证都你自己过），它只从调试端口读登录后的 Cookie。账号密码它一个字符都看不到，
  也永远不会被写进任何文件。

**不是**：不是自动签到工具，也不提供代做/代考。v1 时期这个仓库是超星单平台的
看板；v2 重写为多平台聚合器，取的是「自己的进度自己看全」这一半；v3 之后补上了
可选的写能力（上报任务点、章节测验答题、刷学习次数，见 `CHANGELOG.md` 的「未发布」），
但默认是关闭的，要显式 `--apply` 或在页面上二次确认；签到、代做、批量注册仍然没有。

---

## 快速开始

### 方式一：下载 exe（不用装 Python）

从 [Releases](https://github.com/zhihang64/studydash/releases) 下载，**两个包选一个就行**：

| 产物 | 说明 |
| --- | --- |
| `studydash.exe` | 单文件版，下载即用，约 9 MB。首次启动要解包，慢几秒 |
| `studydash-onedir.zip` | 文件夹版，解压后用里面的 `studydash.exe`，启动更快、更不容易被杀软误报 |

> 想固定一个下载链接，用 [最新版直链](https://github.com/zhihang64/studydash/releases/latest)，文件名不带版本号，所以
> `…/releases/latest/download/studydash.exe` 一直有效。

放进一个空文件夹**双击**即可：挑数据源 → 渲染 → 用默认浏览器打开看板，并在同目录生成：

```
你的文件夹/
├── studydash.exe
├── config.json         ← 想接自己账号时，把 config.example.json 复制成这个
├── crash.log           ← 只在崩溃时出现
├── data/progress.json  ← 聚合结果
└── out/dashboard.html  ← 看板
```

> 如果单文件版双击「黑框一闪就消失」，换文件夹版。崩溃时 `crash.log` 会记下原因，
> 仓库还应有一份弹窗提示。也欢迎提 issue 附上 `crash.log`。

### 方式二：pip 安装

```bash
pip install git+https://github.com/zhihang64/studydash
studydash demo --open
```

### 方式三：源码跑（开发）

```bash
git clone https://github.com/zhihang64/studydash
cd studydash
pip install -e ".[dev]"      # 只有测试/打包时才需要依赖；运行本身零依赖
studydash demo --open        # 或： python -m studydash demo --open
```

**运行时零第三方依赖**，只用标准库——见下面「设计取舍」。

### 30 秒看效果（不联网）

```bash
studydash demo --open
```

内置 6 门课 / 10 条作业的样例数据，覆盖已逾期、今天到期、远期、无截止、已评分等
各种状态，用来确认看板本身跑得通。不联网、不需要账号。

---

## 网页版（推荐）

不用记命令、不用开命令行窗口。**双击 exe** 就是这个：起一个只跑在本机的网页应用，
用默认浏览器打开 `http://127.0.0.1:8788`。

命令行版同一个东西：

```bash
studydash web            # 起服务并打开浏览器
studydash web --no-open  # 只起服务（CI / 远程桌面里用）
studydash web --port 9000
```

### 五个页面

| 页面 | 干什么 |
| --- | --- |
| **总览** | 6 张 KPI、平台分布、最要紧的几件、进度告急的几门课；页头有「抓取数据」和「上报任务点」两个按钮 |
| **待办** | 按 已逾期 / 3 天内 / 7 天内 / …… 分组的 DDL 时间轴，可按平台筛 |
| **课程** | 搜索 + 排序 + 平台筛选；每门课带分项进度条和可展开的作业明细；超星课程卡右上角有「上报任务点」按钮（带范围与并发两个小下拉），点下去按钮下面就地显示上报进度条和成败 |
| **已隐藏课程** | 从「课程」页进的二级页面：结课/不想再看的课藏在这里，可一键恢复 |
| **账号** | 在页面里登录绑账号、启用/停用、删账号、单独抓某个平台、探测接口 |
| **设置** | 深浅主题、抓取参数（超时/限速/重试/抓取并发）、提醒阈值、环境自检 |

「隐藏」是给已经结课、或者这学期不想再占位置的课程准备的：藏起来之后，它和它的
作业不再出现在总览、待办和平台统计里，但**数据不会被删**——`progress.json` 仍是
完整的一份，恢复显示不需要重新抓取。隐藏列表存在 `config.json` 的
`ui.hidden_courses`，按 `平台:课程id:学期id` 记录，所以同一门课的不同学期互不影响。

### 在页面里登录

「账号」页有四个页签，**默认是扫码**：

1. **扫码** —— 页面上直接出二维码，手机「学习通」App 扫一下就登上了。约两分钟
   过期，程序自动换一张。
2. **账号密码** —— 输账号和密码。密码走 `password` 类型输入框，提交后立刻从内存
   清掉，不进配置、不进日志、不进任务快照。
3. **粘贴 Cookie** —— 你已经有 Cookie 的话直接贴。
4. **本机浏览器** —— 从你日常浏览器里读（要先完全退出它，见下面「隐私」）。

选中**中国大学 MOOC** 时，扫码用的是它自己的协议（二维码由 `icourse163.org` 下发，
手机要用「中国大学 MOOC」App 扫），账号密码页签会隐藏——那条路还没做，扫码或
Cookie 都能登。

选中**头歌**时只剩「粘贴 Cookie」和「本机浏览器」两个页签：它没有可复用的扫码
协议，账号密码登录又要过验证码。浏览器那个页签勾上「另开独立窗口」就能登。

登录是**后台任务**：进度、日志、二维码都通过 SSE 实时推到页面上，可以关掉抽屉去
看别的，任务在后台继续跑。平台要图形验证码或二次验证时会明确告诉你「改用扫码」，
不会去猜验证码。

### 为什么它敢把登录放在网页里

一个网页能操作你的账号，通常意味着它能被别的网页借道攻击。这里的三条约束是写进
代码的，不是靠自觉：

- **只监听回环地址**（`127.0.0.1`）。`--host` 传非回环地址会**直接拒绝启动**。
- **每个请求都校验来源 IP 是回环**，不是回环就 `403`。
- **不用 Cookie 会话**，所以没有 CSRF 面。令牌每次启动重新生成，通过
  `<script>window.__STUDYDASH__</script>` 注入 index.html，之后每个 `/api/*` 请求
  都要带 `x-studydash-token` 头。令牌只在你自己打开的这个页面里，别的站点拿不到
  （同源策略 + 自定义头预检）。
- 交互式 API 文档（`/docs`、`/openapi.json`）在网页版里是**关掉的**，免得把整张
  接口表摊在页面上。

## 写操作（实验性，只对超星）

下面两节是**本项目唯一的写能力**，和前面「只读聚合」是两条独立的路：默认全部只演练（一个请求都不发），必须显式 `--apply` 或在页面上二次确认才真的发。
它们不在 `fetch` / `all` / `web` 的任何调用链里，只在你亲手触发时运行。

### 上报任务点（音视频 / 阅读 / 文档 / 图书 / 直播）

超星课程每张卡右上角有个按钮，就是这个项目**唯一会写平台数据**的入口——等价于 `studydash
writeback`，但只作用于这一门课（前端传 `course_id`，后端按课程号锁死，不碰别的课）。

点一下按钮变「确认上报？」，**四秒内再点一次**才真的发；不点它自己退回原样。
写平台数据不该是一次误触就发生的事，但也不值得为它开弹窗（全仓没有 modal 组件）。

按钮下面两个小下拉：**范围**决定补哪几类任务点，**并发**决定一门课用几个线程。

上报覆盖学习通六类任务点，协议彼此不同（移植自 `chaoxing_tool` 的 `classis/Media/`）：

| 任务点 | 做什么 | 端点 |
| --- | --- | --- |
| 视频 / 音频 | 按秒上报播放位置，md5 签名（盐 `d_yHJ!$pdA~5`） | `mooc1.chaoxing.com/mooc-ans/multimedia/log/a/…` |
| 阅读 | 一次性提交任务点 | `mooc1-2.chaoxing.com/ananas/job/readv2?…` |
| 文档 | 一次性提交任务点 | `mooc1-2.chaoxing.com/ananas/job/document?…` |
| 图书 | 一次性提交任务点 | `mooc1-2.chaoxing.com/ananas/job?…` |
| 直播 | 提交观看时长 | `zhibo.chaoxing.com/saveTimePc?…` |

判定：音视频看平台回包里的 `isPassed` 是否为真（true 才算这个任务点被标成已完成）；
阅读 / 文档 / 图书看回包里的 `status` 是否为真；直播看响应正文是不是 `@success`。
这些地址都钉在 `WRITE_URL_PREFIXES` 里，别的地址拒发。

不选范围就是六类全补。想要演练、要逐分钟（每段 59 秒、`isdrag` 3 → 0 → 4）、
要限个数或按课名过滤，走总览页页头那张带完整开关的卡片
（`web/src/components/WritebackPanel.vue`），或者命令行：

```
studydash writeback                       # 演练：六类任务点全清点一遍，一个请求都不发
studydash writeback --apply               # 真的报（音视频 / 阅读 / 文档 / 图书 / 直播）
studydash writeback --apply --paced       # 音视频按平台真实节奏，一段一段等它「看完」
studydash writeback --apply -j 4          # 4 个线程并发上报，嫌一条一条太慢时用
studydash writeback --apply -k read -k document   # 只补阅读和文档，不碰音视频
studydash writeback --apply -k live       # 只补直播
```

音视频**别一次跳满**。实测把播放位置一次性送到片尾，5 个任务点平台只认 1 个，另外 4 个
直接被标上人脸抓拍、`playTime` 占满而 `isPassed` 永远是 false——推不动的死态，比不报更糟。
改成**递进上报**（播放位置按 30% → 70% → 100% 分三步发，每步等 1.5 秒）后 4 个全过。
网页端默认就开（课程卡上的「递进」，总览页的「递进上报」），命令行要显式加 `--ramp`：

```
studydash writeback --apply --ramp                     # 音视频分三步递进，推荐
studydash writeback --apply --ramp --ramp-delay 0.5    # 赶时间就缩间隔

上报完**看板会自动跟上**：每个动过的课程都会重读一次平台的「已完成任务点 X/Y」，
就地改写 `data/progress.json` 里的进度条与总览均值，不需要再点一次「抓取」。
只改真动过的课，别的平台、别的课一个字节都不动；平台计数落库有延迟，
数字比实际少一两个时隔几十秒再刷新一次即可。

```bash
studydash writeback --apply --ramp --no-sync     # 关掉自动同步（保留旧行为）
```
```

三次请求换通过率，比事后发现白刷划算。

抓取那一侧也有并发，但开关在设置页里：`设置 → 抓取并发线程`（1~8，默认 1）。
配置项是 `http.fetch_workers`，也可以直接写进 `config.json`：

```json
"http": { "timeout": 8.0, "delay": 0.4, "retries": 2, "fetch_workers": 4 }
```

>1 时账号之间、同账号下的课程页进度、作业详情、逐章任务点卡片会各自并行抓；
所有结果仍按原序回填，平台卡和课程顺序不会随线程快慢漂移。注意 `delay` 是**每个线程**
的间隔，所以整体请求速率约等于 `fetch_workers / delay`——别把 8 和 0.05 一起用，
那是拿自己的 Cookie 去撞平台的限流。

课程卡按钮下面还有一个「并发」小下拉（1 / 2 / 4 / 8），选中后这一门课的任务点会多个线程同时上报——`tasks.start` 里那个 `workers` 直接透传给后端。默认 1，也就是一条一条发；一门课几十个任务点全是互相独立的 GET，4 并发通常能压到四分之一时间。逐分钟模式（`--paced`）会强制退回串行，因为那种模式的意义就是让请求在时间上摊开，并发反而违背它。

跑起来之后按钮下面直接出一条进度条（`ProgressBar`），按任务点的实时 `progress`
填充，右侧是百分比，下面一行跟当前任务点；跑完那一行变成结局——绿色「平台确认 3/3 个
任务点」、红色「2/3 成功，1 个失败」或灰色「没有需要上报的任务点」。进度走 SSE，
断了会自动降级成轮询，不需要手动刷新。

音视频任务点以平台回包为准：`isPassed=true` 才算这个任务点真的被标成完成；
`isPassed=false` 就是**没完成**，结果行如实写成「平台没确认」，不再算成功
（早期版本把 false 解释成「没有剩余可报」并显示成功，是假阳性，已推翻）。
实测 false 有两种：一种是播放位置已经满了（`playTime` 等于总时长）平台却没给通过
——这种再怎么重发都没用，工具直接跳过、一个请求都不发；另一种是发送本身没成
（网络中断、平台没回 JSON、签名拼不出来），原因照实写进结果行。

**怎么确认真的干活了。** 只看我们自己的回包容易自欺，所以每门课上报前、上报后各读一次
平台课程页上的「已完成任务点 X/Y」（`https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse?courseid=…`，
`writeback.py` 的 `mission_counts()`）：命令行打成
`已完成任务点：课程名 60 → 61 / 1442（+1）`，网页版结局文案后面也带这一段，
`op_writeback` 的返回体里是 `mission: {before: {...}, after: {...}}`。
实测：章节级的待完成数在上报后**立刻**下降（2 → 1），课程页那个总计数由平台定时汇总，
要过几分钟才跟上——看到总数一时没动，不等于没生效。

没绑学习通账号时这个按钮点了只弹一句提示，不会发出任何东西。
### 章节测验自动答题 / 刷学习次数

课程卡上「上报任务点」旁边还有两个按钮，和它一样点两次才真动手：

- **测验答题**（`src/studydash/quiz.py`）：按章节把 `type == "workid"` 的任务点抓下来，
  取整页 HTML（`mooc1.chaoxing.com/mooc-ans/api/work`），解析题号 / 题型 / 选项，
  逐题查题库换答案，再 POST `mooc-ans/work/addStudentWorkNew` 交卷。题型按平台编号走：
  0 单选、1 多选（答案必须**排序**）、2 填空、3 判断、4 简答。题库覆盖率低于「最低覆盖率」
  （默认 0.8）就只保存不交卷（`pyFlag=1`）——平台会把低分卷记成成绩，硬交比不交更糟。
- **刷学习次数**（`src/studydash/learning.py`）：每轮 = `mycourse/studentstudyAjax` 拿到
  `fystat-ans.chaoxing.com/log/setlog…` 地址 → GET 那个地址（真正计次）→
  `detect.chaoxing.com/api/monitor` 心跳 → 等 30 秒 → 再心跳一次。服务端按时间窗口计次，
  所以这块是**串行**的，一轮约半分钟，并发没有意义。

题库在「设置 → 题库」里配：token、接口地址、最低覆盖率。token 只写进本机配置文件，
回读时打码成 `已配置 …abcd`；留空表示不改。命令行等价物：

```
studydash quiz                          # 演练：只清点章节测验，一份都不交
studydash quiz --apply                  # 真的答题交卷（覆盖率不够的只保存不交卷）
studydash quiz --apply --cover-rate 0.9  # 这一轮把门槛提到 90%
studydash learning --apply --times 2     # 刷两轮学习次数（每轮约 30 秒）
```

题目和选项里的字体反爬（屏幕上看到的方块字）由 `src/studydash/cxfont.py` 处理：把页面里
内嵌的 base64 字体解出来，按字形哈希查 `src/studydash/data/font_map_table.json`，
康熙部首先走 NFKC 归一。

接口契约（错误码、任务状态机、SSE 事件格式、每个端点的字段）在
[docs/web-api.md](docs/web-api.md)，前端 `web/` 照着它写。

---

## 接上你自己的账号

### 一条命令：登录，然后直接看到你的学业页面

```bash
studydash login
```

它把二维码**画在终端里**——不用开浏览器、不用装扩展、不用 F12 手抄 Cookie：

```text
  ▸ 向超星要一张登录二维码
      （终端里出现一张二维码）
  用手机上的「学习通」App 扫上面这个码，然后在手机上点确认。
  二维码大约两分钟会失效，程序会自动换一张，不用重跑命令。
    · 已扫码，请在手机上确认
  ✓ 会话可用（uid=1234567，来源：qr）
  ✓ 已存入配置：…\config.json
  ▸ 接着抓取「chaoxing」的学业数据
  ✓ 超星学习通：12 门课
  共 12 门课 / 1 个平台，平均进度 41.3%
  待办 6 项（逾期 1 · 三天内 2），已完成 9 项
```

登录成功后命令行会自己往下走：**取会话 → 抓课程和作业 → 渲染 → 打开看板**。
也就是说，登录完你面对的就已经是学业总览页面了。

不想扫码就输账号密码：

```bash
studydash login --source password --user 13800000000
```

密码走 `getpass`（不回显、不进 shell 历史），进程里也**只经过一次 AES 加密**就发出去，
不落盘、不打日志——配置里存的永远只有会话 Cookie。学习通侧要求验证码或二次验证时，
程序会明确告诉你「改用扫码」，而不是拿一个必然失败的请求去撞。

### 六条取会话的路，挑一条走

| 来源 | 命令 | 什么时候用 |
| --- | --- | --- |
| 终端扫码 | `studydash login` | **首选**。码画在终端里，手机扫一下就好 |
| 账号密码 | `studydash login --source password` | 手上没手机、或想脚本化。密码不存、不进历史 |
| 隔离窗口 | `studydash login --source live` | 想在自己熟悉的浏览器界面里登（扫码/验证码/二次验证都你自己过）。开一份独立配置档，**不用关**你日常的浏览器 |
| 读日常配置档 | `studydash login --source browser` | 你已经在日常浏览器里登录好了。注意：**要先完全退出那个浏览器**（Chromium 对 Cookie 库持有独占锁，不是程序不给力） |
| 扩展/工具导出 | `studydash login --file cookies.json` | 浏览器换成不支持的新格式，或上面几种都不行。吃 Netscape `cookies.txt` 和常见扩展导出的 JSON |
| 手动粘贴 | `studydash login --cookie "_uid=..."` | 兜底。F12 → Network → 任一请求 → Request Headers → 整条 `Cookie` |

不管哪条路，都会**立刻验证**会话是否真的有效，不会把死 Cookie 写进配置然后让你自己发现。
不想让它自动接着抓取，加 `--no-fetch`；只想存不想验证，加 `--no-check`。

`--source live` 用的那份配置档留在 `~/.studydash/browser`，所以下次走那条路不用再登一遍
（可用 `--browser` 换浏览器、`STUDYDASH_BROWSER_EXE` 换可执行文件、`--port` 指定调试端口）。
它的 Cookie 就在那里，等于一份本地会话副本——不想留，直接删掉这个目录。扫码和密码这两条
路不写任何浏览器配置档。

### 超星学习通（status: stable）

`login` 之外，也可以纯命令行把 Cookie 塞进 `config.json`：

```bash
studydash init            # 生成 config.json（含注释字段）
studydash probe chaoxing  # 只读探测，原始响应存到 data/raw/，先确认能通
studydash fetch           # 抓课程、进度与作业/待办
studydash build --open    # 渲染 + 打开
```

**作业/待办默认开启**：列表页一次请求拿全账号的作业，未提交项再逐条读详情页
拿绝对截止时间。不想抓就关掉；详情请求数量可以用 `max_work_details` 封顶：

```json
"options": { "works": false, "max_work_details": 60 }
```

**章节明细也默认开启**（不需要配置）：课程页本身带着整棵章节树，逐节写着「还差几个
任务点」。这份数据和进度一起拿到，不多发一次请求，看板在课程卡里折叠显示
「还差任务点的章节」（第几章、第几节、各差多少）。

`options.chapters` 是另一条路——逐条展开每节的视频/阅读/测验清单，走
`knowledge/cards`，每节一次请求，**这条仍是实验性的**：

```json
"options": { "chapters": true, "max_chapters": 40 }
```

校验不过或平台改版时，先跑 `studydash probe chaoxing` 留档原始响应。

**抓不到课程时不要猜。** `studydash fetch` 会直接告诉你它看到了什么：

```text
× 超星学习通: 课程列表解析结果为空
  诊断：响应 34521 字节；正文看起来是登录页（Cookie 没生效）。
  → 跑 `studydash probe chaoxing` 把原始响应留档下来（data/raw/）
```

这几句话就能分清三种情况：Cookie 失效、账号确实没课、平台改版了。

### 中国大学 MOOC（课程 / 进度 / 作业已验证）

扫码走的是 `icourse163.org` 自己的协议（`logonByQRCode`），和超星不是一套：

```bash
studydash login --platform icourse163            # 终端出码，用「中国大学 MOOC」App 扫
studydash login --platform icourse163 --source live   # 想在自己熟悉的浏览器里登
studydash login --platform icourse163 --source manual --cookie "NTESSTUDYSI=..."
```

二维码约两分钟过期，程序会自动换。手机确认后，工具拿服务端下发的 `token` 换成
会话 Cookie，再用用户信息接口确认**真的登录了**——`NTESSTUDYSI` 这种 Cookie 在
未登录的页面上也会下发，只看「有没有 Cookie」会把游客当成已登录。

**账号密码登录还没做**（网易 URS 那条路会碰上验证码/二次验证），请用扫码或
Cookie。课程数据走两个 RPC，2026-10-20 已用真实账号验证：

| 接口 | 取到什么 |
| --- | --- |
| `learnerCourseRpcBean.getMyLearnedCoursePanelList.rpc`（`type=30`） | 我的课程面板：MOOC + SPOC 全部课程、已学/总小节数 |
| `courseBean.getMocTermDto.rpc`（`termId`, `isDraft=0`, `excludeUnrelease=0`） | 学期章节树，作业/测验/考试及其截止时间、得分 |

两个坑：`csrfKey` 取的是 **`NTESSTUDYSI` Cookie 的值**（不是叫 `csrfKey` 的
Cookie）；面板接口的参数是 `type`，传 `pageIndex` 会被 `-1007 不含有该参数` 顶回来。

### 头歌（课程 / 实训 + 图文作业已验证）

头歌的网页是 React 单页应用，数据走 `/api/*.json`，而且**每个请求都要带 PC 端签名**
（`X-EDU-Signature = md5(base64("method=GET&ak=…&sk=…&time=毫秒"))`，ak/sk 写在前端
bundle 里）。少带签名会被顶回 `-102 服务器时间与您的设备时间不匹配`——这句报错是
幌子，真实原因是签名缺失。

登录页是表单 + 验证码，没有可复用的扫码协议，程序也不替你输密码，所以只有两条路：

```bash
studydash login --platform educoder --source live     # 开独立窗口，登完自动取会话
studydash login --platform educoder --source browser  # 读日常浏览器（要先完全退出它）
studydash login --platform educoder --source manual --cookie "_educoder_session=..."
```

抓课程、任务和进度用的三个端点：

| 接口 | 取到什么 |
| --- | --- |
| `/api/users/get_user_info.json` | 当前登录用户。游客也会返回 200，只是 `login=""`、`username="游客"` |
| `/api/courses/mine.json` | 「我的课程」列表 |
| `/api/courses/{id}/homework_commons.json?type=4` | 实训/作业：截止时间、任务点、已发布/已完成计数 |
| `/api/courses/{id}/homework_commons.json?type=1` | 图文作业：截止时间、交没交 |

几个坑：① 课程列表接口对**游客也返回成功 + 空数组**，拿它判登录会把「会话过期」
显示成「0 门课」，登录判据必须看 `get_user_info.json`；② 任务接口**默认只回
`type=4`（实训）**，图文作业要再发一次 `type=1`，不传就整门课的图文作业一条都看不到；
③ 任务接口默认每页 20 条，要传 `per_page` 才能一次取全；④ `status: ["提交中"]` 是
「提交窗口还开着」，不是「已提交」——实训完成与否看 `shixun_finished_status == 1`
或 `student_passed_time` 非空，图文作业看 `un_commit_work`（`false` 才是交了；
`work_status` 和 `student_work_id` 在没交时也可能有值，不能拿来判）。进度按
**已完成 / 已发布**合并两类任务（`published_count` 才是分母，`unpublished_count`
不算；`online_learning.json` 对实训型课程全是 0，别用）。

作业链接用的是**教室里的作业页**：`/classrooms/{课程id}/shixun_homework/{作业id}/detail?tabs=0`
和 `/classrooms/{课程id}/common_homework/{作业id}/detail`。头歌页面自己给的
`/shixuns/{identifier}/shixun_exec` 是「进入实战」的动作地址，直接打开会渲染成
「404 您访问的页面已经离开地球」，别用它。

### 手动导入（最稳，推荐作为兜底）

抓取失败、平台没适配、线下课、教务系统——都可以手工喂进来：

```bash
studydash import --init              # 生成 data/manual.json 模板
studydash import D:\path\mine.json   # 或 .csv，然后直接渲染
```

JSON 里每门课可以带自己的 `platform` / `platform_name`，所以线下课也能打上
「教务系统」的标：

```json
{
  "courses": [
    {
      "id": "x1", "name": "形势与政策（线下）",
      "platform": "manual", "platform_name": "教务系统",
      "assignments": [
        { "id": "a1", "title": "课程论文提纲", "kind": "homework",
          "status": "pending", "due": "2026-11-01 23:59" }
      ]
    }
  ]
}
```

截止时间支持 `2026-11-01 23:59` / `2026-11-01` / `2026年11月1日` / `11-01 23:59` /
unix 时间戳。

### 平台适配器状态

| id | 平台 | 状态 | 鉴权 | 说明 |
| --- | --- | --- | --- | --- |
| `chaoxing` | 超星学习通 | stable | Cookie | 课程、进度、章节剩余任务点、作业/待办已验证（2026-10-20）；逐条任务点清单为实验性 |
| `icourse163` | 中国大学 MOOC | **beta** | 扫码 / Cookie | 课程、进度、作业/测验/考试已验证（2026-10-20）；成绩单与证书未接入 |
| `educoder` | 头歌 | **beta** | Cookie | 课程、实训 + 图文作业、截止时间与完成状态已验证（2026-10-20）；登录走「独立窗口」或「读浏览器」，没有扫码/账号密码 |
| `zhihuishu` | 智慧树 | **未验证** | Cookie | 同上 |
| `chaoxing_stat` | 超星学习统计 | **未验证** | Cookie | 学习次数、累计观看时长、任务点明细（stat2-ans 这一路数据，`chaoxing` 拿不到）；复用超星 Cookie，移植自第三方工具 `chaoxing_tool` 的只读查询部分，接口未经真实账号验证 |
| `manual` | 手动导入 | stable | 无 | JSON / CSV，可给任意平台打标 |
| `demo` | 离线演示 | stable | 无 | 内嵌样例，不联网 |

状态含义：

- **stable** — 不依赖外部平台，或有人用真实账号跑通过。
- **beta** — 主流程通过，附加功能可能被平台改版打断。
- **未验证** — 照接口形状写的，**字段可能与实际不符，请核对后再采信**。未验证的
  平台会在看板顶部挂一条提示，不会装作没问题。

`studydash platforms` 随时看这张表（含最近验证日期）。

---

## 命令

| 命令 | 作用 |
| --- | --- |
| *（不带子命令）* | 一键：有 Cookie 就抓、否则用已有数据或演示数据，渲染后打开浏览器 |
| **`login`** | **登录并把会话存起来**：默认**在终端画二维码**用手机扫；`--source password/live/browser/file/manual` 换来源；默认登录后直接抓取＋渲染＋打开 |
| `init` | 生成 `config.json`（已有则不覆盖，要重建加 `--force`） |
| `demo` | 内置样例数据 → payload → 渲染 |
| `platforms` | 列出所有适配器及其状态 |
| `probe [id]` | 只读探测接口可用性，原始响应对应存到 `data/raw/` |
| `fetch` | 用配置里的 Cookie 抓取各平台 |
| `import FILE` | 从 JSON / CSV 导入（`--init` 生成模板） |
| `build` | `data/progress.json` → `out/dashboard.html` |
| `open` | 用默认浏览器打开已生成的看板 |
| `serve --port 8787` | 本地起 http 服务预览 |
| `all [--demo]` | 抓取（或 demo）+ 渲染 |
| `config` | 打印配置（Cookie 已脱敏） |
| `doctor` | 自检：环境、工作区、模板、只读性扫描 |
| `writeback` | **把任务点补成已完成并上报给学习通**（实验性，只支持 `chaoxing`）。六类：音视频（签名上报播放位置）、阅读、文档、图书、直播。默认只演练，加 `--apply` 才真的发；`-k/--kind` 只做某一类（可重复），`--paced` 走「59 秒一段」逐段上报，`--limit N` 只做前 N 个，`--course` 按课名过滤，`-j/--workers N` 开 N 个线程并发上报（默认 1，`--paced` 时强制串行） |
| `quiz` | **章节测验自动答题**（实验性，只支持 `chaoxing`）。需要先在设置里填题库 token（言溪题库之类），`--cover-rate` 控制最低覆盖率，低于它只存不交；默认演练，`--apply` 才提交 |
| `learning` | **刷章节学习次数**（实验性，只支持 `chaoxing`）。`--times N` 每章刷几轮，每轮走 `studentstudyAjax` → `setlog` 两跳并带心跳，默认演练 |
| `version` | 版本与 payload schema 版本 |

通用参数：`--home DIR` 指定工作区，`-v/--verbose`，`-q/--quiet`，`--version`。

`login` 的完整参数：

| 参数 | 说明 |
| --- | --- |
| `--source qr\ password\ live\ browser\ file\ manual` | 会话来源，默认 `qr`（终端出二维码，手机扫） |
| `--user NAME` | `--source password` 的账号，不填就现场问 |
| `--password-stdin` | `--source password` 从标准输入读一行当密码（给脚本用；**别用命令行参数传密码**，那会进 shell 历史） |
| `--browser edge\ chrome` | `--source live/browser` 用哪个浏览器，默认 `edge` |
| `--profile NAME` | 指定配置档（`--source browser` 时用），默认取第一个 |
| `--file PATH` | `--source file` 时的 cookies.json / cookies.txt |
| `--cookie TEXT` | `--source manual` 时的整条 Cookie |
| `--platform ID` | 存到哪个平台名下，默认 `chaoxing` |
| `--label NAME` | 存进 `config.json` 时的账号名，默认自动（`默认（uid）`） |
| `--port N` | `--source live` 的调试端口，默认自动挑一个空闲的 |
| `--timeout N` | 最多等多少秒（`qr` 默认 240，`live` 默认 300） |
| `--no-check` | 跳过会话验证（不推荐） |
| `--no-save` | 不写 `config.json` |
| `--no-fetch` | 存完就停，不接着抓取渲染 |
| `--open` | 渲染完顺手打开浏览器 |

`probe` 输出的是**存档**（`data/raw/*.json` 里有请求记录与原始响应），用于平台改版时
定位解析问题，不是用来绕过限制的。

---

## 看板能看到什么

- **总览**：课程数、涉及平台、平均进度、待办数、已逾期、3 天内到期。
- **平台分布**：每个平台多少门课、平均进度、待办、逾期；取不到数据的平台会说明原因。
  点平台卡或筛选 chips 可以只看某个平台。
- **DDL 时间轴**：全部待办按 已逾期 / 3 天内 / 7 天内 / 以后 / 无截止 / 已完成 分组。
- **课程卡**：总百分比、分项进度条（任务点 / 作业 / 出勤…随数据源变化）、
  最近活跃、下一项截止、已完成项数，可展开作业明细（含分数与剩余天数）；
  超星课程还能展开「还差任务点的章节」，逐节显示还差几个。
- 搜索（课程 / 教师 / 标题）、排序、深浅色主题、键盘可操作、响应式。
- **静态单文件**：模板里已内嵌数据，`file://` 直接打开就能用，零 CDN、零外部请求。
  主题可用链接强制：`out/dashboard.html#dark`、`#light`。

---

## 数据结构与契约

前端消费的 payload 形状见 [`docs/payload.example.json`](docs/payload.example.json)
（带注释版样例）。顶层六个键：

```
meta      { version, generated_at, mode, readonly, theme, warnings[] }
overview  { courses, platforms, avg_progress, pending, overdue, due_soon, done }
platforms [{ id, name, short, status, status_label, color, courses, ... }]
timeline  [ Assignment ]   // 已按 due 升序
courses   [{ platform, name, progress_parts[], assignments[], ... }]
accounts  [{ platform, label, ok, message, fetched_at }]
```

`data/progress.json` 就是这个 payload，所以 `build` 单独跑得起来、可以手工编辑、
也可以拿它当别的工具的数据源。枚举：`kind` ∈ video/read/quiz/homework/exam/
discussion/live/other；`state` ∈ overdue/urgent/soon/later/none/done。

`courses[]` 里的超星课程还可能带一个可选的 `chapters[]`：每项是
`{ name, unit, sbar, depth, pending }`，只包含「还差任务点」的节（`pending` 是还剩
几个，不是总数）。没有章节数据时这个字段不出现。

`render` 注入 payload 时会转义 `<`、`&`、U+2028/U+2029，课程名里带 HTML 也注入不出去；
前端全部走 DOM API + `textContent`，不用 `innerHTML` 拼字符串。

---

## 写一个适配器

适配器是插件：放进 `src/studydash/providers/` 就自动被发现，或者打包成第三方包通过
`entry_points` 挂进来，**不用改本仓库一行代码**：

```toml
[project.entry-points."studydash.providers"]
my-university = "my_pkg.provider:CampusProvider"
```

最小实现（详见 [`docs/plugin.md`](docs/plugin.md)）：

```python
from studydash.model import Assignment, Course, Progress
from studydash.providers.base import Provider, ProviderMeta, register

@register
class CampusProvider(Provider):
    meta = ProviderMeta(
        id="campus", name="本校教务", short="教务",
        status="unverified", color="#f59e0b", auth="cookie",
        note="按接口形状写的，待真实账号验证",
    )

    def fetch(self) -> list[Course]:
        self.require_cookie()
        html = self.http.get_text("https://jw.example.edu/my/courses")
        ...
```

RPC 风格平台继承 `RpcProvider`，用 `endpoints` 声明 POST 白名单、`queries` 声明查询，
基类负责「逐个试、解析为空就换下一个、全空就告警」。

**三条自律**：只读；只收 Cookie 不收账号密码；失败要响——平台改版就让 `fetch` 抛
`ProviderError`，不要在 `meta` 里假装没事。

---

## 目录

```
studydash/
├── src/studydash/
│   ├── cli.py           命令行入口与子命令
│   ├── model.py         统一数据模型 + 派生计算（分类、排序、日期解析）
│   ├── http.py          唯一网络出口（只读白名单在这里）
│   ├── htmlx.py         极简 HTML 解析器（替掉 lxml 依赖）
│   ├── store.py         工作区、配置、模板查找、原子写
│   ├── aggregate.py     逐平台抓取 → 聚合 → payload
│   ├── render.py        注入 payload，转义（静态看板用）
│   ├── log.py           零依赖日志
│   ├── diagnostics.py   `doctor` 的自检项（CLI 与网页版共用一套判断）
│   ├── login.py         会话获取与校验（六种来源的调度层）
│   ├── qrlogin.py       超星扫码登录协议        ┐
│   ├── mooclogin.py     中国大学 MOOC 扫码登录   ├─ 让工具自己走完登录
│   ├── educoder_login.py 头歌会话校验/浏览器取会话 │
│   ├── passlogin.py     超星账号密码登录         │
│   ├── qr.py            零依赖二维码解码 + 终端渲染 │
│   ├── aes.py           纯 Python AES-128-CBC    ┘
│   ├── browsers.py      读浏览器 Cookie（DPAPI + AES-GCM）
│   ├── cdp.py           本机浏览器调试端口客户端（唯一允许直连的例外）
│   ├── writeback.py     任务点上报（唯一写操作，只在 `writeback --apply` 时跑）
│   ├── web/             网页版后端（FastAPI）
│   │   ├── app.py       应用装配、鉴权中间件、SPA 回退
│   │   ├── security.py  令牌与回环校验
│   │   ├── tasks.py     任务状态机 + 事件流
│   │   ├── operations.py 7 种任务的实现
│   │   ├── routers/     system / accounts / tasks / data
│   │   └── static/      前端构建产物（要进仓库）
│   ├── templates/dashboard.html   静态看板模板（单文件自包含）
│   └── providers/       适配器：chaoxing / chaoxing_stat / icourse163 / educoder / zhihuishu / manual / demo
├── web/                 Vue 3 + TypeScript 前端源码（Vite）
├── tests/               535 个用例（pytest）
├── docs/                payload 契约、web API 契约、插件文档、预览图
├── tools/               图标生成、PyInstaller 入口、前端/打包脚本、版本资源模板
├── assets/              icon.png / icon.ico（由 tools/make_icon.py 生成）
├── config.example.json  配置样例
└── pyproject.toml
```

> 从 v1（`chaoxing-dashboard`）升上来的话：仓库已改名，单文件 `dashboard.py` 被
> `src/studydash/` 包取代，旧的 `data/progress.json` schema 也换了，重新 `fetch`
> 或 `import` 一次即可。

---

## 设计取舍

**为什么是 Python。** 目标形态是「一个能双击运行的小工具 + 可 pip 安装的库 + 社区
能直接改的适配器」。Python 三样都顺：标准库自带 HTTP、HTML 解析、JSON、argparse；
PyInstaller 出单文件；适配器就是几十行的类，谁都能写一个 PR。

**为什么零第三方运行时依赖。** 单文件分发最怕原生依赖（`lxml` 的 wheel、
`cryptography` 的 DLL 之类），一是体积，二是杀软误报，三是 Python 版本一换就崩。
所以自带一个小 HTML 解析器（`htmlx.py`，基于 `html.parser`），不引 `requests`、
不引 `lxml`。代价是解析器不如 lxml 健壮——用宽松容错 + 正则兜底覆盖实际页面形状，
真遇到怪页面就 `probe` 存档再补规则。

**为什么 POST 白名单按地址判定。** 见开头「只读承诺」。

**为什么模板不引 CDN、不用构建工具。** 看板要能在没网的机器上、从 `file://`
直接打开，还要能当附件发给别人。所以模板是单个 HTML，全部内联，零外部请求。

---

## 打包

```bash
python -m pip install pillow pyinstaller
pwsh -File tools\build_web.ps1               # 先构建前端（npm ci + vite build）
pwsh -File tools\build_exe.ps1              # 默认同时出 onedir 和 onefile
pwsh -File tools\build_exe.ps1 -Mode onedir # 只出文件夹版
```

`build_web.ps1` 把 Vue 应用构建到 `src/studydash/web/static/`，**产物要进仓库**
（不然 `pip install` 完还得让用户自己装 Node）。`build_exe.ps1` 会先画图标（纯代码
绘制，无需外部素材），再用 `tools/entry.py` 打包，最后打印体积、SHA256 和版本资源。

> 打包踩坑：PyInstaller 一旦指定 `--specpath`，`--icon` / `--add-data` /
> `--version-file` 都会相对 **spec 所在目录**解析，必须传绝对路径，否则报
> `Unable to find ...\build\templates`。

## 测试

```bash
python -m pytest --ignore=tests/test_web.py   # 626 passed / 2 skipped
python -m pytest                               # 连网页版后端一起跑，需要 httpx
python -m ruff check .                         # 可选：装了 lint 依赖
```

`tests/test_web.py` 依赖 `httpx`（`starlette.testclient` 要求），只有装了 `[dev]` 才会跑；
没装时它会在 collect 阶段报错，所以上面给了 `--ignore`。

用例覆盖：数据模型与派生计算、HTML 解析器、只读网络层（含白名单拒绝路径）、
工作区与配置脱敏、聚合的失败隔离、渲染转义、适配器注册表、CLI 子命令契约、
纯 Python AES 与二维码编解码（含 FIPS-197 标准答案和「渲染→反解→逐格比对」往返）、
浏览器 Cookie 解密与 CDP 客户端、以及网页版后端（鉴权、任务状态机、SSE、
每个端点的字段契约）。

---

## 版本与发布

当前版本 **v3.0.0**，更新内容见 [CHANGELOG.md](CHANGELOG.md)。

**版本号只有一处源头**：`src/studydash/__init__.py` 的 `__version__`。
`pyproject.toml` 动态读它，前端 `web/package.json` 与 exe 的 Windows 版本资源都由
构建脚本填（`tools/version_info.tmpl.txt` 在打包时渲染成 `build/version_info.txt`）——
不再有「三处手改走样」的问题。

发版：

```bash
git commit -am "chore: 升到 v3.1.0"
git tag -a v3.1.0 -m "v3.1.0"
git push origin main v3.1.0
```

推 `v*` 标签会触发 [`.github/workflows/build-exe.yml`](.github/workflows/build-exe.yml)：
在 windows-latest 上跑测试 → 构建前端 → 打包两种产物 → 在干净目录做冒烟测试 →
挂到同名 Release。手动触发（workflow_dispatch）只出 artifact，不发 Release。

---

## 已知限制

- **`writeback --apply` 是本项目唯一的写操作，而且是实验性的**，只对 `chaoxing` 生效。
  它只补「任务点」：音视频报播放位置，阅读 / 文档 / 图书提交一次任务点接口，直播报时长；
  **不答题、不交作业、不签到**；默认不加 `--apply` 就只演练
  （打印每个任务点会被写成什么样，一个请求都不发）。`--paced` 会按平台真实播放节奏
  逐段发（每段 59 秒，`isdrag` 走 3 → 0 → 4），一个 40 分钟的视频要跑 40 分钟；不加
  `--paced` 就直接一次报到片尾。上报判定以平台回包为准：`isPassed=true` 才算完成，
  `false` 记「平台没确认」；播放位置已满却没过的那种直接跳过，不发无效请求。

- **平台有两种「上报也救不回来」的视频，工具会跳过并说明原因**：

  1. **开了人脸抓拍**（知识卡里 `videoFaceCaptureEnc` 非空）。一次报到片尾，平台只把
     `playTime` 记满，`isPassed` 恒为 `false`；此后任何变体（换 `isdrag`、补
     `videoFaceCaptureEnc`、加 `randomCaptureTime`、改成逐分钟上报）都还是 `false`。
     这类视频只能真人带摄像头看完。现在的做法是**一次请求都不发**，直接标 `skipped`，
     免得把它推到下面那个死态。
  2. **已经是死态的**：`playTime == attDuration` 却没判通过（大量是上面一类被推过之后的
     残局）。再上报不会带来任何增量，所以直接标 `stuck`，请求数 0，界面单独统计，
     不混进「未确认」。

  后果要说清楚：`stuck` 的比例取决于这门课有没有开人脸抓拍。纯视频课（没有
  `videoFaceCaptureEnc`、`playTime` 还是空的）能一次报满并立刻 `isPassed=true`；
  而像「石文化与宝玉石鉴赏」这种 88 个任务点全部带抓拍标记的课，自动化结果是 0/88，
  剩下只能手动看。死态的 `playTime` 平台不给回退，要课程方重置学习记录才能重来。

  2026-10 全库实测（22 门课 / 1194 个视频任务点）：通过 286、未开始可刷 726、
  带抓拍未看 56、**死态 125**。死态高度集中且与人脸抓拍强相关——
  「石文化与宝玉石鉴赏」87/88 死态、「大数据统计分析基础」38/39 死态，两门课占掉 125 里的
  全部，且这些死态节点的 `videoFaceCaptureEnc` **100% 非空**。对同一节点穷举过
  `playing_time` 从 0 到满递增、`videoFaceCaptureEnc` 传原值/空串/不传、
  `randomCaptureTime` 传 0/半程、`isdrag` 3/4、`--paced`、`--ramp` 等组合，全部
  `isPassed:false` 且课程计数器不动；播放器页 19 个脚本里也搜不到任何「重置学习记录」
  的接口（该能力在教师端）。结论：**死态只能由课程方在教师端重置，或换班级/重修后重来**；
  重置后用 `writeback --apply --ramp --force` 可批量补完（`--force` 才能越过
  带抓拍节点的 `skipped` 拦截）。

  `-j/--workers N` 能开 N 个线程同时上报多个任务点加速，代价是同一时刻有多个写请求
  打到平台，要不要这么做自己衡量。章节测验答题与刷学习次数已经作为独立模块实现
  （见 [写操作](#写操作实验性只对超星)），签到没有。
- **有两个适配器未验证**（`zhihuishu`、`chaoxing_stat`）。字段是照接口形状写的，可能对不上。
  看板会给它们挂提示，不会假装数据可信。欢迎拿 `probe` 的存档提 issue 修正。
- **`chaoxing_stat`（超星学习统计）是 `chaoxing` 的补充数据源**，不是替代：它单独占一个
  `id`，复用同一份超星 Cookie，从 `stat2-ans.chaoxing.com` 拿 `chaoxing` 拿不到的三样东西
  ——累计观看时长（分钟）、学习次数（按月）、逐条任务点明细。它是移植自第三方工具
  `chaoxing_tool` 的**只读查询部分**；原工具里伪造播放进度、代刷时长、账号密码登录那些
  写操作一律没有搬过来，因为 `providers` 层有 `ReadOnlyError` 闸门。接口没经过真实账号
  验证，改版后请用 `studydash probe chaoxing_stat` 留档。
  另外它的「学习次数」只能查**当月**（原工具把 `year=2023&month=03` 写死了，这里改成取
  当前年月），要看历史月份的分布得另开口子。
- **中国大学 MOOC 是 beta**：课程、进度、作业/测验/考试都已用真实账号验证，
  但成绩单、证书等外围数据还没接。接口改版时用 `studydash probe icourse163` 留档。
- **超星的章节剩余任务点**默认跟着课程页一起抓（第几章 → 第几节 → 还差几个），
  22 门课里 7 门有数据；逐条任务点清单（`options.chapters`）仍是实验性的。
  课程、进度与作业列表都已用真实账号验证（2026-10-19）；解析失败时它会把「响应多少
  字节、是不是登录页、有没有 `courseList`、里面几个 `<li>`」直接打出来，便于定位是
  Cookie 还是改版。
- **部分平台/课程只给总百分比**，不给分项任务点数，也不给 DDL。超星作业的 DDL 已经
  能读出来；其它来源拿不到的，仍可以靠手动导入补——把散落各处的 deadline 拢到一处，
  本身就是这个工具最值钱的部分。
- **静态看板**：数据在渲染时内嵌，刷新页面不会重新抓取；更新要重跑 `build`/`fetch`。
  网页版没有这个问题（每次都读最新的 `data/progress.json`）。
- **网页版只能本机访问**。想从手机看，用 `studydash serve`——它只发布静态看板，
  不发布会话。这是刻意的取舍，不是没做。
- exe 未做代码签名，SmartScreen 可能提示未知发布者；未适配 macOS/Linux 的打包。

## 隐私

`config.json` 含登录 Cookie，`data/` 含你账号的页面原文，`out/` 含课程名与 uid。
三者都在 `.gitignore` 里，绝不提交。`studydash config` 输出默认脱敏，
`Config.to_dict(redact=True)` 会把 cookie/token/password/secret/authorization
换掉。分享看板 HTML 前留意它内嵌了你的课程与账号信息。

**关于登录**（`studydash login`）：

- **扫码和账号密码这两条路不碰任何浏览器数据。** 二维码是工具直接向超星要的，画在
  终端上，只包含一次性的登录票据；你在手机上点确认之前，程序手上没有任何凭据。
- **不存密码。** `--source password` 的密码走 `getpass`（不回显、不进 shell 历史、
  不用命令行参数），只在本进程内存里存到发出去为止，随后按超星的协议做一次
  AES-128-CBC 加密发出；**不落盘、不写日志**。`config.json` 里永远只有会话 Cookie。
- **不代过验证码和二次验证。** 平台要求这些时程序会明确拒绝并建议改用扫码，
  而不是去猜、去绕。

**关于网页版**（`studydash web` / 双击 exe）：

- 服务**只监听 `127.0.0.1`**，并逐请求校验来源是回环；`--host` 传别的地址直接拒绝启动。
- 令牌每次启动重新生成、只注入到你打开的那个页面里；**不用 Cookie 会话**，所以没有
  CSRF 面。别的网站既读不到这个令牌，也没法带自定义头跨域请求。
- 账号列表接口只回 `cookie_hint`（形如 `<已设置 412 字符>`），**任何接口都不返回
  Cookie 的值**。
- 密码登录走任务通道：密码读完立刻从任务选项里删掉，不进 `/api/tasks` 的响应、
  不进日志、不落盘。
- 关掉浏览器标签不会留下后台服务吗？——不会。服务跟着你启动 `studydash web` 的那个
  进程活着，Ctrl+C（或关掉那个窗口）就结束。

**关于读浏览器 Cookie**（`studydash login` 的 `live` / `browser` 两种来源）：

- 必须你在命令行点名浏览器才会读，**没有后台扫描**。`--source live` 开的是
  一份独立配置档（`~/.studydash/browser`），跟你的日常浏览器完全隔离。
- 默认只挑 `*.chaoxing.com` / `*.chaoxing.cn` 的 Cookie，其他站的**不取**。
- 读的是数据库的**副本**（先复制到临时目录再只读打开），不修改浏览器的任何数据。
- 解密用的是 Windows 的 DPAPI：密钥由**你的登录凭据**保护，所以这份数据只有在你
  自己的用户会话里才能解开——换个用户或换台机器就是一堆密文。
- Cookie 值只出现在内存和 `config.json` 里，**不写日志**（`Cookie.as_dict()` 故意只给
  长度不给值）。`--verbose` 也不会把值打出来。
- Chrome 127+ 的 app-bound 加密（`v20`）读不了，会明确报错并给出替代路径，不会
  悄悄失败。
- `--source live` 用的那份配置档会留着你登录后的会话（这正是「下次不用重登」的原因）。
  想清掉就删掉 `~/.studydash/browser`。`config.json` 里的 Cookie 已经是完整会话，
  不依赖它。

一切都在本地：程序不向任何第三方服务器发送数据，也不上传你的进度。唯一的外部流量
是去各平台读你自己的课程页面——和你自己打开浏览器看是同一件事。

## 相关文档

| 文档 | 内容 |
| --- | --- |
| [`docs/handoff.md`](docs/handoff.md) | 交接文档：架构、模块职责、平台协议细节、踩过的坑与硬约束 |
| [`docs/web-api.md`](docs/web-api.md) | 本地 HTTP API 契约（路由、请求体、SSE 事件、错误码） |
| [`docs/payload.example.json`](docs/payload.example.json) | 看板 payload 带注释样例 |
| [`docs/plugin.md`](docs/plugin.md) | 写一个适配器（provider 接口） |
| [`CHANGELOG.md`](CHANGELOG.md) | 版本变更 |
| [`config.example.json`](config.example.json) | 配置模板 |

## License

[MIT](LICENSE)

