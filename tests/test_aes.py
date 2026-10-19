"""``studydash.aes`` 的测试。

这个纯 Python 的 AES-128-CBC 是超星密码登录的必经之路：它算错了不会报错，
只会让服务端说「密码错误」，用户会以为是自己记错密码。所以必须有已知答案向量钉住。

两组向量：
* **FIPS-197 C.1** 校验分组加密的核心（教科书写死的值，最容易发现 S 盒/列混淆写错）。
* **生产密钥的固定密文**（``u2oh6Vu^HWe4_AES``，超星登录页里硬编码的那个），
  由 ``cryptography`` 的 OpenSSL 实现算出后抄进来，覆盖空明文、整块边界、
  中文、超长明文这些容易在补齐（PKCS7）上出错的地方。
"""

from __future__ import annotations

import base64

import pytest

from studydash import aes

#: 超星登录页里写死的传输密钥，正好 16 字节。
TRANSFER_KEY = b"u2oh6Vu^HWe4_AES"

#: 用 OpenSSL（cryptography 包）算出来的标准答案。
VECTORS = [
    (b"test1234", "9BfA7hlj4yrEG4mPZ6Fc6Q=="),
    (b"", "LXYPGIOJyFUvuG1SPO0nVQ=="),
    (b"aaaaaaaaaaaaaaa", "1FCJbOwNmPOg9sn4TGmPXg=="),
    (b"aaaaaaaaaaaaaaaa", "YaUb/y6VdOUq8BImCZkv2KHpF+lcIDY7wyzg3Njg5CU="),
    (b"aaaaaaaaaaaaaaaaa", "YaUb/y6VdOUq8BImCZkv2ELH45aT/QUDvdxmawAz5yE="),
    ("中文密码123".encode(), "5iwU745q53VMSx2jLDlFwQ=="),
    (b"0123456789abcdef0123456789abcdef",
     "hmUF/tdyZDwfeahGBesCEANKTRBVKnZG9Qc6aSKYoaSystEJM7/5A/E1/BuZbrz2"),
    (b"p@ss word!", "Vd1zWi2sd/uQcZCqZu3aqA=="),
]


# ---------------------------------------------------------------- 核心分组加密


def test_fips197_known_answer() -> None:
    """FIPS-197 附录 C.1 的标准向量——分组加密本体算对了，别的才有意义。"""
    key = bytes(range(16))
    plaintext = bytes.fromhex("00112233445566778899aabbccddeeff")
    expected = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")

    state = list(plaintext)
    aes._encrypt_block(state, aes._expand_key(key))
    assert bytes(state) == expected


def test_key_expansion_produces_eleven_round_keys() -> None:
    """AES-128 是 10 轮 + 1 个初始加轮密钥 = 11 个。"""
    keys = aes._expand_key(bytes(16))
    assert len(keys) == 11
    assert all(len(k) == 16 for k in keys)
    assert keys[0] == bytes(16)


def test_sbox_spot_checks() -> None:
    """S 盒首尾和几个中点值；写错一个字节整张表就废了。"""
    sbox = aes._SBOX
    assert len(sbox) == 256
    assert (sbox[0], sbox[1], sbox[0x53], sbox[0xFF]) == (0x63, 0x7C, 0xED, 0x16)


# ---------------------------------------------------------------- 生产密钥的固定密文


@pytest.mark.parametrize("plaintext,expected", VECTORS)
def test_matches_reference_vectors(plaintext: bytes, expected: str) -> None:
    assert aes.encrypt_cbc_b64(TRANSFER_KEY, plaintext) == expected


@pytest.mark.parametrize("plaintext,expected", VECTORS)
def test_iv_defaults_to_key(plaintext: bytes, expected: str) -> None:
    """超星页面里就是拿密钥当 IV；显式传同样的 IV 必须得到同样的结果。"""
    assert aes.encrypt_cbc_b64(TRANSFER_KEY, plaintext, TRANSFER_KEY) == expected


def test_base64_wrapper_is_the_raw_ciphertext() -> None:
    raw = aes.encrypt_cbc(TRANSFER_KEY, b"test1234")
    assert base64.b64encode(raw).decode() == aes.encrypt_cbc_b64(TRANSFER_KEY, b"test1234")


def test_matches_openssl() -> None:
    """有 cryptography 时再拿它的 AES 当参照物做一轮交叉验证。"""
    cryptography = pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    for plaintext, _expected in VECTORS:
        enc = Cipher(algorithms.AES(TRANSFER_KEY), modes.CBC(TRANSFER_KEY)).encryptor()
        reference = enc.update(aes.pkcs7_pad(plaintext)) + enc.finalize()
        assert aes.encrypt_cbc(TRANSFER_KEY, plaintext) == reference


def test_openssl_can_decrypt_our_output() -> None:
    """方向反过来验：让 OpenSSL 解我们的密文，解出来必须还是原密码。"""
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    for plaintext, _expected in VECTORS:
        dec = Cipher(algorithms.AES(TRANSFER_KEY), modes.CBC(TRANSFER_KEY)).decryptor()
        padded = dec.update(aes.encrypt_cbc(TRANSFER_KEY, plaintext)) + dec.finalize()
        pad = padded[-1]
        assert padded[:-pad] == plaintext


# ---------------------------------------------------------------- 补齐与边界


@pytest.mark.parametrize("length,expected_len", [(0, 16), (1, 16), (15, 16), (16, 32),
                                                 (17, 32), (31, 32), (32, 48)])
def test_pkcs7_pad_boundaries(length: int, expected_len: int) -> None:
    """整块边界是补齐最容易写错的地方：刚好 16 字节也要再加一整块。"""
    padded = aes.pkcs7_pad(b"x" * length)
    assert len(padded) == expected_len
    assert padded[-1] == expected_len - length
    assert padded[:length] == b"x" * length


def test_ciphertext_is_always_a_whole_number_of_blocks() -> None:
    for length in range(0, 40):
        assert len(aes.encrypt_cbc(TRANSFER_KEY, b"y" * length)) % 16 == 0


def test_empty_password_still_produces_one_block() -> None:
    ct = aes.encrypt_cbc(TRANSFER_KEY, b"")
    assert len(ct) == 16
    assert aes.encrypt_cbc_b64(TRANSFER_KEY, b"") == "LXYPGIOJyFUvuG1SPO0nVQ=="


def test_same_input_encrypts_to_the_same_output() -> None:
    """CBC 的 IV 固定成密钥，所以同样输入必须每次得到同样密文（服务端就这么期望）。"""
    first = aes.encrypt_cbc_b64(TRANSFER_KEY, b"same-password")
    assert all(aes.encrypt_cbc_b64(TRANSFER_KEY, b"same-password") == first for _ in range(5))


def test_chinese_password_is_utf8_encoded() -> None:
    """中文密码按 UTF-8 编码（3 字节/字），不能按平台默认编码。"""
    assert len(aes.encrypt_cbc(TRANSFER_KEY, "中文密码123".encode("utf-8"))) == 16
    assert aes.encrypt_cbc_b64(TRANSFER_KEY, "中文密码123".encode("utf-8")) == \
        "5iwU745q53VMSx2jLDlFwQ=="


# ---------------------------------------------------------------- 输入校验


@pytest.mark.parametrize("bad_key", [b"", b"short", b"fifteen-bytes!!", b"x" * 24, b"x" * 32])
def test_rejects_key_that_is_not_sixteen_bytes(bad_key: bytes) -> None:
    with pytest.raises(aes.AesError, match="16 字节"):
        aes.encrypt_cbc(bad_key, b"test")


def test_rejects_non_bytes_key() -> None:
    with pytest.raises(aes.AesError, match="密钥必须是 bytes"):
        aes.encrypt_cbc("u2oh6Vu^HWe4_AES", b"test")  # type: ignore[arg-type]


def test_rejects_non_bytes_plaintext() -> None:
    with pytest.raises(aes.AesError, match="明文必须是 bytes"):
        aes.encrypt_cbc(TRANSFER_KEY, "test")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_iv", [b"", b"short", b"x" * 17])
def test_rejects_iv_that_is_not_sixteen_bytes(bad_iv: bytes) -> None:
    with pytest.raises(aes.AesError, match="IV 需要 16 字节"):
        aes.encrypt_cbc(TRANSFER_KEY, b"test", bad_iv)


def test_aes_error_is_a_value_error() -> None:
    """调用方可能只 ``except ValueError``，所以继承关系要稳住。"""
    assert issubclass(aes.AesError, ValueError)
