"""使用例の WebRTC の載せ替えのうち、カメラ無しで確かめられる部分。"""

import asyncio

import pytest
from tapo_c220 import TapoConfig
from tapo_c220.packets import Packet

CFG = TapoConfig(host="192.0.2.1", user="user", password="password", cloud_password="cloud")


async def test_webrtc_stops_tracks_when_offer_is_cancelled(monkeypatch):
    from webrtc import QueueTrack, WebRtcSessions

    stopped = []
    sessions = WebRtcSessions(CFG)
    track = QueueTrack("video", lambda: stopped.append(True))

    async def cancelled(self, description):
        raise asyncio.CancelledError

    monkeypatch.setattr("webrtc.RTCPeerConnection.setRemoteDescription", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await sessions.answer_tracks("v=0", "offer", [track])
    assert stopped and len(sessions) == 0


async def test_feeder_distributes_packets_and_closes_source():
    from webrtc import Feeder, PcmFrames, _g711_table

    assert _g711_table("mulaw")[0xFF] == 0 and _g711_table("mulaw")[0x00] == -32124 and _g711_table("mulaw")[0x80] == 32124
    assert _g711_table("alaw")[0xD5] == 8 and _g711_table("alaw")[0x55] == -8

    class Source:
        closed = False

        async def __aiter__(self):
            yield Packet("video", b"\x00\x00\x00\x01\x65", 0.04, "h264", keyframe=True)
            yield Packet("audio", b"\xff" * 640, 0.0, "mulaw", 16000)  # 40ms ぶん

        async def aclose(self):
            self.closed = True

    feeder = Feeder(source := Source(), audio=PcmFrames())
    feeder.begin()
    video = await feeder.video.recv()
    assert (video.pts, video.time_base.denominator, bytes(video)) == (3600, 90000, b"\x00\x00\x00\x01\x65")
    frames = [await feeder.audio.recv(), await feeder.audio.recv()]
    assert [(f.samples, f.sample_rate, f.pts) for f in frames] == [(320, 16000, 0), (320, 16000, 320)]
    await asyncio.sleep(0)
    assert feeder.finished and source.closed and not feeder.error
