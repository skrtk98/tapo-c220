"""Tapo 独自のストリーム（8800 番）への窓口。スピーカーへの音声（talk）と、録画の取り出し（sdcard）が使う。

1本の TCP 接続の上で、HTTP の multipart として JSON の要求とメディア（MPEG-TS）をやり取りし続ける。
手順は go2rtc の pkg/tapo を参考にした：

1. POST /stream を Digest 認証する。ユーザは admin、パスワードは Tapo アカウント（クラウド）のパスワードの
   大文字の SHA256（encrypt_type="3"）または MD5。カメラアカウントのパスワードではない。
2. 認証が通ると、応答の Key-Exchange に nonce が付く（録画の復号の鍵に使う）。
3. 以降は、境界で区切った部分（JSON の要求、audio/mp2t、video/mp2t）を双方が送り合う。

接続は用途ごとに1本ずつ作る（使い回せない）。非公式のプロトコルなので、ファームウェアの更新で使えなくなる可能性がある。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass

BOUNDARY = b"----client-stream-boundary--"


class StreamError(RuntimeError):
    """カメラの 8800 番とやり取りできなかった。"""


def digest_password(cloud_password: str, encrypt_type: str) -> str:
    algorithm = hashlib.sha256 if encrypt_type == "3" else hashlib.md5
    return algorithm(cloud_password.encode()).hexdigest().upper()


def _md5(*parts: str) -> str:
    return hashlib.md5(":".join(parts).encode()).hexdigest()


def _field(text: str, name: str) -> str:
    found = re.search(name + r'="([^"]*)"', text)
    return found.group(1) if found else ""


@dataclass
class Connection:
    """認証を終えた1本の接続。"""

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    nonce: str      # Key-Exchange の nonce（録画の復号に使う）
    password: str   # ハッシュしたパスワード（同上）

    def request(self, params: dict, seq: int = 1) -> None:
        """JSON の要求を1つ書く（送り切るのは、次の drain か、カメラからの読み取りのとき）。"""
        body = json.dumps({"type": "request", "seq": seq, "params": params}).encode()
        self.writer.write(BOUNDARY + b"\r\nContent-Type: application/json\r\nContent-Length: "
                          + str(len(body)).encode() + b"\r\n\r\n" + body + b"\r\n")

    def part(self, content_type: str, body: bytes, headers: str = "") -> None:
        """メディアの部分を1つ書く。headers は `X-Session-Id: 1\\r\\n` のような追加の見出し。"""
        self.writer.write(BOUNDARY + f"\r\nContent-Type: {content_type}\r\n{headers}Content-Length: {len(body)}\r\n\r\n"
                          .encode() + body)

    async def close(self) -> None:
        self.writer.close()
        try:
            # カメラが受信を止めていると、残りの送信を待って閉じられない。待ちすぎず、接続を破棄する
            await asyncio.wait_for(self.writer.wait_closed(), 2.0)
        except (OSError, asyncio.TimeoutError):
            self.writer.transport.abort()


class TapoStream:
    def __init__(self, host: str, cloud_password: str, *, port: int = 8800, timeout: float = 10.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._password = cloud_password

    @property
    def available(self) -> bool:
        """Tapo アカウントのパスワードが設定されている。"""
        return bool(self._password)

    def _post(self, extra: str = "") -> bytes:
        return (f"POST /stream HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n"
                f"Content-Type: multipart/mixed; boundary=--client-stream-boundary--\r\n{extra}"
                "Content-Length: 0\r\n\r\n").encode()

    async def connect(self) -> Connection:
        """つないで認証する。使い終えたら Connection.close() を呼ぶこと。"""
        if not self._password:
            raise StreamError("TAPO_CLOUD_PASSWORD（Tapo アカウントのパスワード）が設定されていない")
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), self.timeout)
        except (OSError, asyncio.TimeoutError) as e:
            raise StreamError(f"カメラの 8800 番につながらない（{type(e).__name__}）") from None
        connection = Connection(reader, writer, "", "")
        try:
            connection.nonce, connection.password = await self._authenticate(reader, writer)
        except BaseException as e:
            await connection.close()
            if isinstance(e, (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError)):
                raise StreamError(f"カメラの認証の途中で切れた（{type(e).__name__}）") from None
            raise
        return connection

    async def _authenticate(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> tuple[str, str]:
        async def headers() -> str:
            return (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self.timeout)).decode(errors="replace")

        writer.write(self._post())
        challenge = await headers()
        if "401" not in challenge.split("\r\n", 1)[0] or not _field(challenge, "nonce"):
            raise StreamError("カメラが認証を求めなかった（想定外の応答）")
        realm, nonce, qop = _field(challenge, "realm"), _field(challenge, "nonce"), _field(challenge, "qop") or "auth"
        password = digest_password(self._password, _field(challenge, "encrypt_type"))
        cnonce, nc = os.urandom(16).hex(), "00000001"
        response = _md5(_md5("admin", realm, password), nonce, nc, cnonce, qop, _md5("POST", "/stream"))
        writer.write(self._post(
            f'Authorization: Digest username="admin", realm="{realm}", nonce="{nonce}", uri="/stream", '
            f'qop={qop}, nc={nc}, cnonce="{cnonce}", response="{response}", opaque="{_field(challenge, "opaque")}", '
            "algorithm=MD5\r\n"))
        answer = await headers()
        if " 200" not in answer.split("\r\n", 1)[0]:
            raise StreamError(f"カメラの認証に失敗した（{answer.split(chr(13), 1)[0].strip()}）。"
                              "Tapo アカウントのパスワードを確かめること")
        exchange = re.search(r"Key-Exchange: ([^\r\n]*)", answer)
        return _field(exchange.group(1) if exchange else "", "nonce"), password


__all__ = ["BOUNDARY", "Connection", "StreamError", "TapoStream", "digest_password"]
