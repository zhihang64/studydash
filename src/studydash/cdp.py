"""本机浏览器的 DevTools 协议客户端——只连 ``127.0.0.1``。

为什么需要它
------------

Chromium 在运行期间对 ``Cookies`` 库持**独占**句柄（实测任何 share 模式都拿到
``ERROR_SHARING_VIOLATION``），所以「浏览器开着的时候直接读它的 Cookie 文件」是
做不到的，只能请用户关掉浏览器。这对一个想「点一下就登录」的工具来说太难用了。

改成：我们**自己**起一个配置档隔离的浏览器实例、开一个本地调试端口，然后通过
DevTools 协议向浏览器要 Cookie（``Network.getAllCookies``）。浏览器不用关、不用
装扩展、不用你手抄。

安全边界
--------

* 只跟 ``127.0.0.1`` 的调试端口讲话。所有地址都由这个常量拼出来，模块里没有
  任何外部主机名——想连出去都写不出来。
* 用裸 socket 手写 WebSocket 帧，而不是借现成的 HTTP 客户端：这样本模块就不必
  引入 ``urllib``/``requests``/标准库的 HTTP 模块，``studydash doctor`` 的只读
  体检能继续把话说满（``http.py`` 的扫描会放行本模块，但另有专门检查确认这里
  只连 ``127.0.0.1``）。
* 调试端口是**本机**端口，浏览器会校验 ``Origin``；我们不发送该头，只在自己
  起的实例上连。
"""

from __future__ import annotations

import base64
import json
import os
import random
import socket
import struct
import subprocess
import time
from pathlib import Path

__all__ = [
    "CdpError",
    "CdpSession",
    "Launched",
    "browser_exe",
    "free_port",
    "launch",
    "wait_for_port",
    "cookies_via_cdp",
]

#: 唯一允许连接的主机。写死成常量，模块里不出现第二个主机名。
HOST = "127.0.0.1"

#: 挑调试端口用的区间。
#:
#: **必须避开系统的临时端口段**（Windows 默认 49152-65535）。踩过一次：
#: 先 bind(0) 拿一个端口再放掉，那个端口随即被浏览器自己的出站连接抢走
#: （本机光 Edge 就 45 个进程），于是浏览器的调试端口根本绑不上，表现就是
#: 「浏览器起来了但端口没响应」。改成从一个固定低位区间里随机挑。
PORT_RANGE = (24000, 32000)
_PORT_TRIES = 24

#: 浏览器可执行文件的候选位置（按 id）。
_EXE_CANDIDATES: dict[str, tuple[str, ...]] = {
    "edge": (
        r"{ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
        r"{ProgramFiles}\Microsoft\Edge\Application\msedge.exe",
        r"{LocalAppData}\Microsoft\Edge\Application\msedge.exe",
    ),
    "chrome": (
        r"{ProgramFiles}\Google\Chrome\Application\chrome.exe",
        r"{ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        r"{LocalAppData}\Google\Chrome\Application\chrome.exe",
    ),
    "brave": (
        r"{ProgramFiles}\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"{ProgramFiles(x86)}\BraveSoftware\Brave-Browser\Application\brave.exe",
    ),
}


class CdpError(RuntimeError):
    """和浏览器调试端口打交道失败。消息面向用户。"""


# --------------------------------------------------------------------------- #
# 找浏览器可执行文件 / 选端口
# --------------------------------------------------------------------------- #

def browser_exe(browser: str = "edge") -> Path:
    """找浏览器的可执行文件。支持用 ``STUDYDASH_BROWSER_EXE`` 直接指定。"""
    override = os.environ.get("STUDYDASH_BROWSER_EXE", "").strip()
    if override:
        path = Path(override)
        if path.is_file():
            return path
        raise CdpError(f"STUDYDASH_BROWSER_EXE 指向的 {path} 不存在")

    key = (browser or "edge").strip().lower()
    templates = _EXE_CANDIDATES.get(key)
    if not templates:
        raise CdpError(f"不知道怎么启动 {browser!r}；支持：{'、'.join(sorted(_EXE_CANDIDATES))}")

    for tpl in templates:
        raw = tpl.format(
            **{
                "ProgramFiles": os.environ.get("ProgramFiles", r"C:\Program Files"),
                "ProgramFiles(x86)": os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                "LocalAppData": os.environ.get("LOCALAPPDATA", ""),
            }
        )
        if raw and Path(raw).is_file():
            return Path(raw)
    raise CdpError(
        f"没找到 {key} 的可执行文件。装好它，或者用 STUDYDASH_BROWSER_EXE 指出路径。"
    )


def free_port() -> int:
    """挑一个**不在系统临时端口段里**的空闲端口。

    见 :data:`PORT_RANGE` 里的踩坑记录：用 bind(0) 让系统分配，会把端口分到
    49152+ 那段，那个端口随时会被本机其它进程（尤其浏览器自己）抢走。
    """
    low, high = PORT_RANGE
    for _ in range(_PORT_TRIES):
        port = random.randint(low, high)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((HOST, port))
            except OSError:
                continue
        return port
    raise CdpError(f"{low}-{high} 里找不到空闲端口")


# --------------------------------------------------------------------------- #
# 极简 HTTP（只为拿 /json/version）
# --------------------------------------------------------------------------- #

def _content_length(head: bytes) -> int | None:
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None


def _http_get(port: int, path: str, *, timeout: float = 2.0) -> bytes:
    """向调试端口发一个极简 GET，按 ``Content-Length`` 读满就返回。

    注意别写成「读到 EOF 为止」：Chromium 的 ``/json/*`` 虽然吃
    ``Connection: close``，实际却常常不关连接，于是读循环一直等到超时——
    表现是端口明明活着（用别的方式能取到 200）但这里永远判失败。
    """
    with socket.create_connection((HOST, port), timeout=timeout) as sock:
        sock.sendall(
            (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {HOST}:{port}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("ascii")
        )
        buf = b""
        while b"\r\n\r\n" not in buf:
            block = sock.recv(65536)
            if not block:
                raise CdpError("调试端口回了半截响应")
            buf += block
        head, _, body = buf.partition(b"\r\n\r\n")

        status = head.split(b"\r\n", 1)[0].split(b" ", 2)
        if len(status) < 2 or status[1] != b"200":
            raise CdpError(f"调试端口回了 {status[0].decode('latin-1') if status else '空响应'}")

        if b"transfer-encoding: chunked" in head.lower():
            try:
                while b"\r\n0\r\n\r\n" not in body:
                    block = sock.recv(65536)
                    if not block:
                        break
                    body += block
            except (TimeoutError, socket.timeout):
                pass
            return _dechunk(body)

        length = _content_length(head)
        if length is None:
            try:
                while True:
                    block = sock.recv(65536)
                    if not block:
                        break
                    body += block
            except (TimeoutError, socket.timeout):
                pass
            return body
        while len(body) < length:
            block = sock.recv(65536)
            if not block:
                break
            body += block
        return body[:length]


def _dechunk(body: bytes) -> bytes:
    out = bytearray()
    while body:
        line, sep, rest = body.partition(b"\r\n")
        if not sep:
            break
        size = int(line.split(b";", 1)[0].strip() or b"0", 16)
        if size == 0:
            break
        out += rest[:size]
        body = rest[size + 2:]
    return bytes(out)


# --------------------------------------------------------------------------- #
# 极简 WebSocket 客户端（RFC 6455，够用即可）
# --------------------------------------------------------------------------- #

class _Socket:
    """带缓冲的 socket 读；WebSocket 握手后残余字节不能丢。"""

    def __init__(self, sock: socket.socket, buffer: bytes = b""):
        self.sock = sock
        self.buf = buffer

    def need(self, count: int) -> bytes:
        while len(self.buf) < count:
            block = self.sock.recv(max(4096, count - len(self.buf)))
            if not block:
                raise CdpError("浏览器把调试连接关了")
            self.buf += block
        head, self.buf = self.buf[:count], self.buf[count:]
        return head

    def read_until(self, marker: bytes) -> bytes:
        while marker not in self.buf:
            block = self.sock.recv(4096)
            if not block:
                raise CdpError("浏览器把调试连接关了")
            self.buf += block
        head, _, rest = self.buf.partition(marker)
        self.buf = rest
        return head


def _ws_open(port: int, path: str, *, timeout: float = 10.0) -> _Socket:
    sock = socket.create_connection((HOST, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    sock.sendall(
        (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {HOST}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
    )
    conn = _Socket(sock)
    head = conn.read_until(b"\r\n\r\n")
    if b" 101" not in head.split(b"\r\n", 1)[0]:
        sock.close()
        raise CdpError(f"WebSocket 握手被拒绝：{head.split(b'\r\n', 1)[0].decode('latin-1')}")
    return conn


def _ws_send(conn: _Socket, text: str) -> None:
    payload = text.encode("utf-8")
    mask = os.urandom(4)
    header = bytearray([0x81])                      # FIN + text
    size = len(payload)
    if size < 126:
        header.append(0x80 | size)
    elif size < (1 << 16):
        header.append(0x80 | 126)
        header += struct.pack(">H", size)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", size)
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    conn.sock.sendall(bytes(header) + masked)


def _ws_recv(conn: _Socket) -> str:
    """收一条完整**文本**消息（自动拼续帧、回 pong、整条丢掉二进制消息）。

    二进制消息必须整条丢：DevTools 的响应都是 JSON 文本，一旦有二进制帧混进来
    被当成文本返回，调用方拿到的就是一段解不开的字节，错误还会出现在很远的地方。
    丢掉时要连它的续帧一起丢，否则续帧会被误当成上一条文本消息的延续。
    """
    pieces: list[bytes] = []
    skipping_binary = False
    while True:
        b0, b1 = conn.need(2)
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        size = b1 & 0x7F
        if size == 126:
            (size,) = struct.unpack(">H", conn.need(2))
        elif size == 127:
            (size,) = struct.unpack(">Q", conn.need(8))
        key = conn.need(4) if masked else b""
        data = conn.need(size) if size else b""
        if masked:
            data = bytes(b ^ key[i % 4] for i, b in enumerate(data))

        if opcode == 0x8:                            # close
            raise CdpError("浏览器关闭了调试连接")
        if opcode == 0x9:                            # ping -> pong
            conn.sock.sendall(b"\x8a\x80" + os.urandom(4))
            continue
        if opcode == 0xA:                            # pong
            continue
        if opcode == 0x2:                            # 二进制消息：整条丢
            skipping_binary = not fin
            continue
        if opcode == 0x0:                            # 续帧
            if skipping_binary:
                skipping_binary = fin is False
                continue
            pieces.append(data)
            if fin:
                return b"".join(pieces).decode("utf-8", "replace")
            continue
        if opcode == 0x1:                            # 文本
            skipping_binary = False
            if fin:
                return data.decode("utf-8", "replace")
            pieces = [data]
            continue
        # 未知 opcode：丢掉这一帧


class CdpSession:
    """一个 DevTools 会话（浏览器级），用完请 ``close()``。"""

    def __init__(self, port: int, *, timeout: float = 15.0):
        self.port = port
        self._next_id = 1
        version = json.loads(_http_get(port, "/json/version", timeout=timeout).decode("utf-8"))
        ws_url = version.get("webSocketDebuggerUrl") or ""
        if not ws_url.startswith(f"ws://{HOST}:{port}/"):
            raise CdpError(f"调试端口给的 WebSocket 地址不对：{ws_url!r}")
        self.path = "/" + ws_url.split(f"ws://{HOST}:{port}/", 1)[1]
        self.conn = _ws_open(port, self.path, timeout=timeout)

    def call(self, method: str, params: dict | None = None, *, timeout: float = 15.0, session_id: str = ""):
        mid = self._next_id
        self._next_id += 1
        payload: dict = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        self.conn.sock.settimeout(timeout)
        _ws_send(self.conn, json.dumps(payload))
        while True:
            message = json.loads(_ws_recv(self.conn))
            if message.get("id") != mid:
                continue                             # 事件通知，跳过
            if "error" in message:
                err = message["error"]
                raise CdpError(f"{method} 失败：{err.get('message', err)}")
            return message.get("result", {})

    def close(self) -> None:
        try:
            self.conn.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 对外：启动浏览器 + 拿 Cookie
# --------------------------------------------------------------------------- #

class Launched:
    """一个我们启动的浏览器实例。"""

    def __init__(self, proc: subprocess.Popen, port: int, profile_root: Path, exe: Path):
        self.proc = proc
        self.port = port
        self.profile_root = profile_root
        self.exe = exe

    def stop(self, *, timeout: float = 5.0) -> None:
        """关掉它。先好好说，不行再动手。"""
        if self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def wait_for_port(port: int, *, timeout: float = 20.0) -> bool:
    """等调试端口起来。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _http_get(port, "/json/version", timeout=1.0)
            return True
        except (OSError, CdpError):
            time.sleep(0.25)
    return False


def launch(
    url: str,
    *,
    browser: str = "edge",
    profile_root: Path | None = None,
    port: int | None = None,
    wait: float = 25.0,
    attempts: int = 3,
) -> Launched:
    """用**隔离配置档**起一个浏览器实例，打开 ``url``，并开调试端口。

    隔离配置档是关键：不去动你日常在用的那个配置档，所以
    (1) 不需要你关掉浏览器，(2) 也不碰你现有的登录状态。

    端口可能被抢（见 :data:`PORT_RANGE`），所以失败会换个端口重试。
    """
    if os.name != "nt":
        raise CdpError("启动浏览器取会话目前只支持 Windows")
    exe = browser_exe(browser)
    root = Path(profile_root) if profile_root else Path.home() / ".studydash" / "browser"
    root.mkdir(parents=True, exist_ok=True)

    last: str = ""
    for attempt in range(1, max(1, attempts) + 1):
        chosen = int(port or free_port())
        args = [
            str(exe),
            f"--user-data-dir={root}",
            f"--remote-debugging-port={chosen}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate",
            url,
        ]
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if wait_for_port(chosen, timeout=wait):
            return Launched(proc, chosen, root, exe)

        exited = proc.poll()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        last = (
            f"第 {attempt} 次：端口 {chosen} 没起来（浏览器{'已退出，返回码 %s' % exited if exited is not None else '还在跑'}）"
        )
        if port:
            break                                    # 用户指定了端口，不换

    raise CdpError(
        f"浏览器没能开成调试端口。\n"
        f"      {last}\n"
        f"      → 试试点名一个空闲端口：`--port 25000`；\n"
        f"      → 或者干脆不用这套：登录后用浏览器扩展导出 Cookie，再 `studydash login --file`。"
    )


def _cookies_from_page(session: CdpSession, *, timeout: float) -> list[dict]:
    """退回一步：附到某个页面 target 上，再让它的 Network 域把 Cookie 交出来。"""
    targets = (session.call("Target.getTargets", timeout=timeout).get("targetInfos") or [])
    pages = [t for t in targets if t.get("type") == "page" and t.get("targetId")]
    if not pages:
        session.call(
            "Target.createTarget", {"url": "about:blank"}, timeout=timeout
        )
        targets = (session.call("Target.getTargets", timeout=timeout).get("targetInfos") or [])
        pages = [t for t in targets if t.get("type") == "page" and t.get("targetId")]
    if not pages:
        raise CdpError("浏览器里没有可用页面，拿不到 Cookie")

    attached = session.call(
        "Target.attachToTarget",
        {"targetId": pages[0]["targetId"], "flatten": True},
        timeout=timeout,
    )
    session_id = attached.get("sessionId") or ""
    if not session_id:
        raise CdpError("附到页面上失败，拿不到 sessionId")
    result = session.call("Network.getAllCookies", timeout=timeout, session_id=session_id)
    return result.get("cookies") or []


def cookies_via_cdp(port: int, *, timeout: float = 15.0) -> list[dict]:
    """向浏览器要全部 Cookie。

    先试浏览器级的 ``Storage.getCookies``（一次调用，不含页面上下文）；
    老版本浏览器没有这个命令，则退回附到页面上走 ``Network.getAllCookies``。
    """
    session = CdpSession(port, timeout=timeout)
    try:
        try:
            cookies = session.call("Storage.getCookies", timeout=timeout).get("cookies") or []
        except CdpError:
            cookies = _cookies_from_page(session, timeout=timeout)
    finally:
        session.close()
    if not isinstance(cookies, list):
        raise CdpError("取 Cookie 的返回结构意料之外")
    return cookies
