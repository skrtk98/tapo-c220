"""ライブ映像と音声を WebRTC でブラウザへ送る（aiortc が要る。`tapo-c220-survey[webui]` に含まれる）。

カメラの RTSP を受け、H.264 と G.711 A-law を変換せずそのまま RTP に載せ替える。どちらも WebRTC の標準のコーデックなので、
再エンコードは要らない。相手（ブラウザ）ごとに RTSP を1本ずつ張るため、カメラの同時接続数（3本）がそのまま上限になる。

合図のやり取りは1往復だけ：ブラウザの offer（ICE の候補を集め終えたもの）を受け取り、answer を返す。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable

import av
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCRtpSender, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError

from . import media
from .config import TapoConfig
from .media import MediaError

_CODECS = {"video": "video/H264", "audio": "audio/PCMA"}
_OPEN_TIMEOUT = 10  # RTSP の接続と読み取りを諦めるまでの秒数


class QueueTrack(MediaStreamTrack):
    """キューに入れられたもの（変換しないパケットか、フレーム）を順に渡す。None が来たら終わり。"""

    def __init__(self, kind: str, on_stop: Callable[[], None] | None = None):
        super().__init__()
        self.kind = kind
        self.queue: asyncio.Queue = asyncio.Queue()
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


class _Reader:
    """カメラの RTSP を別スレッドで読み、映像と音声のパケットを変換せずにトラックへ配る。どちらかのトラックを止めると読むのもやめる。

    aiortc の MediaPlayer（decode=False）は使わない。同じことをさせると、メモリが毎分約 0.7MB ずつ増え続けた
    （40 分の連続視聴で 27MB。自前で読むと増えない）。
    """

    def __init__(self, cfg: TapoConfig, stream: str):
        """RTSP につなぐ（ブロックする）。つなげなければ av.FFmpegError など。"""
        self._container = av.open(media.rtsp_url(cfg, stream), format="rtsp", timeout=_OPEN_TIMEOUT,
                                  options={"rtsp_transport": "tcp", "fflags": "nobuffer"})
        self._quit = threading.Event()
        self.tracks: dict[str, QueueTrack] = {}
        self._streams = []
        for kind in ("video", "audio"):
            found = getattr(self._container.streams, kind)
            if found:
                self._streams.append(found[0])
                self.tracks[kind] = QueueTrack(kind, self._quit.set)

    def start(self) -> None:
        """読み始める。イベントループの中から呼ぶこと。"""
        loop = asyncio.get_running_loop()
        threading.Thread(target=self._run, args=(loop,), daemon=True, name="rtsp-reader").start()

    def close(self) -> None:
        """読み始める前にやめるとき。"""
        self._quit.set()
        self._container.close()

    def _run(self, loop: asyncio.AbstractEventLoop) -> None:
        try:
            for packet in self._container.demux(*self._streams):
                if self._quit.is_set():
                    break
                if packet.size and packet.pts is not None:
                    loop.call_soon_threadsafe(self.tracks[packet.stream.type].queue.put_nowait, packet)
        except Exception:  # noqa: BLE001, S110 - カメラが切った、届かなくなった等。トラックを終わらせるだけでよい
            pass
        finally:
            self._container.close()
            for track in self.tracks.values():
                try:
                    loop.call_soon_threadsafe(track.queue.put_nowait, None)
                except RuntimeError:  # イベントループがもう無い
                    pass


class _Retimed(MediaStreamTrack):
    """カメラのパケットの時刻（pts）を、途切れなく進むように付け直す。

    C220 は RTP の時刻そのものは正しく進めるが、約 0.5 秒ごとに送ってくる RTCP の Sender Report の時刻（NTP）が
    交互に約 ±0.38 秒ずれている。libav はこれを信じて pts を合わせ直すので、pts が約 0.35 秒戻っては約 0.42 秒飛ぶ。
    そのまま WebRTC に載せると、ブラウザは映像を毎秒のように止め、音声の4割ほどを補間で埋める。
    """

    _JUMP = 0.2  # 1つ前との差がこの秒数を超えるか、負なら、Sender Report による合わせ直しとみなす

    def __init__(self, source: MediaStreamTrack):
        super().__init__()
        self.kind = source.kind
        self._source = source
        self._pts: int | None = None   # 付け直した時刻（パケットの time_base の単位）
        self._last = 0                 # 1つ前のパケットの、もとの時刻
        self._step = 0                 # 1つ前までの、普通の進み幅

    async def recv(self):
        packet = await self._source.recv()
        if self.kind == "audio":
            # A-law は 1 バイトが 1 標本で、time_base は 1/8000。届いた標本の数で数える
            step, self._step = self._step, packet.size
            self._pts = 0 if self._pts is None else self._pts + step
        else:
            delta = packet.pts - self._last
            if self._pts is None:
                self._pts = 0
            elif 0 < delta * packet.time_base < self._JUMP:
                self._pts, self._step = self._pts + delta, delta
            else:
                self._pts += self._step or round(1 / 30 / packet.time_base)
            self._last = packet.pts
        packet.pts = packet.dts = self._pts
        return packet

    def stop(self) -> None:
        super().stop()
        self._source.stop()


class WebRtcSessions:
    """進行中の WebRTC の接続の集まり。接続が切れたら、対応する RTSP も閉じる。"""

    def __init__(self, cfg: TapoConfig):
        self.cfg = cfg
        self._peers: dict[RTCPeerConnection, Callable[[], Awaitable[None]] | None] = {}

    def __len__(self) -> int:
        return len(self._peers)

    async def answer(self, sdp: str, sdp_type: str = "offer", stream: str = media.SUB) -> RTCSessionDescription:
        """ブラウザの offer に対する answer を返し、ライブの映像と音声を送り始める。"""
        try:
            reader = await asyncio.to_thread(_Reader, self.cfg, stream)  # 接続はブロックするので、別スレッドで
        except Exception as e:  # PyAV の例外は URL（認証情報つき）を含むことがあるので、種類だけ伝える
            raise MediaError(f"カメラの映像を開けない（{type(e).__name__}）。同時に見られるのは3接続まで") from None
        tracks = [_Retimed(track) for track in reader.tracks.values()]
        if not tracks:
            reader.close()
            raise MediaError("カメラの映像に、映像も音声も無い")
        reader.start()
        return await self.answer_tracks(sdp, sdp_type, tracks, codecs=_CODECS)

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
        except Exception as e:
            await self._close(peer)
            for track in tracks:  # 送り始める前に失敗したトラックは、自分で止める（ライブなら RTSP が閉じる）
                track.stop()
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
