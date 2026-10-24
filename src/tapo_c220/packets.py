"""映像と音声を、変換していないパケットの列として取り出す（PyAV が要る。`tapo-c220[packets]`）。

ライブ（RTSP）も microSD の録画も、同じ形（Packet）で順に渡す。受け取った側が、WebRTC へ載せ替える、ファイルへ書く、
解析に回すなど、好きに使う（WebRTC でブラウザへ送る例が `examples/webui/webrtc.py` にある）。

- ライブ：`LivePackets.open(cfg, stream)`（または `TapoC220.live_packets()`）。映像は H.264、音声は G.711 A-law 8kHz。
- 録画：`SdPackets(sdcard, start)`（または `SdCard.packets()`）。映像は H.264、音声は G.711（この個体は µ-law 16kHz）。

どちらも `async for` で読み、やめるときは `aclose()` を呼ぶ（カメラとの接続を返す）。
読む側が遅くても、溜めるのは 1000 パケットまで。ライブは待たせられないので、超えたら MediaError で終える。
録画は、読む側が追いつくまでカメラから受け取るのを止めて待つ。

C220 のライブは、RTP の時刻そのものは正しく進むが、約 0.5 秒ごとに届く RTCP の Sender Report の時刻（NTP）が
交互に約 ±0.38 秒ずれている。libav はこれを信じて pts を合わせ直すので、pts が約 0.35 秒戻っては約 0.42 秒飛ぶ。
そのまま再生すると、映像は毎秒のように止まり、音声の4割ほどが欠ける。ここで、途切れなく進む時刻に付け直してから渡す。
"""

from __future__ import annotations

import asyncio
import errno
import queue
import threading
import time
from dataclasses import dataclass

import av

from . import media
from .config import TapoConfig
from .media import MediaError

_OPEN_TIMEOUT = 10  # RTSP の接続と読み取りを諦めるまでの秒数
_BACKLOG = 1000     # 読む側が受け取っていないパケット（イベントループに予約しただけのものも含む）の上限（20 秒ぶんほど）
_AUDIO_NAMES = {"pcm_alaw": "alaw", "pcm_mulaw": "mulaw"}


@dataclass(frozen=True)
class Packet:
    kind: str        # "video" か "audio"
    data: bytes      # 映像は H.264（Annex B）の1フレームぶん、音声は G.711 の標本の列
    time: float      # 最初のパケットからの秒数。種類ごとに、途切れず増えていく
    codec: str       # "h264" / "alaw" / "mulaw"
    rate: int = 0    # 音声のサンプリング周波数（映像は 0）
    keyframe: bool = False


class Retimer:
    """映像の pts（秒）を、途切れなく進むように付け直す。前との差が大きすぎるか負なら、1つ前と同じ幅だけ進める。"""

    _JUMP = 0.2  # 1つ前との差がこの秒数を超えるか、負なら、Sender Report による合わせ直しとみなす

    def __init__(self) -> None:
        self._time: float | None = None
        self._last = 0.0
        self._step = 0.0

    def __call__(self, pts: float) -> float:
        delta = pts - self._last
        if self._time is None:
            self._time = 0.0
        elif 0 < delta < self._JUMP:
            self._time, self._step = self._time + delta, delta
        else:
            self._time += self._step or 1 / 30
        self._last = pts
        return self._time


class LivePackets:
    """カメラの RTSP を別スレッドで読み、パケットを順に渡す。`await LivePackets.open(...)` で作る。

    aiortc の MediaPlayer（decode=False）に同じことをさせると、メモリが毎分約 0.7MB ずつ増え続けた
    （40 分の連続視聴で 27MB）。自前で読むと増えない。
    """

    def __init__(self, container: av.container.InputContainer):
        self._container = container
        self._streams = [found[0] for found in (container.streams.video, container.streams.audio) if found]
        # 含まれる種類 → コーデック（{"video": "h264", "audio": "alaw"}）
        self.kinds = {s.type: _AUDIO_NAMES.get(s.codec_context.name, s.codec_context.name) for s in self._streams}
        self._quit = threading.Event()
        self._queue: asyncio.Queue[Packet | None] = asyncio.Queue()
        self._pending = 0
        self._pending_lock = threading.Lock()
        self._started = False
        self._overflow = False

    @classmethod
    async def open(cls, cfg: TapoConfig, stream: str = media.SUB) -> LivePackets:
        """RTSP につなぐ。つなげなければ MediaError（カメラが同時に受けるのは3接続まで）。"""
        def connect() -> av.container.InputContainer:
            return av.open(media.rtsp_url(cfg, stream), format="rtsp", timeout=_OPEN_TIMEOUT,
                           options={"rtsp_transport": "tcp", "fflags": "nobuffer"})
        try:
            return cls(await asyncio.to_thread(connect))  # 接続はブロックするので、別スレッドで
        except Exception as e:  # noqa: BLE001 - PyAV の例外は URL（認証情報つき）を含むことがあるので、種類だけ伝える
            raise MediaError(f"カメラの映像を開けない（{type(e).__name__}）。同時に見られるのは3接続まで") from None

    def __aiter__(self) -> LivePackets:
        return self

    async def __anext__(self) -> Packet:
        if not self._started:
            self._started = True
            threading.Thread(target=self._run, args=(asyncio.get_running_loop(),), daemon=True,
                             name="rtsp-reader").start()
        packet = await self._queue.get()
        if packet is None:
            self._queue.put_nowait(None)  # 次に呼ばれても終わりを返す
            if self._overflow:
                raise MediaError("ライブのパケットを読むのが遅すぎて、溜めきれなくなった")
            raise StopAsyncIteration
        with self._pending_lock:
            self._pending -= 1
        return packet

    async def aclose(self) -> None:
        """読むのをやめて、RTSP を閉じる（読んでいる途中なら、次のパケットが届いたところで閉じる）。"""
        self._quit.set()
        if not self._started:
            self._started = True
            self._container.close()
            self._queue.put_nowait(None)

    def _run(self, loop: asyncio.AbstractEventLoop) -> None:
        retime, samples = Retimer(), 0
        try:
            while not self._quit.is_set():
                try:
                    for raw in self._container.demux(*self._streams):
                        if self._quit.is_set():
                            break
                        if not raw.size or raw.pts is None:
                            continue
                        kind = raw.stream.type
                        if kind == "video":
                            packet = Packet(kind, bytes(raw), retime(float(raw.pts * raw.time_base)), self.kinds[kind],
                                            keyframe=raw.is_keyframe)
                        else:  # G.711 は 1 バイトが 1 標本。届いた標本の数で時刻を数える
                            rate = raw.stream.codec_context.sample_rate
                            packet = Packet(kind, bytes(raw), samples / rate, self.kinds[kind], rate)
                            samples += raw.size
                        with self._pending_lock:
                            if self._pending > _BACKLOG:
                                self._overflow = True
                                self._quit.set()
                                break
                            self._pending += 1
                        loop.call_soon_threadsafe(self._queue.put_nowait, packet)
                    break  # カメラが流すのを終えた
                except av.FFmpegError as e:
                    if e.errno != errno.EAGAIN:  # 一時的に読めないだけなら、少し待って続ける
                        raise
                    time.sleep(0.01)
        except Exception:  # noqa: BLE001, S110 - カメラが切った、届かなくなった等。終わりを知らせるだけでよい
            pass
        finally:
            self._container.close()
            try:
                loop.call_soon_threadsafe(self._queue.put_nowait, None)
            except RuntimeError:  # イベントループがもう無い
                pass


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


class SdPackets:
    """microSD の録画を、時刻 start（UNIX 時刻）から、パケットとして順に渡す（`[tapo]` の依存も要る）。

    実時間の速さで届く。finished の通知で正常終了する。その時刻に録画が無ければ NoRecording、
    映像のあと finished より先に無通信や接続終了になれば PlaybackInterrupted。
    流している間、ほかの取り出しはできない（すでに取り出し中なら SdCardError）。
    """

    def __init__(self, sd, start: float):
        from .sdcard import TsReader

        self.sd, self.start = sd, start
        self._ts = TsReader()
        self._feed = _Feed()
        self._queue: asyncio.Queue[Packet | None] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._samples = 0
        self._closed = False

    @property
    def position(self) -> float:
        """いま届いている位置（UNIX 時刻）。録画の時刻（PTS）の進みから求める。"""
        return self.start + self._ts.video_seconds

    def __aiter__(self) -> SdPackets:
        return self

    async def __anext__(self) -> Packet:
        if self._closed:
            raise StopAsyncIteration
        if self._task is None:
            loop = asyncio.get_running_loop()
            threading.Thread(target=self._demux, args=(loop,), daemon=True, name="sd-reader").start()
            self._task = asyncio.create_task(self._receive())
        packet = await self._queue.get()
        if packet is None:
            self._closed = True
            await self._task  # PlaybackInterrupted を含め、取り出しタスクの例外は読む側へ伝える
            raise StopAsyncIteration
        return packet

    async def aclose(self) -> None:
        """読むのをやめて、カメラとの接続を閉じる。"""
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _receive(self) -> None:
        """カメラから TS を受け取り、音声を取り出して渡す。映像は、別スレッドの libav に切り出させる。"""
        try:
            async for chunk in self.sd.playback(self.start, exclusive=True):
                # libav が予約した映像は数えないが、与えた TS の範囲しか進まないので溜まる量は限られる
                while self._queue.qsize() > _BACKLOG:  # 読む側が追いつくまで、カメラから受け取るのを止める
                    await asyncio.sleep(0.05)
                self._ts.feed(chunk)
                self._feed.chunks.put(chunk)
                audio = self._ts.take_audio()
                if audio and self._ts.codec is not None:
                    form, rate = self._ts.codec
                    self._queue.put_nowait(Packet("audio", audio, self._samples / rate, form, rate))
                    self._samples += len(audio)
        finally:
            self._feed.chunks.put(None)  # libav のスレッドを終わらせる（残りを読み切ると、終わりの印を入れる）

    def _demux(self, loop: asyncio.AbstractEventLoop) -> None:
        first: float | None = None
        try:
            # 既定のままだと、形式を調べるために数 MB（この録画で 20 秒ぶんほど）読み込んでから始まる
            container = av.open(self._feed, format="mpegts", mode="r",
                                options={"probesize": "200000", "analyzeduration": "0", "fflags": "nobuffer"})
            with container:
                for raw in container.demux(container.streams.video[0]):
                    if not raw.size or raw.pts is None:
                        continue
                    seconds = float(raw.pts * raw.time_base)
                    if first is None:
                        first = seconds
                    loop.call_soon_threadsafe(self._queue.put_nowait, Packet(
                        "video", bytes(raw), seconds - first, "h264", keyframe=raw.is_keyframe))
        except Exception:  # noqa: BLE001, S110 - 壊れた TS や、止められて読み切れなかったとき。終わらせるだけでよい
            pass
        finally:
            try:
                loop.call_soon_threadsafe(self._queue.put_nowait, None)
            except RuntimeError:  # イベントループがもう無い
                pass


__all__ = ["LivePackets", "Packet", "Retimer", "SdPackets"]
