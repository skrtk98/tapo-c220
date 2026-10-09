"""Tapo 独自の双方向音声（カメラのスピーカーから鳴らす）。

RTSP/ONVIF の標準にはカメラへ音声を送る経路が無い（SDP に送信用のトラックが無く、GetAudioOutputs も空）。
Tapo 独自のプロトコル（8800 番、HTTP の Digest 認証 + multipart）を使う。手順は go2rtc の pkg/tapo を参考にした：

1. POST /stream を Digest 認証する。ユーザは admin、パスワードは Tapo アカウント（クラウド）のパスワードの
   大文字の SHA256（encrypt_type="3"）または MD5。カメラアカウントのパスワードではない。
2. {"params":{"talk":{"mode":"aec"},"method":"get"},...} を送り、session_id を受け取る。
3. A-law の音声を MPEG-TS（PES のストリーム種別 0x90）に包み、20ms ずつ実時間の速さで multipart として送る。

非公式のプロトコルなので、ファームウェアの更新で使えなくなる可能性がある。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time

_BOUNDARY = b"----client-stream-boundary--"
_RATE = 8000           # カメラへ送る A-law のサンプリング周波数
_CHUNK = 160           # 20ms ぶんのバイト数
_STREAM_TYPE_PCMA = 0x90
_PCR_PID = 0x1FFF
_PES_PID = 0x100
_PMT_PID = 0x1000
_MAX_REPLY = 64 * 1024  # 開始の応答として受け取る大きさの上限
_DRAIN_TIMEOUT = 5.0    # 送信が1回詰まって待てる時間（カメラが受信を止めたとき）
_SILENCE = b"\xd5"      # A-law の 0


class TalkError(RuntimeError):
    """カメラのスピーカーへ送れなかった。partial が真なら、すでに一部は鳴っている（やり直すと重複する）。"""

    def __init__(self, message: str, *, partial: bool = False):
        super().__init__(message)
        self.partial = partial


def crc32_mpeg(data: bytes) -> int:
    """MPEG-2 の CRC32（多項式 0x04C11DB7、初期値 0xFFFFFFFF、反転なし）。PAT・PMT の検査に使う。"""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


def _packet(pid: int, payload: bytes, counter: int, *, start: bool) -> bytes:
    """188 バイトの TS パケット1つ。余りはアダプテーションフィールドの詰め物で埋める。"""
    assert len(payload) <= 184
    head = bytes([0x47, (0x40 if start else 0) | (pid >> 8), pid & 0xFF])
    if len(payload) == 184:
        return head + bytes([0x10 | counter & 0xF]) + payload
    fill = 184 - len(payload) - 1                      # アダプテーションの長さのバイトの分
    adaptation = bytes([fill]) + (bytes([0x00]) + b"\xff" * (fill - 1) if fill else b"")
    return head + bytes([0x30 | counter & 0xF]) + adaptation + payload


def _section(table_id: int, body: bytes) -> bytes:
    length = 5 + len(body) + 4
    head = bytes([table_id, 0xB0 | (length >> 8), length & 0xFF, 0x00, 0x01, 0xC1, 0x00, 0x00])
    data = head + body
    return data + crc32_mpeg(data).to_bytes(4, "big")


def header_packets(stream_type: int = _STREAM_TYPE_PCMA) -> bytes:
    """PAT と PMT（音声1本だけの番組表）。送信の最初に1度だけ送る。"""
    pat = _section(0x00, (1).to_bytes(2, "big") + (0xE000 | _PMT_PID).to_bytes(2, "big"))
    pmt = _section(
        0x02,
        (0xE000 | _PCR_PID).to_bytes(2, "big") + (0xF000).to_bytes(2, "big")
        + bytes([stream_type]) + (0xE000 | _PES_PID).to_bytes(2, "big") + (0xF000).to_bytes(2, "big"),
    )
    return _packet(0, b"\x00" + pat, 0, start=True) + _packet(_PMT_PID, b"\x00" + pmt, 0, start=True)


def _pts(value: int) -> bytes:
    v = value & 0x1FFFFFFFF
    return bytes([0x21 | ((v >> 30) & 7) << 1, (v >> 22) & 0xFF, ((v >> 15) & 0x7F) << 1 | 1,
                  (v >> 7) & 0xFF, (v & 0x7F) << 1 | 1])


class Muxer:
    """A-law の音声を、MPEG-TS のパケット列にする。"""

    def __init__(self, stream_type: int = _STREAM_TYPE_PCMA) -> None:
        self._counter = 0
        self._pts = 0
        self._type = stream_type

    def header(self) -> bytes:
        return header_packets(self._type)

    def audio(self, alaw: bytes) -> bytes:
        pes = b"\x00\x00\x01\xc0" + (3 + 5 + len(alaw)).to_bytes(2, "big") + b"\x80\x80\x05" + _pts(self._pts) + alaw
        self._pts += len(alaw) * 90000 // _RATE
        out = b""
        first = True
        while pes:
            out += _packet(_PES_PID, pes[:184], self._counter, start=first)
            pes, first, self._counter = pes[184:], False, (self._counter + 1) & 0xF
        return out


def digest_password(cloud_password: str, encrypt_type: str) -> str:
    algorithm = hashlib.sha256 if encrypt_type == "3" else hashlib.md5
    return algorithm(cloud_password.encode()).hexdigest().upper()


def _md5(*parts: str) -> str:
    return hashlib.md5(":".join(parts).encode()).hexdigest()


def _stream_request(host: str, port: int, extra: str = "") -> bytes:
    return (f"POST /stream HTTP/1.1\r\nHost: {host}:{port}\r\n"
            f"Content-Type: multipart/mixed; boundary=--client-stream-boundary--\r\n{extra}"
            "Content-Length: 0\r\n\r\n").encode()


def _field(text: str, name: str) -> str:
    found = re.search(name + r'="([^"]*)"', text)
    return found.group(1) if found else ""


async def authenticate(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, host: str, port: int,
                       cloud_password: str, timeout: float = 10.0) -> None:
    """8800 番の /stream を Digest 認証する。"""
    async def headers() -> str:
        return (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)).decode(errors="replace")

    writer.write(_stream_request(host, port))
    challenge = await headers()
    if "401" not in challenge.split("\r\n", 1)[0] or not _field(challenge, "nonce"):
        raise TalkError("カメラが認証を求めなかった（想定外の応答）")
    realm, nonce, qop = _field(challenge, "realm"), _field(challenge, "nonce"), _field(challenge, "qop") or "auth"
    password = digest_password(cloud_password, _field(challenge, "encrypt_type"))
    cnonce, nc = os.urandom(16).hex(), "00000001"
    response = _md5(_md5("admin", realm, password), nonce, nc, cnonce, qop, _md5("POST", "/stream"))
    writer.write(_stream_request(
        host, port,
        f'Authorization: Digest username="admin", realm="{realm}", nonce="{nonce}", uri="/stream", '
        f'qop={qop}, nc={nc}, cnonce="{cnonce}", response="{response}", opaque="{_field(challenge, "opaque")}", '
        "algorithm=MD5\r\n",
    ))
    answer = await headers()
    if " 200" not in answer.split("\r\n", 1)[0]:
        raise TalkError(f"カメラの認証に失敗した（{answer.split(chr(13), 1)[0].strip()}）。"
                        "Tapo アカウントのパスワードを確かめること")


def _reply_params(reply: bytes) -> dict | None:
    """開始の応答（multipart の中の JSON）を、最後まで届いていれば解析して params を返す。途中なら None。"""
    start = reply.find(b"{")
    if start < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(reply[start:].decode("utf-8"))
    except ValueError:  # JSON が途中で切れている（UnicodeDecodeError もここに含まれる）
        return None
    params = value.get("params") if isinstance(value, dict) else None
    return params if isinstance(params, dict) else {}


class TapoTalk:
    """カメラのスピーカーへ音声を送る1回の接続。`async with` で使う。"""

    def __init__(self, host: str, cloud_password: str, *, port: int = 8800, timeout: float = 10.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._password = cloud_password
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._session = ""
        self._mux = Muxer()
        self._drain: asyncio.Task | None = None
        # 最初の音声がカメラ向けの送信に通ったか。カメラからの受信確認は無いので、鳴ったことの保証ではない
        self.sent = False

    async def __aenter__(self) -> TapoTalk:
        await self.open()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def open(self) -> None:
        """接続して認証し、双方向音声を始める。全体に期限がある（応答を少しずつ返し続けるカメラで止まらない）。"""
        if not self._password:
            raise TalkError("TAPO_CLOUD_PASSWORD（Tapo アカウントのパスワード）が設定されていない")
        try:
            await asyncio.wait_for(self._open(), self.timeout * 3)
        except asyncio.TimeoutError:
            await self.close()
            raise TalkError("カメラの応答が遅すぎる") from None
        except OSError as e:
            raise TalkError(f"カメラの 8800 番につながらない（{type(e).__name__}）") from None

    async def _open(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        try:
            await authenticate(self._reader, self._writer, self.host, self.port, self._password, self.timeout)
            body = json.dumps({"params": {"talk": {"mode": "aec"}, "method": "get"}, "seq": 3, "type": "request"})
            self._writer.write(_BOUNDARY + b"\r\nContent-Type: application/json\r\nContent-Length: "
                               + str(len(body)).encode() + b"\r\n\r\n" + body.encode() + b"\r\n")
            reply, params = b"", None
            while params is None:
                chunk = await self._reader.read(4096)
                if not chunk:
                    raise TalkError("双方向音声の開始の応答が無かった")
                reply += chunk
                if len(reply) > _MAX_REPLY:
                    raise TalkError("双方向音声の開始の応答が大きすぎる")
                params = _reply_params(reply)  # JSON が最後まで届いてから判定する（TCP の分割に左右されない）
            code, session = params.get("error_code"), params.get("session_id")
            if code not in (0, None) or not session:
                raise TalkError(f"カメラが双方向音声を開始しなかった（error_code {code}）")
            self._session = str(session)
            self._drain = asyncio.create_task(self._discard())
            self._write(self._mux.header())
        except BaseException:
            await self.close()
            raise

    async def _discard(self) -> None:
        """カメラからの応答を読み捨てる（詰まらせない）。"""
        try:
            while await self._reader.read(4096):
                pass
        except (OSError, asyncio.CancelledError):
            pass

    def _write(self, body: bytes) -> None:
        self._writer.write(_BOUNDARY + b"\r\nContent-Type: audio/mp2t\r\nX-If-Encrypt: 0\r\nX-Session-Id: "
                           + self._session.encode() + b"\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)

    async def send(self, alaw: bytes) -> None:
        """A-law（8kHz）の音声を、実時間の速さで送る。送り終えるまで戻らない。
        カメラが受信を止めたら諦める（送信ごとの期限と、全体の期限＝音声の長さ＋余裕）。"""
        try:
            await asyncio.wait_for(self._send(alaw), len(alaw) / _RATE + 15.0)
        except (asyncio.TimeoutError, OSError) as e:
            raise TalkError(f"カメラへの送信が止まった（{type(e).__name__}）", partial=self.sent) from None

    async def _send(self, alaw: bytes) -> None:
        start = time.monotonic()
        for index in range(0, len(alaw), _CHUNK):
            chunk = alaw[index:index + _CHUNK].ljust(_CHUNK, _SILENCE)   # 端数は無音で埋める
            self._write(self._mux.audio(chunk))
            await asyncio.wait_for(self._writer.drain(), _DRAIN_TIMEOUT)
            self.sent = True  # 最初の送信が通ってから（バッファに入っただけでは、まだ鳴っていない）
            wait = start + (index // _CHUNK + 1) * _CHUNK / _RATE - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)

    async def close(self) -> None:
        if self._drain:
            self._drain.cancel()
            await asyncio.gather(self._drain, return_exceptions=True)
            self._drain = None
        if self._writer:
            writer, self._writer = self._writer, None
            writer.close()
            try:
                # カメラが受信を止めていると、残りの送信を待って閉じられない。待ちすぎず、接続を破棄する
                await asyncio.wait_for(writer.wait_closed(), 2.0)
            except (OSError, asyncio.TimeoutError):
                writer.transport.abort()


async def play_alaw(host: str, cloud_password: str, alaw: bytes, *, port: int = 8800) -> None:
    """A-law（8kHz モノラル）の音声を、カメラのスピーカーで鳴らす。"""
    async with TapoTalk(host, cloud_password, port=port) as talk:
        await talk.send(alaw)
        await asyncio.sleep(0.3)  # 送り終えたあと、スピーカーが鳴り終わるまで
