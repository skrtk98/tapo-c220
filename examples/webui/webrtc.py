"""tapo_c220 のパケット（tapo_c220.packets）を、WebRTC でブラウザへ送る（aiortc）。

ライブラリは映像と音声を「変換していないパケットの列」として渡すだけで、WebRTC のことは知らない。ここで aiortc のトラックへ載せ替える。

- ライブ：カメラの H.264 と G.711 A-law（8kHz）は、どちらも WebRTC の標準のコーデックなので、変換せずそのまま RTP に載せる。
  相手（ブラウザ）ごとに RTSP を1本ずつ張るため、カメラの同時接続数（3本）がそのまま上限になる。
- 録画：映像の H.264 はそのまま送る。音声（G.711 µ-law 16kHz など）は WebRTC の標準の形（8kHz）ではないので、
  PCM に戻して渡し、aiortc に Opus へ変換させる。

合図のやり取りは1往復だけ：ブラウザの offer（ICE の候補を集め終えたもの）を受け取り、answer を返す。
"""

from __future__ import annotations

import array
import asyncio
from collections.abc import Awaitable, Callable
from fractions import Fraction

import av
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCRtpSender, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError

from tapo_c220 import MediaError, StreamError, TapoConfig
from tapo_c220.packets import LivePackets, Packet

_CODECS = {"video": "video/H264", "audio": "audio/PCMA"}
_VIDEO_CLOCK = 90000   # 映像の時刻の刻み（RTP と同じ）
_FRAME_SECONDS = 0.02  # 録画の音声を渡す1回ぶんの長さ
_TRACK_BACKLOG = 300   # 映像15fpsと音声50個/秒で、合わせて約4.6秒ぶん


class QueueTrack(MediaStreamTrack):
    """キューに入れられたもの（変換しないパケットか、フレーム）を順に渡す。None が来たら終わり。"""

    def __init__(self, kind: str, on_stop: Callable[[], None] | None = None):
        super().__init__()
        self.kind = kind
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=_TRACK_BACKLOG)
        self._on_stop = on_stop

    async def recv(self):
        if self.readyState != "live":
            raise MediaStreamError
        item = await self.queue.get()
        if item is None:
            self.stop()
            raise MediaStreamError
        return item

    def stop(self) -> None:
        super().stop()
        if self._on_stop is not None:
            self._on_stop()


def passthrough(packet: Packet) -> list[av.Packet]:
    """ライブラリのパケットを、aiortc が変換せずに送れる形（時刻つきの av.Packet）にする。"""
    clock = _VIDEO_CLOCK if packet.kind == "video" else packet.rate
    out = av.Packet(packet.data)
    out.pts = out.dts = round(packet.time * clock)
    out.time_base = Fraction(1, clock)
    return [out]


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


class PcmFrames:
    """G.711 の音声のパケットを、20ms ずつの PCM のフレームにする（aiortc が Opus に変換できる形）。"""

    def __init__(self) -> None:
        self._table: list[int] | None = None
        self._pcm = b""
        self._samples = 0

    def __call__(self, packet: Packet) -> list[av.AudioFrame]:
        if self._table is None:
            self._table = _g711_table(packet.codec)
        table = self._table
        self._pcm += array.array("h", [table[b] for b in packet.data]).tobytes()
        size, frames = int(packet.rate * _FRAME_SECONDS) * 2, []
        while len(self._pcm) >= size:
            frame = av.AudioFrame(format="s16", layout="mono", samples=size // 2)
            frame.planes[0].update(self._pcm[:size])
            frame.sample_rate, frame.pts, frame.time_base = packet.rate, self._samples, Fraction(1, packet.rate)
            self._samples += size // 2
            self._pcm = self._pcm[size:]
            frames.append(frame)
        return frames


class Feeder:
    """パケットの源（LivePackets や SdPackets）を読み、映像と音声のトラックへ配る。

    トラックのキューが満杯なら源から読むのを待つ。録画は源の上限でカメラから受け取るのを止め、ライブは読み手が止まると源の上限で MediaError になる。
    aiortc は接続後に全トラックの recv を呼び続けるため、片方が満杯になると両方の読み込みが止まってもよい。
    接続確立前はどちらも読まれず、キューと源の上限ぶんだけ溜まり、確立が遅すぎるとライブはエラーになる。
    """

    def __init__(self, source, kinds=("video", "audio"), *, audio: Callable[[Packet], list] = passthrough):
        self.source = source
        self.tracks = {kind: QueueTrack(kind, self._track_stopped) for kind in kinds}
        self.video, self.audio = self.tracks.get("video"), self.tracks.get("audio")
        self.error = ""
        self.finished = False  # 源が終わった、エラーで止まった、または止められた
        self._convert = {"video": passthrough, "audio": audio}
        self._task: asyncio.Task | None = None

    def begin(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self.finished = True
        if self._task is not None:
            task, self._task = self._task, None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        else:
            await self.source.aclose()

    def _track_stopped(self) -> None:
        if self.finished:
            return
        try:
            asyncio.get_running_loop().create_task(self.stop())
        except RuntimeError:  # イベントループがもう無い
            pass

    async def _run(self) -> None:
        try:
            async for packet in self.source:
                track = self.tracks.get(packet.kind)
                if track is not None:
                    for item in self._convert[packet.kind](packet):
                        await track.queue.put(item)
        except (StreamError, MediaError) as e:
            self.error = str(e)
        finally:
            self.finished = True
            await self.source.aclose()
            for track in self.tracks.values():
                if track.queue.full():  # 終わったトラックの未送信分を捨て、終わりの印を入れる
                    while not track.queue.empty():
                        track.queue.get_nowait()
                track.queue.put_nowait(None)


class SdPlayer(Feeder):
    """microSD の録画の、時刻 start（UNIX 時刻）からの再生1回ぶん。video と audio を相手に送り、begin() で始める。"""

    def __init__(self, sd, start: float):
        super().__init__(sd.packets(start), audio=PcmFrames())
        self.start = start

    @property
    def position(self) -> float:
        """いま流している位置（UNIX 時刻）。"""
        return self.source.position


class WebRtcSessions:
    """進行中の WebRTC の接続の集まり。接続が切れたら、対応するパケットの源も閉じる。"""

    def __init__(self, cfg: TapoConfig):
        self.cfg = cfg
        self._peers: dict[RTCPeerConnection, Callable[[], Awaitable[None]] | None] = {}

    def __len__(self) -> int:
        return len(self._peers)

    async def answer(self, sdp: str, sdp_type: str = "offer", stream: str = "stream2") -> RTCSessionDescription:
        """ブラウザの offer に対する answer を返し、ライブの映像と音声を送り始める。"""
        feeder = Feeder(source := await LivePackets.open(self.cfg, stream), source.kinds)
        if not feeder.tracks:
            await feeder.stop()
            raise MediaError("カメラの映像に、映像も音声も無い")
        answer = await self.answer_tracks(sdp, sdp_type, list(feeder.tracks.values()), codecs=_CODECS,
                                          on_close=feeder.stop)
        feeder.begin()
        return answer

    async def answer_tracks(self, sdp: str, sdp_type: str, tracks: list[MediaStreamTrack], *,
                            codecs: dict[str, str] | None = None,
                            on_close: Callable[[], Awaitable[None]] | None = None) -> RTCSessionDescription:
        """offer に対する answer を返し、渡されたトラックを送り始める。codecs は種類ごとに使うコーデック（変換せず送るとき）。
        接続が切れるか answer に失敗すると、トラックを止めて on_close を呼ぶ。"""
        peer = RTCPeerConnection()
        self._peers[peer] = on_close

        @peer.on("connectionstatechange")
        async def on_state() -> None:
            if peer.connectionState in ("failed", "closed"):
                await self._close(peer)

        try:
            for track in tracks:
                sender = peer.addTrack(track)
                if codecs and track.kind in codecs:
                    transceiver = next(t for t in peer.getTransceivers() if t.sender == sender)
                    transceiver.setCodecPreferences([c for c in RTCRtpSender.getCapabilities(track.kind).codecs
                                                     if c.mimeType == codecs[track.kind]])
            await peer.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=sdp_type))
            await peer.setLocalDescription(await peer.createAnswer())
        except BaseException as e:  # 要求が取り消されたとき（CancelledError）も、カメラとの接続を残さない
            for track in tracks:  # 送り始める前に失敗したトラックは、自分で止める
                track.stop()
            await asyncio.shield(self._close(peer))
            if isinstance(e, ValueError):
                raise ValueError(f"offer を受け付けられない（{e}）") from None
            raise
        return peer.localDescription

    async def _close(self, peer: RTCPeerConnection) -> None:
        if peer in self._peers:
            on_close = self._peers.pop(peer)
            await peer.close()
            if on_close is not None:
                await on_close()

    async def close(self) -> None:
        await asyncio.gather(*(self._close(peer) for peer in list(self._peers)), return_exceptions=True)
