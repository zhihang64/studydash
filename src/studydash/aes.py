"""纯 Python 的 AES-128-CBC 加密。

为什么自己写：超星的网页登录要用 CryptoJS 的 AES-CBC 把密码封起来再发
（``encryptByAES(pwd, "u2oh6Vu^HWe4_AES")``——key 与 IV 同一个字符串、PKCS7
补齐、最后取密文的 Base64）。本项目对外承诺零第三方运行时依赖，所以不能引
``cryptography`` / ``pycryptodome``；而 AES 是对称分组密码里最容易写对的一个。

**只实现加密方向，故意不写解密。** 这里全部用途都是「把要发出去的东西封起来」，
没有一处需要解密——少写一半代码就少一半出错的地方。

限制：只支持 128 位密钥与 CBC 模式（超星用的就是 16 字节 key）。DES/RSA 不在
范围内。正确性由 ``tests/test_aes.py`` 用 FIPS-197 官方向量卡住。
"""

from __future__ import annotations

import base64

__all__ = [
    "AesError",
    "encrypt_cbc",
    "encrypt_cbc_b64",
    "pkcs7_pad",
    "BLOCK_SIZE",
]

BLOCK_SIZE = 16
_KEY_WORDS = 4                        # AES-128：4 个字 = 16 字节
_ROUNDS = 10                          # AES-128：10 轮
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)

# FIPS-197 图 7 的 S 盒
_SBOX_HEX = (
    "637c777bf26b6fc53001672bfed7ab76"
    "ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d83115"
    "04c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f84"
    "53d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa8"
    "51a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d1973"
    "60814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479"
    "e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a"
    "703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df"
    "8ca1890dbfe6426841992d0fb054bb16"
)
_SBOX = tuple(bytes.fromhex(_SBOX_HEX))


class AesError(ValueError):
    """参数不合法（密钥长度不对之类）。纯代码缺陷才会走到这里。"""


# --------------------------------------------------------------------------- #
# 基础运算
# --------------------------------------------------------------------------- #
def _xtime(value: int) -> int:
    """GF(2^8) 上乘 2，模不可约多项式 x^8+x^4+x^3+x+1（0x11B）。"""
    value <<= 1
    if value & 0x100:
        value ^= 0x11B
    return value & 0xFF


def _mul(a: int, b: int) -> int:
    """GF(2^8) 乘法，用最朴素的「俄式农民乘法」——这里只用来算 2 和 3。"""
    result = 0
    while b:
        if b & 1:
            result ^= a
        a = _xtime(a)
        b >>= 1
    return result


# 预先把 MixColumns 会用到的倍乘表算好，免得每轮都跑循环
_MUL2 = tuple(_xtime(i) for i in range(256))
_MUL3 = tuple(_mul(i, 3) for i in range(256))


# --------------------------------------------------------------------------- #
# 密钥扩展
# --------------------------------------------------------------------------- #
def _expand_key(key: bytes) -> list[bytes]:
    """密钥扩展成 11 组轮密钥（每组 16 字节）。"""
    if len(key) != 16:
        raise AesError(f"AES-128 需要 16 字节密钥，收到 {len(key)} 字节")

    words = [int.from_bytes(key[i * 4:i * 4 + 4], "big") for i in range(_KEY_WORDS)]
    total = _KEY_WORDS * (_ROUNDS + 1)
    for i in range(_KEY_WORDS, total):
        temp = words[i - 1]
        if i % _KEY_WORDS == 0:
            # RotWord（循环左移一字节）+ SubWord + Rcon
            temp = ((temp << 8) | (temp >> 24)) & 0xFFFFFFFF
            temp = int.from_bytes(bytes(_SBOX[b] for b in temp.to_bytes(4, "big")), "big")
            temp ^= _RCON[i // _KEY_WORDS - 1] << 24
        words.append(words[i - _KEY_WORDS] ^ temp)

    return [
        b"".join(words[r * 4 + c].to_bytes(4, "big") for c in range(4))
        for r in range(_ROUNDS + 1)
    ]


def _add_round_key(state: list[int], round_key: bytes) -> None:
    """就地异或轮密钥。state 是 16 个字节的扁平列主序数组。"""
    for i in range(16):
        state[i] ^= round_key[i]


def _sub_bytes(state: list[int]) -> None:
    for i in range(16):
        state[i] = _SBOX[state[i]]


def _shift_rows(state: list[int]) -> None:
    """第 r 行循环左移 r 个字节。列主序下第 r 行的下标是 ``r, r+4, r+8, r+12``。"""
    state[1], state[5], state[9], state[13] = state[5], state[9], state[13], state[1]
    state[2], state[6], state[10], state[14] = state[10], state[14], state[2], state[6]
    state[3], state[7], state[11], state[15] = state[15], state[3], state[7], state[11]


def _mix_columns(state: list[int]) -> None:
    for c in range(0, 16, 4):
        a0, a1, a2, a3 = state[c], state[c + 1], state[c + 2], state[c + 3]
        state[c] = _MUL2[a0] ^ _MUL3[a1] ^ a2 ^ a3
        state[c + 1] = a0 ^ _MUL2[a1] ^ _MUL3[a2] ^ a3
        state[c + 2] = a0 ^ a1 ^ _MUL2[a2] ^ _MUL3[a3]
        state[c + 3] = _MUL3[a0] ^ a1 ^ a2 ^ _MUL2[a3]


def _encrypt_block(state: list[int], round_keys: list[bytes]) -> None:
    """就地加密一个分组。"""
    _add_round_key(state, round_keys[0])
    for rnd in range(1, _ROUNDS):
        _sub_bytes(state)
        _shift_rows(state)
        _mix_columns(state)
        _add_round_key(state, round_keys[rnd])
    # 最后一轮不做 MixColumns
    _sub_bytes(state)
    _shift_rows(state)
    _add_round_key(state, round_keys[_ROUNDS])


# --------------------------------------------------------------------------- #
# 对外 API
# --------------------------------------------------------------------------- #
def pkcs7_pad(data: bytes, block: int = BLOCK_SIZE) -> bytes:
    """PKCS#7 补齐。数据本身就是整块时**也要**补一整块（这是规范，不是 bug）。"""
    if block <= 0 or block > 255:
        raise AesError(f"块大小必须在 1..255，收到 {block}")
    pad = block - len(data) % block
    return data + bytes([pad]) * pad


def encrypt_cbc(key: bytes, data: bytes, iv: bytes | None = None) -> bytes:
    """AES-128-CBC 加密，返回密文（含补齐）。``iv`` 省略时取与 key 相同。"""
    if not isinstance(key, (bytes, bytearray)):
        raise AesError("密钥必须是 bytes")
    if not isinstance(data, (bytes, bytearray)):
        raise AesError("明文必须是 bytes")
    if len(key) != 16:
        raise AesError(f"AES-128 需要 16 字节密钥，收到 {len(key)} 字节")

    if iv is None:
        iv = key
    if len(iv) != 16:
        raise AesError(f"IV 需要 16 字节，收到 {len(iv)} 字节")

    round_keys = _expand_key(bytes(key))
    padded = pkcs7_pad(bytes(data))
    chain = bytes(iv)
    out = bytearray()

    for offset in range(0, len(padded), BLOCK_SIZE):
        block = padded[offset:offset + BLOCK_SIZE]
        state = [block[i] ^ chain[i] for i in range(BLOCK_SIZE)]
        _encrypt_block(state, round_keys)
        chain = bytes(state)
        out += chain
    return bytes(out)


def encrypt_cbc_b64(key, data, iv: bytes | None = None) -> str:
    """加密并返回 Base64 密文（超星网页登录要的就是这个形状）。

    ``key`` 与 ``data`` 传 ``str`` 时按 UTF-8 编码——超星的 key 是 ASCII、
    密码可能有中文，两边都得先落成字节。
    """
    if isinstance(key, str):
        key = key.encode("utf-8")
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.b64encode(encrypt_cbc(key, data, iv)).decode("ascii")
