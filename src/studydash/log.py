"""极简日志：带前缀、可静音、可落盘。不引 loguru 是为了零依赖。"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TextIO

__all__ = ["Log", "make_log"]

_LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40, "silent": 99}

_PREFIX = {
    "debug": "·",
    "info": " ",
    "warn": "!",
    "error": "×",
}


@dataclass
class Log:
    level: str = "info"
    stream: TextIO | None = None
    file: Path | None = None
    quiet: bool = False
    _fh: TextIO | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.stream = self.stream or sys.stdout
        if self.file is not None:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.file.open("a", encoding="utf-8")

    # -- 级别 -------------------------------------------------------------- #

    def _emit(self, level: str, message: str) -> None:
        if self.quiet or _LEVELS.get(level, 20) < _LEVELS.get(self.level, 20):
            return
        line = f"{_PREFIX.get(level, ' ')} {message}"
        if self.stream is not None:
            try:
                print(line, file=self.stream, flush=True)
            except (OSError, UnicodeEncodeError):
                # 控制台编码兜不住时退回替换字符，绝不因为日志把程序打挂
                try:
                    enc = getattr(self.stream, "encoding", None) or "utf-8"
                    print(line.encode(enc, "replace").decode(enc, "replace"), file=self.stream)
                except Exception:                          # pragma: no cover
                    pass
        if self._fh is not None:
            stamp = time.strftime("%H:%M:%S")
            self._fh.write(f"[{stamp}] {level.upper():5} {message}\n")
            self._fh.flush()

    def debug(self, message: str) -> None:
        self._emit("debug", message)

    def info(self, message: str) -> None:
        self._emit("info", message)

    def warn(self, message: str) -> None:
        self._emit("warn", message)

    def error(self, message: str) -> None:
        self._emit("error", message)

    def ok(self, message: str) -> None:
        self._emit("info", f"✓ {message}")

    def step(self, message: str) -> None:
        self._emit("info", f"▸ {message}")

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def make_log(
    level: str = "info",
    *,
    quiet: bool = False,
    file: Path | None = None,
    stream: TextIO | None = None,
) -> Log:
    return Log(level=level, quiet=quiet, file=file, stream=stream)


def collect(messages: list[str]) -> Callable[[str], None]:
    """给测试用：把日志收进列表而不是打到终端。"""
    def sink(message: str) -> None:
        messages.append(message)
    return sink
