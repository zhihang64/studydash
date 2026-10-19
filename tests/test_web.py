"""网页版后端测试。

原则和其余用例一致：**不碰真网络、不起真服务器**。用 ``TestClient`` 直接调
ASGI 应用，适配器一律用 monkeypatch 换掉。

重点钉住三件容易悄悄坏掉的事：

1. **会话令牌真的在拦人**。带上一个 guard 中间件却忘了给某条路由挂上，是这类
   应用最典型的漏洞；这里的用例逐个接口要 401。
2. **密码绝不出现在任务快照里**。快照会回浏览器、会被贴进 issue，所以
   ``Task.to_dict`` 永远不吐 ``options``。这条要有钉子。
3. **配置每次读盘**。网页刚登录完紧接着的抓取必须看见新账号，缓存正是
   「改了没生效」的经典来源。
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from studydash import __version__  # noqa: E402
from studydash.providers import AccountConfig, AuthError, ProviderError  # noqa: E402
from studydash.store import Config, Workspace  # noqa: E402
from studydash.web import app as app_mod  # noqa: E402
from studydash.web import errors, operations, security, state as state_mod, tasks as tasks_mod  # noqa: E402
from studydash.web.app import _safe_join, create_app, render_index  # noqa: E402

# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #

#: 一个最小的前端产物：带注入点，其余什么都不用有。
INDEX_HTML = (
    "<!doctype html><html><head>"
    "<script>window.__STUDYDASH__ = null;</script>"
    "<title>studydash</title></head><body><div id=\"app\"></div></body></html>"
)

BASE = "http://127.0.0.1"


@pytest.fixture
def static_dir(tmp_path):
    """假的前端产物目录。"""
    root = tmp_path / "static"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (root / "assets" / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")
    return root


@pytest.fixture
def home(tmp_path):
    ws = Workspace(tmp_path / "home").ensure()
    return ws.root


@pytest.fixture(autouse=True)
def local_ip(monkeypatch):
    """让中间件把我们当成从回环地址来的。

    ``TestClient`` 在 ``scope["client"]`` 里填的是 ``"testclient"``，而 guard 中间件
    拿它做回环校验 —— 不换掉的话每条 API 用例都会先撞 403。这里换的是**取值函数**
    而不是造假 IP：测的是中间件自己的判断，跟 httpx 怎么填 scope 无关，用例也不会
    因为测试库升级而集体变红。返回的字典让单个用例改成远端地址。
    """
    holder = {"host": "127.0.0.1"}
    monkeypatch.setattr(app_mod, "client_host", lambda request: holder["host"])
    return holder


@pytest.fixture
def app(home, static_dir):
    return create_app(home=home, token="tok-123456", static_dir=static_dir)


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def auth():
    return {"X-Studydash-Token": "tok-123456"}


# --------------------------------------------------------------------------- #
# security
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "localhost", "::1", "", "::ffff:127.0.0.1", "127.0.0.2"],
)
def test_is_loopback_accepts_local(host):
    assert security.is_loopback(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "10.0.0.1", "example.com"])
def test_is_loopback_rejects_remote(host):
    assert security.is_loopback(host) is False


def test_token_ok_uses_constant_time_compare():
    assert security.token_ok("abc", "abc") is True
    assert security.token_ok("abc", "abd") is False
    assert security.token_ok("abc", "") is False
    # 期望值是空的时候不能放行，否则「没配令牌」会变成「谁都能进」
    assert security.token_ok("", "") is False


def test_new_token_is_long_and_unique():
    a, b = security.new_token(), security.new_token()
    assert a != b
    assert len(a) >= 24


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::"])
def test_assert_bind_host_refuses_public_binds(host):
    with pytest.raises(ValueError):
        security.assert_bind_host(host)


def test_assert_bind_host_passes_loopback():
    assert security.assert_bind_host("127.0.0.1") == "127.0.0.1"


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #

def test_error_envelope_shape():
    err = errors.error_body("busy", "有任务在跑", hint="等它跑完", detail={"task_id": "t_1"})
    assert set(err) == {"error"}
    assert err["error"] == {
        "code": "busy",
        "message": "有任务在跑",
        "hint": "等它跑完",
        "detail": {"task_id": "t_1"},
    }


def test_error_status_table_matches_contract():
    for code, status in errors.ERROR_STATUS.items():
        assert isinstance(code, str) and 400 <= status <= 599
    assert errors.ERROR_STATUS["unauthorized"] == 401
    assert errors.ERROR_STATUS["busy"] == 409
    assert errors.ERROR_STATUS["provider_error"] == 502


def test_api_error_defaults_status_from_code():
    exc = errors.ApiError("not_found", "没了")
    assert exc.status == 404
    assert exc.to_dict()["error"]["code"] == "not_found"


def test_api_error_allows_status_override():
    exc = errors.ApiError("not_found", "没了", status=410)
    assert exc.status == 410


def test_to_api_error_maps_the_interesting_exceptions():
    from studydash.http import HttpError, ReadOnlyError
    from studydash.login import LoginError
    from studydash.passlogin import CaptchaRequired, TwoFactorRequired
    from studydash.browsers import BrowserError

    assert errors.to_api_error(AuthError("会话过期了")).code == "auth_error"
    assert errors.to_api_error(CaptchaRequired("要验证码")).code == "captcha_required"
    assert errors.to_api_error(TwoFactorRequired("要二次验证")).code == "two_factor_required"
    # 浏览器占着 Cookie 库是本机状态冲突，不是平台挂了 —— 409 而不是 502
    assert errors.to_api_error(BrowserError("浏览器开着")).status == 409
    assert errors.to_api_error(ProviderError("平台改版")).code == "provider_error"
    assert errors.to_api_error(HttpError(BASE, "超时")).code == "provider_error"
    assert errors.to_api_error(LoginError("会话不管用")).code == "auth_error"
    assert errors.to_api_error(ReadOnlyError("想写")).code == "internal"
    assert errors.to_api_error(ValueError("天知道")).code == "internal"


def test_to_api_error_extracts_the_hint_line():
    # ProviderError 有自己的 hint 字段，不靠从消息里抠
    exc = ProviderError("抓住了", hint="跑 studydash probe chaoxing")
    assert errors.to_api_error(exc).hint == "跑 studydash probe chaoxing"

    # 其他异常把「该怎么办」写在消息的 → 行里，要单独抽出来
    from studydash.login import LoginError

    indirect = LoginError("没登上\n  → 换个浏览器配置档\n  → 或者扫码")
    assert errors.to_api_error(indirect).hint == "换个浏览器配置档 或者扫码"


def test_api_error_is_not_matched_as_internal_by_its_own_handler():
    # 处理器要按 ApiError 先匹配，否则会被 Exception 兜底吞成 internal
    original = errors.ApiError("busy", "忙")
    assert errors.to_api_error(original) is original


# --------------------------------------------------------------------------- #
# tasks / 任务模型
# --------------------------------------------------------------------------- #

def test_task_snapshot_never_leaks_options():
    """任务快照一律不含 options —— login_password 的密码就在里面。"""
    mgr = tasks_mod.TaskManager()
    task = mgr.create(
        "login_password",
        {"username": "13800000000", "password": "hunter2-SECRET"},
        lambda state, rep, options: {},
    )
    mgr.wait(task.id, timeout=5.0)
    blob = json.dumps(task.to_dict(full=True), ensure_ascii=False)
    assert "hunter2-SECRET" not in blob
    assert "options" not in task.to_dict(full=True)


def test_task_to_dict_full_false_trims_heavy_fields():
    mgr = tasks_mod.TaskManager()
    task = mgr.create("fetch", {}, lambda state, rep, options: {"ok": True})
    mgr.wait(task.id, timeout=5.0)
    slim = task.to_dict(full=False)
    assert "logs" not in slim and "artifacts" not in slim
    assert "result" in slim


def test_task_manager_rejects_a_second_task_of_the_same_kind():
    """同 kind 只允许一个在天上。

    两条扫码任务并行，用户不知道该扫哪张；两条抓取并行，只会互相抢限速额度。
    所以「拒绝」必须真发生 —— 得让第一条卡住不退，否则一跑完就没了 active。
    """
    import threading

    mgr = tasks_mod.TaskManager()
    release = threading.Event()

    def slow(state, rep, options):
        release.wait(timeout=5.0)
        return {}

    first = mgr.create("login_qr", {}, slow)
    try:
        with pytest.raises(errors.ApiError) as caught:
            mgr.create("login_qr", {}, slow)
        assert caught.value.code == "busy"
        assert caught.value.detail["task_id"] == first.id
    finally:
        release.set()
        mgr.wait(first.id, timeout=5.0)


def test_different_kinds_do_not_block_each_other():
    mgr = tasks_mod.TaskManager()
    a = mgr.create("fetch", {}, lambda state, rep, options: {})
    b = mgr.create("probe", {}, lambda state, rep, options: {})
    assert a.id != b.id
    mgr.wait(a.id, timeout=5.0)
    mgr.wait(b.id, timeout=5.0)


def test_failing_task_still_ends_and_carries_the_error_envelope():
    mgr = tasks_mod.TaskManager()

    def boom(state, rep, options):
        raise ProviderError("平台改版了", hint="跑 probe 留档")

    task = mgr.create("fetch", {}, boom)
    mgr.wait(task.id, timeout=5.0)
    assert task.state == "failed"
    assert task.error["code"] == "provider_error"
    assert task.error["hint"] == "跑 probe 留档"
    # 失败之后同 kind 必须能再起 —— 否则一次失败就把功能永久锁死
    mgr.create("fetch", {}, lambda state, rep, options: {})


def test_task_crash_with_base_exception_is_still_recorded():
    """线程里抛 BaseException 也必须落到 failed，不能永远停在 running。"""
    mgr = tasks_mod.TaskManager()

    def die(state, rep, options):
        raise KeyboardInterrupt("模拟外部中断")

    task = mgr.create("fetch", {}, die)
    mgr.wait(task.id, timeout=5.0)
    assert task.state == "failed"


def test_reporter_records_logs_progress_and_artifacts():
    mgr = tasks_mod.TaskManager()

    def work(state, rep, options):
        rep.progress(0.5, "一半了")
        rep.artifact("qr_data_url", "data:image/png;base64,AAA")
        rep.stream.write("✓ 顺手写一行日志\n")
        return {"done": True}

    task = mgr.create("fetch", {}, work)
    mgr.wait(task.id, timeout=5.0)
    assert task.state == "done"
    # 成功收尾时管理器会把进度顶到 1.0，这里的 0.5 只是中途那个点
    assert task.progress == 1.0
    assert task.message == "一半了"
    assert task.artifacts["qr_data_url"].endswith("AAA")
    assert any("顺手写一行日志" in entry["text"] for entry in task.logs)


def test_log_limit_trims_from_the_front():
    mgr = tasks_mod.TaskManager(log_limit=10)
    task = mgr.create("fetch", {}, lambda state, rep, options: {})
    mgr.wait(task.id, timeout=5.0)
    for index in range(40):
        mgr.add_log(task, "info", f"第 {index} 行")
    assert len(task.logs) == 10
    assert "第 39 行" in task.logs[-1]["text"]


@pytest.mark.parametrize(
    "line,level",
    [("· 细节", "debug"), (" 普通", "info"), ("! 注意", "warn"), ("× 错了", "error"), ("✓ 成了", "ok")],
)
def test_level_of_reads_the_log_prefix(line, level):
    assert tasks_mod._level_of(line) == level


def test_cancel_marks_the_task_and_the_reporter_can_see_it():
    mgr = tasks_mod.TaskManager()
    seen = {}

    def work(state, rep, options):
        seen["rep"] = rep
        for _ in range(200):
            if rep.cancelled:
                rep.check_cancelled()
            import time

            time.sleep(0.01)
        return {}

    task = mgr.create("fetch", {}, work)
    mgr.cancel(task.id)
    mgr.wait(task.id, timeout=5.0)
    assert seen["rep"].cancelled is True


def test_cancel_unknown_task_raises_not_found():
    mgr = tasks_mod.TaskManager()
    with pytest.raises(errors.ApiError) as caught:
        mgr.cancel("t_nope")
    assert caught.value.code == "not_found"


def test_require_unknown_task_raises_not_found():
    mgr = tasks_mod.TaskManager()
    with pytest.raises(errors.ApiError):
        mgr.require("t_nope")


def test_history_is_capped():
    mgr = tasks_mod.TaskManager(history=3)
    for _ in range(6):
        task = mgr.create("fetch", {}, lambda state, rep, options: {})
        mgr.wait(task.id, timeout=5.0)
    assert len(mgr.list(limit=50)) == 3


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #

def test_boot_payload_has_nothing_private_in_it(home):
    payload = state_mod.boot_payload("tok-abc")
    assert payload["token"] == "tok-abc"
    assert payload["version"] == __version__
    assert set(payload) == {"token", "version", "basePath"}


def test_config_is_read_from_disk_every_time(home):
    """网页刚登录完紧接着的抓取必须看见新账号 —— 所以不能缓存配置。"""
    app_state = state_mod.AppState.create(home=home)
    assert app_state.config().accounts == []

    cfg = Config(accounts=[AccountConfig(platform="chaoxing", label="默认", cookie="uid=9")])
    cfg.save(home / "config.json")

    assert [a.label for a in app_state.config().accounts] == ["默认"]


def test_save_config_roundtrips(home):
    app_state = state_mod.AppState.create(home=home)
    cfg = app_state.config()
    cfg.accounts.append(AccountConfig(platform="manual", label="手动"))
    app_state.save_config(cfg)
    assert [a.label for a in app_state.config().accounts] == ["手动"]


def test_static_path_explains_itself_when_missing(home, tmp_path):
    """指错目录时要当场说清楚，而不是等读到文件才 500。"""
    app_state = state_mod.AppState.create(home=home, static_dir=tmp_path / "nope")
    with pytest.raises(errors.ApiError) as caught:
        app_state.static_path()
    assert caught.value.code == "internal"
    assert "build_web" in caught.value.hint
    assert "nope" in caught.value.message


def test_rotate_token_changes_it(home):
    app_state = state_mod.AppState.create(home=home)
    before = app_state.token
    assert app_state.rotate_token() != before


# --------------------------------------------------------------------------- #
# render_index / _safe_join
# --------------------------------------------------------------------------- #

def test_render_index_replaces_the_placeholder(home):
    app_state = state_mod.AppState.create(home=home, token="tok-xyz")
    html = render_index(INDEX_HTML, app_state)
    assert "window.__STUDYDASH__ = null;" not in html
    assert '"token": "tok-xyz"' in html.replace('"token":"tok-xyz"', '"token": "tok-xyz"')


def test_render_index_falls_back_to_head_when_placeholder_is_gone(home):
    app_state = state_mod.AppState.create(home=home, token="tok-1")
    html = render_index("<html><head><title>x</title></head><body></body></html>", app_state)
    assert "tok-1" in html
    assert html.index("tok-1") < html.index("</head>")


def test_render_index_survives_a_html_without_head(home):
    app_state = state_mod.AppState.create(home=home, token="tok-2")
    html = render_index("<h1>hi</h1>", app_state)
    assert "tok-2" in html


def test_render_index_escapes_script_close(home):
    app_state = state_mod.AppState.create(home=home, token="</script><script>alert(1)</script>")
    html = render_index(INDEX_HTML, app_state)
    assert html.count("</script>") == 1


def test_safe_join_blocks_traversal(static_dir):
    assert _safe_join(static_dir, "assets/index-abc123.js") is not None
    assert _safe_join(static_dir, "../secret.txt") is None
    assert _safe_join(static_dir, "../../etc/passwd") is None
    assert _safe_join(static_dir, "a/../../b") is None


# --------------------------------------------------------------------------- #
# 中间件与接口
# --------------------------------------------------------------------------- #

def test_health_needs_the_token(client):
    assert client.get("/api/health").status_code == 401


def test_wrong_token_is_401_with_the_contract_envelope(client):
    resp = client.get("/api/health", headers={"X-Studydash-Token": "nope"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"
    assert resp.headers.get("X-Request-Id")


def test_remote_client_is_rejected(client, auth, local_ip):
    """非回环来源一律 403 —— 这个服务从来不打算给别的机器用。"""
    local_ip["host"] = "192.168.1.9"
    resp = client.get("/api/health", headers=auth)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_health_and_version(client, auth):
    health = client.get("/api/health", headers=auth).json()
    assert health["ok"] is True and health["version"] == __version__
    version = client.get("/api/version", headers=auth).json()
    assert version["version"] == __version__
    assert "python" in version


def test_platforms_lists_providers_and_task_kinds(client, auth):
    data = client.get("/api/platforms", headers=auth).json()
    ids = {p["id"] for p in data["platforms"]}
    assert {"chaoxing", "manual", "demo"} <= ids
    assert "qr" in data["sources"]
    assert {k["kind"] for k in data["task_kinds"]} == set(operations.KINDS)


def test_doctor_reports_checks_without_failing_on_a_fresh_home(client, auth):
    data = client.get("/api/doctor", headers=auth).json()
    assert data["checks"]
    levels = {c["level"] for c in data["checks"]}
    assert levels <= {"ok", "warn", "fail"}
    # 全新工作区没有 config.json，那只是 warn
    config_check = next(c for c in data["checks"] if c["name"] == "配置文件")
    assert config_check["level"] == "warn"


def test_settings_get_and_patch(client, auth):
    before = client.get("/api/settings", headers=auth).json()
    assert before["ui"]["theme"] == "auto"
    assert before["config_path"].endswith("config.json")
    assert "cookie" not in json.dumps(before)

    after = client.patch("/api/settings", headers=auth, json={"ui": {"theme": "dark"}}).json()
    assert after["ui"]["theme"] == "dark"
    assert client.get("/api/settings", headers=auth).json()["ui"]["theme"] == "dark"


def test_settings_patch_merges_http_instead_of_replacing(client, auth):
    before = client.get("/api/settings", headers=auth).json()["http"]
    after = client.patch("/api/settings", headers=auth, json={"http": {"timeout": 22.5}}).json()["http"]
    assert after["timeout"] == 22.5
    assert after["user_agent"] == before["user_agent"]


def test_accounts_crud(client, auth):
    assert client.get("/api/accounts", headers=auth).json()["accounts"] == []

    created = client.post(
        "/api/accounts",
        headers=auth,
        json={"platform": "chaoxing", "label": "小号", "cookie": "uid=1; _d=2"},
    )
    assert created.status_code == 201

    listed = client.get("/api/accounts", headers=auth).json()["accounts"]
    assert len(listed) == 1
    # cookie_hint 只给长度，绝不回真值
    assert listed[0]["cookie_hint"].startswith("<已设置")
    assert "uid=1" not in json.dumps(listed)

    duplicated = client.post("/api/accounts", headers=auth, json={"platform": "chaoxing", "label": "小号"})
    assert duplicated.status_code == 409

    bad_platform = client.post("/api/accounts", headers=auth, json={"platform": "nope"})
    assert bad_platform.status_code == 400
    assert "chaoxing" in json.dumps(bad_platform.json()["error"]["detail"])

    patched = client.patch("/api/accounts/chaoxing/小号", headers=auth, json={"enabled": False})
    assert patched.status_code == 200
    # POST / PATCH 都返回整份列表，前端 store 直接拿它覆盖 items
    assert patched.json()["accounts"][0]["enabled"] is False

    assert client.delete("/api/accounts/chaoxing/小号", headers=auth).status_code == 204
    assert client.delete("/api/accounts/chaoxing/小号", headers=auth).status_code == 404


def test_payload_is_not_found_before_any_fetch(client, auth):
    resp = client.get("/api/payload", headers=auth)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert "演示" in resp.json()["error"]["hint"]


def test_demo_payload_then_readable(client, auth):
    made = client.post("/api/payload/demo", headers=auth).json()
    assert made["mode"] == "demo"
    assert made["summary"]["courses"] >= 1

    payload = client.get("/api/payload", headers=auth).json()
    assert payload["meta"]["mode"] == "demo"
    assert payload["overview"]["courses"] >= 1
    meta = client.get("/api/payload/meta", headers=auth).json()
    assert set(meta) == {"generated_at", "mode", "warnings", "overview"}


def test_unknown_task_kind_is_rejected_with_a_list(client, auth):
    resp = client.post("/api/tasks", headers=auth, json={"kind": "nope", "options": {}})
    assert resp.status_code == 400
    assert "login_qr" in json.dumps(resp.json()["error"]["detail"])


def test_create_and_read_back_a_real_task(client, auth, monkeypatch):
    """走真 op（把网络那一步换掉），验证 202 → 轮询 → done 的完整形状。"""
    from studydash.login import Session

    # operations 是 `from ..passlogin import login as password_login`，
    # 所以必须打在 operations 命名空间上，打在 passlogin 模块上不起作用。
    monkeypatch.setattr(
        operations,
        "password_login",
        lambda username, password, **kw: Session(
            cookie="uid=777; _d=x", source="password", uid="777", cookie_count=2
        ),
    )
    created = client.post(
        "/api/tasks",
        headers=auth,
        json={"kind": "login_password", "options": {"username": "13800000000", "password": "pw"}},
    )
    assert created.status_code == 202
    task_id = created.json()["task"]["id"]

    listed = client.get("/api/tasks", headers=auth).json()["tasks"]
    assert any(item["id"] == task_id for item in listed)

    snapshot = client.get(f"/api/tasks/{task_id}", headers=auth).json()
    assert snapshot["state"] in {"pending", "running", "done", "failed"}
    assert "pw" not in json.dumps(snapshot)

    done = overall = None
    for _ in range(120):
        overall = client.get(f"/api/tasks/{task_id}", headers=auth).json()
        if overall["state"] in {"done", "failed", "cancelled"}:
            break
        import time

        time.sleep(0.05)
    assert overall is not None and overall["state"] == "done", overall
    done = overall
    assert done["result"]["account"]["uid"] == "777"
    # 登录成功要落盘，且后续抓取能看见它
    cfg = json.loads((home_of(client)).read_text(encoding="utf-8"))
    assert cfg["accounts"][0]["label"] == "默认（777）"


def home_of(client) -> "object":
    from pathlib import Path

    return Path(client.app.state.studydash.workspace.root) / "config.json"


def test_task_events_stream_emits_log_then_done(client, auth):
    created = client.post("/api/tasks", headers=auth, json={"kind": "fetch", "options": {}})
    task_id = created.json()["task"]["id"]
    seen: list[str] = []
    with client.stream("GET", f"/api/tasks/{task_id}/events", headers=auth) as stream:
        for line in stream.iter_lines():
            if line.startswith("event: "):
                seen.append(line.removeprefix("event: "))
            if "done" in seen:
                break
    assert "done" in seen


def test_spa_fallback_serves_index_with_the_token(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "tok-123456" in resp.text
    # 带令牌的 index.html 不能进缓存，否则重启后满屏 401
    assert "no-store" in resp.headers.get("cache-control", "")


def test_spa_fallback_covers_client_routes_but_not_api(client, auth):
    assert client.get("/courses").status_code == 200
    # 没带令牌时先 401：不向未授权调用者透露哪些接口存在
    assert client.get("/api/nope").status_code == 401
    # 带了令牌才是真的 404（而不是掉进 SPA 回退返回一个 HTML）
    assert client.get("/api/nope", headers=auth).status_code == 404


def test_hashed_assets_are_cached_hard(client):
    resp = client.get("/assets/index-abc123.js")
    assert resp.status_code == 200
    assert "immutable" in resp.headers.get("cache-control", "")


def test_unknown_kind_and_task_ids_are_handled_by_the_router(client, auth):
    assert client.get("/api/tasks/t_nope", headers=auth).status_code == 404
    assert client.post("/api/tasks/t_nope/cancel", headers=auth).status_code == 404


# --------------------------------------------------------------------------- #
# operations
# --------------------------------------------------------------------------- #

def test_resolve_rejects_unknown_kind_with_the_available_list():
    with pytest.raises(errors.ApiError) as caught:
        operations.resolve("nope")
    assert caught.value.code == "invalid_request"
    assert "login_qr" in caught.value.detail["available"]


def test_every_kind_has_a_label_and_a_runner():
    for kind in operations.KINDS:
        assert operations.KIND_LABELS.get(kind)
    assert set(operations.OPERATIONS) == set(operations.KINDS)


def test_password_is_dropped_from_options_immediately(home, monkeypatch):
    """不等任务结束 —— 密码不该在内存里多躺一秒，也不该进任何快照。"""
    from studydash.login import Session

    monkeypatch.setattr(
        operations,
        "password_login",
        lambda username, password, **kw: Session(cookie="uid=1", source="password", uid="1"),
    )
    app_state = state_mod.AppState.create(home=home)
    mgr = tasks_mod.TaskManager()
    task = mgr.create("login_password", {"username": "u", "password": "SECRET"}, None)
    rep = tasks_mod.Reporter(mgr, task)
    options = {"username": "u", "password": "SECRET"}
    operations.op_login_password(app_state, rep, options)
    assert "password" not in options


def test_probe_unknown_platform_is_invalid_request(home):
    app_state = state_mod.AppState.create(home=home)
    mgr = tasks_mod.TaskManager()
    task = mgr.create("probe", {"platform": "nope"}, None)
    rep = tasks_mod.Reporter(mgr, task)
    with pytest.raises(errors.ApiError) as caught:
        operations.op_probe(app_state, rep, {"platform": "nope"})
    assert caught.value.code == "invalid_request"
    assert "chaoxing" in caught.value.detail["available"]
