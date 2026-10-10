"""カメラ本体の microSD カードの録画（pytapo と cryptography が要る。`tapo-c220-survey[tapo]`）。

ONVIF に録画の検索・再生のサービス（Profile G）は無く、RTSP でも再生できない。どちらも Tapo 独自の経路を使う。
認証はどちらも Tapo アカウント（クラウド）のパスワード。

- 一覧（録画のある日、1日の中の区間、カードの状態）：443 番の独自の API。ログインの手順が込み入っているので pytapo に任せる。
- 取り出し：8800 番の /stream に、スピーカーと同じ Digest 認証でつなぎ、playback の要求を送る。
  録画は AES-CBC で暗号化された MPEG-TS（映像は H.264、音声は G.711。この個体は µ-law 16kHz でストリーム種別 0x91）として届く。
  ファイルの転送ではなく「再生」なので、録画の長さと同じ時間がかかる。
  scale で 2 倍以上を指定するとデータは速く届くが、音声が来なくなり、映像の時刻の進み方も等速のときと変わる
  （8 秒間に進んだ PTS は 2 倍で 7.9 秒、4 倍で 14 秒、16 倍で 8.9 秒）。扱いが定まらないので、等速だけを使う。
  end_time は守られず、指定を過ぎても流れ続けるので、必要なぶんを受け取ったらこちらから切る。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from .api import TapoApi, TapoApiError
from .config import TapoConfig
from .media import MediaError
from .talk import TalkError, authenticate, crc32_mpeg

_BOUNDARY = b"----client-stream-boundary--"
_MAX_PART = 4 * 1024 * 1024    # 1つの部分の大きさの上限
# 音声のストリーム種別 → (ffmpeg の形式名, サンプリング周波数)。ファームウェアによって A-law 8kHz か µ-law 16kHz
_AUDIO_TYPES = {0x90: ("alaw", 8000), 0x91: ("mulaw", 16000)}
_VIDEO_TYPE = 0x1B             # H.264
_FIRST_DATA_TIMEOUT = 12.0     # 開始してから最初の映像が届くまで（録画が無い時刻なら届かない）
_STALL_TIMEOUT = 15.0          # 途中でこれだけ何も届かなければ、そこまでで終える
_KINDS = {1: "continuous", 2: "event"}  # 区間の種別（vedio_type）。1 は長く連続した区間、2 は短い区間として返る


class SdCardError(TalkError):
    pass


class NoRecording(SdCardError):
    """その時刻の録画が SD カードに無い。"""


@dataclass(frozen=True)
class Segment:
    start: float  # UNIX 時刻（秒）
    end: float
    kind: str     # "continuous"（常時録画）か "event"（検知）。それ以外は種別の番号


def decryptor(nonce: str, password: str) -> Callable[[bytes], bytes]:
    """録画の部分ごとの復号。鍵は md5(nonce:パスワード)、IV は md5(admin:nonce)、AES-CBC で、末尾は PKCS7。"""
    key = hashlib.md5(f"{nonce}:{password}".encode()).digest()
    iv = hashlib.md5(f"admin:{nonce}".encode()).digest()

    def decrypt(data: bytes) -> bytes:
        if not data or len(data) % 16:
            raise SdCardError("録画の暗号の長さが不正")
        out = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        plain = out.update(data) + out.finalize()
        pad = plain[-1]
        if not 1 <= pad <= 16 or plain[-pad:] != bytes([pad]) * pad:  # 末尾の pad バイトがすべて同じ値であること
            raise SdCardError("録画の復号に失敗した（パスワードが違う可能性）")
        return plain[:-pad]

    return decrypt


class TsReader:
    """MPEG-TS の断片を順に受け取り、G.711 の音声を取り出し、映像がどこまで進んだか（PTS）を追う。
    ffmpeg はこの音声のストリーム種別（0x90・0x91）を知らないので、音声は自分で取り出す。
    断片の切れ目はパケット（188 バイト）の途中でもよい。"""

    def __init__(self) -> None:
        self.audio = bytearray()
        self.codec: tuple[str, int] | None = None  # (形式名, サンプリング周波数)。PMT を読むまで不明
        self._pending = b""
        self._pmt_pid: int | None = None
        self._audio_pid: int | None = None
        self._video_pid: int | None = None
        self._first_pts: int | None = None
        self._last_pts = 0

    @property
    def video_seconds(self) -> float:
        """最初の映像から、いちばん進んだ映像までの長さ（秒）。"""
        return 0.0 if self._first_pts is None else ((self._last_pts - self._first_pts) & 0x1FFFFFFFF) / 90000

    def take_audio(self) -> bytes:
        data, self.audio = bytes(self.audio), bytearray()
        return data

    def feed(self, data: bytes) -> None:
        buffer = self._pending + data
        offset = 0
        while len(buffer) - offset >= 188:
            if buffer[offset] != 0x47:
                nxt = buffer.find(b"\x47", offset + 1)
                if nxt < 0:
                    offset = len(buffer)
                    break
                offset = nxt
                continue
            try:
                self._parse(buffer[offset:offset + 188])
            except IndexError:
                pass  # 壊れたパケットは捨てる（番組表は 1 パケットに収まる前提。この機種は映像1・音声1）
            offset += 188
        self._pending = buffer[offset:]

    @staticmethod
    def _section(payload: bytes, table_id: int) -> bytes | None:
        """PSI のセクションを返す。長さと CRC が正しいときだけ（壊れた番組表で状態を変えない）。"""
        if not payload or 1 + payload[0] + 3 > len(payload):
            return None
        section = payload[1 + payload[0]:]
        length = 3 + ((section[1] & 0x0F) << 8 | section[2])
        if section[0] != table_id or length > len(section) or length < 12 or crc32_mpeg(section[:length]) != 0:
            return None
        return section[:length]

    def _parse(self, packet: bytes) -> None:
        pid = (packet[1] & 0x1F) << 8 | packet[2]
        start = bool(packet[1] & 0x40)
        body = 4
        if packet[3] & 0x20:  # アダプテーションフィールド
            body += 1 + packet[4]
        if not packet[3] & 0x10 or body >= 188:
            return
        payload = packet[body:]
        if pid == 0 and start:
            section = self._section(payload, 0x00)
            if section is not None:
                self._pmt_pid = (section[10] & 0x1F) << 8 | section[11]
        elif pid == self._pmt_pid and start:
            section = self._section(payload, 0x02)
            if section is None or len(section) < 16:
                return
            end = len(section) - 4
            index = 12 + ((section[10] & 0x0F) << 8 | section[11])
            while index + 5 <= end:
                stream_pid = (section[index + 1] & 0x1F) << 8 | section[index + 2]
                if section[index] in _AUDIO_TYPES:
                    self._audio_pid, self.codec = stream_pid, _AUDIO_TYPES[section[index]]
                elif section[index] == _VIDEO_TYPE:
                    self._video_pid = stream_pid
                index += 5 + ((section[index + 3] & 0x0F) << 8 | section[index + 4])
        elif pid == self._audio_pid:
            if start and payload[:3] == b"\x00\x00\x01":
                if len(payload) < 9 or 9 + payload[8] > len(payload):
                    return
                payload = payload[9 + payload[8]:]  # PES のヘッダを除く
            self.audio += payload
        elif pid == self._video_pid and start and payload[:3] == b"\x00\x00\x01" and payload[7] & 0x80:
            p = payload[9:14]
            pts = (p[0] >> 1 & 7) << 30 | p[1] << 22 | (p[2] >> 1) << 15 | p[3] << 7 | p[4] >> 1
            if self._first_pts is None:
                self._first_pts = pts
            if ((pts - self._last_pts) & 0x1FFFFFFFF) < 1 << 32 or self._last_pts == 0:  # 進んだときだけ覚える
                self._last_pts = pts


async def _read_parts(reader: asyncio.StreamReader, timeout: Callable[[], float]
                      ) -> AsyncIterator[tuple[dict[str, str], bytes]]:
    """multipart の応答を、(ヘッダ, 本体) の組として順に返す。timeout() 秒なにも届かなければ TimeoutError。"""
    while True:
        line = (await asyncio.wait_for(reader.readline(), timeout())).strip()
        if not line:
            if reader.at_eof():
                return
            continue
        if not line.startswith(b"--"):
            continue
        headers: dict[str, str] = {}
        while True:
            raw = (await asyncio.wait_for(reader.readline(), timeout())).decode(errors="replace").strip()
            if not raw:
                break
            key, _, value = raw.partition(":")
            headers[key.strip().lower()] = value.strip()
            if len(headers) > 32:
                raise SdCardError("録画の応答のヘッダが多すぎる")
        size = int(headers.get("content-length", "0") or 0)
        if not 0 <= size <= _MAX_PART:
            raise SdCardError("録画の応答の部分が大きすぎる")
        yield headers, await asyncio.wait_for(reader.readexactly(size), timeout())


class SdCard:
    def __init__(self, cfg: TapoConfig, *, api: TapoApi | None = None, timeout: float = 10.0):
        self.cfg, self.timeout = cfg, timeout
        self.api = api or TapoApi(cfg)
        self._playback_lock = asyncio.Lock()  # 再生は1本ずつ

    # --- 一覧（443 番、pytapo） ---------------------------------------------------------------------------------------

    async def _call(self, work: Callable[[object], object]):
        try:
            return await self.api.call(work)
        except TapoApiError as e:
            raise SdCardError(str(e)) from None

    async def status(self) -> dict[str, str]:
        """カードの状態（status、total_space、free_space など。カードが無ければ空）。"""
        reply = await self._call(lambda api: api.executeFunction(
            "getSdCardStatus", {"harddisk_manage": {"table": ["hd_info"]}}))
        cards = reply.get("harddisk_manage", {}).get("hd_info", [])
        return dict(next(iter(cards[0].values()))) if cards else {}

    async def days(self, start: str = "20000101", end: str | None = None) -> list[str]:
        """録画のある日（カメラの時刻での YYYYMMDD）。"""
        found = await self._call(lambda api: api.getRecordingsList(start, end))
        return sorted(next(iter(day.values()))["date"] for day in found)

    async def segments(self, date: str) -> list[Segment]:
        """その日（カメラの時刻での YYYYMMDD）の録画の区間。"""
        if not re.fullmatch(r"\d{8}", date):
            raise ValueError(f"日付は YYYYMMDD: {date}")
        found = await self._call(lambda api: api.getRecordings(date))
        result = []
        for item in found:
            value = next(iter(item.values()))
            kind = value.get("vedio_type")
            result.append(Segment(float(value["startTime"]), float(value["endTime"]), _KINDS.get(kind, str(kind))))
        return sorted(result, key=lambda s: s.start)

    async def detections(self, start: float, end: float) -> list[dict]:
        """その間（UNIX 時刻）にカメラが検知して記録した出来事。start と end（UNIX 時刻）と、機種が返す種類の番号（alarm_type）。
        1件も無いときは、カメラが検索の失敗（-71105）を返すので、空として扱う。"""
        try:
            found = await self.api.call(lambda api: api.getEvents(int(start), int(end)))
        except TapoApiError as e:
            if "-71105" in str(e):
                return []
            raise SdCardError(str(e)) from None
        return [{"start": float(e["start_time"]), "end": float(e["end_time"]), "alarm_type": e.get("alarm_type")}
                for e in found]

    # --- 取り出し（8800 番） -------------------------------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        """いま録画を取り出している（再生は1本ずつ）。"""
        return self._playback_lock.locked()

    async def playback(self, start: float, *, exclusive: bool = False) -> AsyncIterator[bytes]:
        """時刻 start（UNIX 時刻）からの録画を、復号した MPEG-TS の断片として流し続ける。読む側がやめるまで続く。
        実時間の速さで届く。その時刻に録画が無ければ NoRecording。
        exclusive を真にすると、流している間ほかの取り出しを待たせる（すでに取り出し中なら SdCardError）。"""
        if exclusive:
            if self._playback_lock.locked():
                raise SdCardError("ほかの取り出しが進んでいる")
            async with self._playback_lock:
                async for chunk in self._playback(start):
                    yield chunk
        else:
            async for chunk in self._playback(start):
                yield chunk

    async def _playback(self, start: float) -> AsyncIterator[bytes]:
        waiting = _FIRST_DATA_TIMEOUT
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(self.cfg.host, self.cfg.talk_port),
                                                    self.timeout)
        except (OSError, asyncio.TimeoutError) as e:
            raise SdCardError(f"カメラの 8800 番につながらない（{type(e).__name__}）") from None
        try:
            nonce, password = await authenticate(reader, writer, self.cfg.host, self.cfg.talk_port,
                                                 self.cfg.cloud_password, self.timeout)
            decrypt = decryptor(nonce, password)
            body = json.dumps({"type": "request", "seq": 1, "params": {"playback": {
                "client_id": 1, "channels": [0], "scale": "1/1", "start_time": str(int(start)),
                "end_time": str(int(start) + 86400), "event_type": [1, 2]}, "method": "get"}})
            writer.write(_BOUNDARY + b"\r\nContent-Type: application/json\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\n\r\n" + body.encode() + b"\r\n")
            got_video = False
            try:
                async for headers, data in _read_parts(reader, lambda: waiting):
                    kind = headers.get("content-type", "")
                    if kind == "application/json":
                        code = re.search(rb'"error_code"\s*:\s*(-?\d+)', data)
                        if code and int(code.group(1)) != 0:
                            raise NoRecording(f"カメラが再生を始めなかった（error_code {code.group(1).decode()}）")
                        if b'"finished"' in data:  # 録画の終わりまで流し終えた
                            break
                    elif kind == "video/mp2t":
                        got_video, waiting = True, _STALL_TIMEOUT
                        yield decrypt(data)
            except asyncio.TimeoutError:
                pass
            if not got_video:
                raise NoRecording("その時刻の録画が SD カードに無い")
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 2.0)
            except (OSError, asyncio.TimeoutError):
                writer.transport.abort()

    async def export(self, start: float, seconds: float, path: str | Path, *,
                     progress: Callable[[float], None] | None = None) -> float:
        """時刻 start から seconds 秒ぶんの録画を MP4（H.264 はそのまま、音声は AAC）として保存する。保存できた秒数を返す。
        実時間の速さで届くので、seconds 秒ほどかかる。
        指定した時刻の直前のキーフレームから始まるので、少し手前から入ることがある。
        progress には、取り出せた秒数が随時渡される。"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = TsReader()
        async with self._playback_lock:
            with tempfile.TemporaryDirectory(dir=path.parent) as tmp:
                video, audio = Path(tmp) / "video.ts", Path(tmp) / "audio.raw"
                with video.open("wb") as video_file, audio.open("wb") as audio_file:
                    stream = self.playback(start)
                    try:
                        async for chunk in stream:
                            ts.feed(chunk)
                            video_file.write(chunk)
                            audio_file.write(ts.take_audio())
                            if progress:
                                progress(min(ts.video_seconds, seconds))
                            if ts.video_seconds >= seconds:
                                break
                    finally:
                        await stream.aclose()
                got = min(ts.video_seconds, seconds)
                argv = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-fflags", "+genpts",
                        "-f", "mpegts", "-i", str(video)]
                if ts.codec and audio.stat().st_size:
                    form, rate = ts.codec
                    argv += ["-f", form, "-ar", str(rate), "-ac", "1", "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
                             "-c:a", "aac", "-b:a", "64k"]
                else:
                    argv += ["-map", "0:v:0"]
                argv += ["-c:v", "copy", "-t", f"{seconds:.3f}", "-movflags", "+faststart", "-f", "mp4", "-y", str(path)]
                proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.DEVNULL,
                                                            stderr=asyncio.subprocess.PIPE)
                try:
                    _, err = await proc.communicate()
                except BaseException:
                    proc.kill()
                    await proc.wait()
                    path.unlink(missing_ok=True)
                    raise
                if proc.returncode != 0 or not path.is_file() or path.stat().st_size == 0:
                    path.unlink(missing_ok=True)
                    raise MediaError(f"録画を MP4 にできなかった（{err.decode(errors='replace').strip()[-300:]}）")
        return got

    def close(self) -> None:
        self.api.close()
