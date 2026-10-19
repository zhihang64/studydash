"""``studydash.cdp`` 的测试：手写的 WebSocket 帧 + 只读约束的放行规则。

帧编解码是这里最值得测的部分——它没有第三方库兜底，一块字节错位就整条链路不通，
而在真机上排查这种问题成本极高。这里用 ``socket.socketpair()`` 造一条内存里的连接，
自己扮服务端，把「客户端帧发得对不对」和「服务端帧收得对不对」都钉住。
"""

from __future__ import annotations

import socket
import struct

import pytest

from studydash import cdp, http

# --------------------------------------------------------------------------- #
# WebSocket 帧：测试自带的「服务端」工具
# --------------------------------------------------------------------------- #

def _recvn(sock: socket.socket, count: int) -> bytes:
    buf = b""
    while len(buf) < count:
        block = sock.recv(count - len(buf))
        if not block:
            raise AssertionError(f"对端提前关了连接（还差 {count - len(buf)} 字节）")
        buf += block
    return buf


def _server_frame(payload: str | bytes, *, opcode: int = 0x1, fin: bool = True) -> bytes:
    """服务端 -> 客户端方向的帧（RFC 6455 规定**不掩码**）。"""
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    head = bytearray([(0x80 if fin else 0x00) | opcode])
    size = len(data)
    if size < 126:
        head.append(size)
    elif size < (1 << 16):
        head.append(126)
        head += struct.pack(">H", size)
    else:
        head.append(127)
        head += struct.pack(">Q", size)
    return bytes(head) + data


def _read_client_frame(sock: socket.socket) -> tuple[int, bytes, bool]:
    """读一帧客户端 -> 服务端的帧，顺便校验掩码。"""
    b0, b1 = _recvn(sock, 2)
    opcode = b0 & 0x0F
    fin = bool(b0 & 0x80)
    masked = bool(b1 & 0x80)
    size = b1 & 0x7F
    if size == 126:
        (size,) = struct.unpack(">H", _recvn(sock, 2))
    elif size == 127:
        (size,) = struct.unpack(">Q", _recvn(sock, 8))
    assert masked, "客户端发出的帧必须带掩码（RFC 6455）"
    mask = _recvn(sock, 4)
    data = _recvn(sock, size)
    return opcode, bytes(b ^ mask[i % 4] for i, b in enumerate(data)), fin


@pytest.fixture
def wire():
    """一对内存 socket，一端给 ``_Socket`` 用，另一端当服务端。"""
    client, server = socket.socketpair()
    client.settimeout(3.0)
    server.settimeout(3.0)
    try:
        yield cdp._Socket(client), server
    finally:
        client.close()
        server.close()


# --------------------------------------------------------------------------- #
# 发帧
# --------------------------------------------------------------------------- #

def test_ws_send_encodes_masked_text(wire):
    conn, server = wire
    cdp._ws_send(conn, "你好 CDP")
    opcode, data, fin = _read_client_frame(server)
    assert (opcode, fin) == (0x1, True)
    assert data.decode("utf-8") == "你好 CDP"


def test_ws_send_uses_16bit_length_beyond_125(wire):
    conn, server = wire
    payload = "x" * 200
    cdp._ws_send(conn, payload)
    _, data, _ = _read_client_frame(server)
    assert data.decode("utf-8") == payload


def test_ws_send_handles_exactly_125_and_126(wire):
    """125 是 7 位长度的上限，126 必须换成 16 位长度——差一字节就整帧错位。"""
    conn, server = wire
    for size in (125, 126):
        cdp._ws_send(conn, "a" * size)
        _, data, _ = _read_client_frame(server)
        assert len(data) == size


# --------------------------------------------------------------------------- #
# 收帧
# --------------------------------------------------------------------------- #

def test_ws_recv_reads_text(wire):
    conn, server = wire
    server.sendall(_server_frame("hello"))
    assert cdp._ws_recv(conn) == "hello"


def test_ws_recv_joins_continuation_frames():
    client, server = socket.socketpair()
    client.settimeout(3.0)
    try:
        server.sendall(_server_frame("he", fin=False))
        server.sendall(_server_frame("llo", opcode=0x0, fin=True))
        assert cdp._ws_recv(cdp._Socket(client)) == "hello"
    finally:
        client.close()
        server.close()


def test_ws_recv_answers_ping_with_pong(wire):
    conn, server = wire
    server.sendall(_server_frame(b"", opcode=0x9))     # ping
    server.sendall(_server_frame("after-ping"))
    assert cdp._ws_recv(conn) == "after-ping"          # ping 被跳过
    opcode, data, _ = _read_client_frame(server)
    assert opcode == 0xA                               # 回了 pong
    assert data == b""


def test_ws_recv_skips_binary_frames(wire):
    conn, server = wire
    server.sendall(_server_frame(b"\x00\x01\x02", opcode=0x2))
    server.sendall(_server_frame("text-after-binary"))
    assert cdp._ws_recv(conn) == "text-after-binary"


def test_ws_recv_drops_fragmented_binary_entirely(wire):
    """分片的二进制消息要连续帧一起丢，否则续帧会被拼进下一条文本消息。"""
    conn, server = wire
    server.sendall(_server_frame(b"\x00\x01", opcode=0x2, fin=False))
    server.sendall(_server_frame(b"\x02\x03", opcode=0x0, fin=False))
    server.sendall(_server_frame(b"\x04", opcode=0x0, fin=True))
    server.sendall(_server_frame("clean"))
    assert cdp._ws_recv(conn) == "clean"


def test_ws_recv_raises_on_close_frame(wire):
    conn, server = wire
    server.sendall(_server_frame(b"", opcode=0x8))
    with pytest.raises(cdp.CdpError, match="调试连接"):
        cdp._ws_recv(conn)


def test_ws_recv_uses_preloaded_buffer():
    """握手后残留的字节已经躺在缓冲区里，就不该再去读 socket。"""
    client, server = socket.socketpair()
    client.settimeout(0.5)
    try:
        conn = cdp._Socket(client, _server_frame("buffered"))
        assert cdp._ws_recv(conn) == "buffered"
    finally:
        client.close()
        server.close()


def test_ws_recv_survives_split_delivery():
    """一个帧分两次到达（网络抖动）不能解析失败。"""
    client, server = socket.socketpair()
    client.settimeout(3.0)
    try:
        frame = _server_frame("split me")
        server.sendall(frame[:3])
        server.sendall(frame[3:])
        assert cdp._ws_recv(cdp._Socket(client)) == "split me"
    finally:
        client.close()
        server.close()


# --------------------------------------------------------------------------- #
# _Socket 缓冲
# --------------------------------------------------------------------------- #

def test_read_until_keeps_remainder(wire):
    conn, server = wire
    server.sendall(b"head\r\n\r\nrest-of-stream")
    assert conn.read_until(b"\r\n\r\n") == b"head"
    assert conn.buf == b"rest-of-stream"


def test_need_reads_exactly(wire):
    conn, server = wire
    server.sendall(b"abcdef")
    assert conn.need(4) == b"abcd"
    assert conn.need(2) == b"ef"


def test_socket_error_when_peer_closes():
    client, server = socket.socketpair()
    server.close()
    try:
        with pytest.raises(cdp.CdpError, match="调试连接"):
            cdp._Socket(client).need(1)
    finally:
        client.close()


# --------------------------------------------------------------------------- #
# HTTP 小工具
# --------------------------------------------------------------------------- #

def test_content_length_parsing():
    head = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 42\r\n"
    assert cdp._content_length(head) == 42
    assert cdp._content_length(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n") is None
    assert cdp._content_length(b"HTTP/1.1 200 OK\r\nContent-Length: abc\r\n") is None


def test_dechunk():
    body = b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
    assert cdp._dechunk(body) == b"hello world"


def test_dechunk_tolerates_trailing_garbage():
    assert cdp._dechunk(b"3\r\nabc\r\n") == b"abc"


# --------------------------------------------------------------------------- #
# 端口与浏览器发现
# --------------------------------------------------------------------------- #

def test_free_port_is_bindable_and_in_range():
    port = cdp.free_port()
    assert cdp.PORT_RANGE[0] <= port <= cdp.PORT_RANGE[1]
    probe = socket.socket()
    try:
        probe.bind((cdp.HOST, port))                   # 没被别人抢走
    finally:
        probe.close()


def test_launch_rejects_unknown_browser(monkeypatch):
    with pytest.raises(cdp.CdpError, match="不知道怎么启动"):
        cdp.browser_exe("netscape")


def test_launch_reports_non_windows(monkeypatch):
    monkeypatch.setattr(cdp.os, "name", "posix")
    with pytest.raises(cdp.CdpError, match="Windows"):
        cdp.launch("https://example.com")


# --------------------------------------------------------------------------- #
# 只读约束：放行 cdp.py，但必须证明它只连本机
# --------------------------------------------------------------------------- #

def test_real_package_passes_readonly_scan():
    assert http.assert_readonly() == []


def test_scan_flags_module_using_requests(tmp_path):
    p = tmp_path / "sneaky.py"
    p.write_text("import requests\nrequests.get('https://example.com')\n", encoding="utf-8")
    assert http.assert_readonly([str(p)]) == [str(p)]


def test_scan_allows_local_only_module(tmp_path):
    p = tmp_path / "cdp.py"
    p.write_text('HOST = "127.0.0.1"\nimport socket\n', encoding="utf-8")
    assert http.assert_readonly([str(p)]) == []


def test_scan_flags_local_only_module_with_remote_host(tmp_path):
    """放行的前提是「只连本机」——偷偷写了外网地址就必须报出来。"""
    p = tmp_path / "cdp.py"
    p.write_text('HOST = "127.0.0.1"\nURL = "https://evil.example/x"\n', encoding="utf-8")
    assert http.assert_readonly([str(p)]) == [str(p)]


def test_scan_flags_local_only_module_without_loopback_pin(tmp_path):
    """没把主机钉在回环地址上，就不算「只连本机」。"""
    p = tmp_path / "cdp.py"
    p.write_text("import socket\nHOST = whatever()\n", encoding="utf-8")
    assert http.assert_readonly([str(p)]) == [str(p)]


def test_is_local_only_accepts_localhost_and_ipv6():
    assert http._is_local_only('HOST = "localhost"\n')
    assert http._is_local_only('HOST = "::1"\n')
    assert http._is_local_only('HOST = "127.0.0.1"\nU = "http://127.0.0.1:1/x"\n')
    assert not http._is_local_only('HOST = "0.0.0.0"\n')
