"""microSD の録画を、指定した時刻から WebRTC で流し続ける（aiortc と `[tapo]` の依存が要る）。

カメラからは実時間の速さで MPEG-TS が届く。映像（H.264）は変換せずそのまま送る。音声（G.711 µ-law 16kHz など）は
WebRTC の標準の形（8kHz）ではないので、PCM に戻して渡し、aiortc に Opus へ変換させる。
いま流している位置（録画の時刻）は position で分かる。
"""

from __future__ import annotations

import array
import asyncio
import queue
import threading
from fractions import Fraction

import av

from .sdcard import SdCard, SdCardError, TsReader
from .webrtc import QueueTrack

_FRAME_SECONDS = 0.02  # 音声を渡す1回ぶんの長さ


def _g711_table(form: str) -> list[int]:
    """G.711 の 1 バイト → 16bit の標本、の対応表。"""
    table = []
    for value in range(256):
        if form == "mulaw":
            v = ~value & 0xFF
            sample = (((v & 0x0F) << 3) + 0x84 << ((v & 0x70) >> 4)) - 0x84
            table.append(-sample if v & 0x80 else sample)
        else:
            v = value ^ 0x55
            exponent, mantissa = (v & 0x70) >> 4, v & 0x0F
            sample = (mantissa << 4) + 8 if exponent == 0 else ((mantissa << 4) + 0x108) << (exponent - 1)
            table.append(sample if v & 0x80 else -sample)
    return table


class _Feed:
    """別スレッドの libav に、届いた TS を順に読ませるための入れ物（read だけを持つファイルのふり）。"""

    def __init__(self) -> None:
        self.chunks: queue.Queue[bytes | None] = queue.Queue()
        self._rest = b""
        self._ended = False

    def read(self, size: int) -> bytes:
        if not self._rest and not self._ended:
            chunk = self.chunks.get()
            if chunk is None:
                self._ended = True
            else:
                self._rest = chunk
        data, self._rest = self._rest[:size], self._rest[size:]
        return data


class SdPlayer:
    """時刻 start（UNIX 時刻）からの録画の再生1回ぶん。video と audio を RTCPeerConnection に加え、begin() で始める。"""

    def __init__(self, sd: SdCard, start: float):
        self.sd, self.start = sd, start
        self.video, self.audio = QueueTrack("video"), QueueTrack("audio")
        self.error = ""
        self.finished = False  # 録画の終わりまで流し終えたか、エラーで止まった
        self._ts = TsReader()
        self._feed = _Feed()
        self._task: asyncio.Task | None = None
        self._samples = 0
        self._pcm = b""
        self._table: list[int] | None = None

    @property
    def position(self) -> float:
        """いま流している位置（UNIX 時刻）。録画の時刻（PTS）の進みから求める。"""
        return self.start + self._ts.video_seconds

    def begin(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        threading.Thread(target=self._demux, args=(loop,), daemon=True, name="sd-player").start()
        try:
            async for chunk in self.sd.playback(self.start, exclusive=True):
                self._ts.feed(chunk)
                self._feed.chunks.put(chunk)
                self._push_audio(self._ts.take_audio())
        except SdCardError as e:
            self.error = str(e)
        finally:
            self.finished = True
            self._feed.chunks.put(None)   # libav のスレッドを終わらせる（残りを読み切ると、映像のキューに None を入れる）
            self.audio.queue.put_nowait(None)

    def _demux(self, loop: asyncio.AbstractEventLoop) -> None:
        """TS から映像のパケットを切り出して、映像のトラックへ渡す（別スレッド）。"""
        try:
            # 既定のままだと、形式を調べるために数 MB（この録画で 20 秒ぶんほど）読み込んでから始まる
            container = av.open(self._feed, format="mpegts", mode="r",
                                options={"probesize": "200000", "analyzeduration": "0", "fflags": "nobuffer"})
            with container:
                for packet in container.demux(container.streams.video[0]):
                    if packet.size and packet.pts is not None:
                        loop.call_soon_threadsafe(self.video.queue.put_nowait, packet)
        except Exception:  # 壊れた TS や、止められて読み切れなかったとき。映像を終わらせるだけでよい
            pass
        finally:
            try:
                loop.call_soon_threadsafe(self.video.queue.put_nowait, None)
            except RuntimeError:  # イベントループがもう無い
                pass

    def _push_audio(self, data: bytes) -> None:
        if not data or self._ts.codec is None:
            return
        form, rate = self._ts.codec
        if self._table is None:
            self._table = _g711_table(form)
        table = self._table
        self._pcm += array.array("h", [table[b] for b in data]).tobytes()
        size = int(rate * _FRAME_SECONDS) * 2
        while len(self._pcm) >= size:
            frame = av.AudioFrame(format="s16", layout="mono", samples=size // 2)
            frame.planes[0].update(self._pcm[:size])
            frame.sample_rate, frame.pts, frame.time_base = rate, self._samples, Fraction(1, rate)
            self._samples += size // 2
            self._pcm = self._pcm[size:]
            self.audio.queue.put_nowait(frame)
