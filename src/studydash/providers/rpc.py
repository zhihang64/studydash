"""RPC 风格平台的公共骨架。

慕课、智慧树这类平台没有「一个 URL 返回一个 HTML 页面」的朴素接口，它们是
RPC：所有查询都打同一个 ``*.rpc`` 地址，用表单参数区分。这种接口天生只能用
POST 调用。

这就和「只读」冲突了。处理方式是**白名单**（见 :mod:`studydash.http`）：

* 适配器在 ``endpoints`` 里声明自己只会 POST 到哪几个地址；
* 白名单地址里出现 ``submit`` / ``save`` / ``answer`` / ``commit`` 之类的
  词，客户端在构造阶段就直接拒绝启动；
* 没声明的地址一律 ``ReadOnlyError``。

也就是说，**能不能 POST 不取决于调用点，取决于地址本身**。想偷偷提交作业，
必须先改白名单，而白名单是被测试和代码审查盯着的。
"""

from __future__ import annotations

from typing import Any, ClassVar, Iterable

from ..http import ReadOnlyError
from ..model import Course
from .base import AuthError, Provider

__all__ = ["RpcProvider", "find_lists", "walk_dicts"]


class RpcProvider(Provider):
    """按候选查询逐个尝试，谁先返回能解析的数据就用谁。"""

    #: POST 查询端点白名单（地址前缀）
    endpoints: ClassVar[tuple[str, ...]] = ()

    #: 依次尝试的查询：``(说明, url)``。前一个返回空就试下一个。
    queries: ClassVar[tuple[tuple[str, str], ...]] = ()

    #: 是否必须先有 Cookie
    needs_cookie: ClassVar[bool] = True

    def client_kwargs(self) -> dict[str, Any]:
        return {"query_endpoints": self.endpoints}

    def query_form(self, url: str) -> dict[str, Any]:
        """POST 表单。子类按平台要求覆盖。"""
        return {}

    def parse(self, note: str, url: str, payload: Any) -> list[Course]:
        raise NotImplementedError

    # ------------------------------------------------------------------ #

    def fetch(self) -> list[Course]:
        # 这里**故意不捕 AuthError**。曾经捕了并 return []，结果 Cookie 过期时
        # 看板上显示「0 门课」——和「这个账号真的没选课」长得一模一样，用户会以为
        # 工具坏了或者自己没课，而不是「去重新贴一次 Cookie」。让它抛出去：
        # aggregate() 会把它收进 Account(ok=False, message=...)，红着显示。
        if self.needs_cookie:
            self.require_cookie()

        if not self.queries:
            self.log.warn(f"{self.meta.name}: 适配器没有声明任何查询端点")
            return []

        for note, url in self.queries:
            try:
                payload = self.http.post_query_json(
                    url, self.query_form(url), note=note, default=None
                )
            except ReadOnlyError as exc:                   # 白名单没配上，属于代码 bug
                self.log.error(f"{self.meta.name}: {exc}")
                return []
            if payload is None:
                continue
            try:
                courses = self.parse(note, url, payload)
            except Exception as exc:                        # noqa: BLE001 - 解析脏数据不该崩
                self.log.warn(f"{self.meta.name}: 解析 {note} 失败: {exc}")
                continue
            if courses:
                total = sum(len(c.assignments) for c in courses)
                self.log.ok(f"{self.meta.name}: {len(courses)} 门课 / {total} 条作业（{note}）")
                return courses

        self.log.warn(
            f"{self.meta.name}: 所有候选查询都没取到数据。"
            f"接口可能已改版或 Cookie 已过期；跑 `studydash probe {self.meta.id}` "
            "把原始响应贴到 issue 里，或者改用 manual 适配器手动登记。"
        )
        return []

    def probe_plan(self) -> list[tuple]:
        plan: list[tuple] = []
        for note, url in self.queries:
            plan.append((note, url, "POST", self.query_form(url)))
        return plan


# --------------------------------------------------------------------------- #
# JSON 兜底工具
# --------------------------------------------------------------------------- #

def walk_dicts(obj: Any) -> Iterable[dict[str, Any]]:
    """深度遍历，产出所有 dict。"""
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from walk_dicts(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk_dicts(item)


def find_lists(obj: Any, keys: Iterable[str]) -> list[list]:
    """找出「键名命中 keys 且值是 list」的那些列表，按出现顺序返回。

    平台把结果包在 ``{"result": {"list": [...]}}`` 还是 ``{"data": [...]}``
    里都无所谓，这里都能捞出来——这也是这类适配器唯一现实可行的写法：
    没有稳定的字段文档，只能按名字找。
    """
    wanted = {k.lower() for k in keys}
    found: list[list] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, list) and str(key).lower() in wanted:
                found.append(value)
            found.extend(find_lists(value, wanted))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(find_lists(item, wanted))
    return found


def first_value(obj: Any, keys: Iterable[str], default: Any = None) -> Any:
    """在嵌套结构里找第一个命中的键值。"""
    wanted = [k.lower() for k in keys]
    for d in walk_dicts(obj):
        for k, v in d.items():
            if str(k).lower() in wanted and v not in (None, ""):
                return v
    return default
