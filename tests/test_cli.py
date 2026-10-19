"""``studydash.cli``：只跑不联网的子命令。

CLI 是用户真正接触的那一层，所以这里检查的是「命令跑完会不会留下该留下的
东西、退出码对不对、报错是不是人能看懂」。
"""

from __future__ import annotations

import json
import os

import pytest

from studydash import cli


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

def run(*argv: str) -> int:
    """跑一次 CLI，把 SystemExit 也折算成退出码——两种「失败」对调用方是一回事。"""
    try:
        return cli.main(list(argv))
    except SystemExit as exc:
        return int(exc.code or 0)


@pytest.fixture
def home(tmp_path):
    return str(tmp_path / "home")


def demo(home, *extra, expect=0):
    code = run("--home", home, "demo", *extra)
    assert code == expect, f"demo 退出码 {code}，期望 {expect}"
    return code


# --------------------------------------------------------------------------- #
# 无参数 / 版本 / 帮助
# --------------------------------------------------------------------------- #

def test_no_command_prints_help_and_succeeds(capsys):
    assert run() == 0
    out = capsys.readouterr().out
    assert "demo" in out and "fetch" in out and "studydash" in out


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert "studydash" in capsys.readouterr().out


def test_version_subcommand(capsys):
    assert run("version") == 0
    assert cli.__version__ in capsys.readouterr().out


def test_unknown_command_exits_two():
    with pytest.raises(SystemExit) as exc:
        cli.main(["根本没有这个命令"])
    assert exc.value.code == 2


# --------------------------------------------------------------------------- #
# -v / -q / --home 的位置容错
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "argv",
    [
        ("-v", "platforms"),
        ("platforms", "-v"),
        ("-v", "platforms", "-v"),
        ("--verbose", "platforms"),
    ],
)
def test_verbose_works_before_and_after_the_subcommand(argv, capsys):
    """`-v` 写在子命令后面也要认。

    这曾经是个真 bug：argparse 的子解析器会把全局解析出来的 verbose 覆盖成
    False，于是 `studydash platforms -v` 静默地不输出调试信息。
    """
    assert run(*argv) == 0
    assert capsys.readouterr().out


def test_home_works_after_the_subcommand(tmp_path):
    """`studydash demo --home X` 是人的直觉写法，不该报 unrecognized arguments。"""
    home = str(tmp_path / "位置在后面")
    assert run("demo", "--home", home) == 0
    assert (tmp_path / "位置在后面" / "out" / "dashboard.html").is_file()


def test_home_env_var_is_honoured(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDYDASH_HOME", str(tmp_path))
    assert run("demo") == 0
    assert (tmp_path / "out" / "dashboard.html").is_file()


# --------------------------------------------------------------------------- #
# platforms / config / doctor
# --------------------------------------------------------------------------- #

def test_platforms_lists_every_builtin(capsys):
    """表格里显示的是完整平台名，不是短名——别拿 short 去断言。"""
    assert run("platforms") == 0
    out = capsys.readouterr().out
    for name in ("超星学习通", "中国大学 MOOC", "智慧树", "手动导入", "离线演示"):
        assert name in out, f"platforms 表里没有 {name}"
    for pid in ("chaoxing", "icourse163", "zhihuishu", "manual", "demo"):
        assert pid in out, f"platforms 表里没有 id {pid}"


def test_platforms_json_is_machine_readable(capsys):
    assert run("platforms", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list)
    ids = {item["id"] for item in data}
    assert {"chaoxing", "icourse163", "zhihuishu", "manual", "demo"} <= ids
    for item in data:
        assert item["status"] and item["name"]


def test_config_path_works_without_a_config(home, capsys):
    assert run("--home", home, "config", "--path") == 0
    assert "config.json" in capsys.readouterr().out


def test_config_validate_reports_missing_config(home, capsys):
    code = run("--home", home, "config", "--validate")
    out = capsys.readouterr().out + capsys.readouterr().err
    assert code in (0, 1)
    assert "config" in out.lower()


def test_config_show_never_leaks_a_cookie(home, capsys):
    assert run("--home", home, "init", "--cookie", "UID=绝对不能泄漏", "--platform", "chaoxing") == 0
    capsys.readouterr()
    assert run("--home", home, "config", "--show") == 0
    out = capsys.readouterr().out
    assert "绝对不能泄漏" not in out
    assert "chaoxing" in out


def test_init_creates_config_and_dirs(home, capsys):
    assert run("--home", home, "init") == 0
    capsys.readouterr()
    from pathlib import Path

    root = Path(home)
    assert (root / "config.json").is_file()
    assert (root / "data").is_dir()
    assert (root / "out").is_dir()
    cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
    assert "accounts" in cfg and "http" in cfg


def test_init_refuses_to_clobber_without_force(home):
    assert run("--home", home, "init") == 0
    from pathlib import Path

    cfg = Path(home) / "config.json"
    cfg.write_text('{"accounts": [{"platform": "chaoxing", "cookie": "我的"}]}', encoding="utf-8")
    assert run("--home", home, "init") != 0
    assert "我的" in cfg.read_text(encoding="utf-8")     # 原文件没被动
    assert run("--home", home, "init", "--force") == 0


def test_doctor_is_clean_on_a_fresh_workspace(home, capsys):
    """全新检出（还没 config.json）不是故障：``doctor`` 必须退出 0。

    这条以前写成 ``in (0, 1)``，松到看不出问题——于是 CI 拿它当门禁时，
    干净检出上永远红，而真正的坏掉（只读约束被绕过）和「还没建配置」混在一起。
    """
    from pathlib import Path

    assert run("--home", home, "doctor") == 0
    out = capsys.readouterr().out
    assert "配置文件" in out
    assert "还没创建" in out
    # 自检是只读的，不该顺手创建任何东西
    assert not (Path(home) / "config.json").exists()


def test_doctor_marks_missing_config_as_a_warning_not_a_failure(home, capsys):
    assert run("--home", home, "doctor", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    by_name = {item["name"]: item for item in data}

    assert by_name["配置文件"]["level"] == "warn"
    assert by_name["配置文件"]["ok"] is True
    assert by_name["Python 版本"]["level"] == "ok"
    assert all(item["ok"] for item in data), "干净检出上不该有任何 fail"


def test_doctor_fails_when_the_readonly_contract_is_broken(home, monkeypatch, capsys):
    """真的坏掉时必须非 0——否则这个门禁没有意义。"""
    from studydash import http as http_mod

    monkeypatch.setattr(
        http_mod, "assert_readonly", lambda paths=None: ["src/studydash/oops.py"]
    )
    assert run("--home", home, "doctor") == 1
    out = capsys.readouterr().out
    assert "只读约束" in out
    assert "oops.py" in out


def test_doctor_json_is_parseable_alongside_text_mode(home, capsys):
    run("--home", home, "doctor", "--json")
    assert json.loads(capsys.readouterr().out)


# --------------------------------------------------------------------------- #
# 控制台编码
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(os.name != "nt", reason="控制台编码处理只在 Windows 上生效")
def test_setup_console_forces_utf8_on_redirected_output(monkeypatch):
    """管道 / 重定向时也得吐 UTF-8。

    Windows 上 Python 的 stdout 默认跟着本机 ANSI 代码页（简中 GBK），于是
    `studydash doctor --json | jq` 拿到的是 GBK 字节、解析直接失败；重定向到
    文件写出来的也不是 UTF-8。所以这里不区分是不是真控制台，一律对齐 UTF-8。
    """

    class Recorder:
        def __init__(self):
            self.calls = []

        def isatty(self):
            return False

        def reconfigure(self, **kwargs):
            self.calls.append(kwargs)

    out, err = Recorder(), Recorder()
    monkeypatch.setattr(cli.sys, "stdout", out)
    monkeypatch.setattr(cli.sys, "stderr", err)

    cli._setup_console()

    assert out.calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert err.calls == [{"encoding": "utf-8", "errors": "replace"}]


@pytest.mark.skipif(os.name != "nt", reason="控制台编码处理只在 Windows 上生效")
def test_setup_console_tolerates_streams_without_reconfigure(monkeypatch):
    """pytest / IDE 会把 sys.stdout 换成没有 reconfigure 的对象，别在这儿炸。"""

    class Bare:
        def isatty(self):
            return False

    monkeypatch.setattr(cli.sys, "stdout", Bare())
    monkeypatch.setattr(cli.sys, "stderr", None)

    cli._setup_console()          # 不抛就算过


# --------------------------------------------------------------------------- #
# demo / build / import / probe / open / serve
# --------------------------------------------------------------------------- #

def test_demo_writes_a_dashboard(home, capsys):
    demo(home)
    from pathlib import Path

    out_file = Path(home) / "out" / "dashboard.html"
    assert out_file.is_file()
    assert out_file.stat().st_size > 20_000
    assert "__STUDYDASH_PAYLOAD__" not in out_file.read_text(encoding="utf-8")


def test_demo_save_writes_progress_json(home):
    demo(home, "--save")
    from pathlib import Path

    data = json.loads((Path(home) / "data" / "progress.json").read_text(encoding="utf-8"))
    assert data["meta"]["mode"] == "demo"
    assert data["courses"]


def test_demo_json_prints_the_payload(home, capsys):
    demo(home, "--json")
    payload = json.loads(capsys.readouterr().out)
    assert payload["meta"]["readonly"] is True
    assert payload["overview"]["courses"] > 0


def test_build_needs_data_and_says_so(home, capsys):
    code = run("--home", home, "build")
    out = capsys.readouterr().out + capsys.readouterr().err
    assert code != 0
    assert "demo" in out          # 提示里应该指向 demo


def test_build_from_an_explicit_payload_file(home, tmp_path, capsys):
    src = tmp_path / "p.json"
    src.write_text(
        json.dumps(
            {
                "meta": {"version": "2.0.0", "mode": "test"},
                "overview": {}, "platforms": [], "timeline": [],
                "courses": [{"id": "c", "name": "只有一门课", "platform": "manual"}],
                "accounts": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert run("--home", home, "build", "--from", str(src)) == 0
    from pathlib import Path

    html = (Path(home) / "out" / "dashboard.html").read_text(encoding="utf-8")
    assert "只有一门课" in html


def test_import_init_writes_a_template(home):
    assert run("--home", home, "import", "--init") == 0
    from pathlib import Path

    candidates = list(Path(home).rglob("manual.*"))
    assert candidates, "import --init 没有生成模板文件"
    text = candidates[0].read_text(encoding="utf-8")
    assert "courses" in text or "course_id" in text


def test_import_reads_a_manual_file(home, tmp_path):
    src = tmp_path / "mine.json"
    src.write_text(
        json.dumps({"courses": [{"id": "m", "name": "线下课", "platform": "manual"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert run("--home", home, "import", str(src)) == 0
    from pathlib import Path

    assert (Path(home) / "out" / "dashboard.html").is_file()


def test_open_without_a_dashboard_explains_itself(home, capsys):
    code = run("--home", home, "open")
    out = capsys.readouterr().out + capsys.readouterr().err
    assert code != 0
    assert "demo" in out or "build" in out


def test_fetch_without_config_points_at_init(home, capsys):
    code = run("--home", home, "fetch")
    out = capsys.readouterr().out + capsys.readouterr().err
    assert code != 0
    assert "init" in out or "config" in out


def test_fetch_demo_mode_needs_no_cookie(home):
    demo(home, "--save")
    assert run("--home", home, "fetch", "--demo") == 0


def test_all_demo_no_open_produces_everything(home):
    assert run("--home", home, "all", "--demo", "--no-open") == 0
    from pathlib import Path

    root = Path(home)
    assert (root / "out" / "dashboard.html").is_file()
    assert (root / "data" / "progress.json").is_file()


def test_probe_unknown_platform_fails_clearly(home, capsys):
    code = run("--home", home, "probe", "根本没有这个平台")
    out = capsys.readouterr().out + capsys.readouterr().err
    assert code != 0
    assert "根本没有这个平台" in out


def test_quiet_suppresses_the_banner(home, capsys):
    run("--home", home, "-q", "demo")
    assert "已生成" not in capsys.readouterr().out


def test_verbose_prints_debug_lines(home, capsys):
    run("--home", home, "-v", "demo")
    out = capsys.readouterr().out
    assert "debug" in out.lower() or "工作区" in out


# --------------------------------------------------------------------------- #
# 共用 cmd_* 不能假设 args 来自自己的 parser
# --------------------------------------------------------------------------- #

def test_cmd_all_survives_foreign_namespace(tmp_path, monkeypatch):
    """``all`` 的两个开关（demo / no_open）不该被 cmd_all 用属性直取。

    跑 `all` 的时候它们当然在；但 cmd_all 是共用的，只要有人从别处调它，或者某个
    将来新增的调用方少给了默认值，属性直取就是 AttributeError——用户看到的是
    「功能调用失败」这种毫无信息量的错。这里故意递一个残缺的 Namespace。
    """
    import argparse

    from studydash import cli as cli_mod

    ws = tmp_path / "ws"
    (ws / "data").mkdir(parents=True)
    # 先造出一份能渲染的 payload
    assert run("--home", str(ws), "demo", "--save") == 0

    monkeypatch.setattr(cli_mod.webbrowser, "open", lambda url: None)
    args = argparse.Namespace(
        home=str(ws), verbose=False, quiet=True, json=False, demo=True,
        only="", open=False,
    )
    # 没有 demo / no_open 字段，也不该炸
    rc = cli_mod.cmd_all(args)
    assert rc == 0
    assert (ws / "out" / "dashboard.html").exists()


def test_cmd_all_renders_even_when_fetch_fails(tmp_path, monkeypatch):
    """抓取失败不该把已有的看板一起吞掉：进度文件里带着警告，看板照出。"""
    import argparse
    from pathlib import Path

    from studydash import cli as cli_mod

    ws = tmp_path / "ws"
    assert run("--home", str(ws), "demo", "--save") == 0
    before = (ws / "out" / "dashboard.html").read_bytes()

    def boom(*a, **kw):
        return 1

    monkeypatch.setattr(cli_mod, "cmd_fetch", boom)
    monkeypatch.setattr(cli_mod.webbrowser, "open", lambda url: None)
    args = argparse.Namespace(home=str(ws), verbose=False, quiet=True, json=False)
    assert cli_mod.cmd_all(args) == 0
    assert Path(ws / "out" / "dashboard.html").read_bytes() == before
