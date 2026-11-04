"""知乎 Web 端 x-zse-96（2.0）请求签名的纯 Python 实现。

算法来源：知乎前端 JS 的逆向结果，本文件按 empthylucky/zhihu-core 的 zse-signer.mjs
（其本身移植自 zly2006/zhihu-plus-plus）移植为 Python，仅用于学习研究，请遵守知乎用户协议。

签名流程：
  src  = "101_3_3.0" + "+" + <URL 路径含查询串> + "+" + <d_c0 cookie 值> [+ "+" + <POST body>]
  sig  = "2.0_" + custom_encode( sm4_like_cbc( [210, 0] + urlencode(md5hex(src)) + PKCS7 ) )
"""
from __future__ import annotations

import hashlib
from urllib.parse import quote

ZSE93 = "101_3_3.0"

_ZK = [
    1170614578, 1024848638, 1413669199, 3951632832, 3528873006, 2921909214, 4151847688, 3997739139,
    1933479194, 3323781115, 3888513386, 460404854, 3747539722, 2403641034, 2615871395, 2119585428,
    2265697227, 2035090028, 2773447226, 4289380121, 4217216195, 2200601443, 3051914490, 1579901135,
    1321810770, 456816404, 2903323407, 4065664991, 330002838, 3506006750, 363569021, 2347096187,
]

_ZB = bytes([
    20, 223, 245, 7, 248, 2, 194, 209, 87, 6, 227, 253, 240, 128, 222, 91, 237, 9, 125, 157, 230,
    93, 252, 205, 90, 79, 144, 199, 159, 197, 186, 167, 39, 37, 156, 198, 38, 42, 43, 168, 217,
    153, 15, 103, 80, 189, 71, 191, 97, 84, 247, 95, 36, 69, 14, 35, 12, 171, 28, 114, 178, 148,
    86, 182, 32, 83, 158, 109, 22, 255, 94, 238, 151, 85, 77, 124, 254, 18, 4, 26, 123, 176, 232,
    193, 131, 172, 143, 142, 150, 30, 10, 146, 162, 62, 224, 218, 196, 229, 1, 192, 213, 27, 110,
    56, 231, 180, 138, 107, 242, 187, 54, 120, 19, 44, 117, 228, 215, 203, 53, 239, 251, 127, 81,
    11, 133, 96, 204, 132, 41, 115, 73, 55, 249, 147, 102, 48, 122, 145, 106, 118, 74, 190, 29, 16,
    174, 5, 177, 129, 63, 113, 99, 31, 161, 76, 246, 34, 211, 13, 60, 68, 207, 160, 65, 111, 82,
    165, 67, 169, 225, 57, 112, 244, 155, 51, 236, 200, 233, 58, 61, 47, 100, 137, 185, 64, 17, 70,
    234, 163, 219, 108, 170, 166, 59, 149, 52, 105, 24, 212, 78, 173, 45, 0, 116, 226, 119, 136,
    206, 135, 175, 195, 25, 92, 121, 208, 126, 139, 3, 75, 141, 21, 130, 98, 241, 40, 154, 66, 184,
    49, 181, 46, 243, 88, 101, 183, 8, 23, 72, 188, 104, 179, 210, 134, 250, 201, 164, 89, 216,
    202, 220, 50, 221, 152, 140, 33, 235, 214,
])
assert len(_ZB) == 256

_ALPHABET = "6fpLRqJO8M/c3jnYxFkUVC4ZIG12SiH=5v0mXDazWBTsuw7QetbKdoPyAl+hN9rgE"
_KEY16 = b"059053f7d15e01d7"
_M32 = 0xFFFFFFFF


def _rotl(n: int, bits: int) -> int:
    return ((n << bits) | (n >> (32 - bits))) & _M32


def _g(tt: int) -> int:
    ti = ((_ZB[(tt >> 24) & 0xFF] << 24) | (_ZB[(tt >> 16) & 0xFF] << 16)
          | (_ZB[(tt >> 8) & 0xFF] << 8) | _ZB[tt & 0xFF])
    return (ti ^ _rotl(ti, 2) ^ _rotl(ti, 10) ^ _rotl(ti, 18) ^ _rotl(ti, 24)) & _M32


def _r_block(block: bytes) -> bytes:
    tr = [int.from_bytes(block[i:i + 4], "big") for i in range(0, 16, 4)]
    for i in range(32):
        ta = _g((tr[i + 1] ^ tr[i + 2] ^ tr[i + 3] ^ _ZK[i]) & _M32)
        tr.append((tr[i] ^ ta) & _M32)
    return b"".join(tr[j].to_bytes(4, "big") for j in (35, 34, 33, 32))


def _x_blocks(data: bytes, iv: bytes) -> bytes:
    out = bytearray()
    for off in range(0, len(data), 16):
        mixed = bytes(data[off + i] ^ iv[i] for i in range(16))
        iv = _r_block(mixed)
        out += iv
    return bytes(out)


def _custom_encode(data: bytes) -> str:
    rem = len(data) % 3
    if rem:
        data = data + bytes(3 - rem)
    out: list[str] = []
    i = 0
    p = len(data) - 1
    while p >= 0:
        v = 0
        for k in range(3):
            m = (58 >> (8 * (i % 4))) & 0xFF
            i += 1
            v |= ((data[p - k] ^ m) & 0xFF) << (8 * k)
        out.append(_ALPHABET[v & 63] + _ALPHABET[(v >> 6) & 63]
                   + _ALPHABET[(v >> 12) & 63] + _ALPHABET[(v >> 18) & 63])
        p -= 3
    return "".join(out)


def encrypt_zse(text: str) -> str:
    plain = bytearray([210, 0]) + quote(text, safe="-_.!~*'()").encode("ascii")
    pad = 16 - len(plain) % 16
    plain += bytes([pad]) * pad
    first = bytes(plain[i] ^ _KEY16[i] ^ 42 for i in range(16))
    c0 = _r_block(first)
    cipher = c0 + (_x_blocks(bytes(plain[16:]), c0) if len(plain) > 16 else b"")
    return _custom_encode(cipher)


def pathname_of(url: str) -> str:
    """与前端一致：取主机之后的全部内容（含查询串）。"""
    if "//" in url:
        rest = url.split("//", 1)[1]
        return "/" + rest.split("/", 1)[1] if "/" in rest else "/"
    return url if url.startswith("/") else "/" + url


def sign(url: str, d_c0: str, body: str | None = None, zse93: str = ZSE93) -> str:
    parts = [zse93, pathname_of(url), d_c0]
    if body is not None:
        parts.append(body)
    digest = hashlib.md5("+".join(parts).encode("utf-8")).hexdigest()
    return "2.0_" + encrypt_zse(digest)


def parse_cookie(cookie: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in cookie.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out
