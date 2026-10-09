"""ライブ映像と音声を WebRTC でブラウザへ送る（aiortc が要る。`tapo-c220-survey[webui]` に含まれる）。

カメラの RTSP を受け、H.264 と G.711 A-law を変換せずそのまま RTP に載せ替える。どちらも WebRTC の標準のコーデックなので、
再エンコードは要らない。相手（ブラウザ）ごとに RTSP を1本ずつ張るため、カメラの同時接続数（3本）がそのまま上限になる。

合図のやり取りは1往復だけ：ブラウザの offer（ICE の候補を集め終えたもの）を受け取り、answer を返す。
"""

from __future__ import annotations

import asyncio

from aiortc import MediaStreamTrack, RTCPeerConnection, RTCRtpSender, RTCSessionDescription
from aiortc.contrib.media import MediaPlayer

from . import media
from .config import TapoConfig
from .media import MediaError

_CODECS = {"video": "video/H264", "audio": "audio/PCMA"}
_OPEN_TIMEOUT = 10  # RTSP の接続と読み取りを諦めるまでの秒数


def _open(cfg: TapoConfig, stream: str) -> MediaPlayer:
    return MediaPlayer(media.rtsp_url(cfg, stream), format="rtsp", decode=False, timeout=_OPEN_TIMEOUT,
                       options={"rtsp_transport": "tcp", "fflags": "nobuffer"})


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
        self._peers: set[RTCPeerConnection] = set()

    def __len__(self) -> int:
        return len(self._peers)

    async def answer(self, sdp: str, sdp_type: str = "offer", stream: str = media.SUB) -> RTCSessionDescription:
        """ブラウザの offer に対する answer を返し、映像と音声を送り始める。"""
        try:
            player = await asyncio.to_thread(_open, self.cfg, stream)  # 接続はブロックするので、別スレッドで
        except Exception as e:  # PyAV の例外は URL（認証情報つき）を含むことがあるので、種類だけ伝える
            raise MediaError(f"カメラの映像を開けない（{type(e).__name__}）。同時に見られるのは3接続まで") from None
        peer = RTCPeerConnection()
        self._peers.add(peer)

        @peer.on("connectionstatechange")
        async def on_state() -> None:
            if peer.connectionState in ("failed", "closed"):
                await self._close(peer)

        try:
            for track in (player.video, player.audio):
                if track is None:
                    continue
                sender = peer.addTrack(_Retimed(track))
                transceiver = next(t for t in peer.getTransceivers() if t.sender == sender)
                transceiver.setCodecPreferences([c for c in RTCRtpSender.getCapabilities(track.kind).codecs
                                                 if c.mimeType == _CODECS[track.kind]])
            await peer.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=sdp_type))
            await peer.setLocalDescription(await peer.createAnswer())
        except Exception as e:
            await self._close(peer)
            for track in (player.video, player.audio):  # 送り始める前に失敗したトラックは、自分で止めて RTSP を閉じる
                if track is not None:
                    track.stop()
            if isinstance(e, ValueError):
                raise ValueError(f"offer を受け付けられない（{e}）") from None
            raise
        return peer.localDescription

    async def _close(self, peer: RTCPeerConnection) -> None:
        if peer in self._peers:
            self._peers.discard(peer)
            await peer.close()

    async def close(self) -> None:
        await asyncio.gather(*(self._close(peer) for peer in list(self._peers)), return_exceptions=True)

