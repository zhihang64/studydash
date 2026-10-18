# 写一个 studydash 适配器

适配器（provider）是插件：把平台的数据读出来，翻译成 studydash 的统一模型，交给聚合层。
它**不用改本仓库一行代码**——放进 `src/studydash/providers/` 会被自动发现，打包成第三方包
也能通过 `entry_points` 挂进来。

前提：Python ≥ 3.10（用到 `X | None` 和 `match` 之外的现代语法），运行时零第三方依赖。

---

## 三条自律

这三条不是风格建议，是审查 PR 时会盯的东西。

1. **只读。** 适配器能做的只有「把数据读出来看看」。不提交作业，不交卷，不答题，不刷
   视频，不签到，不上报学习进度。网络出口统一走 `self.http`，而 `http.py` 在地址层面
   拦写操作（见下面「客户端能力」）。
2. **只收 Cookie，不收账号密码。** 用户在浏览器里登录，把 Cookie 复制进配置。适配器永远
   拿不到、也不需要明文密码——这既是隐私，也是职责边界：登录流程留给平台自己。
3. **失败要响。** 平台改版就让 `fetch` 抛 `ProviderError`，**不要**悄悄 `return []`。
   空列表和「这个账号真的没选课」在界面上长得一模一样，用户会以为是自己没课，而不是
   「快去更新适配器」。抛出去，`aggregate()` 会把它收成 `Account(ok=False, message=...)`，
   看板上红着显示。

---

## 最小适配器（HTTP + HTML）

假设本校教务有一个课程列表页，登录态靠 Cookie：

```python
# src/studydash/providers/campus.py
from __future__ import annotations

from ..htmlx import parse
from ..model import Assignment, Course, Progress, parse_dt
from .base import AuthError, Provider, ProviderError, ProviderMeta, register

COURSE_LIST = "https://jw.example.edu/my/courses"


@register
class CampusProvider(Provider):
    meta = ProviderMeta(
        id="campus",
        name="本校教务",
        short="教务",
        status="unverified",          # 还没人用真实账号跑通过
        color="#f59e0b",
        auth="cookie",
        homepage="https://jw.example.edu",
        note="课程列表已验证；作业字段待核对",
    )

    def fetch(self) -> list[Course]:
        self.require_cookie()                       # 没配 Cookie → AuthError，带操作提示

        html = self.http.get(COURSE_LIST).text
        if self.looks_logged_out(html):             # 前 6000 字符里找登录页特征
            raise AuthError(
                f"{self.meta.name} Cookie 已失效",
                hint="重新在浏览器登录后复制 Cookie，填到 config.json 的 cookie 字段。",
            )

        courses = []
        for row in parse(html).findall("tr", cls="course-row"):
            cid = row.get("data-course-id", "").strip()
            name = row.find_text("a", cls="title")
            if not cid or not name:
                continue

            done = row.get("data-done", "0")
            total = row.get("data-total", "0")
            progress = []
            if total.isdigit() and int(total) > 0:
                progress.append(
                    Progress(label="任务点", done=int(done or 0), total=int(total))
                )

            courses.append(
                Course(
                    id=cid,
                    name=name,
                    platform=self.meta.id,          # 必须等于 meta.id
                    teacher=row.find_text("span", cls="teacher"),
                    url=f"{COURSE_LIST}/{cid}",
                    progress=progress,
                    assignments=self._assignments(row, cid),
                )
            )

        if not courses:
            raise ProviderError(
                "课程列表页解析出 0 门课",
                hint="页面结构可能已改版：跑 `studydash probe campus` 存下原始响应，"
                     "再按它调整选择器，或附到 issue 里。",
            )
        return courses

    def _assignments(self, row, course_id: str) -> list[Assignment]:
        items = []
        for cell in row.findall("li", cls="hw"):
            title = cell.get("data-title", "")
            if not title:
                continue
            items.append(
                Assignment(
                    id=cell.get("data-id", "") or title,
                    title=title,
                    platform=self.meta.id,
                    course_id=course_id,
                    kind="homework",
                    status="submitted" if cell.has_class("done") else "pending",
                    due=parse_dt(cell.get("data-due", "")),
                )
            )
        return items

    def probe_plan(self) -> list[tuple[str, str]]:
        # probe 命令会按这个清单逐个请求并留档，方便改版时定位
        return [("课程列表页", COURSE_LIST)]
```

跑：

```bash
studydash probe campus      # 先确认能通，原始响应对应存到 data/raw/
studydash platforms         # 应该看到 campus 在列表里
studydash fetch             # 抓取 + 聚合
studydash build --open
```

---

## 解析 HTML：`htmlx`

`studydash.htmlx` 是自带的小解析器（基于标准库 `html.parser`），替代 `lxml`，所以别指望
它有 XPath。核心是 `Node`：

| 用法 | 说明 |
| --- | --- |
| `parse(html)` | 返回根节点（`#document`）。容错：未闭合标签自动收，多余闭合忽略。 |
| `node.get(name, default=None)` | 读**这个节点自己**的属性。 |
| `node.attr_text(attr, tag=None, cls=None, id=None, default="", **attrs)` | 在**后代**里找第一个匹配的节点，读**它的** `attr`。 |
| `node.findall(tag=None, cls=None, id=None, **attrs)` | 匹配的所有后代。`tag` 可以传字符串或字符串集合。 |
| `node.find(...)` / `node.find_text(...)` | 第一个匹配节点 / 它的文本。 |
| `node.text` / `node.all_text` | 直接子文本 / 递归全部文本。 |
| `node.has_class(name)` / `node.classes` / `node.id` / `node.href` | 常用属性快捷方式。 |
| `node.children_by_tag("li")` | 直接子节点里指定标签。 |
| `node.json_attr("data-json")` | 读属性并 `json.loads`，失败返回 default。 |

⚠️ **`iter_nodes()` 不含节点自己**，所以 `find*` / `attr_text` 只往后代找。
要读节点自身的属性用 `node.get(...)`——这是个容易踩的坑：`<tr data-id="7">` 上调用
`row.attr_text("data-id")` 会返回 `""`，因为 `<tr>` 自己不在遍历范围里。

模块级还有三个正则小工具：`regex_first(pattern, text, group=1, default="")`、
`regex_all(pattern, text, group=1)`、`strip_tags(html)`。页面里数据嵌在
`<script>var mArg = {...}</script>` 这种地方时，正则比解析器好用——超星适配器就是这么干的。

---

## `ProviderMeta` 字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | ✅ | 配置里用的平台标识。**只能小写字母/数字/连字符/下划线**，且必须以小写字母开头。构造时就校验，非法直接 `ValueError`。 |
| `name` | ✅ | 界面上显示的全名，如「超星学习通」。 |
| `short` | ✅ | 徽标 / chips 里的短名，如「超星」，建议 ≤ 4 个汉字。 |
| `status` | — | `stable` / `beta` / `unverified` / `broken`，默认 `beta`。取值非法会报错。 |
| `color` | — | 平台色，形如 `#3b82f6`。 |
| `auth` | — | `cookie`（默认）/ `none`。 |
| `homepage` | — | 平台主页，看板上可点。 |
| `verified` | — | **最近一次由真人用真实账号验证通过的日期**（`YYYY-MM-DD`）。空 = 从没验证过。 |
| `note` | — | 一句话说明限制或已知问题，会显示在平台卡上。 |

**关于 `status`，请对自己诚实**：`stable` 意味着「有人用真实账号端到端跑通过」。
照接口形状猜出来、没实测的适配器写 `stable`，比写 `unverified` 伤害大得多——看板会给
`unverified` 的平台挂一条顶部提示，让用户知道数据要核对；写 `stable` 就什么提示都没有，
用户会直接采信可能错的字段。

`id` 冲突（和内置或别的已注册适配器撞名）会在启动时报错。注册表拒绝 `_` 开头的 id，
那是留给抽象基类的。

---

## 统一模型

从 `studydash.model` 导入。**所有时间字段都传 `datetime` 或可被 `parse_dt` 解析的值**
（`2026-11-01 23:59` / `2026-11-01` / `2026年11月1日` / `11-01 23:59` / unix 时间戳 /
毫秒时间戳），解析失败返回 `None` 而不是抛异常——宁可少一个 DDL，不要整张看板崩掉。

### `Course`

```python
Course(
    id="12345",                 # 平台内的课程 id
    name="数据结构与算法",
    platform="campus",          # 必须等于 meta.id
    teacher="张明",
    term="2025-2026-2",         # 学期
    url="https://...",          # 课程主页，看板上可点
    progress=[Progress(...)],   # 分项进度，可多条
    assignments=[Assignment(...)],
    last_active=parse_dt("2026-11-14 09:30"),   # 最近一次活跃，可空
    extra={"platform_name": "本校教务"},          # 可选；platform_name 会覆盖平台显示名
)
```

不依赖 `progress` 也能出百分比：没有 `progress` 时会退回用作业完成率。两者都没有，
看板会如实显示「无数据」。

### `Progress`

```python
Progress(label="任务点", done=34, total=51)     # → 66.7%，看板上一条进度条
```

`label` 是**给你自己看的**分项名，可以按平台习惯起：`任务点` / `作业` / `课时` / `测验` /
`出勤`。`total <= 0` 时该项目不产生进度条。

### `Assignment`

```python
Assignment(
    id="hw-3",                  # 平台内唯一即可
    title="第三周 单元作业",
    platform="campus",          # 必须等于 meta.id
    course_id="12345",
    course="数据结构与算法",      # 课程名，看板时间轴上要显示
    kind="homework",            # 见下
    status="pending",           # 见下
    due=parse_dt("2026-11-14 23:59"),
    score=None,                 # 得分
    max_score=10,               # 满分
    url="https://...",          # 作业页
    detail="测验 · 第 3 周",     # 一句话补充
)
```

`kind` 取值：`video` `read` `quiz` `homework` `exam` `discussion` `live` `other`。
`status` 取值：`pending` `submitted` `graded` `overdue` `unknown`。
**猜不准就用 `other` / `unknown`**，别硬塞。

`state`（逾期/紧急/即将/以后/无截止/已完成）不用自己算，聚合层会调 `classify_all()`
统一填。`url` 填了看板上作业标题就是可点的。

---

## RPC 风格平台（慕课、智慧树这类）

这类平台的接口是「POST 到某个 `*.rpc` / 服务端点，返回 JSON」，没有稳定的字段文档，
结果还可能包在 `{"result": {"list": [...]}}` 或 `{"data": [...]}` 里。继承 `RpcProvider`：

```python
from typing import Any, ClassVar

from ..model import Course
from ..providers.rpc import RpcProvider, find_lists
from .base import ProviderMeta, register


@register
class MyRpcProvider(RpcProvider):
    meta = ProviderMeta(id="myrpc", name="某平台", short="某平台", status="unverified")

    # POST 白名单：只有这里列出的地址前缀允许 POST，其余一律 ReadOnlyError
    endpoints: ClassVar[tuple[str, ...]] = (
        "https://api.example.com/web/j/courseBean.getMyCourse.rpc",
    )

    # 依次尝试，前一个返回空就试下一个（接口改版时换名字很常见）
    queries: ClassVar[tuple[tuple[str, str], ...]] = (
        ("我的课程", "https://api.example.com/web/j/courseBean.getMyCourse.rpc"),
        ("我的课程(备用)", "https://api.example.com/web/j/courseBean.getMyCourseV2.rpc"),
    )

    def query_form(self, url: str) -> dict[str, Any]:
        return {"pageIndex": 1, "pageSize": self._page_size(), "csrfKey": self._csrf()}

    def parse(self, note: str, url: str, payload: Any) -> list[Course]:
        courses = []
        for raw in find_lists(payload, ("list", "courseList", "result")):
            for item in raw:
                if isinstance(item, dict) and item.get("id"):
                    courses.append(self._course(item))
        return courses
```

`RpcProvider` 替你做了这些：

- `client_kwargs()` 自动把 `endpoints` 交给 `HttpClient` 当 POST 白名单（**你必须声明，
  基类不会猜**）。
- `fetch()` 按 `queries` 逐个 `post_query_json`，`None` 就换下一个；解析异常只 warn 不崩；
  第一个解析出课程的候选胜出。
- `needs_cookie = True`（默认）时先 `require_cookie()`。**故意不捕 `AuthError`**：Cookie
  过期必须显式报错，不能退化成「0 门课」。
- 全部候选都空 → 打一条带 `probe` 指引的 warn。
- `probe_plan()` 自动生成 POST 四元组。

辅助函数：

- `find_lists(obj, keys)` — 深度找出「键名命中 `keys` 且值是 list」的列表。
- `walk_dicts(obj)` — 深度产出所有 dict。
- `first_value(obj, keys, default=None)` — 按候选键名取第一个非空值。

**关于 `csrfKey` / `_csrf()`**：慕课这类平台要求表单里带一个从 Cookie 里能取到的
csrf 值（`options.csrf_key` 优先，其次扫 Cookie）。它只是防跨站请求的参数，**不是**在
绕鉴权。

---

## `probe_plan()` 与 `probe` 命令

平台改版时，维护者最需要的是**原始响应**里字段长什么样。所以每个适配器都应声明自己
依赖的端点：

```python
def probe_plan(self) -> list[tuple]:
    return [
        ("登录态自检", "https://api.example.com/user/me"),
        ("课程列表", "https://api.example.com/web/j/courseBean.getMyCourse.rpc",
         "POST", {"pageIndex": 1, "pageSize": 50}),
    ]
```

`studydash probe <id>` 会逐个请求，把请求记录与原始响应存进 `data/raw/`，并打印一份
摘要。**Cookie 在存档里会被脱敏**，可以安全贴进 issue——社区贡献者靠这个让维护者不用
自己的账号也能修适配器。

POST 条目必须同时在 `endpoints` 白名单里，否则 probe 自己就会被 `ReadOnlyError` 挡下来。

---

## 客户端能力（`self.http`）

`HttpClient` 是全项目**唯一**的网络出口。别的地方（包括你的适配器）直接 `import requests`
或 `urllib` 会被 `studydash doctor` 扫出来。

| 方法 | 说明 |
| --- | --- |
| `get(url, params=None, *, headers=None, note="")` | 返回 `Response`。默认带 Cookie、UA、Referer。 |
| `get_text(url, ..., default="")` | 失败时返回 `default` 而不抛——用于「有更好，没有也行」的可选请求。 |
| `get_json(url, ..., default=None)` | 同上，返回解析后的对象。 |
| `post_query(url, data, *, headers=None, note="")` | POST 一个**查询**。地址不在白名单里 → `ReadOnlyError`。 |
| `post_query_json(url, data, ..., default=None)` | 同上，吞异常返回 `default`。 |

`Response` 有 `url` / `status` / `headers` / `content` / `text` / `.json()`。
编码依次试 `Content-Type` 里的 charset → `utf-8` → `gb18030` → `latin-1`，中文页面
（超星有些接口是 GBK）不用自己处理。

**只读是分两层做的：**

- `READ_METHODS = ("GET", "POST")` —— 除了这两个，`PUT`/`DELETE`/`PATCH` 一律
  `ReadOnlyError`。GET 可以打任何地址。
- POST 只允许打到 `query_endpoints` 里的前缀，而且 `validate_query_endpoints()` 会**直接
  拒绝**地址里含写动作词的端点（`submit` / `save` / `answer` / `commit` / `upload` /
  `create` / `delete` / `update` / `modify` / `doWork` / `doHomeWork` / `finish` /
  `report` / `sign` / `checkin` / `signin` / `submitWork` 等，见
  `http.py:_FORBIDDEN_IN_POST_ENDPOINT`）。

所以 **声明白名单时不要试图往里面塞提交类地址——程序会在启动时拒绝。** 这是有意设计的。

`HttpSettings` 的默认值刻意保守（`timeout=8.0`、`delay=0.4`、`retries=2`）——慢一点，
别给人家服务器添麻烦。适配器不要私自调快。

---

## 异常

从 `.base` 导入：

| 异常 | 什么时候抛 |
| --- | --- |
| `ProviderError(message, *, hint="")` | 适配器层面的失败。`hint` 是给用户看的下一步操作，会被显示出来。 |
| `AuthError(ProviderError)` | 未配置 / 已失效的登录态。`require_cookie()` 与 `looks_logged_out()` 帮你抛。 |
| `RedirectedError(AuthError)` | 期望 `allow_redirects=False` 却没跟上跳转（典型：登录态过期被踢到登录页）。 |

`looks_logged_out(html)` 是现成的启发式：在前 6000 字符里找
`passport` / `fanyalogin` / `用户登录` / `请先登录` / `login.` / `账号登录`，且页面里
**没有** `logout`。

---

## 注册与分发

### 放进本仓库

丢进 `src/studydash/providers/`，以 `@register` 装饰类即可。`discover()` 会
`pkgutil.iter_modules` 扫这个包，导入失败的模块只打一条警告，不拖垮整个程序。

### 作为第三方包

```toml
# pyproject.toml
[project]
name = "studydash-campus"
dependencies = ["studydash>=2.0"]

[project.entry-points."studydash.providers"]
campus = "studydash_campus:CampusProvider"
```

组名是 `studydash.providers`（`ENTRY_POINT_GROUP`）。装好后 `studydash platforms` 就能
看到它，模块名和类名都随意。

> 打包成 exe 时要注意：PyInstaller 的静态分析看不到运行时 import，所以**内置适配器由
> `providers/__init__.py` 末尾的显式 eager import 块负责登记**；第三方包的 entry_points
> 在冻结环境里也不可用。这是分发形态的固有限制，不是适配器的锅。

---

## 调试排查

| 症状 | 多半是 |
| --- | --- |
| 平台卡上「0 门课」 | `_parse_*` 的选择器/键名对不上，或页面结构改版了。跑 `probe` 存原始响应再看。 |
| 顶部挂「Cookie 已失效」 | 登录态过期，或 Cookie 复制时少了分号后的项。 |
| `ReadOnlyError: 拒绝把 ... 当查询端点` | `endpoints` 里塞了带写动作词的地址。改地址，或换一个真正的查询端点。 |
| `ReadOnlyError: 未声明的查询端点` | 忘了在 `client_kwargs()` / `endpoints` 里声明这个前缀。 |
| 启动时 `ProviderMeta.id 只允许...` | id 里有大写、空格或别的符号。 |
| 启动时 `适配器 id 冲突` | 和内置适配器撞名了，换一个。 |
| 时间字段全空 | `due` / `last_active` 传了 `parse_dt` 认不出的格式；先用 `parse_dt` 在 REPL 里试一下。 |

`-v` 打开调试日志：`studydash -v fetch`。

---

## 测试你的适配器

用假响应测，**不要联网**。仓库里 `tests/` 有现成模式：把 `HttpClient.get` 打桩，
喂一段保存下来的 HTML / JSON，断言解析出的 `Course` 字段。

至少要覆盖：

- Cookie 缺失 → `AuthError`（不是空列表）。
- 登录页 HTML → `AuthError`。
- 页面结构对不上（解析出 0 门课）→ `ProviderError`。
- 正常响应 → 课程数、进度、作业的 `kind`/`status`/`due` 正确。
- `parse_dt` 认不出的截止时间 → `due is None`，其余字段照常。

```bash
python -m pytest tests/test_providers.py -q
```

---

## 提 PR 的检查清单

- [ ] 只读：没有任何提交 / 交卷 / 答题 / 刷课 / 签到 / 上报进度调用。
- [ ] 只收 Cookie，不接收账号密码字段。
- [ ] `status` 与 `verified` 如实填写；没实测过就写 `unverified`、`verified=""`。
- [ ] 失败抛 `ProviderError`（带 `hint`），不静默 `return []`。
- [ ] `probe_plan()` 声明了所有依赖端点。
- [ ] 网络只走 `self.http`；`studydash doctor` 的只读扫描干净。
- [ ] 有单测，且用固定响应不联网。
- [ ] `python -m pytest` 全绿。
- [ ] 截图或 `probe` 存档能说明字段来源（贴到 PR 描述里）。

有问题直接开 issue，附上 `studydash probe <id>` 的存档和 `crash.log`（如果有）。
