"""FastAPI 装配。

三件事在这儿做掉，别处不用管：来源与令牌校验（中间件）、异常到错误信封的翻译、
前端资源与 SPA 回退。
"""

from __future__ import annotations

import json
import mimetypes
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .. import __version__
from .errors import ApiError, to_api_error
from .security import TOKEN_HEADER, assert_bind_host, client_host, is_loopback, token_ok
from .state import AppState, boot_payload

__all__ = ["ASSET_CACHE", "INDEX_CACHE", "create_app", "render_index", "run"]

#: ``index.html`` 里带令牌，绝不能被缓存住——否则服务重启后浏览器还在用旧令牌，
#: 用户看到的是一个莫名其妙的 401。
INDEX_CACHE = "no-store, must-revalidate"
#: Vite 产出的资源文件名带内容哈希，可以放心长缓存。
ASSET_CACHE = "public, max-age=31536000, immutable"

_MIME_OVERRIDES = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".wasm": "application/wasm",
    ".ico": "image/x-icon",
}


def _script_json(payload: dict) -> str:
    """把启动参数序列化成能安全嵌进 ``<script>`` 的 JSON 字面量。

    只要有一个值自带 ``</script>``，浏览器的 HTML 词法分析就会在字符串中间结束
    脚本块 —— 后面跟着的任意标记都会被当标签执行。现在这些值全是我们自己造的
    （令牌是 ``token_urlsafe``，版本号是字面量），但 ``render_index`` 是公开函数，
    别把「今天恰好安全」当成设计。转义 ``<`` 的代价是零。
    """
    blob = json.dumps(payload, ensure_ascii=False)
    return blob.replace("<", "\\u003c").replace("&", "\\u0026")


def render_index(html: str, state: AppState) -> str:
    """把启动参数注进 ``index.html``。

    优先替换前端模板里那行占位（``window.__STUDYDASH__ = null;``）——它本身就是合法
    JS，所以直接用浏览器打开构建产物时会得到 ``null``，前端据此显示「请从命令行
    启动」而不是白屏。找不到占位就退回往 ``</head>`` 前插一段。
    """
    from . import BOOT_PLACEHOLDER

    script = "window.__STUDYDASH__ = " + _script_json(boot_payload(state.token)) + ";"
    if BOOT_PLACEHOLDER in html:
        return html.replace(BOOT_PLACEHOLDER, script, 1)
    if "</head>" in html:
        return html.replace("</head>", f"  <script>{script}</script>\n</head>", 1)
    return f"<script>{script}</script>\n" + html


def create_app(
    state: AppState | None = None,
    *,
    token: str = "",
    home: str | Path | None = None,
    static_dir: str | Path | None = None,
):
    from fastapi import FastAPI, Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

    from .routers import accounts, data, system, tasks

    state = state or AppState.create(home, token=token, static_dir=static_dir)

    app = FastAPI(
        title="studydash",
        version=__version__,
        description="本机只读的学习进度聚合看板",
        docs_url=None,          # 关掉交互式文档：它会把整张接口表暴露在页面上
        redoc_url=None,
        openapi_url=None,
    )
    app.state.studydash = state

    # -- 来源与令牌 -------------------------------------------------------- #

    @app.middleware("http")
    async def guard(request: Request, call_next):
        request_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()

        host = client_host(request)
        if not is_loopback(host):
            return JSONResponse(
                ApiError(
                    "forbidden",
                    "这个服务只对本机开放",
                    hint="studydash 的网页版持有你的登录会话，不对外提供服务。",
                    detail={"client": host or "unknown"},
                ).to_dict(),
                status_code=403,
                headers={"X-Request-Id": request_id},
            )

        path = request.url.path
        if path.startswith("/api"):
            provided = request.headers.get(TOKEN_HEADER, "")
            if not token_ok(state.token, provided):
                return JSONResponse(
                    ApiError(
                        "unauthorized",
                        "访问令牌不对",
                        hint="服务重启后令牌会变。刷新一下页面就能拿到新的。",
                    ).to_dict(),
                    status_code=401,
                    headers={"X-Request-Id": request_id},
                )

        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        state.requests += 1
        state.last_request = f"{request.method} {path}"
        return response

    # -- 异常 → 错误信封 ---------------------------------------------------- #

    @app.exception_handler(ApiError)
    async def _handle_api_error(request: Request, exc: ApiError):
        return JSONResponse(exc.to_dict(), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: Any):
        # FastAPI 默认回 422 + 一大坨 pydantic 结构。前端只认契约里那张表，
        # 所以压成 400 invalid_request，并把第一个出错的字段名指出来。
        fields: list[str] = []
        for err in exc.errors():
            loc = [str(p) for p in err.get("loc", []) if p not in ("body", "query", "path")]
            if loc:
                fields.append(".".join(loc))
        api = ApiError(
            "invalid_request",
            "请求参数不合法",
            detail={"fields": fields[:5], "field": fields[0] if fields else ""},
        )
        return JSONResponse(api.to_dict(), status_code=400)

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception):
        api = to_api_error(exc)
        api.detail.setdefault("request_id", request.headers.get("X-Request-Id", ""))
        return JSONResponse(api.to_dict(), status_code=api.status)

    # -- 业务路由 ---------------------------------------------------------- #

    app.include_router(system.router)
    app.include_router(accounts.router)
    app.include_router(tasks.router)
    app.include_router(data.router)

    # -- 前端资源与 SPA 回退 ------------------------------------------------ #

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        if full_path.startswith("api/"):
            return JSONResponse(
                ApiError("not_found", f"没有这个接口：/{full_path}").to_dict(), status_code=404
            )
        root = state.static_path()
        if full_path:
            candidate = _safe_join(root, full_path)
            if candidate is not None and candidate.is_file():
                media = _MIME_OVERRIDES.get(candidate.suffix.lower()) or (
                    mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
                )
                # 带内容哈希的资源目录长缓存；其余（favicon 之类）短缓存
                cache = ASSET_CACHE if full_path.startswith("assets/") else "no-cache"
                return FileResponse(candidate, media_type=media, headers={"Cache-Control": cache})
        index = state.index_path()
        if not index.is_file():
            return JSONResponse(
                ApiError("internal", "前端构建产物缺少 index.html").to_dict(), status_code=500
            )
        html = render_index(index.read_text(encoding="utf-8"), state)
        return HTMLResponse(html, headers={"Cache-Control": INDEX_CACHE})

    return app


def _safe_join(root: Path, relative: str) -> Path | None:
    """把 URL 路径拼到静态目录下，越界就返回 None。

    ``../`` 之类的请求不能变成读文件原语。这里用 ``resolve()`` 之后再比前缀，
    而不是靠字符串替换——后者总有绕过写法。
    """
    try:
        root = root.resolve()
        target = (root / relative).resolve()
    except (OSError, ValueError):
        return None
    if target == root or root in target.parents:
        return target
    return None


def _pick_port(host: str, port: int, *, tries: int = 20):
    """端口被占就往后找。返回 ``(端口, 是否换过)``。"""
    for offset in range(max(1, tries)):
        candidate = port + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, candidate))
            except OSError:
                continue
            return candidate, offset > 0
    raise ApiError(
        "conflict",
        f"{port} 起连续 {tries} 个端口都被占用了",
        hint="换一个起始端口：`studydash web --port 9000`",
    )


def run(
    *,
    home: str | Path | None = None,
    port: int = 8788,
    host: str = "127.0.0.1",
    open_browser: bool = True,
    token: str = "",
    static_dir: str | Path | None = None,
    log: Any = None,
) -> int:
    """起服务并阻塞。返回进程退出码。"""
    from ..log import Log

    log = log or Log()
    try:
        host = assert_bind_host(host)
    except ValueError as exc:
        log.error(str(exc))
        return 2

    try:
        port, moved = _pick_port(host, port)
    except ApiError as exc:
        log.error(exc.message)
        if exc.hint:
            log.warn("  " + exc.hint)
        return 2
    if moved:
        log.warn(f"{port - 1} 之前的端口被占用了，改用 {port}")

    try:
        import uvicorn  # noqa: F401
    except ImportError:
        log.error("起网页版需要 fastapi 和 uvicorn，但它们没装。")
        log.warn('  → 装一下：pip install "studydash[web]"')
        return 2

    app = create_app(home=home, token=token, static_dir=static_dir)
    state: AppState = app.state.studydash
    url = f"http://{host}:{port}/"

    log.info("")
    log.ok(f"studydash {__version__} 网页版已就绪：{url}")
    log.info(f"  工作区：{state.workspace.root}")
    log.info(f"  按 Ctrl+C 停止。这个地址只在本机可用。")
    log.info("")

    if open_browser:
        import webbrowser

        try:
            webbrowser.open(url)
        except Exception:                               # pragma: no cover - 无桌面环境
            pass

    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)
    return 0
