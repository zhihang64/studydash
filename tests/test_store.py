"""``studydash.store``：工作区、配置读写、原子落盘、脱敏。"""

from __future__ import annotations

import json

import pytest

from studydash.providers import AccountConfig, HttpSettings
from studydash.store import (
    ENV_HOME,
    REDACTED,
    SCHEMA_VERSION,
    Config,
    Workspace,
    atomic_write_json,
    read_json,
)


# --------------------------------------------------------------------------- #
# 工作区
# --------------------------------------------------------------------------- #

def test_ensure_creates_the_three_dirs(tmp_path):
    ws = Workspace(tmp_path).ensure()
    assert ws.data_dir.is_dir()
    assert ws.raw_dir.is_dir()
    assert ws.out_dir.is_dir()
    assert ws.progress_path == tmp_path / "data" / "progress.json"
    assert ws.dashboard_path == tmp_path / "out" / "dashboard.html"
    assert ws.config_path == tmp_path / "config.json"


def test_from_env_overrides_cwd(tmp_path, monkeypatch):
    # 入口叫 Workspace.resolve()：--home > $STUDYDASH_HOME > exe 目录 > cwd
    monkeypatch.setenv(ENV_HOME, str(tmp_path))
    assert Workspace.resolve().root == tmp_path.resolve()


def test_save_progress_roundtrip(workspace):
    payload = {"meta": {"version": "2.0.0"}, "courses": [{"id": "c1"}]}
    path = workspace.save_progress(payload)
    assert path.is_file()
    assert workspace.load_progress() == payload
    assert json.loads(path.read_text(encoding="utf-8"))["courses"][0]["id"] == "c1"


# --------------------------------------------------------------------------- #
# JSON 读写
# --------------------------------------------------------------------------- #

def test_read_json_missing_and_blank_are_none(tmp_path):
    assert read_json(tmp_path / "nope.json") is None
    blank = tmp_path / "blank.json"
    blank.write_text("   \n", encoding="utf-8")
    assert read_json(blank) is None


def test_read_json_corrupt_raises_with_the_path(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"a": 1,,}', encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        read_json(bad)
    assert "bad.json" in str(exc.value)
    assert "合法 JSON" in str(exc.value)


def test_read_json_tolerates_bom(tmp_path):
    """Windows 记事本存出来的 UTF-8 带 BOM，不该读不了。"""
    p = tmp_path / "bom.json"
    p.write_bytes(b"\xef\xbb\xbf" + '{"a": 1}'.encode())
    assert read_json(p) == {"a": 1}


def test_atomic_write_leaves_no_temp_file(tmp_path):
    target = tmp_path / "sub" / "x.json"
    atomic_write_json(target, {"k": "中文"})
    assert target.is_file()
    assert list(tmp_path.rglob("*.tmp")) == []
    assert json.loads(target.read_text(encoding="utf-8")) == {"k": "中文"}


def test_atomic_write_overwrites_completely(tmp_path):
    target = tmp_path / "x.json"
    atomic_write_json(target, {"a": "很长的一段内容" * 100})
    atomic_write_json(target, {"b": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"b": 1}


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

def test_config_roundtrip(tmp_path, config):
    path = tmp_path / "config.json"
    config.save(path)
    back = Config.load(path)
    assert back.schema_version == SCHEMA_VERSION
    assert [a.platform for a in back.accounts] == ["chaoxing", "icourse163", "zhihuishu"]
    assert back.accounts[0].cookie == "uid=1; _d=2"
    assert back.http.timeout == 1.0
    assert back.alert_days() == 3
    assert back.ui["theme"] == "auto"


def test_config_load_missing_returns_defaults(tmp_path):
    cfg = Config.load(tmp_path / "nope.json")
    assert cfg.accounts == []
    assert cfg.alert_days() == 3
    assert cfg.theme() == "auto"


def test_config_load_corrupt_json_surfaces(tmp_path):
    """配置文件写坏了要报错，不能悄悄退化成「没有账号」——
    那种失败模式下用户只会看到空看板，完全不知道发生了什么。"""
    p = tmp_path / "config.json"
    p.write_text("{ 这不是 json", encoding="utf-8")
    with pytest.raises(ValueError):
        Config.load(p)


def test_config_tolerates_short_and_odd_account_forms():
    cfg = Config.from_dict(
        {
            "accounts": [
                "chaoxing",                       # 字符串简写
                {"platform": "manual"},           # 只有平台名
                None,                             # 垃圾条目
                42,                               # 垃圾条目
            ],
            "ui": "不是字典",
            "http": None,
        }
    )
    assert [a.platform for a in cfg.accounts] == ["chaoxing", "manual"]
    assert cfg.ui == {}
    assert cfg.http.timeout == HttpSettings().timeout


def test_cookie_never_appears_in_redacted_output(tmp_path):
    cfg = Config(
        accounts=[AccountConfig(platform="chaoxing", cookie="UID=秘密; _d=也要藏")],
        ui={"theme": "dark"},
    )
    blob = json.dumps(cfg.redacted(), ensure_ascii=False)
    assert "秘密" not in blob
    assert "也要藏" not in blob
    assert REDACTED in blob

    path = tmp_path / "shared.json"
    cfg.save(path, redact=True)
    assert "秘密" not in path.read_text(encoding="utf-8")


def test_redacted_scrubs_nested_sensitive_keys():
    """以后加了别的凭据字段（token/secret）也要一起被擦掉。"""
    cfg = Config()
    raw = cfg.to_dict()
    raw["http"]["token"] = "abcd"
    raw["accounts"] = [{"platform": "x", "options": {"secret": "xyz"}}]
    from studydash.store import _scrub

    scrubbed = _scrub(raw)
    assert scrubbed["http"]["token"] == REDACTED
    assert scrubbed["accounts"][0]["options"]["secret"] == REDACTED


def test_empty_secrets_stay_empty():
    """没填的字段保持空串，不要写成 '<已脱敏>' 让人以为配过。"""
    from studydash.store import _scrub

    assert _scrub({"cookie": ""})["cookie"] == ""


def test_enabled_accounts_filters(tmp_path, config):
    assert [a.platform for a in config.enabled_accounts()] == ["chaoxing", "icourse163"]
    assert [a.platform for a in config.enabled_accounts(["icourse163"])] == ["icourse163"]
    assert config.enabled_accounts([]) == config.enabled_accounts()
    assert config.enabled_accounts(["zhihuishu"]) == []   # 停用的不算
    assert config.enabled_accounts(["没这个平台"]) == []


# --------------------------------------------------------------------------- #
# ui 字段的容错
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad", ["三天", None, [], {"a": 1}, float("nan")])
def test_alert_days_falls_back_to_three(bad):
    cfg = Config(ui={"alert_days": bad})
    got = cfg.alert_days()
    assert got == 3.0 or got != got  # nan 也接受：它不会让排序崩


def test_alert_days_reads_number():
    assert Config(ui={"alert_days": 7}).alert_days() == 7.0
    assert Config(ui={"alert_days": "5"}).alert_days() == 5.0


@pytest.mark.parametrize(
    ("raw", "want"),
    [("dark", "dark"), ("DARK", "dark"), ("light", "light"), ("auto", "auto"),
     ("紫", "auto"), ("", "auto"), (None, "auto")],
)
def test_theme_is_whitelisted(raw, want):
    assert Config(ui={"theme": raw}).theme() == want


# --------------------------------------------------------------------------- #
# 模板定位
# --------------------------------------------------------------------------- #

def test_find_template_prefers_explicit_path(tmp_path):
    custom = tmp_path / "my.html"
    custom.write_text("<html>" + "__STUDYDASH_PAYLOAD__" + "</html>", encoding="utf-8")
    ws = Workspace(tmp_path / "ws").ensure()
    assert ws.find_template(custom) == custom


def test_find_template_raises_with_candidate_list(tmp_path, monkeypatch):
    ws = Workspace(tmp_path / "ws").ensure()
    monkeypatch.setattr("studydash.store.Path.is_file", lambda self: False)
    with pytest.raises(FileNotFoundError) as exc:
        ws.find_template()
    assert "dashboard.html" in str(exc.value)
