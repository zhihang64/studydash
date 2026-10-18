# 更新日志

本文件格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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

[2.0.0]: https://github.com/zhihang64/studydash/releases/tag/v2.0.0
[1.0.0]: https://github.com/zhihang64/chaoxing-dashboard/releases/tag/v1.0.0
