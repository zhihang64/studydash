"""章节测验（quiz.py）的单元测试：解析、答案映射、演练/交卷路径。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from studydash.http import HttpError
from studydash.model import Course
from studydash.providers.base import HttpSettings
from studydash.quiz import (
    EP_SUBMIT,
    QuizRunner,
    QuizTarget,
    answer_to_field,
    parse_page,
)
from studydash.store import AccountConfig

QUIZ_HTML = """
<html><body>
<form id="form1">
  <input type="hidden" name="courseId" value="266688496">
  <input type="hidden" name="clazzid" value="151567127">
  <input type="hidden" name="answer111" value="">
</form>
<div class="singleQuesId" data="111">
  <div class="TiMu" data="0">
    <div class="Zy_TItle"><span>1. 计算机的英文缩写是</span></div>
    <ul>
      <li aria-label="A、Computer"></li>
      <li aria-label="B、Calculator"></li>
    </ul>
  </div>
</div>
<div class="singleQuesId" data="222">
  <div class="TiMu" data="3">
    <div class="Zy_TItle">2、学习通可以刷课</div>
    <ul>
      <li aria-label="对"></li>
      <li aria-label="错"></li>
    </ul>
  </div>
</div>
</body></html>
"""


def _account() -> AccountConfig:
    return AccountConfig(platform="chaoxing", cookie="UID=1; fid=8888; _uid=10086")


def _runner(**kwargs) -> QuizRunner:
    kwargs.setdefault("sleep", lambda _s: None)
    return QuizRunner(_account(), HttpSettings(delay=0.0, retries=0), **kwargs)


class FakeHttp:
    """只记调用、不发网络。"""

    def __init__(self, html: str = "", *, submit=None, boom: Exception | None = None) -> None:
        self.html = html
        self.submit = submit
        self.boom = boom
        self.calls: list[tuple[str, str]] = []
        self.posted: dict[str, str] = {}

    def get_text(self, url, params=None, *, headers=None, note=""):
        self.calls.append(("GET", url))
        if self.boom is not None:
            raise self.boom
        return self.html

    def post_query(self, url, data=None, *, headers=None, note=""):
        self.calls.append(("POST", url))
        self.posted = dict(data or {})
        if self.boom is not None:
            raise self.boom
        return self.submit


class FakeTiku:
    """按关键词命中给答案。"""

    def __init__(self, answers: dict[str, str] | None = None) -> None:
        self.answers = answers or {}
        self.asked: list[str] = []

    def query(self, title: str):
        self.asked.append(title)
        for needle, answer in self.answers.items():
            if needle in title:
                return SimpleNamespace(answer=answer, message="")
        return SimpleNamespace(answer="", message="没找到")


def _target(**kwargs) -> QuizTarget:
    base = {
        "course_id": "266688496",
        "course_name": "课程 A",
        "clazz_id": "151567127",
        "cpi": "406429824",
        "knowledge_id": "1001",
        "ktoken": "KT",
        "jobid": "work-555",
        "enc": "ENC123",
        "chapter": "第一章",
        "title": "小测",
    }
    base.update(kwargs)
    return QuizTarget(**base)


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #


def test_parse_page_reads_fields_and_questions() -> None:
    page = parse_page(QUIZ_HTML)
    assert page.fields == {"courseId": "266688496", "clazzid": "151567127"}
    assert len(page.questions) == 2
    first, second = page.questions
    assert first.id == "111"
    assert first.type_code == "0"
    assert first.type_name == "single"
    assert "计算机的英文缩写" in first.title
    assert first.options == [("A", "Computer"), ("B", "Calculator")]
    assert second.id == "222"
    assert second.type_code == "3"
    assert second.type_name == "judgement"


def test_parse_page_keeps_letters_in_order() -> None:
    html = """
    <div class="singleQuesId" data="9">
      <div class="TiMu" data="1"><div class="Zy_TItle">多选</div>
        <ul><li aria-label="A、甲"></li><li aria-label="B、乙"></li><li aria-label="C、丙"></li></ul>
      </div>
    </div>"""
    page = parse_page(html)
    assert [letter for letter, _ in page.questions[0].options] == ["A", "B", "C"]
    assert [text for _, text in page.questions[0].options] == ["甲", "乙", "丙"]


def test_submit_data_marks_save_only() -> None:
    page = parse_page(QUIZ_HTML)
    answers = {"111": "A", "222": "true"}
    data = page.submit_data(answers, save_only=True)
    assert data["courseId"] == "266688496"
    assert data["answer111"] == "A"
    assert data["answertype111"] == "0"
    assert data["answer222"] == "true"
    assert data["answerwqbid"] == "111,222,"
    assert data["pyFlag"] == "1"
    assert page.submit_data(answers, save_only=False)["pyFlag"] == ""


# --------------------------------------------------------------------------- #
# 答案映射
# --------------------------------------------------------------------------- #


def _question(type_code: str, options: list[tuple[str, str]]) -> SimpleNamespace:
    return SimpleNamespace(
        id="1",
        type_code=type_code,
        options=options,
        type_name={"0": "single", "1": "multiple", "2": "completion", "3": "judgement"}[type_code],
    )


def test_answer_to_field_single_accepts_letter_and_text() -> None:
    question = _question("0", [("A", "Computer"), ("B", "Calculator")])
    assert answer_to_field(question, "A") == "A"
    assert answer_to_field(question, "b") == "B"
    assert answer_to_field(question, "Computer") == "A"
    assert answer_to_field(question, "计算机") == ""


def test_answer_to_field_multiple_splits_and_sorts() -> None:
    question = _question("1", [("A", "甲"), ("B", "乙"), ("C", "丙")])
    assert answer_to_field(question, "C,A") == "AC"
    assert answer_to_field(question, "AC") == "AC"
    assert answer_to_field(question, "甲") == "A"


def test_answer_to_field_judgement() -> None:
    question = _question("3", [("A", "对"), ("B", "错")])
    assert answer_to_field(question, "对") == "true"
    assert answer_to_field(question, "错误") == "false"
    assert answer_to_field(question, "也许") == ""


def test_answer_to_field_completion_passthrough() -> None:
    question = _question("2", [])
    assert answer_to_field(question, "  十九  ") == "十九"


# --------------------------------------------------------------------------- #
# 采集
# --------------------------------------------------------------------------- #


def _course() -> Course:
    return Course(
        id="266688496",
        name="课程 A",
        platform="chaoxing",
        extra={"class_id": "151567127", "cpi": "406429824"},
    )


def test_iter_targets_only_yields_workid_nodes(monkeypatch) -> None:
    runner = _runner()
    monkeypatch.setattr(runner.provider, "_chapters_of", lambda _c: [("1001", "第一章")])

    cards = {
        "defaults": {"clazzId": "9", "cpi": "77", "ktoken": "KT", "knowledgeid": "1001"},
        "attachments": [
            {"type": "workid", "jobid": "work-555", "enc": "E1", "property": {"title": "小测"}},
            {"type": "video", "jobid": "video-1"},
            {"type": "workid"},
        ],
    }
    pages = {0: cards, 1: None}
    monkeypatch.setattr(runner, "_cards", lambda _c, _k, page: pages.get(page))

    targets = list(runner.iter_targets(_course()))
    assert len(targets) == 1
    target = targets[0]
    assert target.jobid == "work-555"
    assert target.work_id == "555"
    assert target.clazz_id == "9"
    assert target.cpi == "77"
    assert target.ktoken == "KT"
    assert target.title == "小测"
    assert target.chapter == "第一章"


def test_cards_parses_marg_json(monkeypatch) -> None:
    payload = {"defaults": {"clazzId": "9"}, "attachments": [{"type": "workid", "jobid": "w"}]}
    html = "try{\n mArg = " + json.dumps(payload) + ";\n}catch(e){"
    runner = _runner()
    runner.http = FakeHttp(html)
    data = runner._cards(_course(), "1001", 0)
    assert data == payload


def test_fetch_page_builds_platform_url() -> None:
    runner = _runner()
    fake = FakeHttp(QUIZ_HTML)
    runner.http = fake
    assert runner.fetch_page(_target()) == QUIZ_HTML
    assert fake.calls and fake.calls[0][0] == "GET"
    url = fake.calls[0][1]
    assert url.startswith("https://mooc1.chaoxing.com/mooc-ans/api/work?")
    assert "workId=555" in url
    assert "jobid=work-555" in url
    assert "enc=ENC123" in url
    assert "clazzId=151567127" in url


def test_plan_respects_limit(monkeypatch) -> None:
    runner = _runner()
    monkeypatch.setattr(runner, "iter_targets", lambda _c: iter([_target(), _target(jobid="work-9")]))
    assert len(runner.plan([_course()], limit=1)) == 1
    assert len(runner.plan([_course()])) == 2


# --------------------------------------------------------------------------- #
# 演练 / 交卷
# --------------------------------------------------------------------------- #


def test_solve_dry_run_sends_no_submit() -> None:
    runner = _runner(tiku=FakeTiku({"计算机": "A", "刷课": "对"}))
    fake = FakeHttp(QUIZ_HTML)
    runner.http = fake
    result = runner.solve(_target())
    assert result["status"] == "dry"
    assert result["ok"] is None
    assert result["total"] == 2
    assert result["answered"] == 2
    assert result["requests"] == 1
    assert result["submitted"] is False
    assert [kind for kind, _ in fake.calls] == ["GET"]


def test_solve_apply_submits_full_coverage() -> None:
    runner = _runner(apply=True, tiku=FakeTiku({"计算机": "A", "刷课": "对"}))
    fake = FakeHttp(QUIZ_HTML, submit=SimpleNamespace(status=200, content=b'{"status":true,"msg":"ok"}'))
    runner.http = fake
    result = runner.solve(_target())
    assert result["status"] == "passed"
    assert result["ok"] is True
    assert result["submitted"] is True
    assert result["requests"] == 2
    assert fake.calls[-1] == ("POST", EP_SUBMIT)
    assert fake.posted["answer111"] == "A"
    assert fake.posted["answer222"] == "true"
    assert fake.posted["pyFlag"] == ""


def test_solve_low_coverage_saves_only() -> None:
    runner = _runner(apply=True, tiku=FakeTiku())
    fake = FakeHttp(QUIZ_HTML, submit=SimpleNamespace(status=200, content=b'{"status":true,"msg":"ok"}'))
    runner.http = fake
    result = runner.solve(_target())
    assert result["status"] == "unconfirmed"
    assert result["ok"] is False
    assert result["submitted"] is False
    assert "没交卷" in result["reason"]
    assert fake.posted["pyFlag"] == "1"


def test_solve_without_questions_fails() -> None:
    runner = _runner(apply=True)
    runner.http = FakeHttp("<html><body>空的</body></html>")
    result = runner.solve(_target())
    assert result["status"] == "failed"
    assert result["ok"] is False
    assert "没有解析到题目" in result["reason"]


def test_solve_teacher_not_ready_skips() -> None:
    runner = _runner(apply=True)
    runner.http = FakeHttp("<html>教师未创建完成该测验</html>")
    result = runner.solve(_target())
    assert result["status"] == "skipped"
    assert result["ok"] is None


def test_solve_reports_login_gate() -> None:
    runner = _runner(apply=True)
    runner.blocked = "登录态失效"
    result = runner.solve(_target())
    assert result["status"] == "failed"
    assert result["reason"] == "登录态失效"


def test_submit_http_error_marks_failed() -> None:
    runner = _runner(apply=True, tiku=FakeTiku({"计算机": "A", "刷课": "对"}))

    class PostBroken(FakeHttp):
        def post_query(self, url, data=None, *, headers=None, note=""):
            self.calls.append(("POST", url))
            raise HttpError(url, "连接被重置")

    runner.http = PostBroken(QUIZ_HTML)
    result = runner.solve(_target())
    assert result["status"] == "failed"
    assert result["ok"] is False


def test_submit_platform_rejects() -> None:
    runner = _runner(apply=True, tiku=FakeTiku({"计算机": "A", "刷课": "对"}))
    runner.http = FakeHttp(QUIZ_HTML, submit=SimpleNamespace(status=200, content=b'{"status":false}'))
    result = runner.solve(_target())
    assert result["status"] == "unconfirmed"
    assert result["ok"] is False


def test_runner_instantiates_without_log() -> None:
    runner = _runner()
    assert runner.log is None
    assert runner.cover_rate == pytest.approx(0.8)

def test_option_label_strips_letter_prefix_with_space() -> None:
    """有的课程 aria-label 是「A 太湖石」（字母+空格，没有顿号），别把前缀留在选项里。"""
    html = """
    <form action="/work/addStudentWorkNew" method="post">
      <input type="hidden" name="courseId" value="266688496" />
      <div class="singleQuesId" data="1">
        <div class="TiMu" data="0"></div>
        <div class="Zy_TItle">奇石鉴赏</div>
        <ul>
          <li aria-label="A 太湖石">A 太湖石</li>
          <li aria-label="B、灵璧石">B、灵璧石</li>
        </ul>
      </div>
    </form>
    """
    page = parse_page(html)
    assert page.questions[0].options == [("A", "太湖石"), ("B", "灵璧石")]
