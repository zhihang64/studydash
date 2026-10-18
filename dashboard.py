#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""学习进度聚合看板（只读）。

把你自己在各门课程里的学习进度读出来，聚合成一个本地 HTML 看板：
整体进度、待完成任务点、临近截止、进度落后预警。

设计约束
--------
1. **严格只读**：fetch 只发出 HTTP GET 去读课程列表页。本文件里不存在任何
   进度上报 / 任务点提交 / 签到 / 答题 之类的写接口调用，也没有相关代码路径。
2. **零第三方依赖**：只用标准库，不需要 pip install。
3. **数据来源可替换**：抓取层与渲染层解耦。抓不到时可 `import --html` 从浏览器
   另存的页面离线转换，或直接手工编辑 data/progress.json。

子命令
------
  demo                     用内置演示数据生成 data/progress.json（无需登录）
  probe                    只读探测接口可用性，原始响应存到 data/raw/
  fetch                    用你自己的 Cookie 只读抓取，生成 data/progress.json
  import --html FILE       从浏览器另存的课程列表页面离线转换
  build [--data FILE]      渲染 out/dashboard.html
  serve [--port 8787]      本地预览 out/
  all                      demo/fetch + build
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

__version__ = "1.0.0"

SCHEMA = 1
FROZEN = bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """配置、data/、out/ 所在目录；打包成 exe 后就是 exe 所在目录。"""
    if FROZEN:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    """随程序分发的只读资源目录；打包后是 PyInstaller 的临时解包目录。"""
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", app_dir()))
    return Path(__file__).resolve().parent


ROOT = app_dir()


def find_template() -> Path:
    """先在 exe 旁边找（方便用户自定义），再回落到打包进去的那份。"""
    for base in (ROOT, resource_dir()):
        candidate = base / "templates" / "dashboard.html"
        if candidate.exists():
            return candidate
    raise SystemExit("找不到 templates/dashboard.html（exe 同目录或程序内都没有）")


DEFAULT_CONFIG: dict = {
    "cookie": "",
    "timeout": 15,
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    # 学期区间，用于算"时间进度 vs 学习进度"的落差预警；留空则不显示该预警。
    "term": {"name": "", "start": "", "end": ""},
    "endpoints": {
        # 只读接口。登录态自检用个人空间设置页（未登录会 302）。
        "login_check": "https://i.chaoxing.com/base/settings",
        "course_list": (
            "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courses/list"
            "?rss=1&start=0&size=500&catalogId=0&searchname="
        ),
    },
}

CATEGORIES = ["video", "read", "document", "discussion", "quiz", "other"]
CATEGORY_LABEL = {
    "video": "视频",
    "read": "阅读",
    "document": "文档",
    "discussion": "讨论",
    "quiz": "测验",
    "other": "其他",
}
KIND_LABEL = CATEGORY_LABEL

VERBOSE = False


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(msg, flush=True)


def vlog(msg: str) -> None:
    if VERBOSE:
        print("  · " + msg, flush=True)


def warn(msg: str) -> None:
    print("! " + msg, file=sys.stderr, flush=True)


def now() -> datetime:
    return datetime.now().astimezone()


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def fnum(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def inum(value, default: int = 0) -> int:
    return int(round(fnum(value, default)))


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: Path | None = None) -> dict:
    path = path or (ROOT / "config.json")
    if not path.exists():
        vlog(f"未找到 {path.name}，使用默认配置")
        return deep_merge(DEFAULT_CONFIG, {})
    try:
        return deep_merge(DEFAULT_CONFIG, json.loads(read_text(path)))
    except json.JSONDecodeError as exc:
        warn(f"{path} 不是合法 JSON（{exc}），改用默认配置")
        return deep_merge(DEFAULT_CONFIG, {})


# --------------------------------------------------------------------------- #
# 只读 HTTP
# --------------------------------------------------------------------------- #
def decode(raw: bytes, headers) -> str:
    charset = None
    try:
        charset = headers.get_content_charset()
    except Exception:
        pass
    for enc in (charset, "utf-8", "gbk", "latin-1"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def http_get(url: str, cfg: dict) -> tuple[int, dict, bytes]:
    """发出一个 GET 请求（本程序唯一的网络动作）。"""
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", cfg["user_agent"])
    req.add_header("Accept", "text/html,application/json,*/*")
    req.add_header("Accept-Language", "zh-CN,zh;q=0.9")
    if cfg.get("cookie"):
        req.add_header("Cookie", cfg["cookie"].strip())
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as resp:  # noqa: S310
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()
    except urllib.error.URLError as exc:
        raise SystemExit(f"网络请求失败：{url}\n  {exc.reason}") from exc


# --------------------------------------------------------------------------- #
# 解析：课程列表页 -> 规范化课程
# --------------------------------------------------------------------------- #
RE_COURSE_ID = re.compile(r"course[Ii]d=(\d+)")
RE_CLAZZ_ID = re.compile(r"cla[zs]{2}[Ii]d=(\d+)")
RE_CPI = re.compile(r"cpi=(\d+)")
RE_PERCENT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
RE_TAG = re.compile(r"<[^>]+>")
RE_LI_BLOCK = re.compile(r"<li\b[^>]*>(.*?)(?=<li\b|</ul>|\Z)", re.S | re.I)
RE_COURSE_LIST = re.compile(
    r"<ul\b[^>]*id=[\"']courseList[\"'][^>]*>(.*?)</ul>", re.S | re.I
)


def strip_tags(html: str) -> str:
    text = RE_TAG.sub(" ", html)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
    )
    return re.sub(r"\s+", " ", text).strip()


def clean_name(text: str) -> str:
    text = strip_tags(text)
    text = re.sub(r"^(课程|课程名称)[:：]\s*", "", text)
    return text.strip(" -|·")[:120]


def parse_json_course_list(text: str) -> list[dict] | None:
    """有些部署返回 JSON；递归找出形似课程对象的字典。"""
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None

    found: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            keys = {str(k).lower() for k in node.keys()}
            if keys & {"courseid", "course_id", "clazzid", "clazz_id"}:
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(obj)
    return found or None


def course_from_json(item: dict) -> dict:
    def pick(*names, default=""):
        lowered = {str(k).lower(): v for k, v in item.items()}
        for name in names:
            if name.lower() in lowered and lowered[name.lower()] not in (None, ""):
                return lowered[name.lower()]
        return default

    course_id = str(pick("courseId", "course_id", "id", default=""))
    class_id = str(pick("clazzid", "clazzId", "class_id", default=""))
    name = clean_name(str(pick("courseName", "course_name", "name", "title")))
    teacher = clean_name(str(pick("teacher", "teacherName", "personName")))
    percent = fnum(pick("progress", "percent", "schedule", default=0))

    progress = new_progress()
    if percent:
        progress["other"] = {"done": percent, "total": 100, "unit": "percent"}
    return make_course(course_id, class_id, name, teacher, progress)


def parse_html_course_list(text: str) -> list[dict]:
    scope = RE_COURSE_LIST.search(text)
    body = scope.group(1) if scope else text
    if not scope:
        vlog("未找到 <ul id='courseList'>，回退到全文扫描 <li>")

    courses: list[dict] = []
    for match in RE_LI_BLOCK.finditer(body):
        block = match.group(1)
        cid = RE_COURSE_ID.search(block) or RE_COURSE_ID.search(text)
        if not cid:
            continue
        clz = RE_CLAZZ_ID.search(block)
        cpi = RE_CPI.search(block)

        # 课程名：优先 title 属性，其次第一个链接文本
        title = ""
        for pattern in (
            r"<a\b[^>]*title=[\"']([^\"']+)[\"']",
            r"<span\b[^>]*class=[\"'][^\"']*course[^\"']*name[^\"']*[\"'][^>]*>(.*?)</span>",
            r"<h3\b[^>]*>(.*?)</h3>",
        ):
            found = re.search(pattern, block, re.S | re.I)
            if found:
                title = found.group(1)
                break

        teacher = ""
        found = re.search(
            r"(?:讲师|教师|授课教师)\s*[:：]?\s*([^<\s][^<]{0,40}?)\s*<", block
        )
        if found:
            teacher = found.group(1)

        percent_match = RE_PERCENT.search(block)
        progress = new_progress()
        if percent_match:
            progress["other"] = {
                "done": fnum(percent_match.group(1)),
                "total": 100,
                "unit": "percent",
            }

        courses.append(
            make_course(
                cid.group(1),
                clz.group(1) if clz else "",
                clean_name(title),
                clean_name(teacher),
                progress,
                cpi=cpi.group(1) if cpi else "",
            )
        )

    # 去重（同一课程可能出现多次）
    seen, uniq = set(), []
    for course in courses:
        key = (course["id"], course.get("class_id", ""))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(course)
    return uniq


# --------------------------------------------------------------------------- #
# 规范化数据结构
# --------------------------------------------------------------------------- #
def new_progress() -> dict:
    return {key: {"done": 0, "total": 0} for key in CATEGORIES}


def make_course(
    course_id: str,
    class_id: str,
    name: str,
    teacher: str,
    progress: dict | None = None,
    *,
    cpi: str = "",
    term: str = "",
    url: str = "",
    last_study_at: str = "",
    deadlines: list | None = None,
) -> dict:
    base = new_progress()
    for key, value in (progress or {}).items():
        if key not in base or not isinstance(value, dict):
            continue
        entry = {
            "done": fnum(value.get("done")),
            "total": fnum(value.get("total")),
        }
        if value.get("unit"):
            entry["unit"] = value["unit"]
        base[key] = entry

    if not url and course_id:
        url = (
            "https://mooc1.chaoxing.com/mycourse/studentstudy"
            f"?courseId={course_id}&clazzid={class_id or '0'}&cpi={cpi or '0'}"
        )

    return {
        "id": str(course_id or name or "unknown"),
        "class_id": str(class_id or ""),
        "cpi": str(cpi or ""),
        "name": name or "(未命名课程)",
        "teacher": teacher or "",
        "term": term,
        "url": url,
        "progress": base,
        "last_study_at": last_study_at,
        "deadlines": list(deadlines or []),
    }


def normalize(data: dict) -> dict:
    """校验/补齐字段，保证前端拿到的东西一定能渲染。"""
    out = {
        "schema": SCHEMA,
        "generated_at": data.get("generated_at") or iso(now()),
        "source": data.get("source") or "unknown",
        "read_only": True,
        "account": data.get("account") or {"name": "", "uid": ""},
        "term": data.get("term") or {"name": "", "start": "", "end": ""},
        "notes": list(data.get("notes") or []),
        "courses": [],
    }
    for raw in data.get("courses") or []:
        if not isinstance(raw, dict):
            continue
        course = make_course(
            raw.get("id", ""),
            raw.get("class_id", ""),
            clean_name(str(raw.get("name", ""))),
            clean_name(str(raw.get("teacher", ""))),
            raw.get("progress"),
            cpi=raw.get("cpi", ""),
            term=raw.get("term", ""),
            url=raw.get("url", ""),
            last_study_at=raw.get("last_study_at", ""),
            deadlines=[
                {
                    "task": str(d.get("task", ""))[:160],
                    "kind": d.get("kind") if d.get("kind") in KIND_LABEL else "other",
                    "due": d.get("due", ""),
                    "status": "done" if d.get("status") == "done" else "pending",
                }
                for d in (raw.get("deadlines") or [])
                if isinstance(d, dict)
            ],
        )
        out["courses"].append(course)
    out["courses"].sort(key=lambda c: c["name"])
    return out


def totals(progress: dict) -> tuple[float, float]:
    done = sum(fnum(v.get("done")) for v in progress.values())
    total = sum(fnum(v.get("total")) for v in progress.values())
    return done, total


# --------------------------------------------------------------------------- #
# 演示数据
# --------------------------------------------------------------------------- #
DEMO_SPEC = [
    {
        "name": "高等数学（上）",
        "teacher": "张明远",
        "term": "2025-2026-1",
        "progress": {
            "video": (12, 20),
            "read": (5, 8),
            "document": (3, 3),
            "discussion": (1, 4),
            "quiz": (0, 6),
        },
        "last_study_in_hours": -30,
        "deadlines": [
            ("第五章 单元测验", "quiz", -26, "pending"),
            ("第六章 视频（8 节）", "video", 30, "pending"),
            ("课程讨论：极限的应用", "discussion", 96, "pending"),
            ("期中自测", "quiz", 12, "done"),
        ],
    },
    {
        "name": "大学英语（三）",
        "teacher": "李婉",
        "term": "2025-2026-1",
        "progress": {
            "video": (24, 24),
            "read": (10, 10),
            "document": (4, 4),
            "discussion": (3, 3),
            "quiz": (5, 5),
        },
        "last_study_in_hours": -120,
        "deadlines": [("Unit 8 作业", "other", 200, "done")],
    },
    {
        "name": "线性代数",
        "teacher": "王建国",
        "term": "2025-2026-1",
        "progress": {
            "video": (7, 18),
            "read": (2, 6),
            "document": (1, 4),
            "discussion": (0, 3),
            "quiz": (1, 5),
        },
        "last_study_in_hours": -72,
        "deadlines": [
            ("矩阵的秩 视频（5 节）", "video", 6, "pending"),
            ("第三章 测验", "quiz", 78, "pending"),
        ],
    },
    {
        "name": "计算机网络",
        "teacher": "陈立",
        "term": "2025-2026-1",
        "progress": {
            "video": (30, 32),
            "read": (9, 12),
            "document": (6, 6),
            "discussion": (4, 6),
            "quiz": (3, 8),
        },
        "last_study_in_hours": -8,
        "deadlines": [
            ("实验四 报告", "other", 168, "pending"),
            ("TCP 拥塞控制 讨论", "discussion", 44, "pending"),
        ],
    },
    {
        "name": "中国近现代史纲要",
        "teacher": "赵慧",
        "term": "2025-2026-1",
        "progress": {
            "video": (16, 16),
            "read": (8, 8),
            "document": (3, 3),
            "discussion": (2, 2),
            "quiz": (2, 2),
        },
        "last_study_in_hours": -50,
        "deadlines": [],
    },
    {
        "name": "Python 程序设计",
        "teacher": "刘一鸣",
        "term": "2025-2026-1",
        "progress": {
            "video": (14, 26),
            "read": (4, 10),
            "document": (2, 5),
            "discussion": (1, 5),
            "quiz": (0, 7),
        },
        "last_study_in_hours": -3,
        "deadlines": [
            ("面向对象 编程作业", "other", 20, "pending"),
            ("第七章 视频（6 节）", "video", 120, "pending"),
            ("期末综合练习", "quiz", 500, "pending"),
        ],
    },
]


def build_demo() -> dict:
    base = now()
    courses = []
    for index, spec in enumerate(DEMO_SPEC, start=1):
        progress = new_progress()
        for key, (done, total) in spec["progress"].items():
            progress[key] = {"done": done, "total": total}
        deadlines = [
            {
                "task": task,
                "kind": kind,
                "due": iso(base + timedelta(hours=hours)),
                "status": status,
            }
            for task, kind, hours, status in spec["deadlines"]
        ]
        courses.append(
            make_course(
                course_id=f"9000{index:02d}",
                class_id=f"5500{index:02d}",
                name=spec["name"],
                teacher=spec["teacher"],
                progress=progress,
                cpi=f"2300{index:02d}",
                term=spec["term"],
                last_study_at=iso(base + timedelta(hours=spec["last_study_in_hours"])),
                deadlines=deadlines,
            )
        )

    return normalize(
        {
            "generated_at": iso(base),
            "source": "demo",
            "account": {"name": "演示账号", "uid": "0000000"},
            "term": {
                "name": "2025-2026 学年 第一学期",
                "start": iso(base - timedelta(days=75)),
                "end": iso(base + timedelta(days=45)),
            },
            "courses": courses,
            "notes": [
                "这是内置演示数据，用来验证看板本身；运行 fetch 可换成你自己的进度。",
            ],
        }
    )


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_demo(args) -> None:
    data = build_demo()
    out = Path(args.data) if args.data else ROOT / "data" / "progress.json"
    write_text(out, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    log(f"已生成演示数据 → {out}（{len(data['courses'])} 门课程）")


def cmd_probe(args) -> None:
    cfg = load_config(Path(args.config) if args.config else None)
    if not cfg.get("cookie"):
        warn("config.json 里没有 cookie，只读探测大概率会被重定向到登录页。")
    raw_dir = ROOT / "data" / "raw"
    for name, url in cfg["endpoints"].items():
        log(f"[{name}] GET {url}")
        try:
            status, headers, body = http_get(url, cfg)
        except SystemExit as exc:
            warn(str(exc))
            continue
        text = decode(body, headers) if hasattr(headers, "get_content_charset") else body.decode(
            "utf-8", "replace"
        )
        out = raw_dir / f"{name}.html"
        write_text(out, text)
        ctype = headers.get("Content-Type", "?") if isinstance(headers, dict) else "?"
        log(f"    HTTP {status} · {len(body)} 字节 · {ctype} · 已存 {out.name}")
        if status != 200:
            warn(f"    {name} 返回 {status}，可能未登录或接口已变更。")
    log("探测完成。若 course_list 返回体结构和你预期不符，用 --verbose 跑 fetch 看匹配情况，")


def cmd_fetch(args) -> None:
    cfg = load_config(Path(args.config) if args.config else None)
    if not cfg.get("cookie"):
        raise SystemExit(
            "config.json 里没有 cookie。\n"
            "  1) 复制 config.example.json 为 config.json\n"
            "  2) 浏览器登录后从开发者工具复制完整 Cookie 串填进去\n"
            "  （只想先看看效果的话，直接跑：python dashboard.py demo）"
        )

    log("只读自检 login_check ...")
    status, headers, body = http_get(cfg["endpoints"]["login_check"], cfg)
    text = decode(body, headers) if hasattr(headers, "get_content_charset") else body.decode(
        "utf-8", "replace"
    )
    uid_match = re.search(r"_uid=(\d+)", cfg["cookie"]) or re.search(r"_uid=(\d+)", text)
    if status != 200:
        warn(f"login_check 返回 {status}，Cookie 可能已失效；继续尝试课程列表。")
    else:
        log(f"  OK（uid={uid_match.group(1) if uid_match else '未知'}）")

    log("只读抓取 course_list ...")
    status, headers, body = http_get(cfg["endpoints"]["course_list"], cfg)
    text = decode(body, headers) if hasattr(headers, "get_content_charset") else body.decode(
        "utf-8", "replace"
    )
    write_text(ROOT / "data" / "raw" / "course_list.html", text)
    if status != 200:
        raise SystemExit(f"course_list 返回 {status}，未取到数据。原始响应已存 data/raw/。")

    courses = parse_json_course_list(text)
    if courses:
        vlog(f"按 JSON 解析到 {len(courses)} 条")
        parsed = [course_from_json(item) for item in courses]
    else:
        parsed = parse_html_course_list(text)

    if not parsed:
        raise SystemExit(
            "没能从 course_list 里解析出课程。\n"
            "  原始响应已存 data/raw/course_list.html —— 页面结构可能变了。\n"
            "  兜底方案：浏览器打开课程列表页 → 另存为 HTML → python dashboard.py import --html 保存的文件"
        )

    term = cfg.get("term") or {}
    data = normalize(
        {
            "generated_at": iso(now()),
            "source": "chaoxing",
            "account": {"name": "", "uid": uid_match.group(1) if uid_match else ""},
            "term": {
                "name": term.get("name", ""),
                "start": term.get("start", ""),
                "end": term.get("end", ""),
            },
            "courses": parsed,
            "notes": [
                "由 fetch 只读抓取生成；任务点细分进度（视频/阅读/文档）需在 import 模式下从课程页补全。",
            ],
        }
    )
    out = Path(args.data) if args.data else ROOT / "data" / "progress.json"
    write_text(out, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    log(f"已生成 → {out}（{len(data['courses'])} 门课程）")


def cmd_import(args) -> None:
    source = Path(args.html)
    if not source.exists():
        raise SystemExit(f"找不到文件：{source}")
    text = read_text(source)
    courses = parse_json_course_list(text) or []
    if courses and isinstance(courses[0], dict):
        parsed = [course_from_json(item) for item in courses]
    else:
        parsed = parse_html_course_list(text)
    if not parsed:
        raise SystemExit("解析不出课程，看看页面里是否有 <ul id='courseList'> 结构。")
    data = normalize(
        {
            "generated_at": iso(now()),
            "source": "import",
            "courses": parsed,
            "notes": [f"从本地页面导入：{source.name}"],
        }
    )
    out = Path(args.data) if args.data else ROOT / "data" / "progress.json"
    write_text(out, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    log(f"已生成 → {out}（{len(data['courses'])} 门课程）")


def cmd_build(args) -> None:
    data_path = Path(args.data) if args.data else ROOT / "data" / "progress.json"
    if not data_path.exists():
        raise SystemExit(f"找不到 {data_path}；先跑 demo 或 fetch。")
    data = normalize(json.loads(read_text(data_path)))

    template_path = find_template()
    html = read_text(template_path)

    boot = "window.__PROGRESS__ = " + json.dumps(data, ensure_ascii=False) + ";"
    boot = boot.replace("</", "<\\/")
    if "/*__BOOT__*/" not in html:
        raise SystemExit("模板缺少 /*__BOOT__*/ 注入点。")
    html = html.replace("/*__BOOT__*/", boot)

    out = Path(args.out) if args.out else ROOT / "out" / "dashboard.html"
    write_text(out, html)
    done, total = 0.0, 0.0
    for course in data["courses"]:
        d, t = totals(course["progress"])
        done += d
        total += t
    pct = (done / total * 100) if total else 0.0
    log(f"已渲染 → {out}")
    log(
        f"  {len(data['courses'])} 门课程 · 整体进度 {pct:.1f}% "
        f"· 数据时间 {data['generated_at']} · 来源 {data['source']}"
    )


def cmd_serve(args) -> None:
    import http.server
    import socketserver

    directory = str(ROOT / "out")
    if not (ROOT / "out" / "dashboard.html").exists():
        raise SystemExit("out/dashboard.html 不存在，先跑 build。")

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=directory, **kw)

        def log_message(self, fmt, *a):  # 静音
            pass

    with socketserver.TCPServer(("127.0.0.1", args.port), Handler) as httpd:
        url = f"http://127.0.0.1:{args.port}/dashboard.html"
        log(f"预览地址：{url}（Ctrl+C 停止）")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            log("已停止。")


def cmd_all(args) -> None:
    if args.demo:
        cmd_demo(args)
    else:
        cmd_fetch(args)
    cmd_build(args)


def cmd_quick(args) -> None:
    """双击 exe（不带任何参数）时的默认流程。

    有 Cookie 就先只读抓一次；抓不到就退回已有数据，再退回演示数据。
    全程不发任何写请求。最后渲染看板并打开浏览器。
    """
    data_path = Path(args.data) if args.data else ROOT / "data" / "progress.json"
    sub = argparse.Namespace(
        data=getattr(args, "data", ""),
        out=getattr(args, "out", ""),
        config=getattr(args, "config", ""),
    )
    cfg = load_config(Path(args.config) if getattr(args, "config", "") else None)

    log(f"数据目录：{ROOT}")

    if data_path.exists() and not getattr(args, "refresh", False):
        log(f"沿用已有数据：{data_path.name}（想重新抓取请加 --refresh）")
    elif cfg.get("cookie"):
        log("检测到 config.json 里有 Cookie，尝试只读抓取…")
        try:
            cmd_fetch(sub)
        except SystemExit as exc:
            warn(str(exc))
            warn("抓取失败，改用演示数据（看板结构不变，接好账号后重跑即可）。")
            cmd_demo(sub)
    else:
        log("还没接入你的账号，先渲染演示数据。")
        log("接入方法：把 config.example.json 复制成 config.json，填 cookie 后重跑。")
        cmd_demo(sub)

    cmd_build(sub)

    out_path = Path(args.out) if getattr(args, "out", "") else ROOT / "out" / "dashboard.html"
    if not getattr(args, "no_open", False):
        try:
            webbrowser.open(out_path.resolve().as_uri())
            log("已用默认浏览器打开看板。")
        except Exception as exc:  # noqa: BLE001
            warn(f"自动打开失败（{exc}），请手动打开上面那个路径。")


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dashboard.py",
        description="学习进度聚合看板（只读）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="先看效果：python dashboard.py all --demo\n"
               "什么都不带直接跑（或双击 exe）＝一键出看板。",
    )
    parser.add_argument("--version", action="version",
                        version=f"学习进度聚合看板 {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印解析细节")
    parser.add_argument("--no-open", action="store_true",
                        help="一键模式下不自动打开浏览器")
    parser.add_argument("--refresh", action="store_true",
                        help="一键模式下即使已有数据也重新抓取")
    sub = parser.add_subparsers(dest="command")

    def common(sp):
        sp.add_argument("--data", default="", help="progress.json 路径")
        sp.add_argument("--config", default="", help="config.json 路径")
        # 也允许 -v 写在子命令后面（SUPPRESS 以免覆盖全局那份）
        sp.add_argument("-v", "--verbose", action="store_true",
                        default=argparse.SUPPRESS, help="打印解析细节")

    def build_like(sp):
        sp.add_argument("--out", default="", help="输出的 HTML 路径")

    sp = sub.add_parser("demo", help="生成演示数据")
    common(sp)
    sp.set_defaults(func=cmd_demo)

    sp = sub.add_parser("probe", help="只读探测接口")
    common(sp)
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("fetch", help="只读抓取你自己的进度")
    common(sp)
    sp.set_defaults(func=cmd_fetch)

    sp = sub.add_parser("import", help="从本地另存的页面导入")
    common(sp)
    sp.add_argument("--html", required=True, help="另存的课程列表 HTML")
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("build", help="渲染 HTML 看板")
    common(sp)
    build_like(sp)
    sp.set_defaults(func=cmd_build)

    sp = sub.add_parser("serve", help="本地预览")
    sp.add_argument("--port", type=int, default=8787)
    sp.add_argument("-v", "--verbose", action="store_true",
                    default=argparse.SUPPRESS, help="打印细节")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("all", help="抓取 + 渲染")
    common(sp)
    build_like(sp)
    sp.add_argument("--demo", action="store_true", help="用演示数据而不是抓取")
    sp.set_defaults(func=cmd_all)

    return parser


def main(argv: list[str] | None = None) -> int:
    global VERBOSE
    # 输出被重定向/被上层捕获时，按 UTF-8 写，避免中文变乱码；
    # 直接打在真实控制台里则保持系统默认编码。
    try:
        if not sys.stdout.isatty():
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = build_parser().parse_args(argv)
    VERBOSE = bool(getattr(args, "verbose", False))
    if getattr(args, "func", None) is None:  # 没写子命令 = 一键模式（双击 exe 走这里）
        for name in ("data", "out", "config"):
            if not hasattr(args, name):
                setattr(args, name, "")
        args.func = cmd_quick
    started = time.time()
    args.func(args)
    vlog(f"耗时 {time.time() - started:.2f}s")

    # 双击 exe 时用户看不到上面的输出，留个窗口让他读；管道的非交互调用直接返回。
    if FROZEN and getattr(args, "func", None) is cmd_quick:
        try:
            if sys.stdin and sys.stdin.isatty():
                input("\n按回车键关闭本窗口…")
        except (EOFError, KeyboardInterrupt, OSError):
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
