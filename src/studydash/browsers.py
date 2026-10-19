"""从本机浏览器里取 Cookie——只读，只在你自己的机器上。

为什么需要这个模块
------------------

``studydash`` 是只读工具，它不替你登录，所以它需要一份已经登录好的会话
Cookie。让用户开 F12 → Network → 手抄一长串 Cookie，是这个工具此前最大的
使用门槛：Cookie 会过期，每次都得重来一遍。

而用户本来就已经在浏览器里登录着了。这个模块把那串 Cookie 取出来。

原则
----

* **不碰密码。** 只读浏览器已经写好的 Cookie；工具里没有任何一处接收或存储
  你的账号密码。要登录，就在浏览器里正常登录（扫码、验证码、二次验证都由你
  自己过），本模块只负责把结果取出来。
* **只读。** 打开数据库一律先复制到临时目录再读，不改浏览器的任何文件。
* **只取需要的域。** 默认只挑 ``*.chaoxing.com`` 之类的目标域，不是把你所有
  网站的 Cookie 全捞出来。
* **显式授权。** 必须由用户在命令行上点名浏览器，不会偷偷扫。

Windows 上 Chromium 系的 Cookie 是加密的：32 字节主密钥放在 ``Local State``
里、用 DPAPI 保护（只有当前 Windows 用户能解），每个 Cookie 再用 AES-256-GCM
加密。DPAPI 走 ``crypt32.dll``，AES-GCM 走 ``bcrypt.dll``（Windows 自带的
CNG，FIPS 校验过的实现）——都不用引第三方库，符合本项目零依赖的取舍。

Firefox 的 ``cookies.sqlite`` 是明文的，顺手也支持了。
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .model import now

__all__ = [
    "Cookie",
    "BrowserError",
    "BROWSERS",
    "available",
    "profiles",
    "read_cookies",
    "cookie_header",
    "chaoxing_cookie",
]

# Windows FILETIME 的起点（1601-01-01 UTC），Chromium 的过期时间以它为准
_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


class BrowserError(RuntimeError):
    """读浏览器数据失败。消息面向用户，会直接打给他看。"""


@dataclass(frozen=True)
class Cookie:
    name: str
    value: str
    domain: str = ""
    path: str = "/"
    expires: float | None = None          # unix 秒；None = 会话 Cookie
    secure: bool = False
    http_only: bool = False

    def is_expired(self, *, ref: datetime | None = None) -> bool:
        if self.expires is None:
            return False
        ref = ref or now()
        return self.expires <= ref.timestamp()

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "domain": self.domain,
            "path": self.path,
            "secure": self.secure,
            "http_only": self.http_only,
            "expires": self.expires,
            "expired": self.is_expired(),
            # 值本身不在这里出现：as_dict 的结果可能被写进日志或调试输出
            "value_length": len(self.value),
        }


# --------------------------------------------------------------------------- #
# 浏览器位置
# --------------------------------------------------------------------------- #

def _local() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))


def _roaming() -> Path:
    return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))


#: 浏览器 id → 数据目录。Chromium 系都在 ``<root>/<Profile>/Network/Cookies``。
BROWSERS: dict[str, dict] = {
    "edge": {"name": "Microsoft Edge", "root": _local() / "Microsoft" / "Edge" / "User Data"},
    "chrome": {"name": "Google Chrome", "root": _local() / "Google" / "Chrome" / "User Data"},
    "brave": {
        "name": "Brave",
        "root": _local() / "BraveSoftware" / "Brave-Browser" / "User Data",
    },
    "chromium": {"name": "Chromium", "root": _local() / "Chromium" / "User Data"},
    "firefox": {"name": "Firefox", "root": _roaming() / "Mozilla" / "Firefox" / "Profiles"},
}


def available() -> list[dict]:
    """本机装了哪些浏览器、各有哪些配置档。给 ``login --list`` 用。"""
    out: list[dict] = []
    for bid, info in BROWSERS.items():
        root: Path = info["root"]
        if not root.is_dir():
            continue
        entry = {
            "id": bid,
            "name": info["name"],
            "root": str(root),
            "profiles": [p.name for p in profiles(bid)],
            "cookies": 0,
        }
        for prof in profiles(bid):
            entry["cookies"] += len(read_cookies(bid, profile=prof.name, quiet=True))
        out.append(entry)
    return out


def profiles(browser: str, *, root: Path | None = None) -> list[Path]:
    """列出配置档目录（只列真的有 Cookie 库的）。

    ``root`` 给定时用它代替浏览器的默认数据目录——用于我们自己启动的专用配置档
    （``studydash login`` 会拿一个隔离目录起一次浏览器，那样就不必让你关掉正在
    用的浏览器）。
    """
    key = _normalize(browser)
    root = Path(root) if root else BROWSERS[key]["root"]
    if not root.is_dir():
        return []

    if key == "firefox":
        found = [p for p in root.iterdir() if p.is_dir() and (p / "cookies.sqlite").is_file()]
        return sorted(found, key=lambda p: (p.name != "default-release", p.name))

    found = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if (child / "Network" / "Cookies").is_file():
            found.append(child)
    # Default 排最前面：绝大多数人用的是它
    return sorted(found, key=lambda p: (p.name != "Default", p.name))


def _normalize(browser: str) -> str:
    key = (browser or "").strip().lower()
    if key in BROWSERS:
        return key
    hint = "、".join(sorted(BROWSERS))
    raise BrowserError(f"不认识的浏览器 {browser!r}；支持：{hint}")


# --------------------------------------------------------------------------- #
# 读 Cookie 库
# --------------------------------------------------------------------------- #

def read_cookies(
    browser: str,
    *,
    profile: str = "",
    domains: tuple[str, ...] | None = None,
    quiet: bool = False,
    root: Path | None = None,
) -> list[Cookie]:
    """读出指定浏览器的 Cookie。

    ``domains`` 给定时只返回 host 命中其中之一（子域算命中）的条目——默认行为
    是「只取我们要的那几个域」，不是把整库倒出来。

    ``root`` 用来指向非默认的数据目录（见 :func:`profiles`）。
    """
    key = _normalize(browser)
    prof_dirs = profiles(key, root=root)
    if not prof_dirs:
        where = f"{root}" if root else BROWSERS[key]["name"]
        raise BrowserError(f"没找到 {where} 的 Cookie 库。确认它装过、并且至少打开过一次。")

    if profile:
        chosen = [p for p in prof_dirs if p.name.lower() == profile.lower()]
        if not chosen:
            names = "、".join(p.name for p in prof_dirs)
            raise BrowserError(f"没有配置档 {profile!r}；有的是：{names}")
    else:
        chosen = prof_dirs[:1]
        if not quiet and len(prof_dirs) > 1:
            names = "、".join(p.name for p in prof_dirs)
            # 只说一次，别刷屏
            print(f"  提示：{BROWSERS[key]['name']} 有多个配置档（{names}），"
                  f"默认读 {chosen[0].name}，换一个用 --profile", file=sys.stderr)

    main_root = Path(root) if root else BROWSERS[key]["root"]
    cookies: list[Cookie] = []
    for prof in chosen:
        if key == "firefox":
            cookies.extend(_read_firefox(prof, domains))
        else:
            cookies.extend(_read_chromium(key, prof, domains, main_root))
    return cookies


def _wanted(host: str, domains: tuple[str, ...] | None) -> bool:
    if not domains:
        return True
    host = (host or "").lstrip(".").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def _copy_db(src: Path, workdir: Path, browser: str = "") -> Path:
    """复制数据库再读。

    Chromium 在读期间对 Cookies 库持**独占**句柄（实测：任何 share 模式都拿到
    ``ERROR_SHARING_VIOLATION``），所以浏览器开着的时候根本打不开。这不是代码
    能绕的，得让用户关掉浏览器——所以这里要把话说清楚，而不是抛个
    ``PermissionError`` 让人猜。

    WAL 文件也要一起带上，否则刚写入的 Cookie（比如你十分钟前登录的）可能还在
    ``-wal`` 里没落盘。
    """
    dst = workdir / src.name
    pretty = BROWSERS.get(browser, {}).get("name", "浏览器")
    try:
        shutil.copyfile(src, dst)
    except PermissionError as exc:
        raise BrowserError(
            f"{pretty} 正在运行，它把 Cookie 库锁住了，读不了。\n"
            f"      → 把 {pretty} 完全退出（含托盘图标里的后台进程）再跑一次；\n"
            f"      → 或者先登录好，用浏览器扩展把 Cookie 导出成 JSON，再 `studydash login --file`。"
        ) from exc
    except OSError as exc:
        raise BrowserError(f"复制 {src} 失败：{exc}") from exc
    for suffix in ("-wal", "-shm"):
        side = src.with_name(src.name + suffix)
        if side.is_file():
            try:
                shutil.copyfile(side, dst.with_name(dst.name + suffix))
            except OSError:
                pass
    return dst


def _read_chromium(
    browser: str,
    profile_dir: Path,
    domains: tuple[str, ...] | None,
    root: Path | None = None,
) -> list[Cookie]:
    src = profile_dir / "Network" / "Cookies"
    with tempfile.TemporaryDirectory(prefix="studydash-cookies-") as tmp:
        db = _copy_db(src, Path(tmp), browser)
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            raise BrowserError(f"打不开 {src}：{exc}") from exc
        try:
            con.row_factory = sqlite3.Row
            version = _db_version(con)
            rows = con.execute(
                "SELECT host_key, name, value, encrypted_value, path, expires_utc,"
                " is_secure, is_httponly FROM cookies"
            ).fetchall()
        except sqlite3.Error as exc:
            raise BrowserError(
                f"读 {src} 失败：{exc}。浏览器版本可能改过表结构。"
            ) from exc
        finally:
            con.close()

    key = _chromium_key(browser, root or BROWSERS[browser]["root"])
    out: list[Cookie] = []
    app_bound = 0
    domain_bound = 0
    for row in rows:
        if not _wanted(row["host_key"], domains):
            continue
        value = row["value"] or ""
        blob = row["encrypted_value"] or b""
        if not value and blob:
            if blob[:3] == b"v20":
                # Chrome 127+ 的 app-bound 加密：密钥还要过一遍浏览器的
                # elevation service，本模块不碰。数一下最后统一提示。
                app_bound += 1
                continue
            try:
                raw = _decrypt_chromium(blob, key)
            except Exception:                                  # noqa: BLE001
                continue
            raw, stripped = _strip_domain_hash(raw, row["host_key"])
            domain_bound += 1 if stripped else 0
            value = raw.decode("utf-8", "replace")
        if not value:
            continue
        out.append(
            Cookie(
                name=row["name"],
                value=value,
                domain=row["host_key"],
                path=row["path"] or "/",
                expires=_from_filetime(row["expires_utc"]),
                secure=bool(row["is_secure"]),
                http_only=bool(row["is_httponly"]),
            )
        )
    if app_bound and not out:
        raise BrowserError(
            f"这个配置档里的 Cookie 是 v20（app-bound）加密的，本工具解不开。\n"
            f"      两个办法：(1) 用 --browser 让浏览器自己登录再抓取；\n"
            f"      (2) 从浏览器扩展导出 Cookie 后喂给 --file。"
        )
    return out


def _read_firefox(profile_dir: Path, domains: tuple[str, ...] | None) -> list[Cookie]:
    src = profile_dir / "cookies.sqlite"
    with tempfile.TemporaryDirectory(prefix="studydash-cookies-") as tmp:
        db = _copy_db(src, Path(tmp))
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT host, name, value, path, expiry, isSecure, isHttpOnly FROM moz_cookies"
            ).fetchall()
        finally:
            con.close()
    return [
        Cookie(
            name=r["name"],
            value=r["value"] or "",
            domain=r["host"],
            path=r["path"] or "/",
            expires=float(r["expiry"]) if r["expiry"] else None,
            secure=bool(r["isSecure"]),
            http_only=bool(r["isHttpOnly"]),
        )
        for r in rows
        if _wanted(r["host"], domains)
    ]


def _from_filetime(value) -> float | None:
    """Chromium 的过期时间是 1601 年起的微秒数。"""
    try:
        micros = int(value or 0)
    except (TypeError, ValueError):
        return None
    if micros <= 0:
        return None
    return (_FILETIME_EPOCH + timedelta(microseconds=micros)).timestamp()


# --------------------------------------------------------------------------- #
# Windows 解密：DPAPI（拿主密钥）+ AES-256-GCM（解 Cookie）
# --------------------------------------------------------------------------- #

def _chromium_key(browser: str, root: Path) -> bytes:
    """从 ``Local State`` 取出被 DPAPI 保护的主密钥。"""
    if os.name != "nt":
        raise BrowserError("读取 Chromium 加密 Cookie 只在 Windows 上支持；请用 --cookie 手动提供")
    state = root / "Local State"
    if not state.is_file():
        raise BrowserError(f"找不到 {state}，没法取解密主密钥")
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
        b64 = data["os_crypt"]["encrypted_key"]
    except (OSError, ValueError, KeyError) as exc:
        raise BrowserError(f"{state} 里没有 os_crypt.encrypted_key：{exc}") from exc

    blob = base64.b64decode(b64)
    if blob[:5] != b"DPAPI":
        raise BrowserError("Local State 里的主密钥格式不对（预期以 DPAPI 开头）")
    return _dpapi_unprotect(blob[5:])


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _dpapi_unprotect(blob: bytes) -> bytes:
    """调 ``CryptUnprotectData``：只有当前 Windows 用户能解开——所以整件事
    严格是本机、本人的。"""
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    buf = ctypes.create_string_buffer(blob, len(blob))
    incoming = _DataBlob(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    outgoing = _DataBlob()

    ok = crypt32.CryptUnprotectData(
        ctypes.byref(incoming), None, None, None, None, 0, ctypes.byref(outgoing)
    )
    if not ok:
        raise BrowserError(
            "DPAPI 解密失败：这份数据不是当前 Windows 用户加密的。"
            "（换了用户/换了机器都会这样）"
        )
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        kernel32.LocalFree(outgoing.pbData)


class _GcmInfo(ctypes.Structure):
    """``BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO``（x64 下 104 字节）。"""

    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("dwInfoVersion", ctypes.c_uint32),
        ("pbNonce", ctypes.c_void_p),
        ("cbNonce", ctypes.c_uint32),
        ("pbAuthData", ctypes.c_void_p),
        ("cbAuthData", ctypes.c_uint32),
        ("pbTag", ctypes.c_void_p),
        ("cbTag", ctypes.c_uint32),
        ("pbMacContext", ctypes.c_void_p),
        ("cbMacContext", ctypes.c_uint32),
        ("cbAAD", ctypes.c_uint32),
        ("cbData", ctypes.c_uint64),
        ("dwFlags", ctypes.c_uint32),
    ]


_BCRYPT_AES_ALGORITHM = "AES"
_BCRYPT_CHAINING_MODE = "ChainingMode"
_BCRYPT_CHAIN_MODE_GCM = "ChainingModeGCM"
_BCRYPT_OBJECT_LENGTH = "ObjectLength"
_GCM_INFO_VERSION = 1


def _aes_gcm_decrypt(key: bytes, nonce: bytes, blob: bytes) -> bytes:
    """用 Windows CNG 解 AES-256-GCM。``blob`` = 密文 + 16 字节认证标签。"""
    if len(blob) < 16:
        raise BrowserError("密文太短，不像 GCM 数据")
    ct, tag = blob[:-16], blob[-16:]

    bcrypt = ctypes.WinDLL("bcrypt", use_last_error=True)
    alg = ctypes.c_void_p()
    status = bcrypt.BCryptOpenAlgorithmProvider(
        ctypes.byref(alg), _BCRYPT_AES_ALGORITHM, None, 0
    )
    if status < 0:
        raise BrowserError(f"BCryptOpenAlgorithmProvider 失败：0x{status & 0xFFFFFFFF:08x}")
    keyobj = ctypes.c_void_p()
    try:
        mode = ctypes.create_string_buffer(_BCRYPT_CHAIN_MODE_GCM.encode("utf-16-le"))
        bcrypt.BCryptSetProperty(
            alg, _BCRYPT_CHAINING_MODE, mode, len(mode.raw), 0
        )
        obj_len = ctypes.c_uint32()
        bcrypt.BCryptGetProperty(
            alg, _BCRYPT_OBJECT_LENGTH, ctypes.byref(obj_len), 4, ctypes.byref(ctypes.c_uint32()), 0
        )
        obj_buf = ctypes.create_string_buffer(obj_len.value)
        status = bcrypt.BCryptGenerateSymmetricKey(
            alg, ctypes.byref(keyobj), obj_buf, obj_len.value, key, len(key), 0
        )
        if status < 0:
            raise BrowserError(f"BCryptGenerateSymmetricKey 失败：0x{status & 0xFFFFFFFF:08x}")

        nonce_buf = ctypes.create_string_buffer(nonce, len(nonce))
        tag_buf = ctypes.create_string_buffer(tag, len(tag))
        ct_buf = ctypes.create_string_buffer(ct, len(ct))
        info = _GcmInfo()
        info.cbSize = ctypes.sizeof(_GcmInfo)
        info.dwInfoVersion = _GCM_INFO_VERSION
        info.pbNonce = ctypes.cast(nonce_buf, ctypes.c_void_p)
        info.cbNonce = len(nonce)
        info.pbTag = ctypes.cast(tag_buf, ctypes.c_void_p)
        info.cbTag = len(tag)
        info.cbData = len(ct)

        out = ctypes.create_string_buffer(len(ct) + 16)
        written = ctypes.c_uint32()
        status = bcrypt.BCryptDecrypt(
            keyobj,
            ctypes.cast(ct_buf, ctypes.c_void_p),
            len(ct),
            ctypes.byref(info),
            None,
            0,
            out,
            len(out),
            ctypes.byref(written),
            0,
        )
        if status < 0:
            # 认证标签不对（密钥换了、或数据被改过）
            raise BrowserError(f"GCM 解密失败：0x{status & 0xFFFFFFFF:08x}")
        return out.raw[: written.value]
    finally:
        if keyobj:
            bcrypt.BCryptDestroyKey(keyobj)
        if alg:
            bcrypt.BCryptCloseAlgorithmProvider(alg, 0)


def _decrypt_chromium(blob: bytes, key: bytes) -> bytes:
    """解一个 Cookie 值，返回原始字节（可能还带着域哈希前缀）。"""
    if blob[:3] in (b"v10", b"v11"):
        # v10: nonce 12 字节 + 密文 + 16 字节标签
        return _aes_gcm_decrypt(key, blob[3:15], blob[15:])
    # Chrome 80 以前：整个值直接 DPAPI 保护
    return _dpapi_unprotect(blob)


def _db_version(con: sqlite3.Connection) -> int:
    """读 ``meta`` 表里的 Cookie 库格式版本。

    Chromium 从版本 24 起在明文前面加了一个 ``sha256(host_key)``，用来防止把
    一个域的 Cookie 挪到另一个域上复用（`cookie domain integrity check`）。
    版本号是官方给的判断依据；拿不到就当 0（老格式）。
    """
    try:
        row = con.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
    except sqlite3.Error:
        return 0
    if not row:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def _strip_domain_hash(raw: bytes, host_key: str) -> tuple[bytes, bool]:
    """去掉域哈希前缀，返回 ``(真正的值, 是否剥过)``。

    这里**不盲剥 32 字节**（参照实现是 ``data[32:]``，只要版本号判断出错就会
    把值切坏）。改成认那个哈希本身：前 32 字节确实等于 ``sha256(host_key)``
    才剥。哈希对不上就说明这条本来就没有前缀，原样返回。

    误剥概率是 2^-256，比依赖版本号可靠。
    """
    if len(raw) < 32:
        return raw, False
    if hashlib.sha256((host_key or "").encode("utf-8")).digest() == raw[:32]:
        # 正好 32 字节 = 这条 Cookie 的值本来就是空的，剥完是空串
        return raw[32:], True
    return raw, False


# --------------------------------------------------------------------------- #
# 组装成请求头
# --------------------------------------------------------------------------- #

def cookie_header(cookies: list[Cookie], *, domain: str, path: str = "/") -> str:
    """按请求方域/路径挑出该发的 Cookie，拼成 ``Cookie:`` 头的值。

    只挑名称，不带属性——HttpOnly 的条目在浏览器里也照样会随请求发出去，
    这正是我们要复刻的行为。
    """
    host = domain.lstrip(".").lower()
    picked: list[Cookie] = []
    seen: set[str] = set()
    for c in sorted(cookies, key=lambda c: len(c.domain), reverse=True):
        cdom = (c.domain or "").lstrip(".").lower()
        if not (host == cdom or host.endswith("." + cdom)):
            continue
        if not (c.path or "/") == "/" and not path.startswith(c.path or "/"):
            continue
        if c.name in seen:
            continue
        seen.add(c.name)
        picked.append(c)
    return "; ".join(f"{c.name}={c.value}" for c in picked)


#: 读超星会话时需要的域
CHAOXING_DOMAINS = (
    "chaoxing.com",
    "chaoxing.cn",
    "mooc.chaoxing.com",
)


def chaoxing_cookie(
    browser: str, *, profile: str = "", domains: tuple[str, ...] = CHAOXING_DOMAINS
) -> tuple[str, list[Cookie]]:
    """取超星会话 Cookie。返回 ``(请求头字符串, 命中的 Cookie 列表)``。"""
    got = read_cookies(browser, profile=profile, domains=domains)
    if not got:
        raise BrowserError(
            f"{BROWSERS[_normalize(browser)]['name']} 里没有超星的 Cookie。\n"
            "      先在浏览器里打开 chaoxing.com 并登录，然后重跑这条命令。"
        )
    header = cookie_header(got, domain="i.chaoxing.com")
    if not header:
        raise BrowserError("找到了超星的 Cookie，但都不适用于 i.chaoxing.com")
    return header, got
