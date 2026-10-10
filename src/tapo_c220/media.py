"""RTSP の映像と音声。受信・変換はすべて ffmpeg に任せる。

C220 で実測したこと（ファームウェア 1.3.1）：

- `stream1` は H.264 2560×1440、`stream2` は H.264 640×360、`stream8` は MJPEG 640×360（ONVIF の profile_3）だが、
  毎秒1枚ほどしか更新されず、同じ画像を繰り返し送ってくる（約 25Mbps）。Chromium ではその JPEG が灰色にしか描かれない。使っていない。
- 音声はどれも G.711 A-law の 8kHz モノラル（ONVIF は 16kHz と答えるが、SDP と実際の中身は 8kHz）。
- 認証は RTSP の Digest。カメラアカウントの ID とパスワードを使う。
- 音声のトラックだけを SETUP すると、音声は届かない。映像と一緒に受け取って捨てる。
- RTP の時刻は正しく進むが、RTCP の Sender Report の時刻（NTP）が約 0.5 秒ごとに交互に約 ±0.38 秒ずれている。
  libav はこれに合わせて pts を直すので、pts が約 0.35 秒戻っては約 0.42 秒飛ぶ。パケットの到着そのものは滑らか。
- SDP に送信用の音声トラックは無い（`Require: www.onvif.org/ver20/backchannel` をつけても増えない）。
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import signal
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import quote

from .config import TapoConfig
from .onvif import redact

MAIN, SUB = "stream1", "stream2"

_CHUNK = 64 * 1024
_STOP_TIMEOUT = 10.0  # 録音・録画を止めるとき、ffmpeg がファイルを閉じ終えるのを待つ時間


class MediaError(RuntimeError):
    pass


_PR_SET_PDEATHSIG = 1
_libc = ctypes.CDLL(None, use_errno=True) if sys.platform == "linux" else None  # fork の後では読み込まない


async def spawn(*argv: str, **options) -> asyncio.subprocess.Process:
    """ffmpeg を起こす。Linux では、このプロセスが死んだら（強制終了でも）ffmpeg も止まるようにしておく。
    そうしないと、ファイルへ書いている ffmpeg が残り、カメラの RTSP の同時接続（3本）を埋めたままになる。
    止まるのは「起こしたスレッド」が終わったとき。イベントループを動かしているスレッドから呼ぶこと。"""
    if _libc is None:
        return await asyncio.create_subprocess_exec(*argv, **options)
    parent = os.getpid()

    def die_with_parent() -> None:
        _libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)
        if os.getppid() != parent:  # 設定する前に親が死んでいた
            os._exit(1)

    return await asyncio.create_subprocess_exec(*argv, preexec_fn=die_with_parent, **options)


def rtsp_url(cfg: TapoConfig, stream: str = MAIN) -> str:
    """認証情報を含む RTSP の URL。ログやエラーには出さないこと。"""
    user, password = quote(cfg.user, safe=""), quote(cfg.password, safe="")
    return f"rtsp://{user}:{password}@{cfg.host}:{cfg.rtsp_port}/{stream}"


def _input(cfg: TapoConfig, stream: str) -> list[str]:
    # 音声だけ欲しいときも、映像のトラックごと受け取る（`-allowed_media_types audio` で音声だけを SETUP すると何も届かない）
    return ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
            "-timeout", "10000000",  # 10 秒なにも届かなければ諦める（マイクロ秒）
            # 時刻は、届いた時点の時計で付け直す。カメラの RTCP の時刻が約 0.5 秒ごとに交互に約 ±0.38 秒ずれていて、
            # そのままだと pts が戻っては飛ぶ（録画の映像が毎秒つかえ、音声に隙間ができる）
            "-use_wallclock_as_timestamps", "1",
            "-i", rtsp_url(cfg, stream)]


class _Process:
    """ffmpeg 1本。標準エラーは読み捨てつつ末尾だけ覚えておく（パイプを詰まらせない）。"""

    def __init__(self, cfg: TapoConfig, proc: asyncio.subprocess.Process):
        self._cfg, self.proc = cfg, proc
        self._tail = b""
        self._errors = asyncio.create_task(self._read_errors())

    @classmethod
    async def start(cls, cfg: TapoConfig, argv: list[str], *, stdin: bool = False, stdout: bool = False) -> _Process:
        pipe, null = asyncio.subprocess.PIPE, asyncio.subprocess.DEVNULL
        if stdin:
            argv = [a for a in argv if a != "-nostdin"]
        proc = await spawn(*argv, stdin=pipe if stdin else null, stdout=pipe if stdout else null, stderr=pipe)
        return cls(cfg, proc)

    async def _read_errors(self) -> None:
        while chunk := await self.proc.stderr.read(4096):
            self._tail = (self._tail + chunk)[-2000:]

    async def error(self) -> str:
        await asyncio.gather(self._errors, return_exceptions=True)
        text = self._tail.decode(errors="replace").strip()
        return redact(text, self._cfg.password, quote(self._cfg.password, safe="")) or f"終了コード {self.proc.returncode}"

    async def kill(self) -> None:
        if self.proc.returncode is None:
            self.proc.kill()
        if self.proc.stdout is not None:
            # 読み残しを捨てる。受信側のバッファが満杯のままだとパイプの終わりを検知できず、wait() が戻らない
            while await self.proc.stdout.read(_CHUNK):
                pass
        await self.proc.wait()
        await asyncio.gather(self._errors, return_exceptions=True)


async def snapshot(cfg: TapoConfig, stream: str = MAIN, *, timeout: float = 20.0) -> bytes:
    """いまの映像を1枚、JPEG で返す。ONVIF の GetSnapshotUri が無いので、RTSP から最初のキーフレームを取る。"""
    process = await _Process.start(cfg, _input(cfg, stream) + ["-an", "-frames:v", "1", "-q:v", "2", "-f", "image2pipe",
                                                               "-c:v", "mjpeg", "pipe:1"], stdout=True)
    try:
        image = await asyncio.wait_for(process.proc.stdout.read(-1), timeout)
        await process.proc.wait()
    except asyncio.TimeoutError:
        raise MediaError("静止画を取れなかった（時間切れ）") from None
    finally:
        await process.kill()
    if not image:
        raise MediaError(f"静止画を取れなかった（{await process.error()}）")
    return image


async def _pipe(cfg: TapoConfig, argv: list[str]) -> AsyncIterator[bytes]:
    process = await _Process.start(cfg, argv, stdout=True)
    try:
        while chunk := await process.proc.stdout.read(_CHUNK):
            yield chunk
    finally:  # 読む側がやめたら（ブラウザを閉じた等）ffmpeg も止めて、カメラの接続を返す
        await process.kill()


def live_audio(cfg: TapoConfig) -> AsyncIterator[bytes]:
    """マイクの音だけを MP3 で流す（ブラウザの `<audio>` 用）。"""
    return _pipe(cfg, _input(cfg, SUB) + ["-vn", "-c:a", "libmp3lame", "-ar", "16000", "-b:a", "48k",
                                                           "-flush_packets", "1", "-f", "mp3", "pipe:1"])


class Recording:
    """進行中の録音・録画。stop() でファイルを閉じる。"""

    def __init__(self, process: _Process, path: Path, kind: str):
        self._process, self.path, self.kind = process, path, kind
        self.started = time.time()

    @property
    def running(self) -> bool:
        return self._process.proc.returncode is None

    async def wait(self) -> None:
        """max_seconds で自然に終わるのを待つ。"""
        await self._process.proc.wait()
        await self._check()

    async def stop(self) -> Path:
        proc = self._process.proc
        if proc.returncode is None:
            try:
                proc.stdin.write(b"q")  # ffmpeg に終了を頼む（強制終了すると WAV・MP4 の見出しが書かれない）
                await proc.stdin.drain()
                proc.stdin.close()
            except OSError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), _STOP_TIMEOUT)
            except asyncio.TimeoutError:
                pass
        await self._process.kill()
        await self._check()
        return self.path

    async def _check(self) -> None:
        if not self.path.is_file() or self.path.stat().st_size == 0:
            self.path.unlink(missing_ok=True)
            raise MediaError(f"何も記録できなかった（{await self._process.error()}）")


async def record(cfg: TapoConfig, path: str | Path, *, kind: str = "audio", stream: str = MAIN,
                 max_seconds: float | None = None) -> Recording:
    """録音（kind="audio"、16bit PCM の WAV）か録画（kind="video"、H.264 はそのまま・音声は AAC の MP4）を始める。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    limit = ["-t", f"{max_seconds:.3f}"] if max_seconds else []
    if kind == "audio":
        argv = _input(cfg, SUB) + limit + ["-vn", "-c:a", "pcm_s16le", "-f", "wav"]
    elif kind == "video":
        argv = _input(cfg, stream) + limit + ["-c:v", "copy", "-c:a", "aac", "-b:a", "64k", "-f", "mp4"]
    else:
        raise ValueError(f"kind は audio か video: {kind}")
    return Recording(await _Process.start(cfg, argv + ["-y", str(path)], stdin=True), path, kind)


async def siren_alaw(seconds: float) -> bytes:
    """サイレンの音（高低2つの音を交互に）を、カメラのスピーカーへ送る形式で作る。"""
    tone = "0.9*sin(2*PI*t*if(lt(mod(t\\,0.7)\\,0.35)\\,960\\,770))"
    proc = await spawn(
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
        f"aevalsrc={tone}:s=8000:d={seconds:.3f}", "-f", "alaw", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0 or not out:
        raise MediaError(f"サイレンの音を作れない（{err.decode(errors='replace').strip()[-300:] or proc.returncode}）")
    return out


async def to_alaw(source: str | Path, *, max_seconds: float | None = None, audio_filter: str = "") -> bytes:
    """音声ファイルを、カメラのスピーカーへ送る形式（8kHz モノラルの G.711 A-law、見出し無し）にする。"""
    argv = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(source), "-vn", "-ac", "1",
            "-ar", "8000"]
    if max_seconds:  # 指定があれば、先頭のその長さだけにする
        argv += ["-t", f"{max_seconds:.3f}"]
    if audio_filter:
        argv += ["-af", audio_filter]
    proc = await spawn(*argv, "-f", "alaw", "pipe:1", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0 or not out:
        raise MediaError(f"音声を変換できない（{err.decode(errors='replace').strip()[-300:] or proc.returncode}）")
    return out
