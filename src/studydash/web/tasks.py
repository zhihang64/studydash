"""任务中心。

为什么什么都要做成任务：扫码要等用户掏手机（分钟级），抓取要等七八个平台的网络
往返（十秒级）。同步接口在这种时长下必然被浏览器或中间层掐掉，而且用户全程只能看
转圈，不知道卡在哪一步。

每个任务一个线程。这些活的耗时都在网络等待上，不是 CPU 密集，所以线程模型够用，
也不必把同步的 ``requests`` 调用改造成异步。状态一律在锁里改；前端通过轮询任务
快照或订阅 SSE 拿进度，两种方式读的是同一份数据。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping

from ..model import iso, now
from .errors import ApiError, to_api_error

__all__ = [
    "LOG_LIMIT",
    "TASK_HISTORY",
    "Reporter",
    "Task",
    "TaskManager",
    "TaskStream",
    "make_task_id",
]

#: 保留多少个任务的历史（含已结束的）
TASK_HISTORY = 50
#: 单个任务最多留多少条日志。扫码任务会连着跑几分钟，不封顶会漏内存。
LOG_LIMIT = 200

#: 任务实现函数的签名：``(state, reporter, options)``。第一个参数是 ``AppState``，
#: 不是 ``Task`` —— 写成 ``Task`` 的话静态检查会一路点头，运行时每个任务都炸。
Runner = Callable[[Any, "Reporter", Mapping[str, Any]], Any]


def make_task_id() -> str:
    return "t_" + uuid.uuid4().hex[:6]


def _level_of(line: str) -> str:
    """从 ``Log`` 打的带前缀行里还原出级别。

    ``Log._emit`` 写的是 ``f"{前缀} {消息}"``，前缀本身就编码了级别，所以不用改
    ``log.py`` 也能把级别带过来。
    """
    if not line:
        return "info"
    if line[0] == "·":
        return "debug"
    if line[0] == "!":
        return "warn"
    if line[0] == "×":
        return "error"
    rest = line.strip()
    if rest.startswith("✓"):
        return "ok"
    return "info"


class TaskStream:
    """冒充文件对象，把 ``Log`` 的每一行变成一条任务日志。

    ``Log`` 用 ``print(..., file=stream)`` 输出，所以只要实现 ``write`` 就够。
    再给个 ``encoding``，因为 ``Log`` 的兜底分支会读它。
    """

    encoding = "utf-8"

    def __init__(self, reporter: "Reporter") -> None:
        self._rep = reporter

    def write(self, text: str) -> int:
        raw = str(text)
        for line in raw.splitlines():
            if line.strip():
                self._rep.log(_level_of(line), line.strip())
        return len(raw)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False


@dataclass
class Task:
    id: str
    kind: str
    options: dict[str, Any] = field(default_factory=dict)
    state: str = "pending"
    progress: float = 0.0
    message: str = ""
    created_at: datetime = field(default_factory=now)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    cancel_requested: bool = False
    logs: list[dict[str, Any]] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    TERMINAL = ("done", "failed", "cancelled")

    @property
    def finished(self) -> bool:
        return self.state in self.TERMINAL

    def to_dict(self, *, full: bool = True) -> dict[str, Any]:
        """序列化。

        ``options`` **永远不出现在快照里**：``login_password`` 的密码就在里面，
        而任务快照会经过 HTTP 回到浏览器、可能被贴进 issue。这是硬要求。
        """
        out: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "state": self.state,
            "progress": round(self.progress, 4),
            "message": self.message,
            "created_at": iso(self.created_at),
            "started_at": iso(self.started_at),
            "ended_at": iso(self.ended_at),
            "cancel_requested": self.cancel_requested,
            "result": self.result,
            "error": self.error,
        }
        if full:
            out["logs"] = list(self.logs)
            out["artifacts"] = dict(self.artifacts)
        return out


class Reporter:
    """任务实现拿到的汇报口子。

    任务实现只认识这个对象，不认识 HTTP，所以能在测试里同步跑。
    """

    def __init__(self, manager: "TaskManager", task: Task) -> None:
        self._manager = manager
        self._task = task
        self._stream: TaskStream | None = None

    # -- 汇报 -------------------------------------------------------------- #

    def log(self, level: str, text: str) -> None:
        self._manager.add_log(self._task, level, text)

    def progress(self, value: float, message: str = "") -> None:
        self._manager.set_progress(self._task, value, message)

    def message(self, text: str) -> None:
        """只更新文案，不动进度条。"""
        self._manager.set_progress(self._task, None, text)

    def artifact(self, key: str, value: Any) -> None:
        self._manager.set_artifact(self._task, key, value)

    # -- 给 Log 用的假文件 -------------------------------------------------- #

    @property
    def stream(self) -> TaskStream:
        if self._stream is None:
            self._stream = TaskStream(self)
        return self._stream

    # -- 取消 -------------------------------------------------------------- #

    @property
    def cancelled(self) -> bool:
        return self._task.cancel_requested

    def check_cancelled(self) -> None:
        if self._task.cancel_requested:
            raise ApiError("conflict", "任务已被取消")


class TaskManager:
    """任务的创建、查询、取消。

    同一 ``kind`` 同时只允许跑一个：两个并行的抓取除了互相抢限速额度之外没有意义，
    而两个并行的扫码会让用户不知道该扫哪一张。不同 kind 之间不互相阻塞。
    """

    def __init__(self, *, history: int = TASK_HISTORY, log_limit: int = LOG_LIMIT) -> None:
        self._lock = threading.RLock()
        self._tasks: dict[str, Task] = {}
        self._order: list[str] = []
        self.history = history
        self.log_limit = log_limit

    # -- 创建 -------------------------------------------------------------- #

    def active(self, kind: str) -> Task | None:
        with self._lock:
            for task in self._tasks.values():
                if task.kind == kind and not task.finished:
                    return task
        return None

    def create(
        self,
        kind: str,
        options: Mapping[str, Any] | None,
        runner: Runner,
        *,
        state: Any = None,
    ) -> Task:
        with self._lock:
            running = self.active(kind)
            if running is not None:
                raise ApiError(
                    "busy",
                    f"已经有一个「{kind}」任务在跑了",
                    hint="等它结束，或者先取消它。",
                    detail={"task_id": running.id, "kind": kind},
                )
            task = Task(id=make_task_id(), kind=kind, options=dict(options or {}))
            self._tasks[task.id] = task
            self._order.append(task.id)
            self._trim()

        thread = threading.Thread(
            target=self._run,
            args=(task, runner, state),
            name=f"studydash-{task.id}",
            daemon=True,
        )
        thread.start()
        return task

    # -- 查询 -------------------------------------------------------------- #

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def require(self, task_id: str) -> Task:
        task = self.get(task_id)
        if task is None:
            raise ApiError("not_found", f"没有这个任务：{task_id}")
        return task

    def list(self, *, limit: int = 50) -> list[Task]:
        with self._lock:
            ids = list(self._order)[-max(1, limit):]
            return [self._tasks[i] for i in reversed(ids) if i in self._tasks]

    def snapshot(self, task_id: str, *, full: bool = True) -> dict[str, Any]:
        return self.require(task_id).to_dict(full=full)

    def cancel(self, task_id: str) -> dict[str, Any]:
        task = self.require(task_id)
        with self._lock:
            if task.finished:
                done = {"done": "完成", "failed": "失败", "cancelled": "取消"}
                raise ApiError(
                    "conflict",
                    f"任务已经{done.get(task.state, task.state)}了，取消不了",
                    detail={"state": task.state},
                )
            task.cancel_requested = True
            task.message = task.message or "正在取消…"
        return task.to_dict()

    def wait(self, task_id: str, *, timeout: float = 30.0) -> Task:
        """阻塞到任务结束。给测试用；HTTP 层不要用它。"""
        import time as _time

        deadline = _time.monotonic() + timeout
        task = self.require(task_id)
        while not task.finished and _time.monotonic() < deadline:
            _time.sleep(0.02)
        return task

    # -- 内部：状态更新 ---------------------------------------------------- #

    def add_log(self, task: Task, level: str, text: str) -> None:
        with self._lock:
            task.logs.append({"ts": iso(now()), "level": level, "text": text})
            if len(task.logs) > self.log_limit:
                del task.logs[: len(task.logs) - self.log_limit]

    def set_progress(self, task: Task, value: float | None, message: str = "") -> None:
        with self._lock:
            if value is not None:
                try:
                    num = float(value)
                except (TypeError, ValueError):
                    num = task.progress
                task.progress = min(1.0, max(0.0, num))
            if message:
                task.message = message

    def set_artifact(self, task: Task, key: str, value: Any) -> None:
        with self._lock:
            task.artifacts[key] = value

    def _trim(self) -> None:
        while len(self._order) > self.history:
            oldest = self._order[0]
            if not self._tasks.get(oldest, Task("", "")).finished:
                break                                   # 还在跑的不淘汰
            self._order.pop(0)
            self._tasks.pop(oldest, None)

    # -- 内部：跑 ---------------------------------------------------------- #

    def _run(self, task: Task, runner: Runner, state: Any = None) -> None:
        """跑一个任务。

        ``state`` 必须是调用方传进来的 ``AppState`` —— 这里曾经图省事把 ``task``
        当第一个参数递给 runner，症状是每个任务都立刻失败在
        ``'Task' object has no attribute 'workspace'``。更阴的是扫码那条路：
        它在拿到 ``state`` 之前就已经把二维码画到快照里了，所以界面看着完全正常，
        直到真的登录成功才炸。别再把这两个东西搞混。
        """
        with self._lock:
            task.state = "running"
            task.started_at = now()
            task.message = task.message or "开始"
        reporter = Reporter(self, task)
        try:
            result = runner(state, reporter, task.options)
        except ApiError as exc:
            self._fail(task, exc.to_dict()["error"])
        except BaseException as exc:                    # noqa: BLE001
            # 任务线程绝不能带着 running 状态死掉，否则前端会一直转圈、
            # 而且同一个 kind 再也起不来（busy）。所以这里连 BaseException 都要接住。
            self._fail(task, to_api_error(exc).to_dict()["error"])
        else:
            with self._lock:
                task.ended_at = now()
                if task.cancel_requested:
                    task.state = "cancelled"
                    task.message = task.message if task.message != "开始" else "已取消"
                else:
                    task.state = "done"
                    task.progress = 1.0
                    task.result = result if isinstance(result, dict) else {"value": result}
                    if not task.message or task.message == "开始":
                        task.message = "完成"

    def _fail(self, task: Task, error: dict[str, Any]) -> None:
        with self._lock:
            task.state = "failed"
            task.ended_at = now()
            task.error = error
            task.message = str(error.get("message") or "失败")
