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


async def test_feeder_bounds_track_queue_and_waits_for_reader():
    from webrtc import Feeder, _TRACK_BACKLOG

    advanced = asyncio.Event()

    class Source:
        def __init__(self):
            self.read = 0
            self.closed = False

        def __aiter__(self):
            return self.packets()

        async def packets(self):
            packet = Packet("video", b"\x00\x00\x00\x01\x65", 0, "h264", keyframe=True)
            for _ in range(_TRACK_BACKLOG + 10):
                self.read += 1
                if self.read > _TRACK_BACKLOG:
                    advanced.set()
                yield packet

        async def aclose(self):
            self.closed = True

    feeder = Feeder(source := Source(), kinds=("video",))
    feeder.begin()
    await asyncio.wait_for(advanced.wait(), timeout=1)
    assert source.read == _TRACK_BACKLOG + 1
    assert feeder.video.queue.qsize() == _TRACK_BACKLOG
    await feeder.stop()
    assert source.closed and feeder.video.queue.qsize() == 1


async def test_feeder_finishes_when_source_ends_with_full_queue():
    from aiortc.mediastreams import MediaStreamError
    from webrtc import Feeder, _TRACK_BACKLOG

    class Source:
        def __aiter__(self):
            return self.packets()

        async def packets(self):
            packet = Packet("video", b"\x00\x00\x00\x01\x65", 0, "h264", keyframe=True)
            for _ in range(_TRACK_BACKLOG):
                yield packet

        async def aclose(self):
            pass

    feeder = Feeder(Source(), kinds=("video",))
    feeder.begin()
    await asyncio.wait_for(feeder._task, timeout=1)
    assert feeder.video.queue.qsize() == 1
    with pytest.raises(MediaStreamError):
        await feeder.video.recv()


async def test_feeder_stop_cancels_a_put_waiting_on_full_queue():
    from aiortc.mediastreams import MediaStreamError
    from webrtc import Feeder, _TRACK_BACKLOG

    waiting = asyncio.Event()

    class Source:
        def __init__(self):
            self.read = 0
            self.closed = False

        def __aiter__(self):
            return self.packets()

        async def packets(self):
            packet = Packet("video", b"\x00\x00\x00\x01\x65", 0, "h264", keyframe=True)
            while True:
                self.read += 1
                if self.read > _TRACK_BACKLOG:
                    waiting.set()
                yield packet

        async def aclose(self):
            self.closed = True

    feeder = Feeder(source := Source(), kinds=("video",))
    feeder.begin()
    await asyncio.wait_for(waiting.wait(), timeout=1)
    task = feeder._task
    await feeder.stop()
    assert task.done() and source.closed and feeder.video.queue.qsize() == 1
    with pytest.raises(MediaStreamError):
        await feeder.video.recv()
