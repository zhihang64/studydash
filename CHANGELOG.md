# 更新日志

本文件格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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

[1.0.0]: https://github.com/zhihang64/chaoxing-dashboard/releases/tag/v1.0.0
